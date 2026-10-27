"""Scheduled pipeline: Sentinel-2 -> hyacinth & turbidity per river segment.

For each river in config/rivers.yaml:
  1. find new cloud-light Sentinel-2 L2A scenes (Microsoft Planetary Computer, free)
  2. read green/red/NIR + scene classification over the river only
  3. mask clouds and shadows; count only river pixels (see build_river_mask.py)
  4. classify floating vegetation (NDVI), open water (NDWI) and how muddy it is (NDTI)
  5. aggregate into ~500 m river segments and append to history
  6. add Sentinel-1 radar passes (cloud-proof, river-wide only), self-calibrated against step 4
  7. write web/data/<river>.json for the dashboard

Usage:
  uv run python pipeline/run.py                     # normal scheduled run
  uv run python pipeline/run.py --since 2025-10-01  # backfill
"""

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
import shapely
import yaml
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from shapely import STRtree, points
from shapely.geometry import box, mapping, shape
from shapely.ops import substring, unary_union

ROOT = Path(__file__).resolve().parent.parent
UTM = "EPSG:32643"  # UTM 43N covers all of Maharashtra
RES = 10
# Scene classification classes that are usable: dark area, vegetation, bare, water, unclassified.
SCL_OK = [2, 4, 5, 6, 7]
# Radar backscatter histogram bins (VH, dB). Storing the histogram lets the threshold be
# re-calibrated later without downloading the images again.
RADAR_EDGES = np.round(np.arange(-35, 5.001, 0.25), 2)
# Bump when the way history numbers are computed changes; run.py then rebuilds the optical
# history (from the local pixel cache where it exists).
METHOD = 3
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


def _projector(src, dst):
    tr = Transformer.from_crs(src, dst, always_xy=True)
    return lambda geom: shapely.transform(geom, lambda c: np.column_stack(tr.transform(c[:, 0], c[:, 1])))


to_utm = _projector("EPSG:4326", UTM)
to_wgs = _projector(UTM, "EPSG:4326")


def load_river(river: dict, det: dict):
    gj = json.loads((ROOT / f"data/rivers/{river['id']}.geojson").read_text())
    geoms = {f["properties"]["kind"]: shape(f["geometry"]) for f in gj["features"]}
    clip = box(*river["bbox"])
    line = to_utm(geoms["centreline"].intersection(clip))
    water = to_utm(geoms["water"].intersection(clip))
    # Use mapped water area where OSM has it, a buffered centreline elsewhere,
    # then pull back one pixel from the banks to avoid mixed land/water pixels.
    aoi = unary_union([water, line.buffer(det["river_buffer_m"])]).buffer(-RES)
    # Wider search area: OSM's river polygon is missing in places and its centreline can run along
    # one bank. build_river_mask.py finds the real channel inside this from the images themselves.
    corridor = unary_union([water, line.buffer(det["corridor_m"])])
    return line, aoi, corridor


def make_segments(line, seg_len: float):
    segs = []
    for part in getattr(line, "geoms", [line]):
        n = max(1, round(part.length / seg_len))
        step = part.length / n
        for i in range(n):
            segs.append(substring(part, i * step, (i + 1) * step))
    return segs


