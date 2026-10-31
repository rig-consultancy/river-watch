"""Independent check: measure the same river with Landsat 8/9 and compare with Sentinel-2.

For every accepted Sentinel-2 date with a Landsat 8/9 image within max_days, hyacinth cover is
measured from both satellites over the same river pixels (those clear in both), with the same
NDVI threshold. Landsat's pixels are 30 m against 10 m, so narrow stretches mix more; the
agreement figures say how far the two independent sensors land apart. Writes
data/validation/<river>-landsat.json (commit it); the dashboard shows the summary.

Run after a backfill (all pairs again), and monthly by GitHub Actions with --new (only Landsat
images since the last check; all pairs again if the river area changed):
  uv run python pipeline/crosscheck_landsat.py [--new] [river-id ...]
"""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import planetary_computer
import pystac_client
import rasterio
import yaml
from rasterio.enums import Resampling
from shapely import STRtree, points

from run import (ROOT, STAC_URL, day_pixels, load_river, make_grid, make_segments, read_band,
                 reject_dates, river_area, search)

# Bump when what is stored per pair changes; --new then compares every pair again.
VERSION = 3  # 2: muddy water share; 3: and mean NDTI of open water

# QA_PIXEL bits: 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow.
QA_BAD = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)


def landsat_scenes(river, since, until):
    cat = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
    items = cat.search(collections=["landsat-c2-l2"], bbox=river["bbox"], datetime=f"{since}/{until}",
                       query={"platform": {"in": ["landsat-8", "landsat-9"]}, "eo:cloud_cover": {"lt": 60}}).item_collection()
    return sorted(items, key=lambda it: it.datetime)


def landsat_indices(item, tf, w, h, mask):
    """NDVI, NDWI and NDTI per river-corridor pixel from one Landsat scene (NaN where cloudy or
    no data), defined as for Sentinel-2."""
    green, red, nir = (read_band(item.assets[b].href, tf, w, h, Resampling.nearest)[mask].astype("float32")
                       * 0.0000275 - 0.2 for b in ("green", "red", "nir08"))  # Collection 2 reflectance scale
    qa = read_band(item.assets["qa_pixel"].href, tf, w, h, Resampling.nearest)[mask]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = {"ndvi": (nir - red) / (nir + red), "ndwi": (green - nir) / (green + nir),
               "ndti": (red - green) / (red + green)}
    bad = ((qa & QA_BAD) > 0) | (green <= 0) | (red <= 0) | (nir <= 0) | (qa == 1)  # qa 1 = fill
    for v in out.values():
        v[bad] = np.nan
    return out


