"""Scheduled pipeline: Sentinel-2 -> hyacinth & turbidity per river segment.

For each river in config/rivers.yaml:
  1. find new cloud-light Sentinel-2 L2A scenes (Microsoft Planetary Computer, free)
  2. read green/red/NIR + scene classification over the river only
  3. mask clouds, shadows, river banks and year-round green pixels (see build_bank_mask.py)
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
METHOD = 2
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
    return line, aoi


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


def classify(px, det):
    """Return (ok, hyacinth, water) for the pixel vectors from day_pixels.

    Muddiness is only meaningful on open water: mud banks, sparse weed and brown, dying
    hyacinth also reflect more red than green, so they must not count as muddy water.
    """
    ok = np.isfinite(px["ndvi"]) & np.isfinite(px["ndti"]) & np.isfinite(px["ndwi"])
    hyacinth = ok & (px["ndvi"] > det["hyacinth_ndvi"])
    water = ok & ~hyacinth & (px["ndwi"] > det["water_ndwi"])
    return ok, hyacinth, water


def bank_mask_path(river_id: str) -> Path:
    return ROOT / f"data/masks/{river_id}.tif"


def load_bank_mask(river_id: str, tf, w, h):
    """Pixels that are green all year (trees, islands, farmed sandbars), or None if not built yet."""
    path = bank_mask_path(river_id)
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        if (src.width, src.height) != (w, h) or not src.transform.almost_equals(tf):
            print("  bank mask does not match the river grid; rebuild it with build_bank_mask.py")
            return None
        return src.read(1).astype(bool)


def process_day(px, keep, seg_idx, n_segs, det):
    """Aggregate one date's pixels (vectors over the river area) into river and segment stats.

    keep: pixels that count (not fixed vegetation). seg_idx: segment of every pixel.
    """
    ok, hyacinth, water = classify(px, det)
    ok, hyacinth, water = ok & keep, hyacinth & keep, water & keep
    valid_fraction = ok.sum() / keep.sum()
    if valid_fraction < det["min_valid_fraction"]:
        return None
    ndti = px["ndti"]

    total = np.bincount(seg_idx[ok], minlength=n_segs)
    hy_count = np.bincount(seg_idx[hyacinth], minlength=n_segs)
    w_count = np.bincount(seg_idx[water], minlength=n_segs)
    nt_sum = np.bincount(seg_idx[water], weights=ndti[water], minlength=n_segs)
    all_count = np.bincount(seg_idx[keep], minlength=n_segs)

    segs = []
    for i in range(n_segs):
        if total[i] < max(5, 0.5 * all_count[i]):  # too cloudy to say anything
            segs.append(None)
        else:
            nt = round(float(nt_sum[i] / w_count[i]), 4) if w_count[i] >= 5 else None
            segs.append([round(float(hy_count[i] / total[i]), 3), nt])
    # Too little open water (low flow, river choked by weed) gives no reliable muddiness figure.
    enough_water = water.sum() >= max(50, 0.05 * ok.sum())
    return {
        "valid_fraction": round(float(valid_fraction), 3),
        "hyacinth_km2": round(float(hyacinth.sum()) * RES * RES / 1e6, 4),
        "hyacinth_pct": round(float(hyacinth.sum() / ok.sum() * 100), 2),
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
    line, aoi = load_river(river, det)
    segments = make_segments(line, det["segment_length_m"])
    tf, w, h, mask = make_grid(aoi)
    bank = load_bank_mask(river["id"], tf, w, h)
    river_px = mask if bank is None else mask & ~bank
    bank_id = None if bank is None else hashlib.sha1(np.packbits(bank).tobytes()).hexdigest()[:12]

    keep = np.ones(int(mask.sum()), bool) if bank is None else ~bank[mask]

    # Assign every river pixel to its nearest segment once.
    rows, cols = np.nonzero(mask)
    xs, ys = rasterio.transform.xy(tf, rows, cols)
    tree = STRtree(segments)
    seg_idx = tree.query_nearest(points(xs, ys), return_distance=False, all_matches=False)[1]

    hist_path = ROOT / f"data/history/{river['id']}.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else {"dates": {}, "skipped": []}
    rebuild_from = None
    layout = {"n_segments": len(segments), "bank_mask": bank_id}
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

    write_web(river, cfg, segments, np.bincount(seg_idx[keep], minlength=len(segments)), history)


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


def write_web(river, cfg, segments, seg_px, history):
    det, rad = cfg["detection"], cfg.get("radar")
    dates = sorted(history["dates"])
    places = segment_places(river, segments)
    regions = segment_regions(segments, cfg.get("regions", []))
    if not dates:
        print("  no usable dates yet")
        return
    # Hyacinth % per date x segment (NaN where cloudy), the base for everything below.
    hy = np.array([[np.nan if v is None else v[0] * 100 for v in history["dates"][d]["segments"]]
                   for d in dates])
    # Latest known value per segment (a cloudy segment falls back to its previous observation).
    latest = [None] * len(segments)
    for d in dates:
        for i, v in enumerate(history["dates"][d]["segments"]):
            if v is not None:
                latest[i] = (d, v)

    # Change vs ~30 days earlier, per segment (only from observations 30-60 days old).
    last = date.fromisoformat(dates[-1])
    ref_cut = (last - timedelta(days=30)).isoformat()
    ref_start = (last - timedelta(days=60)).isoformat()
    previous = [None] * len(segments)
    for d in dates:
        if d > ref_cut:
            break
        if d < ref_start:
            continue
        for i, v in enumerate(history["dates"][d]["segments"]):
            if v is not None:
                previous[i] = v

    out_cfg = cfg.get("outlook")
    look, seg_look = outlook(dates, hy, [history["dates"][d]["hyacinth_pct"] for d in dates], out_cfg) \
        if out_cfg else (None, [None] * len(segments))

    features = []
    for i, seg in enumerate(segments):
        if latest[i] is None:
            continue
        d, (h, nt) = latest[i]
        prev = previous[i]
        # Round to ~10 cm so output is byte-identical across machines (no spurious commits).
        geom = shapely.transform(to_wgs(seg.simplify(5)), lambda c: np.round(c, 6))
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "id": i, "date": d, "place": places[i][0], "place_mr": places[i][1],
                "regions": regions[i][0], "wards": regions[i][1],
                "hyacinth_pct": round(h * 100, 1),
                "ndti": nt,
                "change_pct": None if prev is None else round((h - prev[0]) * 100, 1),
                "km": round(i * det["segment_length_m"] / 1000, 1),
                "outlook": seg_look[i],
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

    keys = ("hyacinth_pct", "hyacinth_km2", "open_water_pct", "turbid_pct", "mean_ndti", "valid_fraction")
    series = [{"date": d, **{k: history["dates"][d].get(k) for k in keys}} for d in dates]
    out = {
        "river": {"id": river["id"], "name": river["name"], "name_mr": river.get("name_mr"), "bbox": river["bbox"]},
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "latest_date": dates[-1],
        "thresholds": {"hyacinth_ndvi": det["hyacinth_ndvi"], "turbid_ndti": det["turbid_ndti"]},
        "segments": {"type": "FeatureCollection", "features": features},
        "series": series,
        "regions": region_out,
        # Every segment's hyacinth % on every clear date, for the map's date slider.
        "segment_history": {"dates": dates, "hyacinth_pct": [
            [None if np.isnan(v) else round(float(v), 1) for v in row] for row in hy]},
    }
    if look:
        out["outlook"] = look
    cal = calibrate_radar(history, rad) if rad else None
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
