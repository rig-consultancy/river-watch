"""Decide which pixels are river from where the satellite actually saw water.

OpenStreetMap's river outline is missing in places (only a centreline, whose 30 m strip can run
along one bank) and elsewhere covers wide vegetated riverbed below the dams. Instead, every pixel
within corridor_m of the centreline counts as river if Sentinel-2 saw open water there on at
least min_water_share of clear dates. Hyacinth mats pass, because the monsoon flood flushes
them and leaves open water for weeks; bank trees, islands and dry vegetated riverbed never show
water and drop out. Only areas connected to the channel are kept, so nearby ponds don't count.

Pixels inside the OSM river outline that are green on most clear dates and (almost) never open
water are marked separately as "green channel": a stretch permanently choked by weed and a
riverbed overgrown with plants look alike on any one image, so they are not counted as hyacinth.
The flood test then tells them apart: the monsoon flood washes floating hyacinth out (in the
measured channel about 90% of dry-season hyacinth is gone in the first images after it), while
rooted plants are still there. Green-channel pixels are stored as 2 (survived every flood in
place: rooted plants, or a pocket floods don't reach), 3 (cleared by a flood: floating) or
4 (unclear). Evidence for the whole river goes to data/masks/<river>.json.

The result goes to data/masks/<river>.tif (commit it); run.py then uses it and rebuilds its
history automatically. Needs history spanning a monsoon; rerun after each monsoon.

Usage:
  uv run python pipeline/build_river_mask.py [river-id ...]
"""

import argparse
import json
from datetime import date, timedelta

import numpy as np
import rasterio
import yaml
from rasterio.features import rasterize, shapes
from shapely.geometry import Polygon, shape

from run import (ROOT, UTM, classify, day_pixels, load_river, make_grid, reject_dates, river_mask_path,
                 search)

SURVIVED, CLEARED, UNCLEAR = 2, 3, 4


def flood_test(green, ok, dates, cfg):
    """Per pixel: green share in each post-monsoon window (by year) with enough clear looks."""
    shares = []
    for y in sorted({int(d[:4]) for d in dates}):
        sel = np.array([int(d[:4]) == y and int(d[5:7]) in cfg["flood_months"] for d in dates])
        n = ok[sel].sum(0)
        if sel.any():
            shares.append((y, np.where(n >= 2, green[sel].sum(0) / np.maximum(n, 1), np.nan)))
    return shares


