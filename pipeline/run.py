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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import planetary_computer
import requests
import pystac_client
import rasterio
import shapely
import yaml
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely import STRtree, points
from shapely.geometry import box, mapping, shape
from shapely.ops import substring, unary_union

from sources import (SCL_OK, STAC_URL, ES_URL, ES_COLLECTION, S2_ASSETS, read_band, s2_source, scene_id, read_scene,
                     QA_BAD, landsat_scenes, landsat_indices, search, read_day, day_pixels, day_quality, read_quality, prefetch)  # noqa: F401 (re-exported)

ROOT = Path(__file__).resolve().parent.parent
UTM = "EPSG:32643"  # UTM 43N covers all of Maharashtra
RES = 10
# Radar backscatter histogram bins (VH, dB). Storing the histogram lets the threshold be
# re-calibrated later without downloading the images again.
RADAR_EDGES = np.round(np.arange(-35, 5.001, 0.25), 2)
# Bump when the way history numbers are computed changes; run.py then rebuilds the optical
# history (from the local pixel cache where it exists).
METHOD = 5  # 5: weed cover as the covered share of each pixel, not all-or-nothing


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


def inner_pixels(river_px):
    """River pixels at least 20 m from the edge: there a whole 30 m Landsat pixel is river, so it
    doesn't mix in bank plants (on these 40-60 m rivers the edges would add 5-18 points)."""
    inner = river_px.copy()
    for _ in range(2):
        inner[1:] &= inner[:-1].copy(); inner[:-1] &= inner[1:].copy()
        inner[:, 1:] &= inner[:, :-1].copy(); inner[:, :-1] &= inner[:, 1:].copy()
    return inner


def update_landsat_gap(river, det, history, since, until, tf, w, h, mask, keep, seg_idx, n_segs, save):
    """Measure Landsat 8/9 images taken after the newest Sentinel-2 image, so the dashboard has a
    rough figure when Sentinel-2 has none for a while. Same rule as Sentinel-2 (process_day), over
    the inner river pixels only (keep: see inner_pixels). Stored in history["landsat"]; turned
    into a calibrated river-wide estimate by landsat_standin."""
    newest_s2 = max(history["dates"]) if history["dates"] else since.isoformat()
    done = history.setdefault("landsat", {})
    for d in [d for d in done if d <= newest_s2]:  # a newer Sentinel-2 image has closed that gap
        del done[d]
    todo = [it for it in landsat_scenes(river, max(since.isoformat(), newest_s2), until.isoformat())
            if it.datetime.date().isoformat() > newest_s2 and it.datetime.date().isoformat() not in done]
    for it in todo:
        d = it.datetime.date().isoformat()
        try:
            res = process_day(landsat_indices(it, tf, w, h, mask), keep, seg_idx, n_segs, det)
        except Exception as e:
            print(f"  landsat {d}: error {type(e).__name__}: {e}")
            continue
        done[d] = None if res is None else {**res, "scenes": [it.id]}
        print(f"  landsat {d}: " + ("too cloudy over river" if res is None else f"hyacinth {res['hyacinth_pct']}%"))
        save()


def classify(px, det):
    """Return (ok, hyacinth, water) for the pixel vectors from day_pixels.

    Muddiness is only meaningful on open water: mud banks, sparse weed and brown, dying
    hyacinth also reflect more red than green, so they must not count as muddy water.
    """
    ok = np.isfinite(px["ndvi"]) & np.isfinite(px["ndti"]) & np.isfinite(px["ndwi"])
    hyacinth = ok & (px["ndvi"] > det["hyacinth_ndvi"])
    water = ok & ~hyacinth & (px["ndwi"] > det["water_ndwi"])
    return ok, hyacinth, water


def river_det(river, cfg):
    """Detection settings for one river: the shared ones plus its own pure-mat NDVI."""
    return {**cfg["detection"], "mat_ndvi": river["mat_ndvi"]}


def cover_fraction(ndvi, water_ndvi, det, scale=1.0):
    """Share of each pixel covered by floating weed (0-1, NaN where unusable).

    A 10 m pixel partly covered by a mat mixes the mat's reflectance with the water's. In
    reflectance the mix is linear, so with brightness B (red + NIR) and NDVI N of each:
        N_pixel = (f Bm Nm + (1 - f) Bw Nw) / (f Bm + (1 - f) Bw)
    solved for f. Nm is the river's own dense-mat NDVI (mat_ndvi), Nw the open water's that day;
    Bm/Bw is the one assumed number (scale tries it at other values for the likely range). Shares
    under subpixel_floor are set to 0: water pixels scatter a little around Nw. Counting a pixel
    as all weed once it passed NDVI 0.3 over-counted small patches about twofold (planted-weed test).
    """
    bm, bw, nm = det["mat_brightness"] * scale, det["water_brightness"], det["mat_ndvi"]
    with np.errstate(invalid="ignore", divide="ignore"):
        f = bw * (ndvi - water_ndvi) / (bm * (nm - ndvi) + bw * (ndvi - water_ndvi))
    f = np.where(ndvi >= nm, 1.0, np.where(ndvi <= water_ndvi, 0.0, f))
    f = np.where(np.isnan(ndvi), np.nan, np.clip(f, 0, 1))
    return np.where(f < det["subpixel_floor"], 0.0, f)


