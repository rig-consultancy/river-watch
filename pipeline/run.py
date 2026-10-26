"""Scheduled pipeline: Sentinel-2 -> hyacinth & turbidity per river segment.

For each river in config/rivers.yaml:
  1. find new cloud-light Sentinel-2 L2A scenes (Microsoft Planetary Computer, free)
  2. read green/red/NIR + scene classification over the river only
  3. mask clouds, shadows, river banks and year-round green pixels (see build_bank_mask.py)
  4. classify floating vegetation (NDVI) and muddy water (NDTI)
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


def classify(green, red, nir, mask, det):
    """Return (ok, hyacinth, ndti): usable river pixels, floating vegetation, turbidity index."""
    with np.errstate(invalid="ignore", divide="ignore"):
        ndvi = (nir - red) / (nir + red)
        ndti = (red - green) / (red + green)
    # Zero or negative reflectance (sensor saturation, nodata edges) gives non-finite indices.
    ok = mask & np.isfinite(ndvi) & np.isfinite(ndti) & (green > 0) & (red > 0) & (nir > 0)
    return ok, ok & (ndvi > det["hyacinth_ndvi"]), ndti


def day_cache(river_id: str, d: str) -> Path:
    """Per-pixel result for one date (local only, gitignored); reused by build_bank_mask.py."""
    return ROOT / f"data/cache/{river_id}/{d}.npz"


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


def process_day(items, tf, w, h, mask, bank, seg_idx, n_segs, det, cache_path=None):
    ok, hyacinth, ndti = classify(*read_day(items, tf, w, h), mask, det)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache_path, ok=ok, hy=hyacinth)
    if bank is not None:
        ok &= ~bank
        hyacinth &= ~bank
        mask = mask & ~bank
    valid_fraction = ok.sum() / mask.sum()
    if valid_fraction < det["min_valid_fraction"]:
        return None

    idx = seg_idx[ok[mask]]
    hy = hyacinth[mask][ok[mask]]
    nt = ndti[mask][ok[mask]]
    total = np.bincount(idx, minlength=n_segs)
    hy_count = np.bincount(idx, weights=hy, minlength=n_segs)
    nt_sum = np.bincount(idx, weights=nt, minlength=n_segs)
    all_count = np.bincount(seg_idx, minlength=n_segs)

    segs = []
    for i in range(n_segs):
        if total[i] < max(5, 0.5 * all_count[i]):  # too cloudy to say anything
            segs.append(None)
        else:
            segs.append([round(hy_count[i] / total[i], 3), round(nt_sum[i] / total[i], 4)])
    return {
        "valid_fraction": round(float(valid_fraction), 3),
        "hyacinth_km2": round(float(hyacinth.sum()) * RES * RES / 1e6, 4),
        "hyacinth_pct": round(float(hyacinth.sum() / ok.sum() * 100), 2),
        "mean_ndti": round(float(np.nanmean(ndti[ok])), 4),
        "turbid_pct": round(float((ndti[ok] > det["turbid_ndti"]).mean() * 100), 2),
        "segments": segs,
        "scenes": [it.id for it in items],
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


def run_river(river, det, rad, since: date | None):
    line, aoi = load_river(river, det)
    segments = make_segments(line, det["segment_length_m"])
    tf, w, h, mask = make_grid(aoi)
    bank = load_bank_mask(river["id"], tf, w, h)
    river_px = mask if bank is None else mask & ~bank
    bank_id = None if bank is None else hashlib.sha1(np.packbits(bank).tobytes()).hexdigest()[:12]

    # Assign every river pixel to its nearest segment once.
    rows, cols = np.nonzero(river_px)
    xs, ys = rasterio.transform.xy(tf, rows, cols)
    tree = STRtree(segments)
    seg_idx = tree.query_nearest(points(xs, ys), return_distance=False, all_matches=False)[1]

    hist_path = ROOT / f"data/history/{river['id']}.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else {"dates": {}, "skipped": []}
    rebuild_from = None
    if (history.get("n_segments") not in (None, len(segments))
            or (history["dates"] and history.get("bank_mask") != bank_id)):
        # Every stored number depends on which pixels count, so recompute all of it.
        old = list(history["dates"]) + history["skipped"] + list(history.get("radar", {}))
        rebuild_from = date.fromisoformat(min(old)) if old else None
        print(f"  segment layout or bank mask changed; rebuilding history from {rebuild_from}")
        history = {"dates": {}, "skipped": []}
    history["n_segments"] = len(segments)
    history["bank_mask"] = bank_id

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
            res = process_day(items, tf, w, h, mask, bank, seg_idx, len(segments), det,
                              cache_path=day_cache(river["id"], d))
        except Exception as e:  # one bad scene must not stop the scheduled run
            print(f"  {d}: error {type(e).__name__}: {e}")
            continue
        if res is None:
            history["skipped"].append(d)
            print(f"  {d}: too cloudy over river, skipped")
        else:
            history["dates"][d] = res
            print(f"  {d}: hyacinth {res['hyacinth_pct']}%  turbid {res['turbid_pct']}%")
        save()

    if rad:
        try:  # radar is a bonus layer; an outage there must not block the optical update
            update_radar(river, rad, history, since, until, tf, w, h, river_px, save)
        except Exception as e:
            print(f"  radar: skipped this run ({type(e).__name__}: {e})")

    write_web(river, det, rad, segments, history)


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
        names.append(feats[j]["properties"]["name"] if pts[j].distance(mid) <= max_dist_m else None)
    return names


def write_web(river, det, rad, segments, history):
    dates = sorted(history["dates"])
    places = segment_places(river, segments)
    if not dates:
        print("  no usable dates yet")
        return
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

    features = []
    for i, seg in enumerate(segments):
        if latest[i] is None:
            continue
        d, (hy, nt) = latest[i]
        prev = previous[i]
        # Round to ~10 cm so output is byte-identical across machines (no spurious commits).
        geom = shapely.transform(to_wgs(seg.simplify(5)), lambda c: np.round(c, 6))
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "id": i, "date": d, "place": places[i],
                "hyacinth_pct": round(hy * 100, 1),
                "ndti": nt,
                "change_pct": None if prev is None else round((hy - prev[0]) * 100, 1),
                "km": round(i * det["segment_length_m"] / 1000, 1),
            },
        })

    series = [{"date": d, **{k: history["dates"][d][k] for k in
               ("hyacinth_pct", "hyacinth_km2", "turbid_pct", "mean_ndti", "valid_fraction")}}
              for d in dates]
    out = {
        "river": {"id": river["id"], "name": river["name"], "bbox": river["bbox"]},
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "latest_date": dates[-1],
        "thresholds": {"hyacinth_ndvi": det["hyacinth_ndvi"], "turbid_ndti": det["turbid_ndti"]},
        "segments": {"type": "FeatureCollection", "features": features},
        "series": series,
    }
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
    text = json.dumps(out, separators=(",", ":"), allow_nan=False)
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
    index["rivers"].append({"id": river["id"], "name": river["name"], "latest_date": dates[-1]})
    index["rivers"].sort(key=lambda r: r["name"])
    index_path.write_text(json.dumps(index, indent=1))
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
        run_river(river, cfg["detection"], cfg.get("radar"), args.since)


if __name__ == "__main__":
    main()
