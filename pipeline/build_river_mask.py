"""Decide which pixels are river from where the satellite actually saw water.

OpenStreetMap's river outline is missing in places (only a centreline, whose 30 m strip can run
along one bank) and elsewhere covers wide vegetated riverbed below the dams. Instead, every pixel
within corridor_m of the centreline counts as river if Sentinel-2 saw open water there on at
least min_water_share of clear dates. Hyacinth mats pass, because the monsoon flood flushes
them and leaves open water for weeks; bank trees, islands and dry vegetated riverbed never show
water and drop out. Only areas connected to the channel are kept, so nearby ponds don't count.

Pixels inside the OSM river outline that are green on most clear dates and (almost) never open
water are marked separately as "green channel" (value 2): a stretch permanently choked by weed
and a riverbed overgrown with plants look the same from space, so they are reported as needing
a ground check rather than counted as hyacinth or silently dropped.

The result goes to data/masks/<river>.tif (commit it); run.py then uses it and rebuilds its
history automatically. Needs history spanning a monsoon; rerun after each monsoon.

Usage:
  uv run python pipeline/build_river_mask.py [river-id ...]
"""

import argparse
import json
from datetime import date

import numpy as np
import rasterio
import yaml
from rasterio.features import rasterize, shapes
from shapely.geometry import Polygon, shape

from run import ROOT, UTM, classify, day_pixels, load_river, make_grid, river_mask_path, search


def build(river, det, cfg):
    line, aoi, corridor = load_river(river, det)
    tf, w, h, mask = make_grid(corridor)
    hist = json.loads((ROOT / f"data/history/{river['id']}.json").read_text())
    dates = sorted(hist["dates"])
    days = search(river, date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]), det)

    n = int(mask.sum())
    seen, wet, green = np.zeros(n, "uint16"), np.zeros(n, "uint16"), np.zeros(n, "uint16")
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
    path = river_mask_path(river["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="GTiff", width=w, height=h, count=1, dtype="uint8", crs=UTM,
                   transform=tf, compress="deflate", nbits=2)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(river_px.astype("uint8") + 2 * green_ch.astype("uint8"), 1)
        dst.update_tags(dates=f"{dates[0]}/{dates[-1]}", n_dates=len(dates),
                        rule=json.dumps(cfg, separators=(",", ":")))
    km2 = lambda a: a.sum() * 100 / 1e6
    print(f"  river area {km2(river_px):.2f} km² (OSM outline {km2(osm):.2f} km²; "
          f"{km2(river_px & ~osm):.2f} km² outside it, {km2(osm & ~river_px):.2f} km² of it dropped, "
          f"{km2(green_ch):.2f} km² of that green all year) "
          f"-> {path.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rivers", nargs="*", help="river ids (default: all)")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    for river in cfg["rivers"]:
        if args.rivers and river["id"] not in args.rivers:
            continue
        print(river["name"])
        build(river, cfg["detection"], cfg["river_mask"])


if __name__ == "__main__":
    main()