def water_ndvi(px, water, det):
    """Typical NDVI of the day's open water (the background a mat is mixed with)."""
    return float(np.median(px["ndvi"][water])) if water.sum() >= 50 else det["water_ndvi_default"]


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
    # Covered share of every pixel; the same at the assumed mat brightness halved and doubled
    # gives the likely range (a brighter mat needs less cover to look as green: lower cover).
    nw = water_ndvi(px, water, det)
    lo_scale, hi_scale = det["brightness_range"]
    cover, strict, loose = (np.where(ok, np.nan_to_num(cover_fraction(px["ndvi"], nw, det, k)), 0.0)
                            for k in (1.0, hi_scale, lo_scale))

    total = np.bincount(seg_idx[ok], minlength=n_segs)
    hy_count = np.bincount(seg_idx, weights=cover, minlength=n_segs)
    strict_count = np.bincount(seg_idx, weights=strict, minlength=n_segs)
    loose_count = np.bincount(seg_idx, weights=loose, minlength=n_segs)
    thr_count = np.bincount(seg_idx[hyacinth], minlength=n_segs)
    w_count = np.bincount(seg_idx[water], minlength=n_segs)
    nt_sum = np.bincount(seg_idx[water], weights=ndti[water], minlength=n_segs)
    muddy = water & (ndti > det["turbid_ndti"])
    muddy_count = np.bincount(seg_idx[muddy], minlength=n_segs)
    all_count = np.bincount(seg_idx[keep], minlength=n_segs)

    segs = []
    for i in range(n_segs):
        if total[i] < max(5, 0.5 * all_count[i]):  # too cloudy to say anything
            segs.append(None)
        else:
            nt = round(float(nt_sum[i] / w_count[i]), 4) if w_count[i] >= 5 else None
            # [hyacinth cover, mean NDTI of open water, cover at brighter mat, at darker mat, muddy share of
            #  open water, open-water px, share of pixels over the NDVI threshold (the old measure)]
            segs.append([round(float(hy_count[i] / total[i]), 3), nt,
                         round(float(strict_count[i] / total[i]), 3), round(float(loose_count[i] / total[i]), 3),
                         round(float(muddy_count[i] / w_count[i]), 3) if w_count[i] >= 5 else None, int(w_count[i]),
                         round(float(thr_count[i] / total[i]), 3)])
    # Too little open water (low flow, river choked by weed) gives no reliable muddiness figure.
    enough_water = water.sum() >= max(50, 0.05 * ok.sum())
    return {
        "valid_fraction": round(float(valid_fraction), 3),
        "hyacinth_km2": round(float(cover.sum()) * RES * RES / 1e6, 4),
        "hyacinth_pct": round(float(cover.sum() / ok.sum() * 100), 2),
        "hyacinth_pct_strict": round(float(strict.sum() / ok.sum() * 100), 2),
        "hyacinth_pct_loose": round(float(loose.sum() / ok.sum() * 100), 2),
        "hyacinth_pct_threshold": round(float(hyacinth.sum() / ok.sum() * 100), 2),
        "water_ndvi": round(nw, 3),
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
            pairs.append((above, optical[o], d.isoformat()))
    if len(pairs) < rad["min_pairs"]:
        return None

    candidates = np.nonzero((RADAR_EDGES[:-1] >= -25) & (RADAR_EDGES[:-1] <= -10))[0]
    def best(ps):
        return min(candidates, key=lambda k: np.mean([abs(a[k] - y) for a, y, _ in ps]))
    loo = [abs(pairs[i][0][best(pairs[:i] + pairs[i + 1:])] - pairs[i][1]) for i in range(len(pairs))]
    k = best(pairs)
    return {"bin": int(k), "threshold_db": float(RADAR_EDGES[k]), "pairs": len(pairs),
            "mae_pct": round(float(np.mean(loo)), 1),
            "direction": direction_agreement([(d, y, float(a[k])) for a, y, d in pairs])}


def radar_pct(r, k):
    return round(float(sum(r["hist"][k:]) / r["n"] * 100), 1)


def update_radar(river, rad, history, since, until, tf, w, h, mask, save):
    days = search_radar(river, since, until)
    done = set(history.setdefault("radar", {})) | set(history.setdefault("radar_skipped", []))
    todo = {d: v for d, v in days.items() if d not in done}
    print(f"  radar: {len(days)} passes, {len(todo)} new")

    def one(kv):
        d, items = kv
        try:
            return d, process_radar_day(items, tf, w, h, mask, rad), None
        except Exception as e:
            return d, None, e

    # A few passes at a time: downloads are network-bound, backfills have hundreds of passes.
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(one, todo.items()))
    for d, res, err in results:
        if err is not None:
            print(f"  radar {d}: error {type(err).__name__}: {err}")
            continue
        if res is None:
            history["radar_skipped"].append(d)
            print(f"  radar {d}: partial coverage, skipped")
        else:
            history["radar"][d] = res
            print(f"  radar {d}: ok")
        save()