def build(river, full_cfg):
    det, cfg = full_cfg["detection"], full_cfg["river_mask"]
    line, aoi, corridor = load_river(river, det)
    tf, w, h, mask = make_grid(corridor)
    hist = json.loads((ROOT / f"data/history/{river['id']}.json").read_text())
    rejected = reject_dates(hist, sorted(hist["dates"]), full_cfg["quality"])  # hazy or cloud-hit images
    # Only recent years: the channel shifts (floods, riverfront works), and CI has to download them.
    since = (date.fromisoformat(max(hist["dates"])) - timedelta(days=365 * cfg["mask_years"])).isoformat()
    dates = [d for d in sorted(hist["dates"]) if d not in rejected and d > since]
    days = search(river, date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]), det)

    n = int(mask.sum())
    seen, wet, green = np.zeros(n, "uint16"), np.zeros(n, "uint16"), np.zeros(n, "uint16")
    ok_all, green_all = np.zeros((len(dates), n), bool), np.zeros((len(dates), n), bool)
    for i, d in enumerate(dates, 1):
        for attempt in range(3):  # Planetary Computer reads fail now and then
            try:
                px = day_pixels(river["id"], d, days.get(d, []), tf, w, h, mask)
                break
            except Exception as e:
                print(f"  {d}: read failed ({type(e).__name__}), retrying")
        else:
            print(f"  {d}: skipped")
            continue
        ok, hy, water = classify(px, det)
        seen += ok
        wet += water
        green += hy
        ok_all[i - 1], green_all[i - 1] = ok, hy
        print(f"  {i}/{len(dates)} {d}")

    share = wet / np.maximum(seen, 1)
    candidate = np.zeros((h, w), bool)
    candidate[mask] = (seen >= cfg["min_clear_dates"]) & (share >= cfg["min_water_share"])

    # Keep only areas touching the channel, filling small holes (a mat that sat through a
    # whole season) but not islands.
    channel = line.buffer(cfg["channel_m"])
    polys = []
    for geom, _ in shapes(candidate.astype("uint8"), mask=candidate, transform=tf, connectivity=8):
        p = shape(geom)
        if p.intersects(channel):
            holes = [r for r in p.interiors if Polygon(r).area >= cfg["fill_holes_m2"]]
            polys.append(Polygon(p.exterior, holes))
    river_px = rasterize([(p, 1) for p in polys], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool) & mask

    osm = rasterize([(aoi, 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool)
    green_ch = np.zeros((h, w), bool)
    green_ch[mask] = (seen >= cfg["min_clear_dates"]) & (green / np.maximum(seen, 1) >= cfg["min_green_share"])
    green_ch &= osm & ~river_px

    # Flood test for the green channel: survived every post-monsoon window in place, or cleared.
    windows = flood_test(green_all, ok_all, dates, cfg)
    stack = np.stack([s for _, s in windows]) if windows else np.full((1, n), np.nan)
    has = np.isfinite(stack).any(0)
    survived = has & np.all(np.nan_to_num(stack, nan=1) >= cfg["flood_stayed_above"], axis=0)
    cleared = np.any(np.nan_to_num(stack, nan=1) < cfg["flood_cleared_below"], axis=0)
    out = river_px.astype("uint8")
    cls = np.where(cleared, CLEARED, np.where(survived, SURVIVED, UNCLEAR)).astype("uint8")
    out[green_ch] = cls[green_ch[mask]]

    # Evidence for the whole river: everything inside the outline that was green through a dry
    # season, whatever the water rule said, and what the following flood did to it.
    evidence = []
    for y, post in windows:
        dry = np.array([int(d[:4]) == y and int(d[5:7]) in cfg["dry_months"] for d in dates])
        n_dry = ok_all[dry].sum(0)
        was_green = osm[mask] & (n_dry >= 8) & (green_all[dry].sum(0) / np.maximum(n_dry, 1) >= 0.5) & np.isfinite(post)
        if was_green.sum() < 100:
            continue
        in_ch, in_gc = was_green & river_px[mask], was_green & green_ch[mask]
        evidence.append({
            "year": y, "dry_green_px": int(was_green.sum()),
            "cleared_px": int((post[was_green] < cfg["flood_cleared_below"]).sum()),
            "stayed_px": int((post[was_green] >= cfg["flood_stayed_above"]).sum()),
            "channel_cleared_share": round(float((post[in_ch] < cfg["flood_cleared_below"]).mean()), 2) if in_ch.any() else None,
            "green_channel_stayed_share": round(float((post[in_gc] >= cfg["flood_stayed_above"]).mean()), 2) if in_gc.any() else None,
        })
    river_mask_path(river["id"]).with_suffix(".json").write_text(json.dumps({
        "flood_windows": {str(y): [d for d in dates if int(d[:4]) == y and int(d[5:7]) in cfg["flood_months"]]
                          for y, _ in windows},
        "flood_test": evidence,
    }, indent=1))

    path = river_mask_path(river["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="GTiff", width=w, height=h, count=1, dtype="uint8", crs=UTM,
                   transform=tf, compress="deflate")
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(out, 1)
        dst.update_tags(dates=f"{dates[0]}/{dates[-1]}", n_dates=len(dates),
                        rule=json.dumps(cfg, separators=(",", ":")))
    km2 = lambda a: a.sum() * 100 / 1e6
    print(f"  river area {km2(river_px):.2f} km² (OSM outline {km2(osm):.2f} km²; "
          f"{km2(river_px & ~osm):.2f} km² outside it, {km2(osm & ~river_px):.2f} km² of it dropped, "
          f"{km2(green_ch):.2f} km² of that green all year: {km2(out == SURVIVED):.2f} survived the floods, "
          f"{km2(out == CLEARED):.2f} cleared, {km2(out == UNCLEAR):.2f} unclear) -> {path.relative_to(ROOT)}")
    for e in evidence:
        print(f"  flood {e['year']}: of {e['dry_green_px']} px green through the dry season, {e['cleared_px']} cleared, "
              f"{e['stayed_px']} stayed; channel cleared {e['channel_cleared_share']}, green channel stayed "
              f"{e['green_channel_stayed_share']}")


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
