"""Planted-weed test: put weed mats of known size into real clear images and measure what is found.

The images are real Sentinel-2 dates with almost no weed. Each trial plants one mat of a known
area at a random spot in the river: it grows outwards from that spot over river pixels only (as
a floating mat follows the channel), drawn on a 2 m grid so its edges cut pixels the way a real
mat does. Each 10 m pixel then reads as a mix of the mat and what was really there:

  NDVI_mix = (f * B_mat * NDVI_mat + (1 - f) * B_bg * NDVI_bg) / (f * B_mat + (1 - f) * B_bg)

with f the share of the pixel the mat covers, NDVI_mat drawn from the river's own dense mats
(pixels whose neighbours are all dense too), NDVI_bg the pixel's real value that day, and B the
brightness (red + NIR) of each: mixing happens in reflectance, so the brighter mat counts more.
Detection is the normal rule (NDVI above the threshold); the found area is what changed.

Brightness of a mat against water is the one assumed number (configured as synthetic.mat_brightness
and water_brightness); the test also runs at half and double their ratio and reports the spread.
Writes data/validation/<river>-synthetic.json (commit it); the dashboard shows the summary.

  uv run python pipeline/synthetic_test.py [river-id ...]
"""

import argparse
import json
from datetime import date

import numpy as np
import yaml
from shapely import STRtree, points
import rasterio

from run import (ROOT, RES, confidence, day_pixels, load_river, make_grid, make_segments, reject_dates,
                 river_area, search)

SUB = 5  # 2 m sub-pixels per 10 m pixel side


def mixed_ndvi(f, ndvi_mat, ndvi_bg, b_mat, b_bg):
    """NDVI of a pixel a share f of which is mat, the rest background (linear mixing of reflectance)."""
    return (f * b_mat * ndvi_mat + (1 - f) * b_bg * ndvi_bg) / (f * b_mat + (1 - f) * b_bg)


def plant(river2d, rng, area_m2, center_px):
    """Cover per pixel (0-1, sparse dict {(row, col): f}) of a mat of area_m2 grown from a random
    point inside center_px over river pixels, nearest sub-pixels first."""
    r0, c0 = center_px
    cy, cx = (r0 + rng.random()) * SUB, (c0 + rng.random()) * SUB  # in sub-pixel units
    reach = int(np.ceil(np.sqrt(area_m2) * 3 / RES)) + 2  # pixels around the centre to consider
    rows = np.arange(max(r0 - reach, 0), min(r0 + reach + 1, river2d.shape[0]))
    cols = np.arange(max(c0 - reach, 0), min(c0 + reach + 1, river2d.shape[1]))
    win = river2d[np.ix_(rows, cols)]
    pr, pc = np.nonzero(win)
    if not len(pr):
        return {}
    # Every sub-pixel of every river pixel in the window, with its distance to the mat's centre.
    sr = (pr[:, None] + rows[0]) * SUB + np.repeat(np.arange(SUB), SUB)[None, :] + 0.5
    sc = (pc[:, None] + cols[0]) * SUB + np.tile(np.arange(SUB), SUB)[None, :] + 0.5
    dist = (sr - cy) ** 2 + (sc - cx) ** 2
    n_sub = int(round(area_m2 / (RES / SUB) ** 2))
    order = np.argsort(dist, axis=None, kind="stable")[:n_sub]
    owner = order // (SUB * SUB)  # which river pixel each covered sub-pixel belongs to
    cover = np.bincount(owner, minlength=len(pr)) / (SUB * SUB)
    return {(int(pr[i] + rows[0]), int(pc[i] + cols[0])): float(cover[i]) for i in np.nonzero(cover)[0]}


def ndvi_on(river, d, hist, det, tf, w, h, mask):
    """One date's NDVI per corridor pixel, from the local cache or downloaded again (GitHub's
    cache may not have it)."""
    items = [] if (ROOT / f"data/cache/{river['id']}/{d}.npz").exists() else [
        s for s in search(river, date.fromisoformat(d), date.fromisoformat(d), det).get(d, [])
        if s.id in hist["dates"][d]["scenes"]]
    return day_pixels(river["id"], d, items, tf, w, h, mask)["ndvi"]


def dense_mat_ndvi(river, dates, hist, det, tf, w, h, mask, river2d, limit=6):
    """NDVI of pure mat pixels: dense (NDVI > 0.5) with all 8 neighbours dense, on the weediest dates."""
    weedy = sorted(dates, key=lambda d: -hist["dates"][d]["hyacinth_pct"])[:limit]
    vals = []
    for d in weedy:
        nd = np.full(mask.shape, np.nan, "float32")
        nd[mask] = ndvi_on(river, d, hist, det, tf, w, h, mask)
        dense = (nd > 0.5) & river2d
        core = dense.copy()
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                core &= np.roll(np.roll(dense, dr, 0), dc, 1)
        vals.extend(nd[core].tolist())
    return np.array(vals, "float32")