def run_river(river, cfg, since: date | None):
    det, rad = river_det(river, cfg), cfg.get("radar")
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
    if len(todo) > 10:
        prefetch(river["id"], todo, tf, w, h, mask)

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
            history["dates"][d] = {**res, "scenes": [scene_id(it) for it in items]}
            print(f"  {d}: hyacinth {res['hyacinth_pct']}%  turbid {res['turbid_pct']}%")
        save()

    try:  # Landsat only fills gaps; an outage there must not block the optical update
        update_landsat_gap(river, det, history, since, until, tf, w, h, mask, inner_pixels(river_px)[mask],
                           seg_idx, len(segments), save)
    except Exception as e:
        print(f"  landsat: skipped this run ({type(e).__name__}: {e})")

    if rad:
        try:  # radar is a bonus layer; an outage there must not block the optical update
            update_radar(river, rad, history, since, until, tf, w, h, river_px, save)
        except Exception as e:
            print(f"  radar: skipped this run ({type(e).__name__}: {e})")

    if history["dates"]:
        update_rainfall(river, min(history["dates"]), max(history["dates"]))
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
        return [(None, None, None)] * len(segments)
    feats = json.loads(path.read_text())["features"]
    pts = [to_utm(shape(f["geometry"])) for f in feats]
    tree = STRtree(pts)
    names = []
    for seg in segments:
        mid = seg.interpolate(0.5, normalized=True)
        j = tree.nearest(mid)
        p = feats[j]["properties"]
        dist = pts[j].distance(mid)
        # Within reach: "Near X". Further out (sparse villages): the nearest place and how far.
        names.append((p["name"], p.get("name_mr"), None) if dist <= max_dist_m
                     else (None, None, {"name": p["name"], "name_mr": p.get("name_mr"), "km": round(dist / 1000, 1)}))
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

    Forecast = recent level (median of the last 3 clear observations within 45 days) plus the
    typical change over the same 2 weeks of the year in the OTHER years on record (needs 3).
    Backtests over 2017-2026 showed this beats both the level alone and "no change" (river-wide
    4.7 vs 5.4 and 5.2 points). Every past date is replayed as if it were today and compared with
    what was seen ~horizon days later; those errors give the published accuracy, the river's
    likely range, and per segment the chance of being above alert_pct.
    """
    H, alert = cfg["horizon_days"], cfg["alert_pct"]
    t = np.array([date.fromisoformat(d).toordinal() for d in dates])
    years = np.array([int(d[:4]) for d in dates])
    doy = np.array([date.fromisoformat(d).timetuple().tm_yday for d in dates])

    def seasonal(y, i):
        """Median change from this day of year to H days later, across the other years."""
        d0, d1 = doy[i], (doy[i] + H - 1) % 365 + 1
        out = []
        for yr in np.unique(years[years != years[i]]):
            sel = (years == yr) & np.isfinite(y)
            a, b = y[sel & (np.abs(doy - d0) <= 10)], y[sel & (np.abs(doy - d1) <= 10)]
            if len(a) and len(b):
                out.append(np.median(b) - np.median(a))
        return float(np.median(out)) if len(out) >= 3 else 0.0

    def level(y, i):
        m = (t <= t[i]) & (t > t[i] - 45) & np.isfinite(y)
        if m.sum() < 3:
            return None
        return float(np.clip(np.median(y[m][-3:]) + seasonal(y, i), 0, 100))

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


def rain_path(river_id: str) -> Path:
    return ROOT / f"data/rain/{river_id}.json"


def load_rain(river_id: str) -> dict:
    """Daily rainfall (mm) stored in data/rain/<river>.json (committed, like every input the
    published figures depend on, so they can be rebuilt exactly)."""
    path = rain_path(river_id)
    return json.loads(path.read_text()) if path.exists() else {}


def rain_total(rain, d, days):
    """Rain over the `days` days up to and including date d, or None if any of them is unknown
    (NASA POWER lags a few days): an unknown day must never count as a dry one."""
    t = date.fromisoformat(d)
    vals = [rain.get((t - timedelta(days=k)).isoformat()) for k in range(days)]
    return None if any(v is None for v in vals) else sum(vals)


def update_rainfall(river, start: str, end: str):
    """Add days missing from data/rain/<river>.json from NASA POWER (public domain, no key):
    daily rainfall at the river's centre. Only days after the last stored one are requested."""
    path = rain_path(river["id"])
    rain = load_rain(river["id"])
    have = max(rain) if rain else None
    if have is None or have < end:
        lon = (river["bbox"][0] + river["bbox"][2]) / 2
        lat = (river["bbox"][1] + river["bbox"][3]) / 2
        first = (date.fromisoformat(have) + timedelta(days=1)).isoformat() if have else start
        try:
            r = requests.get("https://power.larc.nasa.gov/api/temporal/daily/point", timeout=60, params={
                "parameters": "PRECTOTCORR", "community": "AG", "format": "JSON",
                "longitude": round(lon, 3), "latitude": round(lat, 3),
                "start": first.replace("-", ""), "end": end.replace("-", "")})
            r.raise_for_status()
            for k, v in r.json()["properties"]["parameter"]["PRECTOTCORR"].items():
                if v >= 0:  # -999 = not available yet
                    rain[f"{k[:4]}-{k[4:6]}-{k[6:]}"] = v
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rain, separators=(",", ":"), sort_keys=True))
        except Exception as e:  # rainfall only explains drops; without it they are labelled "unknown"
            print(f"  rainfall: unavailable ({type(e).__name__})")