def make_grid(aoi):
    minx, miny, maxx, maxy = aoi.bounds
    minx, miny = np.floor([minx / RES, miny / RES]) * RES
    maxx, maxy = np.ceil([maxx / RES, maxy / RES]) * RES
    w, h = int((maxx - minx) / RES), int((maxy - miny) / RES)
    tf = from_origin(minx, maxy, RES, RES)
    mask = rasterize([(aoi, 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool)
    return tf, w, h, mask


def read_band(href, tf, w, h, resampling):
    with rasterio.open(href) as src, WarpedVRT(src, crs=UTM, transform=tf, width=w, height=h,
                                                resampling=resampling) as vrt:
        return vrt.read(1)


def read_scene(item, tf, w, h):
    """Return reflectance arrays (green, red, nir) and a validity mask for one scene."""
    scl = read_band(item.assets["SCL"].href, tf, w, h, Resampling.nearest)
    bands = [read_band(item.assets[b].href, tf, w, h, Resampling.bilinear).astype("float32")
             for b in ("B03", "B04", "B08")]
    # Processing baseline 04.00+ (2022 onwards) adds a +1000 offset to digital numbers.
    offset = 1000 if float(item.properties.get("s2:processing_baseline", "0")) >= 4.0 else 0
    green, red, nir = [(b - offset) / 10000 for b in bands]
    valid = np.isin(scl, SCL_OK) & (bands[0] > 0)
    return green, red, nir, valid


def search(river, since: date, until: date, det):
    cat = pystac_client.Client.open(STAC_URL,
                                    modifier=planetary_computer.sign_inplace)
    items = cat.search(collections=["sentinel-2-l2a"], bbox=river["bbox"],
                       datetime=f"{since.isoformat()}/{until.isoformat()}",
                       query={"eo:cloud_cover": {"lt": det["max_scene_cloud"]}}).item_collection()
    by_day = defaultdict(list)
    for it in items:
        by_day[it.datetime.date().isoformat()].append(it)
    return dict(sorted(by_day.items()))


def read_day(items, tf, w, h):
    green = np.full((h, w), np.nan, "float32")
    red, nir = green.copy(), green.copy()
    for it in items:  # mosaic tiles for the same day; first valid pixel wins
        g, r, n, v = read_scene(it, tf, w, h)
        fill = v & np.isnan(green)
        green[fill], red[fill], nir[fill] = g[fill], r[fill], n[fill]
    return green, red, nir


def day_pixels(river_id: str, d: str, items, tf, w, h, mask):
    """NDVI, NDTI and NDWI of every river pixel on one date (NaN where unusable).

    Cached locally (data/cache/, gitignored) as float16 so methods and thresholds can be
    changed and the history rebuilt without downloading the images again.
    """
    path = ROOT / f"data/cache/{river_id}/{d}.npz"
    n = int(mask.sum())
    if path.exists():
        z = np.load(path)
        if "ndvi" in z and z["ndvi"].shape == (n,):
            return {k: z[k].astype("float32") for k in ("ndvi", "ndti", "ndwi")}
    green, red, nir = (b[mask] for b in read_day(items, tf, w, h))
    with np.errstate(invalid="ignore", divide="ignore"):
        px = {"ndvi": (nir - red) / (nir + red),
              "ndti": (red - green) / (red + green),
              "ndwi": (green - nir) / (green + nir)}
    # Zero or negative reflectance (sensor saturation, nodata edges) gives meaningless indices.
    bad = ~((green > 0) & (red > 0) & (nir > 0))
    for a in px.values():
        a[bad | ~np.isfinite(a)] = np.nan
    px = {k: v.astype("float16") for k, v in px.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **px)
    # Use the stored precision now too, so a rebuild from cache gives identical numbers.
    return {k: v.astype("float32") for k, v in px.items()}


def day_quality(river_id: str, d: str, items, tf, w, h, mask):
    """Haze and cloud around the river on one date, as reported with the image itself.

    aot: mean aerosol optical thickness (haze; usually 0.1-0.6 here). cloud_share: share of the
    river corridor flagged as cloud, thin cirrus or cloud shadow; their edges leak into nearby
    pixels. Cached in data/cache/<river>/quality.json.
    """
    path = ROOT / f"data/cache/{river_id}/quality.json"
    q = json.loads(path.read_text()) if path.exists() else {}
    if d not in q:
        aot = read_band(items[0].assets["AOT"].href, tf, w, h, Resampling.nearest)[mask].astype("float32")
        scl = read_band(items[0].assets["SCL"].href, tf, w, h, Resampling.nearest)[mask]
        q[d] = {"aot": round(float(aot[aot > 0].mean()) / 1000, 3) if (aot > 0).any() else None,
                "cloud_share": round(float(np.isin(scl, [3, 8, 9, 10]).mean()), 3)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(q, indent=0, sort_keys=True))
    return q[d]


def classify(px, det):
    """Return (ok, hyacinth, water) for the pixel vectors from day_pixels.

    Muddiness is only meaningful on open water: mud banks, sparse weed and brown, dying
    hyacinth also reflect more red than green, so they must not count as muddy water.
    """
    ok = np.isfinite(px["ndvi"]) & np.isfinite(px["ndti"]) & np.isfinite(px["ndwi"])
    hyacinth = ok & (px["ndvi"] > det["hyacinth_ndvi"])
    water = ok & ~hyacinth & (px["ndwi"] > det["water_ndwi"])
    return ok, hyacinth, water


def river_mask_path(river_id: str) -> Path:
    return ROOT / f"data/masks/{river_id}.tif"


def river_area(river_id: str, tf, w, h, mask, aoi):
    """(river pixels, green-channel classes, whether they come from the satellite-derived mask).

    Uses data/masks/<river>.tif (where the images actually showed water, see
    build_river_mask.py), or the OpenStreetMap outline until that has been built. Green-channel
    pixels (inside the outline, green all year, never open water) are not measured, only reported;
    their class is the flood test: 2 survived the floods in place, 3 cleared by one, 4 unclear,
    0 elsewhere.
    """
    path = river_mask_path(river_id)
    if path.exists():
        with rasterio.open(path) as src:
            if (src.width, src.height) == (w, h) and src.transform.almost_equals(tf):
                m = src.read(1)
                return (m == 1) & mask, np.where(mask & (m >= 2), m, 0), True
        print("  river mask does not match the grid; rebuild it with build_river_mask.py")
    osm = rasterize([(aoi, 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool) & mask
    return osm, np.zeros(osm.shape, "uint8"), False


def process_day(px, keep, seg_idx, n_segs, det):
    """Aggregate one date's pixels (vectors over the river area) into river and segment stats.

    keep: pixels that count as river. seg_idx: segment of every pixel.
    """
    ok, hyacinth, water = classify(px, det)
    ok, hyacinth, water = ok & keep, hyacinth & keep, water & keep
    valid_fraction = ok.sum() / keep.sum()
    if valid_fraction < det["min_valid_fraction"]:
        return None
    ndti = px["ndti"]
    # The same count at a stricter and a looser greenness threshold, for the likely range.
    delta = det["range_ndvi_delta"]
    hy_strict = ok & (px["ndvi"] > det["hyacinth_ndvi"] + delta)
    hy_loose = ok & (px["ndvi"] > det["hyacinth_ndvi"] - delta)

    total = np.bincount(seg_idx[ok], minlength=n_segs)
    hy_count = np.bincount(seg_idx[hyacinth], minlength=n_segs)
    strict_count = np.bincount(seg_idx[hy_strict], minlength=n_segs)
    loose_count = np.bincount(seg_idx[hy_loose], minlength=n_segs)
    w_count = np.bincount(seg_idx[water], minlength=n_segs)
    nt_sum = np.bincount(seg_idx[water], weights=ndti[water], minlength=n_segs)
    all_count = np.bincount(seg_idx[keep], minlength=n_segs)

    segs = []
    for i in range(n_segs):
        if total[i] < max(5, 0.5 * all_count[i]):  # too cloudy to say anything
            segs.append(None)
        else:
            nt = round(float(nt_sum[i] / w_count[i]), 4) if w_count[i] >= 5 else None
            segs.append([round(float(hy_count[i] / total[i]), 3), nt,
                         round(float(strict_count[i] / total[i]), 3), round(float(loose_count[i] / total[i]), 3)])
    # Too little open water (low flow, river choked by weed) gives no reliable muddiness figure.
    enough_water = water.sum() >= max(50, 0.05 * ok.sum())
    return {
        "valid_fraction": round(float(valid_fraction), 3),
        "hyacinth_km2": round(float(hyacinth.sum()) * RES * RES / 1e6, 4),
        "hyacinth_pct": round(float(hyacinth.sum() / ok.sum() * 100), 2),
        "hyacinth_pct_strict": round(float(hy_strict.sum() / ok.sum() * 100), 2),
        "hyacinth_pct_loose": round(float(hy_loose.sum() / ok.sum() * 100), 2),
        "open_water_pct": round(float(water.sum() / ok.sum() * 100), 2),
        "mean_ndti": round(float(ndti[water].mean()), 4) if enough_water else None,
        "turbid_pct": round(float((ndti[water] > det["turbid_ndti"]).mean() * 100), 2) if enough_water else None,
        "segments": segs,
    }


# ---------- Sentinel-1 radar ----------
# Open water reflects radar away (dark, ~-20 dB VH); floating plants scatter it back (~-16 dB).
# The signal is too noisy per 500 m stretch at 10 m pixels, so radar is used river-wide only.

def search_radar(river, since: date, until: date):
    cat = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
    items = cat.search(collections=["sentinel-1-rtc"], bbox=river["bbox"],
                       datetime=f"{since.isoformat()}/{until.isoformat()}").item_collection()
    by_day = defaultdict(list)
    for it in items:
        by_day[it.datetime.date().isoformat()].append(it)
    return dict(sorted(by_day.items()))


def process_radar_day(items, tf, w, h, mask, rad):
    vh = np.zeros((h, w), "float32")
    for it in items:
        a = read_band(it.assets["vh"].href, tf, w, h, Resampling.bilinear)
        fill = (vh == 0) & np.isfinite(a) & (a > 0)
        vh[fill] = a[fill]
    ok = mask & (vh > 0)
    if ok.sum() / mask.sum() < rad["min_coverage"]:
        return None
    db = np.clip(10 * np.log10(vh[ok]), RADAR_EDGES[0], RADAR_EDGES[-1] - 1e-3)
    hist, _ = np.histogram(db, RADAR_EDGES)
    return {"hist": hist.tolist(), "n": int(ok.sum()), "scenes": [it.id for it in items]}


def calibrate_radar(history, rad):
    """Pick the VH threshold that best reproduces optical hyacinth % on same-day pairs.

    Returns the threshold, the number of pairs and a leave-one-out error, or None when
    there are too few pairs to trust it.
    """
    optical = {date.fromisoformat(d): v["hyacinth_pct"] for d, v in history["dates"].items()}
    pairs = []
    for d, r in sorted(history.get("radar", {}).items()):
        d = date.fromisoformat(d)
        near = [o for o in optical if abs((o - d).days) <= rad["pair_days"]]
        if near:
            o = min(near, key=lambda o: abs((o - d).days))
            above = np.cumsum(np.asarray(r["hist"])[::-1])[::-1] / r["n"] * 100  # % of pixels >= edge k
            pairs.append((above, optical[o]))
    if len(pairs) < rad["min_pairs"]:
        return None

    candidates = np.nonzero((RADAR_EDGES[:-1] >= -25) & (RADAR_EDGES[:-1] <= -10))[0]
    def best(ps):
        return min(candidates, key=lambda k: np.mean([abs(a[k] - y) for a, y in ps]))
    loo = [abs(pairs[i][0][best(pairs[:i] + pairs[i + 1:])] - pairs[i][1]) for i in range(len(pairs))]
    k = best(pairs)
    return {"bin": int(k), "threshold_db": float(RADAR_EDGES[k]), "pairs": len(pairs),
            "mae_pct": round(float(np.mean(loo)), 1)}


def radar_pct(r, k):
    return round(float(sum(r["hist"][k:]) / r["n"] * 100), 1)


def update_radar(river, rad, history, since, until, tf, w, h, mask, save):
    days = search_radar(river, since, until)
    done = set(history.setdefault("radar", {})) | set(history.setdefault("radar_skipped", []))
    todo = {d: v for d, v in days.items() if d not in done}
    print(f"  radar: {len(days)} passes, {len(todo)} new")
    for d, items in todo.items():
        try:
            res = process_radar_day(items, tf, w, h, mask, rad)
        except Exception as e:
            print(f"  radar {d}: error {type(e).__name__}: {e}")
            continue
        if res is None:
            history["radar_skipped"].append(d)
            print(f"  radar {d}: partial coverage, skipped")
        else:
            history["radar"][d] = res
            print(f"  radar {d}: ok")
        save()


def run_river(river, cfg, since: date | None):
    det, rad = cfg["detection"], cfg.get("radar")
    line, aoi, corridor = load_river(river, det)
    segments = make_segments(line, det["segment_length_m"])
    tf, w, h, mask = make_grid(corridor)
    river_px, green_ch, from_images = river_area(river["id"], tf, w, h, mask, aoi)
    area_id = hashlib.sha1(np.packbits(river_px).tobytes()).hexdigest()[:12]
    keep = river_px[mask]
    # Pixels on the edge of the river area mix water and bank, so they are less reliable.
    inner = river_px.copy()
    inner[1:] &= river_px[:-1]
    inner[:-1] &= river_px[1:]
    inner[:, 1:] &= river_px[:, :-1]
    inner[:, :-1] &= river_px[:, 1:]
    edge = (river_px & ~inner)[mask]

    # Assign every river pixel to its nearest segment once.
    rows, cols = np.nonzero(mask)
    xs, ys = rasterio.transform.xy(tf, rows, cols)
    tree = STRtree(segments)
    seg_idx = tree.query_nearest(points(xs, ys), return_distance=False, all_matches=False)[1]

    hist_path = ROOT / f"data/history/{river['id']}.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else {"dates": {}, "skipped": []}
    rebuild_from = None
    layout = {"n_segments": len(segments), "river_area": area_id}
    old_layout = {k: history.get(k) for k in layout}
    if history["dates"] and (old_layout != layout or history.get("method", 1) != METHOD):
        # Every stored number depends on which pixels count and how, so recompute all of it.
        # Radar only depends on which pixels count.
        old = list(history["dates"]) + history["skipped"] + list(history.get("radar", {}))
        rebuild_from = date.fromisoformat(min(old)) if old else None
        print(f"  pixel layout or method changed; rebuilding history from {rebuild_from}")
        radar = {} if old_layout != layout else {k: history[k] for k in ("radar", "radar_skipped") if k in history}
        history = {"dates": {}, "skipped": [], **radar}
    history.update(layout, method=METHOD)

    def save():
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(json.dumps(history, separators=(",", ":"), allow_nan=False))

    until = datetime.now(timezone.utc).date()
    since = since or until - timedelta(days=det["lookback_days"])
    if rebuild_from:
        since = min(since, rebuild_from)
    days = search(river, since, until, det)
    done = set(history["dates"]) | set(history["skipped"])
    todo = {d: v for d, v in days.items() if d not in done}
    print(f"  {len(days)} candidate dates, {len(todo)} new")

    for d, items in todo.items():
        try:
            px = day_pixels(river["id"], d, items, tf, w, h, mask)
            res = process_day(px, keep, seg_idx, len(segments), det)
            if res is not None:
                res.update(day_quality(river["id"], d, items, tf, w, h, mask))
        except Exception as e:  # one bad scene must not stop the scheduled run
            print(f"  {d}: error {type(e).__name__}: {e}")
            continue
        if res is None:
            history["skipped"].append(d)
            print(f"  {d}: too cloudy over river, skipped")
        else:
            history["dates"][d] = {**res, "scenes": [it.id for it in items]}
            print(f"  {d}: hyacinth {res['hyacinth_pct']}%  turbid {res['turbid_pct']}%")
        save()

    if rad:
        try:  # radar is a bonus layer; an outage there must not block the optical update
            update_radar(river, rad, history, since, until, tf, w, h, river_px, save)
        except Exception as e:
            print(f"  radar: skipped this run ({type(e).__name__}: {e})")

    n = len(segments)
    write_web(river, cfg, segments, history, area={
        "px": np.bincount(seg_idx[keep], minlength=n),
        "edge_px": np.bincount(seg_idx[keep & edge], minlength=n),
        "green_px": np.bincount(seg_idx[green_ch[mask] > 0], minlength=n),
        # Flood test for the green channel, per segment: survived / cleared / unclear pixels.
        "flood_px": {k: np.bincount(seg_idx[green_ch[mask] == v], minlength=n)
                     for k, v in (("survived", 2), ("cleared", 3), ("unclear", 4))},
        "from_images": from_images})


def segment_places(river, segments, max_dist_m=3000):
    """Name each segment after the nearest OSM neighbourhood/village, if one is close enough."""
    path = ROOT / f"data/rivers/{river['id']}.places.geojson"
    if not path.exists():
        return [None] * len(segments)
    feats = json.loads(path.read_text())["features"]
    pts = [to_utm(shape(f["geometry"])) for f in feats]
    tree = STRtree(pts)
    names = []
    for seg in segments:
        mid = seg.interpolate(0.5, normalized=True)
        j = tree.nearest(mid)
        p = feats[j]["properties"]
        names.append((p["name"], p.get("name_mr")) if pts[j].distance(mid) <= max_dist_m else (None, None))
    return names


def segment_regions(segments, regions, buffer_m=150, min_share=0.2):
    """Tag each segment with the municipal corporation(s) on its banks and the ward names.

    A 150 m corridor around the stretch is overlaid on each corporation's wards; any
    corporation covering at least 20% of it counts. Rivers often form the boundary, so a
    stretch can belong to two (a border stretch). Returns [(region ids, ward names)].
    """
    loaded = []
    for r in regions:
        feats = json.loads((ROOT / r["file"]).read_text())["features"]
        wards = [(f["properties"][r["ward_field"]], to_utm(shape(f["geometry"]).buffer(0))) for f in feats]
        loaded.append((r["id"], unary_union([g for _, g in wards]), wards))
    out = []
    for seg in segments:
        corridor = seg.buffer(buffer_m)
        ids, wards = [], []
        for rid, whole, ws in loaded:
            if corridor.intersection(whole).area / corridor.area >= min_share:
                ids.append(rid)
                wards.append(max(ws, key=lambda w: corridor.intersection(w[1]).area)[0])
        out.append((ids, wards))
    return out


def outlook(dates, matrix, river_pct, cfg):
    """2-week outlook for the river and each segment, with backtested error.

    Level = median of the last 3 clear observations within 45 days. Every past date is
    replayed as if it were today and compared with what was seen ~horizon days later; those
    errors give the published accuracy, the river's likely range, and per segment the
    chance of being above alert_pct.
    """
    H, alert = cfg["horizon_days"], cfg["alert_pct"]
    t = np.array([date.fromisoformat(d).toordinal() for d in dates])

    def level(y, i):
        m = (t <= t[i]) & (t > t[i] - 45) & np.isfinite(y)
        return float(np.median(y[m][-3:])) if m.sum() >= 3 else None

    def backtest(y):
        res = []  # (predicted, actual - predicted, last value error)
        for i in range(len(t)):
            near = np.nonzero((np.abs(t - (t[i] + H)) <= 4) & np.isfinite(y))[0]
            p = level(y, i)
            if p is None or not len(near):
                continue
            j = near[np.argmin(np.abs(t[near] - (t[i] + H)))]
            last = y[(t <= t[i]) & np.isfinite(y)][-1]
            res.append((p, y[j] - p, abs(y[j] - last)))
        return np.array(res).reshape(-1, 3)

    target = date.fromordinal(int(t[-1]) + H)
    # The monsoon flood (mid-June to September) flushes hyacinth; no level-based outlook holds.
    monsoon = (6, 15) <= (target.month, target.day) <= (9, 30)
    riv = backtest(np.asarray(river_pct, float))
    segs = [backtest(matrix[:, k]) for k in range(matrix.shape[1])]
    pooled = np.concatenate([b for b in segs if len(b)]) if any(len(b) for b in segs) else np.zeros((0, 3))
    if len(riv) < 10 or monsoon:
        return {"horizon_days": H, "target_date": target.isoformat(), "monsoon": monsoon}, [None] * matrix.shape[1]

    p = level(np.asarray(river_pct, float), len(t) - 1)
    lo, hi = np.percentile(riv[:, 1], [10, 90])
    bands = [0, 10, 25, 50, 101]
    seg_out = []
    for k in range(matrix.shape[1]):
        ps = level(matrix[:, k], len(t) - 1)
        if ps is None:
            seg_out.append(None)
            continue
        b = np.digitize(ps, bands)
        r = pooled[np.digitize(pooled[:, 0], bands) == b, 1]
        r = r if len(r) >= 30 else pooled[:, 1]
        seg_out.append({"pct": round(ps, 1), "p_alert": round(float(np.mean(ps + r >= alert)), 2)})
    return {
        "horizon_days": H, "target_date": target.isoformat(), "monsoon": False, "alert_pct": alert,
        "river": {"pct": round(p, 1), "low": round(max(0, p + lo), 1), "high": round(min(100, p + hi), 1)},
        "mae_river": round(float(np.mean(np.abs(riv[:, 1]))), 1),
        "mae_river_last": round(float(np.mean(riv[:, 2])), 1),
        "mae_segment": round(float(np.mean(np.abs(pooled[:, 1]))), 1),
        "backtests": len(riv),
    }, seg_out


def confidence(px, edge_px, green_px, from_images, stale, cfg):
    """How far to trust one segment's numbers: ("high" | "medium" | "low", reasons).

    Reasons: "small" (little river area to measure), "edges" (mostly pixels on the river edge,
    which mix water and bank), "green_channel" (much of the channel is green all year and not
    measured), "outline" (river area from the OSM outline, not seen from space), "stale" (no
    clear view for over a month).
    """
    reasons = []
    edge_share = edge_px / px if px else 1
    if green_px / (px + green_px) >= cfg["green_channel_share"]:
        reasons.append("green_channel")
    if px < cfg["high_px"]:
        reasons.append("small")
    if edge_share > cfg["high_edge_share"]:
        reasons.append("edges")
    if not from_images:
        reasons.append("outline")
    if stale:
        reasons.append("stale")
    if px < cfg["low_px"] or edge_share > cfg["low_edge_share"] or len(reasons) >= 2:
        return "low", reasons
    return ("medium" if reasons else "high"), reasons


def flood_verdict(px):
    """Verdict for a segment's green channel from its flood-test pixel counts, or None.

    "rooted": most of it (>= 70%) survived every monsoon flood in place, as rooted riverbed plants
    do (or a pocket the floods don't reach); "floating": at least half was washed out, as
    floating hyacinth is; "unclear": mostly pixels the test can't settle; otherwise "mixed".
    """
    total = sum(px.values())
    if total < 20:
        return None
    share = {k: v / total for k, v in px.items()}
    verdict = ("rooted" if share["survived"] >= 0.7 else "floating" if share["cleared"] >= 0.5
               else "unclear" if share["unclear"] >= 0.5 else "mixed")
    return {"verdict": verdict, **{f"{k}_ha": round(v * RES * RES / 1e4, 1) for k, v in px.items()}}


def reject_dates(history, dates, cfg):
    """Dates to leave out, with the reason: "haze" or "cloud" (see the quality config)."""
    t = np.array([date.fromisoformat(d).toordinal() for d in dates])
    z = np.zeros(len(dates))
    for key in ("open_water_pct", "hyacinth_pct"):
        v = np.array([history["dates"][d][key] for d in dates], float)
        dev = np.full(len(dates), np.nan)
        for i in range(len(dates)):
            near = (np.abs(t - t[i]) <= 21) & (np.arange(len(dates)) != i)
            if near.sum() >= 2:
                dev[i] = v[i] - np.median(v[near])
        scale = 1.4826 * np.nanmedian(np.abs(dev))  # robust standard deviation
        z = np.fmax(z, np.abs(dev) / scale) if scale else z
    out = {}
    for i, d in enumerate(dates):
        e = history["dates"][d]
        if (e.get("aot") or 0) > cfg["max_aot"]:
            out[d] = "haze"
        elif (e.get("cloud_share") or 0) >= cfg["cloud_share"] and z[i] > cfg["max_z"]:
            out[d] = "cloud"
    return out


def one_off_jumps(dates, hy, cfg):
    """(spike, unconfirmed): readings that jump away and straight back, and latest readings
    that jump with no later image yet to confirm them. hy: % per date x segment (NaN = none)."""
    t = np.array([date.fromisoformat(d).toordinal() for d in dates])
    th = cfg["spike_pts"]
    spike = np.zeros(hy.shape, bool)
    unconfirmed = np.zeros(hy.shape[1], bool)
    for k in range(hy.shape[1]):
        seen = np.nonzero(np.isfinite(hy[:, k]))[0]
        for a, b, c in zip(seen, seen[1:], seen[2:]):
            if t[b] - t[a] > 14 or t[c] - t[b] > 14:
                continue
            p, v, n = hy[a, k], hy[b, k], hy[c, k]
            if abs(v - p) > th and abs(v - n) > th and np.sign(v - p) == np.sign(v - n) and abs(p - n) < th / 2:
                spike[b, k] = True
        if len(seen) >= 4 and t[seen[-1]] - t[seen[-2]] <= 14:
            unconfirmed[k] = abs(hy[seen[-1], k] - np.median(hy[seen[-4:-1], k])) > th
    return spike, unconfirmed


def repeat_noise(dates, values):
    """Typical measurement noise of one reading, from images taken 3 days or less apart.

    Hyacinth hardly changes in 3 days, so their difference is mostly noise. For two readings with
    independent errors, mean |difference| = 2 sigma / sqrt(pi). Returns (sigma, number of pairs).
    """
    t = [date.fromisoformat(d) for d in dates]
    diffs = []
    for i in range(len(dates) - 1):
        if (t[i + 1] - t[i]).days <= 3:
            a, b = values[i], values[i + 1]
            ok = np.isfinite(a) & np.isfinite(b)
            diffs.extend(np.abs(np.asarray(a)[ok] - np.asarray(b)[ok]).ravel())
    return (float(np.mean(diffs)) * np.sqrt(np.pi) / 2, len(diffs)) if diffs else (None, 0)


def write_web(river, cfg, segments, history, area):
    det, rad, qual = cfg["detection"], cfg.get("radar"), cfg["quality"]
    seg_px = area["px"]
    all_dates = sorted(history["dates"])
    places = segment_places(river, segments)
    regions = segment_regions(segments, cfg.get("regions", []))
    if not all_dates:
        print("  no usable dates yet")
        return
    rejected = reject_dates(history, all_dates, qual)
    dates = [d for d in all_dates if d not in rejected]
    # Hyacinth % per date x segment (NaN where cloudy), at the standard, strict and loose
    # thresholds; the base for everything below.
    def matrix(j):
        return np.array([[np.nan if v is None else v[j] * 100 for v in history["dates"][d]["segments"]]
                         for d in dates])
    hy_raw, hy_strict, hy_loose = matrix(0), matrix(2), matrix(3)
    spike, unconfirmed = one_off_jumps(dates, hy_raw, qual)
    hy = np.where(spike, np.nan, hy_raw)  # one-off jumps are shown but not used in any figure

    last = date.fromisoformat(dates[-1])
    latest = [None] * len(segments)  # (date index) of the latest usable reading per segment
    for i in range(len(dates)):
        for k in np.nonzero(np.isfinite(hy[i]))[0]:
            latest[k] = i
    # Change vs ~30 days earlier, per segment (only from observations 30-60 days old).
    t = np.array([(last - date.fromisoformat(d)).days for d in dates])
    previous = [None] * len(segments)
    for i in np.nonzero((t >= 30) & (t <= 60))[0]:
        for k in np.nonzero(np.isfinite(hy[i]))[0]:
            previous[k] = hy[i, k]

    out_cfg = cfg.get("outlook")
    river_pct = [history["dates"][d]["hyacinth_pct"] for d in dates]
    look, seg_look = outlook(dates, hy, river_pct, out_cfg) if out_cfg else (None, [None] * len(segments))

    # Confidence first: the likely range of a stretch depends on the noise of its class.
    conf = {}
    for k in range(len(segments)):
        if latest[k] is not None:
            stale = (last - date.fromisoformat(dates[latest[k]])).days > 30
            conf[k] = confidence(int(area["px"][k]), int(area["edge_px"][k]), int(area["green_px"][k]),
                                 area["from_images"], stale, cfg["confidence"])
    z80 = 1.2816  # half-width of an 80% range, in standard deviations
    river_sigma, river_pairs = repeat_noise(dates, [np.array([v]) for v in river_pct])
    seg_sigma = {}
    for level in ("high", "medium", "low"):
        cols = [k for k, (c, _) in conf.items() if c == level]
        seg_sigma[level] = repeat_noise(dates, list(hy[:, cols])) if cols else (None, 0)

    def likely(lo_val, hi_val, sigma):
        pad = z80 * (sigma or 0)
        return [round(max(0.0, lo_val - pad), 1), round(min(100.0, hi_val + pad), 1)]

    features = []
    for i, seg in enumerate(segments):
        # Round to ~10 cm so output is byte-identical across machines (no spurious commits).
        geom = shapely.transform(to_wgs(seg.simplify(5)), lambda c: np.round(c, 6))
        base = {"id": i, "place": places[i][0], "place_mr": places[i][1],
                "regions": regions[i][0], "wards": regions[i][1],
                "km": round(i * det["segment_length_m"] / 1000, 1),
                "area_ha": round(float(area["px"][i]) * RES * RES / 1e4, 1),
                # Channel green on nearly every clear image: weed or riverbed plants. The flood test
                # says which: floating hyacinth is washed out by the monsoon, rooted plants stay.
                "green_channel_ha": round(float(area["green_px"][i]) * RES * RES / 1e4, 1),
                "flood_test": flood_verdict({k: int(v[i]) for k, v in area["flood_px"].items()})}
        if latest[i] is None:
            # Shown greyed out with the reason, rather than silently missing from the map.
            features.append({"type": "Feature", "geometry": mapping(geom), "properties": {
                **base, "measurable": False, "hyacinth_pct": None,
                "why": "green_channel" if area["green_px"][i] >= 20 else
                       "narrow" if area["px"][i] < 5 else "no_clear_view"}})
            continue
        j = latest[i]
        d, h = dates[j], hy[j, i]
        nt = history["dates"][d]["segments"][i][1]
        c, why = conf[i]
        prev = previous[i]
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                **base, "measurable": True, "date": d,
                "hyacinth_pct": round(float(h), 1),
                "range": likely(hy_strict[j, i], hy_loose[j, i], seg_sigma[c][0]),
                # Latest reading jumped and no later image has confirmed it yet.
                "unconfirmed": bool(unconfirmed[i]) and j == len(dates) - 1,
                "ndti": nt,
                "change_pct": None if prev is None else round(float(h - prev), 1),
                "outlook": seg_look[i],
                "confidence": c, "confidence_why": why,
            },
        })

    # Per-region hyacinth % per date, weighted by segment size; only when most of it was seen.
    region_out = []
    for r in cfg.get("regions", []) + [{"id": "other", "name": "Outside PMC/PCMC", "name_mr": "मनपा हद्दीबाहेर"}]:
        member = np.array([(r["id"] in ids) if r["id"] != "other" else not ids for ids, _ in regions])
        if not member.any():
            continue
        wts = seg_px * member
        seen = np.isfinite(hy) * wts
        val = np.nansum(np.nan_to_num(hy) * wts, axis=1) / np.maximum(seen.sum(axis=1), 1)
        ok = seen.sum(axis=1) >= 0.5 * wts.sum()
        region_out.append({
            "id": r["id"], "name": r["name"], "name_mr": r.get("name_mr"),
            "n_segments": int(member.sum()), "km": round(member.sum() * det["segment_length_m"] / 1000, 1),
            "series": [{"date": d, "hyacinth_pct": round(float(v), 2)} for d, v, k in zip(dates, val, ok) if k],
        })

    keys = ("hyacinth_pct", "hyacinth_km2", "open_water_pct", "turbid_pct", "mean_ndti", "valid_fraction",
            "aot", "cloud_share")
    # scene: the Sentinel-2 image behind each date, so anyone can look at the photo itself.
    series = [{"date": d, **{k: history["dates"][d].get(k) for k in keys},
               "range": likely(history["dates"][d]["hyacinth_pct_strict"], history["dates"][d]["hyacinth_pct_loose"],
                               river_sigma),
               "scene": history["dates"][d]["scenes"][0]} for d in dates]
    out = {
        "river": {"id": river["id"], "name": river["name"], "name_mr": river.get("name_mr"), "bbox": river["bbox"]},
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "latest_date": dates[-1],
        "thresholds": {"hyacinth_ndvi": det["hyacinth_ndvi"], "turbid_ndti": det["turbid_ndti"]},
        # Whole-river evidence behind the flood test (see build_river_mask.py).
        "flood_test": (json.loads(river_mask_path(river["id"]).with_suffix(".json").read_text())
                       if river_mask_path(river["id"]).with_suffix(".json").exists() else None),
        "river_area": {"from_images": area["from_images"],
                       "km2": round(float(area["px"].sum()) * RES * RES / 1e6, 2),
                       "green_channel_km2": round(float(area["green_px"].sum()) * RES * RES / 1e6, 2)},
        "segments": {"type": "FeatureCollection", "features": features},
        "series": series,
        "regions": region_out,
        # Images left out of every figure, and why (shown in the data table).
        "rejected_dates": [{"date": d, "reason": rejected[d], "aot": history["dates"][d].get("aot"),
                            "cloud_share": history["dates"][d].get("cloud_share"),
                            "hyacinth_pct": history["dates"][d]["hyacinth_pct"],
                            "scene": history["dates"][d]["scenes"][0]} for d in sorted(rejected)],
        # How far apart two images of the same thing land, and how the greenness threshold
        # moves the figures; together they make the likely ranges.
        "uncertainty": {
            "range_ndvi": [round(det["hyacinth_ndvi"] - det["range_ndvi_delta"], 2),
                           round(det["hyacinth_ndvi"] + det["range_ndvi_delta"], 2)],
            "river_noise_pts": None if river_sigma is None else round(z80 * river_sigma, 1),
            "river_pairs": river_pairs,
            "segment_noise_pts": {k: None if v[0] is None else round(z80 * v[0], 1) for k, v in seg_sigma.items()},
            "one_off_jumps": int(spike.sum()), "readings": int(np.isfinite(hy_raw).sum()),
        },
        # Every segment's hyacinth % on every clear date, for the map's date slider; one-off
        # jumps are kept here (marked) so they can be seen, but not used elsewhere.
        "segment_history": {"dates": dates, "hyacinth_pct": [
            [None if np.isnan(v) else round(float(v), 1) for v in row] for row in hy_raw],
            "one_off": [[int(i), int(k)] for i, k in zip(*np.nonzero(spike))]},
    }
    if look:
        out["outlook"] = look
    cal = calibrate_radar({**history, "dates": {d: history["dates"][d] for d in dates}}, rad) if rad else None
    if cal:
        out["radar"] = {
            "threshold_db": cal["threshold_db"], "pairs": cal["pairs"], "mae_pct": cal["mae_pct"],
            "series": [{"date": d, "hyacinth_pct": radar_pct(r, cal["bin"])}
                       for d, r in sorted(history["radar"].items())],
        }
    web = ROOT / "web/data"
    web.mkdir(parents=True, exist_ok=True)
    out_path = web / f"{river['id']}.json"
    text = json.dumps(out, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if out_path.exists():
        # Compare everything except the timestamp and segment geometry: geometry only changes
        # when a river outline is edited by hand, and its last digits vary across platforms.
        def comparable(o):
            feats = [f["properties"] for f in o["segments"]["features"]]
            return {**o, "updated": None, "segments": feats}
        if comparable(json.loads(out_path.read_text())) == comparable(json.loads(text)):
            print("  no new data; dashboard unchanged")
            return
    out_path.write_text(text)

    index_path = web / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"rivers": []}
    index["rivers"] = [r for r in index["rivers"] if r["id"] != river["id"]]
    index["rivers"].append({"id": river["id"], "name": river["name"], "name_mr": river.get("name_mr"),
                            "latest_date": dates[-1]})
    index["rivers"].sort(key=lambda r: r["name"])
    index_path.write_text(json.dumps(index, indent=1, ensure_ascii=False))
    print(f"  wrote web/data/{river['id']}.json ({len(features)} segments, {len(dates)} dates)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=date.fromisoformat, help="backfill from this date")
    ap.add_argument("rivers", nargs="*", help="river ids (default: all)")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    for river in cfg["rivers"]:
        if args.rivers and river["id"] not in args.rivers:
            continue
        print(f"{river['name']}")
        run_river(river, cfg, args.since)


if __name__ == "__main__":
    main()