def build(river, cfg, new_only=False, max_days=1):
    det = cfg["detection"]
    line, aoi, corridor = load_river(river, det)
    tf, w, h, mask = make_grid(corridor)
    river_px, _, _ = river_area(river["id"], tf, w, h, mask, aoi)
    area_id = hashlib.sha1(np.packbits(river_px).tobytes()).hexdigest()[:12]
    out = ROOT / f"data/validation/{river['id']}-landsat.json"
    old = json.loads(out.read_text()) if out.exists() else None
    if not (new_only and old and old.get("river_area") == area_id and old.get("version") == VERSION):
        old = None  # first check, the river area or what is stored changed: compare every pair again
    # Compare only pixels at least 20 m inside the river: there a whole 30 m Landsat pixel is
    # river, so the comparison isn't dominated by bank pixels that Landsat mixes in.
    inner = river_px.copy()
    for _ in range(2):
        inner[1:] &= inner[:-1].copy(); inner[:-1] &= inner[1:].copy()
        inner[:, 1:] &= inner[:, :-1].copy(); inner[:, :-1] &= inner[:, 1:].copy()
    keep = inner[mask]
    segments = make_segments(line, det["segment_length_m"])
    rows, cols = np.nonzero(mask)
    xs, ys = rasterio.transform.xy(tf, rows, cols)
    seg_idx = STRtree(segments).query_nearest(points(xs, ys), return_distance=False, all_matches=False)[1]

    hist = json.loads((ROOT / f"data/history/{river['id']}.json").read_text())
    rejected = reject_dates(hist, sorted(hist["dates"]), cfg["quality"])
    s2_dates = [d for d in sorted(hist["dates"]) if d not in rejected]
    since = s2_dates[0]
    if old and old["pairs"]:  # a few days of overlap: Landsat scenes appear in the catalogue late
        since = (date.fromisoformat(old["pairs"][-1]["landsat_date"]) - timedelta(days=5)).isoformat()
    known = {r["landsat_id"] for r in old["pairs"]} if old else set()
    scenes = [it for it in landsat_scenes(river, since, s2_dates[-1]) if it.id not in known]
    pairs = []
    for it in scenes:
        ld = it.datetime.date()
        near = [d for d in s2_dates if abs((date.fromisoformat(d) - ld).days) <= max_days]
        if near:
            pairs.append((min(near, key=lambda d: abs((date.fromisoformat(d) - ld).days)), it))
    print(f"  {len(scenes)} Landsat scenes, {len(pairs)} within {max_days} day of a Sentinel-2 date")

    thr = det["hyacinth_ndvi"]
    n_segs = len(segments)

    def compare(pair):
        d, it = pair
        try:
            ls_px = landsat_indices(it, tf, w, h, mask)
            # Sentinel-2 pixels from the cache, or downloaded again (GitHub's cache may not have them).
            s2_items = [] if (ROOT / f"data/cache/{river['id']}/{d}.npz").exists() else [
                s for s in search(river, date.fromisoformat(d), date.fromisoformat(d), det).get(d, [])
                if s.id in hist["dates"][d]["scenes"]]
            s2_px = day_pixels(river["id"], d, s2_items, tf, w, h, mask)
        except Exception as e:
            print(f"  {it.id}: failed ({type(e).__name__})")
            return None
        ls, s2 = ls_px["ndvi"], s2_px["ndvi"]
        both = keep & np.isfinite(ls) & np.isfinite(s2)
        if both.sum() < 0.5 * keep.sum():
            return None  # mostly cloudy in one of them
        a, b = s2[both] > thr, ls[both] > thr
        idx = seg_idx[both]
        n = np.bincount(idx, minlength=n_segs)
        sa = np.bincount(idx, weights=a, minlength=n_segs) / np.maximum(n, 1) * 100
        sb = np.bincount(idx, weights=b, minlength=n_segs) / np.maximum(n, 1) * 100
        big = n >= 100  # stretches with at least 1 ha of inner river seen by both
        # Muddy water: on pixels both see as open water, the share above the muddiness threshold.
        water = (both & (s2_px["ndwi"] > det["water_ndwi"]) & (ls_px["ndwi"] > det["water_ndwi"])
                 & (s2 <= thr) & (ls <= thr) & np.isfinite(s2_px["ndti"]) & np.isfinite(ls_px["ndti"]))
        muddy = {} if water.sum() < 50 else {
            "water_pixels": int(water.sum()),
            "sentinel2_muddy": round(float((s2_px["ndti"][water] > det["turbid_ndti"]).mean() * 100), 2),
            "landsat_muddy": round(float((ls_px["ndti"][water] > det["turbid_ndti"]).mean() * 100), 2),
            # Muddiness as a continuous value (the share above the threshold is mostly 0 here).
            "sentinel2_ndti": round(float(s2_px["ndti"][water].mean()), 4),
            "landsat_ndti": round(float(ls_px["ndti"][water].mean()), 4)}
        return {"date": d, "landsat_date": it.datetime.date().isoformat(), "landsat_id": it.id,
                "pixels": int(both.sum()), "sentinel2_pct": round(float(a.mean() * 100), 2),
                "landsat_pct": round(float(b.mean() * 100), 2), **muddy,
                "stretches": [[int(k), round(float(sa[k]), 1), round(float(sb[k]), 1)] for k in np.nonzero(big)[0]]}

    with ThreadPoolExecutor(4) as pool:
        results = [r for r in pool.map(compare, pairs) if r]
    print(f"  {len(results)} new usable pairs")
    # Keep earlier pairs unless their Sentinel-2 image has since been rejected.
    results += [r for r in (old["pairs"] if old else []) if r["date"] in s2_dates]
    results = list({r["landsat_id"]: r for r in results}.values())
    results.sort(key=lambda r: r["date"])
    if not results:
        print("  no usable pairs")
        return
    s2 = np.array([r["sentinel2_pct"] for r in results])
    ls = np.array([r["landsat_pct"] for r in results])
    st = np.array([s[1:] for r in results for s in r["stretches"]])
    summary = {
        "pairs": len(results),
        "river_mae_pts": round(float(np.mean(np.abs(s2 - ls))), 1),
        "river_bias_pts": round(float(np.mean(ls - s2)), 1),  # Landsat minus Sentinel-2
        "river_correlation": round(float(np.corrcoef(s2, ls)[0, 1]), 3) if len(results) > 2 else None,
        "stretch_comparisons": int(len(st)),
        "stretch_mae_pts": round(float(np.mean(np.abs(st[:, 0] - st[:, 1]))), 1) if len(st) else None,
        "stretch_correlation": round(float(np.corrcoef(st[:, 0], st[:, 1])[0, 1]), 3) if len(st) > 2 else None,
    }
    md = np.array([(r["sentinel2_ndti"], r["landsat_ndti"]) for r in results if "landsat_ndti" in r])
    if len(md) > 2:
        summary.update({"ndti_pairs": len(md),
                        "ndti_mae": round(float(np.mean(np.abs(md[:, 0] - md[:, 1]))), 3),
                        "ndti_bias": round(float(np.mean(md[:, 1] - md[:, 0])), 3),
                        "ndti_correlation": round(float(np.corrcoef(md[:, 0], md[:, 1])[0, 1]), 3)})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "checked": date.today().isoformat(), "river_area": area_id, "version": VERSION,
                               "pairs": results}, separators=(",", ":")))
    print(f"  {summary}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rivers", nargs="*", help="river ids (default: all)")
    ap.add_argument("--new", action="store_true", help="only Landsat images since the last check")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    for river in cfg["rivers"]:
        if args.rivers and river["id"] not in args.rivers:
            continue
        print(river["name"])
        build(river, cfg, new_only=args.new)


if __name__ == "__main__":
    main()