def cleanup_events(dates, hy, sigma, rain, cfg):
    """Sharp drops per segment, labelled by their likely cause.

    A drop: a reading of at least min_before %, then within 3 weeks one at least drop_pts (and
    3x the noise) lower and at most half, outside the monsoon, still low on the next reading if
    there is one. Cause: "rain" if rain_mm or more fell in between, "river_wide" if many stretches
    dropped over the same interval (flow or wind), "drift" if a stretch within drift_stretches gained
    at least drift_share of the lost cover over the same interval (the mat moved, mostly downstream),
    otherwise "removal" (probable clean-up). Probable clean-ups on neighbouring stretches over
    overlapping days are one operation: the same "operation" number, counted once.
    regrowth_days: days until the stretch was back to half its earlier level, if seen.
    """
    t = np.array([date.fromisoformat(d).toordinal() for d in dates])
    monsoon = np.array([(6, 15) <= (int(d[5:7]), int(d[8:10])) <= (9, 30) for d in dates])
    drops = []
    for k in range(hy.shape[1]):
        seen = np.nonzero(np.isfinite(hy[:, k]))[0]
        th = max(cfg["drop_pts"], 3 * (sigma.get(k) or 0))
        for n, (a, b) in enumerate(zip(seen, seen[1:])):
            before, after = hy[a, k], hy[b, k]
            if (t[b] - t[a] > 21 or monsoon[a] or monsoon[b] or before < cfg["min_before"]
                    or before - after < th or after > before / 2):
                continue
            nxt = seen[n + 2] if n + 2 < len(seen) else None
            if nxt is not None and t[nxt] - t[b] <= 21 and hy[nxt, k] > before / 2:
                continue  # bounced straight back: not a lasting drop
            back = next((j for j in seen[n + 2:] if hy[j, k] >= before / 2), None)
            drops.append({"segment": k, "from": dates[a], "to": dates[b], "before": round(float(before), 1),
                          "after": round(float(after), 1), "confirmed": bool(nxt is not None and t[nxt] - t[b] <= 21),
                          "regrowth_days": int(t[back] - t[b]) if back is not None else None})
    # How many stretches with something to lose dropped over each interval: many at once = river-wide.
    for e in drops:
        a, b = dates.index(e["from"]), dates.index(e["to"])
        had = np.nan_to_num(hy[a:b + 1], nan=-1).max(axis=0) >= cfg["min_before"]
        same = sum(1 for o in drops if o["from"] >= e["from"] and o["to"] <= e["to"])
        days = (date.fromisoformat(e["to"]) - date.fromisoformat(e["from"])).days
        mm = rain_total(rain, e["to"], days) if rain else None
        e["rain_mm"] = None if mm is None else round(mm, 1)
        k, lost = e["segment"], e["before"] - e["after"]
        near = [j for j in range(k - cfg["drift_stretches"], k + cfg["drift_stretches"] + 1)
                if j != k and 0 <= j < hy.shape[1] and np.isfinite(hy[a, j]) and np.isfinite(hy[b, j])]
        drifted = any(hy[b, j] - hy[a, j] >= cfg["drift_share"] * lost for j in near)
        e["cause"] = ("rain" if mm is not None and mm >= cfg["rain_mm"]
                      else "river_wide" if had.sum() and same / had.sum() >= cfg["river_wide_share"]
                      else "drift" if drifted
                      else "removal" if mm is not None else "unknown")
    # One clean-up usually covers several stretches: join neighbouring ones whose days overlap.
    removal = sorted((e for e in drops if e["cause"] == "removal"), key=lambda e: (e["from"], e["segment"]))
    parent = list(range(len(removal)))
    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for i, e in enumerate(removal):
        for j in range(i):
            o = removal[j]
            if abs(o["segment"] - e["segment"]) <= 1 and o["from"] < e["to"] and e["from"] < o["to"]:
                parent[root(i)] = root(j)
    numbers = {}
    for i, e in enumerate(removal):
        e["operation"] = numbers.setdefault(root(i), len(numbers))
    # Confirmed: the operation covers at least two neighbouring stretches (1 km or more) and every
    # one of them is still low on the next image, as a machine clearing a reach looks; a single
    # stretch emptying is more often a mat breaking up or sinking.
    size, held = defaultdict(int), defaultdict(lambda: True)
    for e in removal:
        size[e["operation"]] += 1
        held[e["operation"]] &= e["confirmed"]
    for e in drops:
        e.setdefault("operation", None)
        e["confirmed_cleanup"] = e["operation"] is not None and size[e["operation"]] >= 2 and held[e["operation"]]
    return drops


def operations(events):
    """Probable clean-up operations: {number: [its stretch events]}."""
    ops = defaultdict(list)
    for e in events:
        if e["cause"] == "removal":
            ops[e["operation"]].append(e)
    return ops


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


def direction_agreement(rows, min_change=5, max_gap=60):
    """How often a second satellite moves the same way as Sentinel-2.

    rows: (date, sentinel-2 %, other %) for dates both measured, in date order. Every step between
    neighbouring rows at most max_gap days apart where Sentinel-2 changed by min_change points or
    more (well above its noise) counts; it agrees when the other satellite changed the same way.
    Returns {"share", "changes"}, or None with fewer than 5 such changes.
    """
    agree = n = 0
    for (d0, a0, b0), (d1, a1, b1) in zip(rows, rows[1:]):
        if (date.fromisoformat(d1) - date.fromisoformat(d0)).days > max_gap or abs(a1 - a0) < min_change:
            continue
        n += 1
        agree += (a1 - a0) * (b1 - b0) > 0
    return {"share": round(agree / n, 3), "changes": n} if n >= 5 else None


def landsat_standin(river_id, history, dates, cfg):
    """How well Landsat over inner pixels stands in for the published (whole-river Sentinel-2)
    figure, from every past pair of images taken within a day. A straight line maps one to the
    other (mid-channel Landsat misses weed along the edges, so even 0 maps to a few %); its error
    is measured out of sample (each pair predicted from all the others), and 80% of those errors
    are within error80. Used only if the correlation reaches cfg["min_correlation"]."""
    path = ROOT / f"data/validation/{river_id}-landsat.json"
    if not path.exists():
        return None
    pairs = [(history["dates"][p["date"]]["hyacinth_pct"], p["landsat_pct"])
             for p in json.loads(path.read_text())["pairs"] if p["date"] in dates]
    if len(pairs) < 20:
        return None
    a = np.array(pairs)
    s2, ls = a[:, 0], a[:, 1]
    errors = []
    for i in range(len(a)):
        rest = np.arange(len(a)) != i
        errors.append(abs(np.polyval(np.polyfit(ls[rest], s2[rest], 1), ls[i]) - s2[i]))
    slope, intercept = np.polyfit(ls, s2, 1)
    corr = float(np.corrcoef(s2, ls)[0, 1])
    return {"pairs": len(a), "slope": round(float(slope), 3), "intercept": round(float(intercept), 2),
            "correlation": round(corr, 2), "error80": round(float(np.percentile(errors, 80)), 1),
            "used": corr >= cfg["min_correlation"]}


def landsat_check(river_id):
    """Summary of the Landsat 8/9 comparison (crosscheck_landsat.py) with every pair, or None."""
    path = ROOT / f"data/validation/{river_id}-landsat.json"
    if not path.exists():
        return None
    v = json.loads(path.read_text())
    rows = [(p["date"], p["sentinel2_pct"], p["landsat_pct"]) for p in v["pairs"]]
    # Muddiness (mean NDTI of open water) in hundredths, so a change of 0.02 counts.
    mud = [(p["date"], p["sentinel2_ndti"] * 100, p["landsat_ndti"] * 100) for p in v["pairs"] if "landsat_ndti" in p]
    return {**v["summary"], "checked": v.get("checked"), "direction": direction_agreement(rows),
            "ndti_direction": direction_agreement(mud, min_change=2) if mud else None,
            "series": [{"date": d, "sentinel2_pct": a, "landsat_pct": b} for d, a, b in rows]}