def build(river, cfg, trials=150, seed=1):
    det, syn = cfg["detection"], cfg["synthetic"]
    thr = det["hyacinth_ndvi"]
    line, aoi, corridor = load_river(river, det)
    tf, w, h, mask = make_grid(corridor)
    river_px, green_ch, from_images = river_area(river["id"], tf, w, h, mask, aoi)
    segments = make_segments(line, det["segment_length_m"])
    rows, cols = np.nonzero(mask)
    xs, ys = rasterio.transform.xy(tf, rows, cols)
    seg_idx = STRtree(segments).query_nearest(points(xs, ys), return_distance=False, all_matches=False)[1]
    seg2d = np.full(mask.shape, -1)
    seg2d[mask] = seg_idx
    # Each stretch's confidence class, as the dashboard rates it.
    inner = river_px.copy()
    inner[1:] &= river_px[:-1]; inner[:-1] &= river_px[1:]; inner[:, 1:] &= river_px[:, :-1]; inner[:, :-1] &= river_px[:, 1:]
    k = len(segments)
    px = np.bincount(seg_idx[river_px[mask]], minlength=k)
    edge = np.bincount(seg_idx[(river_px & ~inner)[mask]], minlength=k)
    green = np.bincount(seg_idx[green_ch[mask] > 0], minlength=k)
    conf = [confidence(int(px[s]), int(edge[s]), int(green[s]), from_images, False, cfg["confidence"])[0]
            if px[s] + green[s] else None for s in range(k)]

    hist = json.loads((ROOT / f"data/history/{river['id']}.json").read_text())
    all_dates = sorted(hist["dates"])
    rejected = reject_dates(hist, all_dates, cfg["quality"])
    dates = [d for d in all_dates if d not in rejected]
    # Clear, almost weed-free dates, spread over the years.
    calm = [d for d in dates if hist["dates"][d]["hyacinth_pct"] < syn["max_background_pct"]
            and hist["dates"][d]["valid_fraction"] >= 0.95]
    picks = [calm[i] for i in np.linspace(0, len(calm) - 1, min(syn["dates"], len(calm))).round().astype(int)]
    mats = dense_mat_ndvi(river, dates, hist, det, tf, w, h, mask, river_px)
    print(f"  {len(picks)} clear dates, {len(mats)} pure mat pixels (median NDVI {np.median(mats):.2f})")

    ratios = (1.0, 0.5, 2.0)  # mat brightness as configured, then at half and double
    rng = np.random.default_rng(seed)
    river_cells = np.argwhere(river_px)
    results = []  # (size, class, true m2, found m2 at ratio 1, 0.5, 2)
    for d in picks:
        bg = np.full(mask.shape, np.nan, "float32")
        bg[mask] = ndvi_on(river, d, hist, det, tf, w, h, mask)
        for size in syn["sizes_m2"]:
            for _ in range(trials // len(picks) + 1):
                r0, c0 = river_cells[rng.integers(len(river_cells))]
                cover = plant(river_px, rng, size, (r0, c0))
                cells = list(cover)
                if not cells:
                    continue
                cr, cc = np.array(cells).T
                f = np.array([cover[c] for c in cells])
                nd_bg = bg[cr, cc]
                if not np.isfinite(nd_bg).all():
                    continue  # cloud in the way: not a fair trial
                nd_mat = rng.choice(mats, len(cells))
                before = (nd_bg > thr).sum()
                found = []
                for scale in ratios:
                    after = mixed_ndvi(f, nd_mat, nd_bg, syn["mat_brightness"] * scale, syn["water_brightness"])
                    found.append(float(((after > thr).sum() - before) * RES * RES))
                results.append((size, conf[seg2d[r0, c0]], float(f.sum() * RES * RES), *found))

    res = np.array([(s, t, a, b, c) for s, _, t, a, b, c in results], float)
    classes = np.array([c for _, c, *_ in results])

    def summarise(sel):
        true, found = res[sel, 1], res[sel, 2:]
        share = found.sum(axis=0) / true.sum()
        return {"trials": int(sel.sum()), "found_share": round(float(share[0]), 3),
                "found_share_range": [round(float(share.min()), 3), round(float(share.max()), 3)],
                # A mat counts as seen when at least one pixel newly reads as weed.
                "seen_share": round(float((res[sel, 2] > 0).mean()), 3)}

    by_size = [{"m2": int(s), **summarise(res[:, 0] == s)} for s in syn["sizes_m2"]]
    big = res[:, 0] >= syn["big_m2"]
    by_class = {c: summarise(big & (classes == c)) for c in ("high", "medium", "low") if (big & (classes == c)).sum() >= 20}
    out = {"dates": picks, "mat_ndvi_median": round(float(np.median(mats)), 3), "pure_mat_pixels": int(len(mats)),
           "brightness": {"mat": syn["mat_brightness"], "water": syn["water_brightness"], "tried": [0.5, 2.0]},
           "big_m2": syn["big_m2"], "by_size": by_size, "big_by_confidence": by_class}
    path = ROOT / f"data/validation/{river['id']}-synthetic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    for row in by_size:
        print(f"  {row['m2']:>6} m2: found {row['found_share']:.0%} of area "
              f"({row['found_share_range'][0]:.0%}-{row['found_share_range'][1]:.0%}), seen {row['seen_share']:.0%}")
    print(f"  by confidence (mats >= {syn['big_m2']} m2): {by_class}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rivers", nargs="*", help="river ids (default: all)")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    for river in cfg["rivers"]:
        if args.rivers and river["id"] not in args.rivers:
            continue
        print(river["name"])
        build(river, cfg)


if __name__ == "__main__":
    main()