def dry_days(dates, rain, cfg):
    """Dates with under dry_mm of rain over the dry_days before (and on) the image date; a date
    whose rainfall is not known yet is not dry."""
    totals = {d: rain_total(rain, d, cfg["dry_days"]) for d in dates}
    return {d for d, v in totals.items() if v is not None and v < cfg["dry_mm"]}


def muddier_stretches(dates, history, dry, cfg):
    """Per segment, how it compares in dry weather with the river's other stretches on the same day.

    Uses each stretch's mean NDTI of open water (a continuous muddiness measure: redder = muddier)
    minus the river's median stretch that day, on dry-weather images only, so rain run-off is not
    the cause. Returns {segment: {"n", "share_above", "median_diff"}} for segments with enough
    readings; a stretch above the river on nearly every dry day may sit below a drain or outlet,
    or have a shallow muddy bed.
    """
    rel = defaultdict(list)
    for d in dates:
        if d not in dry:
            continue
        segs = history["dates"][d]["segments"]
        vals = [v[1] for v in segs if v is not None and v[1] is not None]
        if len(vals) < 10:
            continue
        med = float(np.median(vals))
        for k, v in enumerate(segs):
            if v is not None and v[1] is not None and len(v) > 5 and v[5] >= cfg["min_water_px"]:
                rel[k].append(v[1] - med)
    return {k: {"n": len(r), "share_above": round(float(np.mean(np.array(r) > 0)), 3),
                "median_diff": round(float(np.median(r)), 3)}
            for k, r in rel.items() if len(r) >= cfg["min_readings"]}


def muddy_rain_check(dates, history, rain, dry, cfg):
    """Does the river look muddier right after rain? Compares the river's mean NDTI on each image
    taken after wet_mm or more of rain in 3 days with dry-weather images within 30 days of it.
    Physics says it should rise; with fewer than min_wet such images there is no verdict."""
    ndti = {d: history["dates"][d].get("mean_ndti") for d in dates}
    diffs = []
    for d in dates:
        wet = rain_total(rain, d, 3)
        if ndti[d] is None or wet is None or wet < cfg["wet_mm"]:
            continue
        t = date.fromisoformat(d)
        near = [ndti[x] for x in dry if ndti[x] is not None and abs((date.fromisoformat(x) - t).days) <= 30]
        if len(near) >= 2:
            diffs.append(ndti[d] - float(np.median(near)))
    return {"wet_images": len(diffs), "enough": len(diffs) >= cfg["min_wet"],
            "share_muddier": round(float(np.mean(np.array(diffs) > 0)), 2) if diffs else None,
            "median_change": round(float(np.median(diffs)), 3) if diffs else None}


def alerts(dates, hy, unconfirmed, conf, events, cfg, alert_pct):
    """Recent changes worth telling someone about, newest first.

    "above": a stretch crossed alert_pct from below; "spread": it rose by spread_pts or more within
    3 weeks; "cleanup": a probable clean-up (see cleanup_events). Low-confidence stretches and a
    latest reading that jumped and is not yet confirmed by another image are left out.
    """
    t = np.array([date.fromisoformat(d).toordinal() for d in dates])
    since = t[-1] - cfg["days"]
    out = []
    for k in range(hy.shape[1]):
        if conf.get(k, ("low",))[0] == "low":
            continue
        seen = np.nonzero(np.isfinite(hy[:, k]))[0]
        for a, b in zip(seen, seen[1:]):
            if t[b] < since or t[b] - t[a] > 21 or (b == len(dates) - 1 and unconfirmed[k]):
                continue
            before, after = hy[a, k], hy[b, k]
            kind = ("above" if before < alert_pct <= after
                    else "spread" if after - before >= cfg["spread_pts"] else None)
            if kind:
                out.append({"date": dates[b], "type": kind, "segment": int(k), "from_date": dates[a],
                            "from_pct": round(float(before), 1), "to_pct": round(float(after), 1)})
    for op in operations(events).values():  # one alert per operation, at its largest drop
        e = max(op, key=lambda x: x["before"] - x["after"])
        if date.fromisoformat(e["to"]).toordinal() >= since:
            out.append({"date": e["to"], "type": "cleanup", "segment": e["segment"], "from_date": e["from"],
                        "from_pct": e["before"], "to_pct": e["after"], "stretches": len(op)})
    return sorted(out, key=lambda a: (a["date"], a["segment"], a["type"]), reverse=True)


def ward_table(dates, hy, seg_px, regions, conf, events, cfg):
    """One row per municipal ward the river runs through, for the dashboard's league table.

    A stretch counts towards the ward of each corporation it touches (p.wards[i] goes with
    p.regions[i]). Figures are weighted by river area and need min_seen of the ward's river area
    seen, else they are None:
      now: each stretch's latest reading if within now_days of the newest image;
      last_year: each stretch's reading closest to a year before the newest image (within year_window days);
      avg_12m: the average over the last 12 months of the ward's cover on each image;
      cleanups: probable clean-ups ever / in the last 12 months, and the median days until weed was
      back at half its earlier level (over those where that was seen, at least min_regrowth);
      reliable: share of the ward's river area in stretches that are not low-confidence.
    """
    t = np.array([date.fromisoformat(d).toordinal() for d in dates])
    last, year_ago = t[-1], t[-1] - 365
    wards = defaultdict(list)
    for k, (ids, names) in enumerate(regions):
        for rid, name in zip(ids, names):
            wards[(rid, name)].append(k)

    def weighted(vals, ks):
        """Area-weighted mean of vals (NaN = not seen) over stretches ks, or None if too little was seen."""
        w = seg_px[ks].astype(float)
        seen = np.isfinite(vals)
        if not w.sum() or w[seen].sum() < cfg["min_seen"] * w.sum():
            return None
        return round(float(np.sum(vals[seen] * w[seen]) / w[seen].sum()), 1)

    rows = []
    for (rid, name), ks in wards.items():
        ks = np.array(ks)
        now = np.full(len(ks), np.nan)
        before = np.full(len(ks), np.nan)
        for j, k in enumerate(ks):
            seen = np.nonzero(np.isfinite(hy[:, k]))[0]
            if len(seen) and last - t[seen[-1]] <= cfg["now_days"]:
                now[j] = hy[seen[-1], k]
            near = seen[np.abs(t[seen] - year_ago) <= cfg["year_window"]]
            if len(near):
                before[j] = hy[near[np.argmin(np.abs(t[near] - year_ago))], k]
        recent = np.nonzero(t > year_ago)[0]
        per_image = [weighted(hy[i, ks], ks) for i in recent]
        per_image = [v for v in per_image if v is not None]
        # Clean-up operations touching the ward, each counted once; regrowth per operation.
        mine = [op for op in operations(events).values() if any(e["segment"] in set(ks.tolist()) for e in op)]
        regrow = [float(np.median([e["regrowth_days"] for e in op if e["regrowth_days"] is not None]))
                  for op in mine if any(e["regrowth_days"] is not None for e in op)]
        latest = {int(k): float(now[j]) for j, k in enumerate(ks) if np.isfinite(now[j])}
        good = [k for k in ks if conf.get(int(k), ("low",))[0] != "low"]
        rows.append({
            "region": rid, "ward": name, "segments": [int(k) for k in ks],
            "km": round(len(ks) * cfg["segment_km"], 1),
            "now": weighted(now, ks), "last_year": weighted(before, ks),
            "avg_12m": round(float(np.mean(per_image)), 1) if len(per_image) >= cfg["min_images"] else None,
            "cleanups": len(mine),
            "cleanups_12m": sum(1 for op in mine if max(date.fromisoformat(e["to"]).toordinal() for e in op) > year_ago),
            "confirmed_cleanups": sum(1 for op in mine if op[0]["confirmed_cleanup"]),
            "confirmed_12m": sum(1 for op in mine if op[0]["confirmed_cleanup"]
                                 and max(date.fromisoformat(e["to"]).toordinal() for e in op) > year_ago),
            # A median of one or two clean-ups says little: needs min_regrowth of them.
            "regrowth_days": int(np.median(regrow)) if len(regrow) >= cfg["min_regrowth"] else None,
            "regrowth_n": len(regrow),
            "worst": max(latest, key=latest.get) if latest else None,
            "reliable": round(float(seg_px[good].sum() / max(seg_px[ks].sum(), 1)), 2),
        })
        rows[-1]["ranked"] = rows[-1]["reliable"] >= cfg["ranked_reliable"] and rows[-1]["now"] is not None
    return sorted(rows, key=lambda r: (r["region"], r["segments"][0]))


def season_forecast(dates, river_pct, cfg):
    """When the weed season usually takes off and peaks, from every past season, and where this one is.

    A season runs from 1 October (after the monsoon) to 15 June. It takes off on the first image of
    two at least start_pct within 3 weeks; its peak is the highest 3-image median. Dates are kept as
    days after 1 October. The forecast is the middle of the past seasons, with the middle half as
    the range; its error is measured by predicting each season from all the others.
    """
    d = [date.fromisoformat(x) for x in dates]
    v = np.asarray(river_pct, float)
    seasons = []
    for y in range(d[0].year - 1, d[-1].year + 1):
        idx = [i for i, x in enumerate(d) if date(y, 10, 1) <= x <= date(y + 1, 6, 15)]
        start = next((d[a] for a, b in zip(idx, idx[1:])
                      if v[a] >= cfg["start_pct"] and v[b] >= cfg["start_pct"] and (d[b] - d[a]).days <= 21), None)
        complete = d[-1] > date(y + 1, 6, 15)
        if not idx or (len(idx) < cfg["min_images"] and complete):
            continue
        smooth = [float(np.median(v[idx[max(0, j - 1):j + 2]])) for j in range(len(idx))]
        k = int(np.argmax(smooth)) if idx else None
        seasons.append({"season": y, "complete": complete, "start": start.isoformat() if start else None,
                        "start_day": (start - date(y, 10, 1)).days if start else None,
                        "peak_date": d[idx[k]].isoformat() if complete and idx else None,
                        "peak_day": (d[idx[k]] - date(y, 10, 1)).days if complete and idx else None,
                        "peak_pct": round(smooth[k], 1) if complete and idx else None})
    past = [s for s in seasons if s["complete"] and s["start_day"] is not None]
    if len(past) < cfg["min_seasons"]:
        return None
    starts = np.array([s["start_day"] for s in past])
    loo = [abs(np.median(np.delete(starts, i)) - starts[i]) for i in range(len(starts))]
    current = seasons[-1] if not seasons[-1]["complete"] else None
    return {
        "seasons": seasons,
        "start_day": int(np.median(starts)), "start_range": [int(np.percentile(starts, 25)), int(np.percentile(starts, 75))],
        "start_error_days": int(round(float(np.mean(loo)))),
        "peak_day": int(np.median([s["peak_day"] for s in past])),
        "peak_pct": round(float(np.median([s["peak_pct"] for s in past])), 1),
        "peak_range": [round(float(np.percentile([s["peak_pct"] for s in past], 25)), 1),
                       round(float(np.percentile([s["peak_pct"] for s in past], 75)), 1)],
        "current": None if current is None else {"season": current["season"], "start": current["start"]},
    }


def write_web(river, cfg, segments, history, area):
    det, rad, qual = river_det(river, cfg), cfg.get("radar"), cfg["quality"]
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

    rain = load_rain(river["id"])
    events = cleanup_events(dates, hy, {k: seg_sigma[c][0] for k, (c, _) in conf.items()}, rain,
                            cfg["cleanup"]) if cfg.get("cleanup") else []

    mud_cfg = cfg.get("muddy")
    dry = dry_days(dates, rain, mud_cfg) if mud_cfg else set()
    muddier = muddier_stretches(dates, history, dry, mud_cfg) if mud_cfg else {}
    alert_list = alerts(dates, hy, unconfirmed, conf, events, cfg["alerts"], cfg["outlook"]["alert_pct"]) \
        if cfg.get("alerts") else []

    def likely(lo_val, hi_val, sigma):
        pad = z80 * (sigma or 0)
        return [round(max(0.0, lo_val - pad), 1), round(min(100.0, hi_val + pad), 1)]

    features = []
    for i, seg in enumerate(segments):
        # Round to ~10 cm so output is byte-identical across machines (no spurious commits).
        geom = shapely.transform(to_wgs(seg.simplify(5)), lambda c: np.round(c, 6))
        base = {"id": i, "place": places[i][0], "place_mr": places[i][1], "nearest": places[i][2],
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
                # Dry weather: share of days muddier than the river's middle stretch, and by how much.
                "dry_muddy": muddier.get(i),
                "muddier": bool(i in muddier and c != "low" and muddier[i]["share_above"] >= mud_cfg["min_share"]
                                and muddier[i]["median_diff"] >= mud_cfg["min_diff"]),
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
        # Muddy water in this area: share of its open water above the muddiness threshold.
        muddy, ndti = [], []
        for d in dates:
            num = den = nt = 0
            for k in np.nonzero(member)[0]:
                v = history["dates"][d]["segments"][k]
                if v is not None and len(v) > 5 and v[4] is not None:
                    num += v[4] * v[5]
                    nt += v[1] * v[5]
                    den += v[5]
            muddy.append(round(num / den * 100, 2) if den >= 50 else None)
            ndti.append(round(nt / den, 4) if den >= 50 else None)  # average muddiness of its open water
        region_out.append({
            "id": r["id"], "name": r["name"], "name_mr": r.get("name_mr"),
            "n_segments": int(member.sum()), "km": round(member.sum() * det["segment_length_m"] / 1000, 1),
            "series": [{"date": d, "hyacinth_pct": round(float(v), 2), "turbid_pct": m, "mean_ndti": nd}
                       for d, v, k, m, nd in zip(dates, val, ok, muddy, ndti) if k],
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
        "thresholds": {"hyacinth_ndvi": det["hyacinth_ndvi"], "turbid_ndti": det["turbid_ndti"],
                       "mat_ndvi": det["mat_ndvi"], "subpixel_floor": det["subpixel_floor"]},
        # Independent check against Landsat 8/9 (see crosscheck_landsat.py).
        "landsat_check": landsat_check(river["id"]),
        # Whole-river evidence behind the flood test (see build_river_mask.py).
        "flood_test": (json.loads(river_mask_path(river["id"]).with_suffix(".json").read_text())
                       if river_mask_path(river["id"]).with_suffix(".json").exists() else None),
        # Share of the river area in low-confidence stretches: over a third (the narrow Pawana: half) and
        # the dashboard says to rely on river-wide trends rather than single stretches.
        "low_confidence_share": round(float(sum(area["px"][k] for k, (c, _) in conf.items() if c == "low")
                                            / max(area["px"].sum(), 1)), 2),
        "river_area": {"from_images": area["from_images"],
                       "km2": round(float(area["px"].sum()) * RES * RES / 1e6, 2),
                       "green_channel_km2": round(float(area["green_px"].sum()) * RES * RES / 1e6, 2)},
        "segments": {"type": "FeatureCollection", "features": features},
        "series": series,
        "regions": region_out,
        # Every ward along the river with its weed now, a year ago, over 12 months and its clean-ups.
        "wards": ward_table(dates, hy, seg_px, regions, conf, events,
                            {**cfg["wards"], "segment_km": det["segment_length_m"] / 1000}) if cfg.get("wards") else [],
        # Sharp, lasting drops per stretch with their likely cause (see cleanup_events).
        "cleanup_events": events,
        # Recent changes worth telling someone about; also published as web/feed.xml.
        "alerts": alert_list,
        # Does muddiness rise after rain, as it physically should? (see muddy_rain_check)
        "muddy_rain_check": muddy_rain_check(dates, history, rain, dry, mud_cfg) if mud_cfg else None,
        # Planted-weed test (see synthetic_test.py).
        "synthetic_test": (json.loads((ROOT / f"data/validation/{river['id']}-synthetic.json").read_text())
                           if (ROOT / f"data/validation/{river['id']}-synthetic.json").exists() else None),
        # Images left out of every figure, and why (shown in the data table).
        "rejected_dates": [{"date": d, "reason": rejected[d], "aot": history["dates"][d].get("aot"),
                            "cloud_share": history["dates"][d].get("cloud_share"),
                            "hyacinth_pct": history["dates"][d]["hyacinth_pct"],
                            "scene": history["dates"][d]["scenes"][0]} for d in sorted(rejected)],
        # How far apart two images of the same thing land, and how the greenness threshold
        # moves the figures; together they make the likely ranges.
        "uncertainty": {
            # Mat brightness (against water) tried at these multiples of the assumed value.
            "range_brightness": det["brightness_range"],
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
    # Landsat images newer than the newest Sentinel-2 one (see update_landsat_gap): a river-wide
    # estimate, corrected by Landsat's past offset and shown with its measured error, until
    # Sentinel-2 catches up. Not used in any other figure, and not on rivers where it tracks poorly.
    standin = landsat_standin(river["id"], history, dates, cfg["gap_fill"]) if cfg.get("gap_fill") else None
    out["landsat_standin"] = standin
    def estimate(x):
        return min(100.0, max(0.0, standin["slope"] * x + standin["intercept"]))
    out["gap_fill"] = [{
        "date": d, "sensor": "landsat", "scene": v["scenes"][0], "landsat_pct": v["hyacinth_pct"],
        "hyacinth_pct": round(estimate(v["hyacinth_pct"]), 1),
        "range": [round(max(0.0, estimate(v["hyacinth_pct"]) - standin["error80"]), 1),
                  round(min(100.0, estimate(v["hyacinth_pct"]) + standin["error80"]), 1)],
    } for d, v in sorted(history.get("landsat", {}).items())
        if v and d > dates[-1] and standin and standin["used"]]
    # When the weed season usually takes off and peaks, and where this one is.
    out["season"] = season_forecast(dates, river_pct, cfg["season"]) if cfg.get("season") else None
    if look:
        out["outlook"] = look
    cal = calibrate_radar({**history, "dates": {d: history["dates"][d] for d in dates}}, rad) if rad else None
    if cal:
        out["radar"] = {
            "threshold_db": cal["threshold_db"], "pairs": cal["pairs"], "mae_pct": cal["mae_pct"],
            "direction": cal["direction"],
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
            text = out_path.read_text()  # keep the published file (and so its version)
        else:
            out_path.write_text(text)
    else:
        out_path.write_text(text)

    index_path = web / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"rivers": []}
    index["rivers"] = [r for r in index["rivers"] if r["id"] != river["id"]]
    # version: changes whenever the river's data does, so open dashboards know to reload it.
    index["rivers"].append({"id": river["id"], "name": river["name"], "name_mr": river.get("name_mr"),
                            "latest_date": dates[-1], "version": hashlib.sha1(text.encode()).hexdigest()[:12]})
    # Same order as config/rivers.yaml: the first river is the one the dashboard opens on.
    order = [r["id"] for r in cfg["rivers"]]
    index["rivers"].sort(key=lambda r: order.index(r["id"]) if r["id"] in order else len(order))
    new_index = json.dumps(index, indent=1, ensure_ascii=False)
    if not index_path.exists() or index_path.read_text() != new_index:
        index_path.write_text(new_index)
    print(f"  web/data/{river['id']}.json: {len(features)} segments, {len(dates)} dates")


SITE = "https://rig-consultancy.github.io/river-watch/"
ALERT_WORDS = {
    "above": ("weed above {a}% ({from_pct}% → {to_pct}%)", "जलपर्णी {a}% पेक्षा जास्त ({from_pct}% → {to_pct}%)"),
    "spread": ("weed spread ({from_pct}% → {to_pct}%)", "जलपर्णी वाढली ({from_pct}% → {to_pct}%)"),
    "cleanup": ("probable clean-up ({from_pct}% → {to_pct}%)", "संभाव्य स्वच्छता ({from_pct}% → {to_pct}%)"),
}


def stretch_name(p, lang="en"):
    """A stretch as the dashboard names it: its place, else the distance from the nearest one."""
    place = p.get("place_mr") if lang == "mr" and p.get("place_mr") else p.get("place")
    if place:
        return f"{place} जवळ" if lang == "mr" else f"Near {place}"
    n = p.get("nearest")
    if n:
        name = n.get("name_mr") if lang == "mr" and n.get("name_mr") else n["name"]
        return f"{name} पासून {n['km']} किमी" if lang == "mr" else f"{n['km']} km from {name}"
    return f"भाग {p['id'] + 1}" if lang == "mr" else f"Stretch {p['id'] + 1}"


def write_feed(cfg):
    """web/feed.xml: every river's alerts as one Atom feed, for feed readers and feed-to-email
    services. Built only from the published data, so it changes only when an alert does."""
    from xml.sax.saxutils import escape
    entries = []
    for river in cfg["rivers"]:
        path = ROOT / f"web/data/{river['id']}.json"
        if river.get("draft") or not path.exists():
            continue
        d = json.loads(path.read_text())
        props = {f["properties"]["id"]: f["properties"] for f in d["segments"]["features"]}
        a_pct = (d.get("outlook") or {}).get("alert_pct", cfg["outlook"]["alert_pct"])
        for al in d.get("alerts", []):
            p = props[al["segment"]]
            en, mr = (w.format(a=a_pct, **al) for w in ALERT_WORDS[al["type"]])
            link = f"{SITE}?river={river['id']}&stretch={al['segment'] + 1}"
            entries.append((al["date"], f"""  <entry>
    <id>tag:rig-consultancy.github.io,2026:river-watch/{river['id']}/{al['type']}/{al['segment']}/{al['date']}</id>
    <title>{escape(f"{river['name']}, {stretch_name(p)}: {en}")}</title>
    <updated>{al['date']}T00:00:00Z</updated>
    <link href="{escape(link)}"/>
    <summary>{escape(f"{en} ({al['from_date']} to {al['date']}). {river.get('name_mr', '')}, {stretch_name(p, 'mr')}: {mr}.")}</summary>
  </entry>"""))
    entries.sort(key=lambda e: e[0], reverse=True)
    entries = entries[:100]
    updated = entries[0][0] if entries else "2026-01-01"
    text = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>tag:rig-consultancy.github.io,2026:river-watch</id>
  <title>River Watch alerts</title>
  <subtitle>Weed above the alert level, sudden spread and probable clean-ups on Pune's rivers, from Sentinel-2</subtitle>
  <link href="{SITE}"/>
  <link rel="self" href="{SITE}feed.xml"/>
  <updated>{updated}T00:00:00Z</updated>
  <author><name>River Watch</name></author>
{chr(10).join(e for _, e in entries)}
</feed>
"""
    out = ROOT / "web/feed.xml"
    if not out.exists() or out.read_text() != text:
        out.write_text(text)
        print(f"wrote web/feed.xml ({len(entries)} alerts)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=date.fromisoformat, help="backfill from this date")
    ap.add_argument("rivers", nargs="*", help="river ids (default: all)")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    for river in cfg["rivers"]:
        if args.rivers and river["id"] not in args.rivers:
            continue
        if river.get("draft") and river["id"] not in args.rivers:
            continue  # being set up; only runs when named
        print(f"{river['name']}")
        run_river(river, cfg, args.since)
    write_feed(cfg)


if __name__ == "__main__":
    main()
