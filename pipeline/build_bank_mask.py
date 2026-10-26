"""One-off: mark river pixels that are green all year (bank trees, islands, farmed sandbars).

Floating hyacinth is flushed out by the monsoon flood; fixed vegetation is not. A pixel counts
as fixed when it is green on most clear dates overall AND in both the pre-monsoon and
post-monsoon seasons. The result goes to data/masks/<river>.tif (commit it); run.py then
leaves those pixels out and rebuilds its history automatically.

Needs a year of history that spans a monsoon. Rerun after a monsoon or when the outline changes.

Usage:
  uv run python pipeline/build_bank_mask.py [river-id ...]
"""

import argparse
import json
from datetime import date

import numpy as np
import rasterio
import yaml

from run import (ROOT, UTM, bank_mask_path, classify, day_cache, load_river, make_grid,
                 read_day, search)


def day_pixels(river, det, d, items, tf, w, h, mask):
    """(ok, hyacinth) for one date, from the local cache when run.py already saw it."""
    path = day_cache(river["id"], d)
    if path.exists():
        z = np.load(path)
        if z["ok"].shape == mask.shape:
            return z["ok"], z["hy"]
    for attempt in range(3):  # Planetary Computer reads fail now and then
        try:
            ok, hy, _ = classify(*read_day(items, tf, w, h), mask, det)
            break
        except Exception as e:
            print(f"  {d}: read failed ({type(e).__name__}), retrying")
    else:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, ok=ok, hy=hy)
    return ok, hy


def build(river, det, cfg):
    _, aoi = load_river(river, det)
    tf, w, h, mask = make_grid(aoi)
    hist = json.loads((ROOT / f"data/history/{river['id']}.json").read_text())
    dates = sorted(hist["dates"])
    days = search(river, date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]), det)

    seasons = {"all": None, "pre": set(cfg["pre_monsoon_months"]), "post": set(cfg["post_monsoon_months"])}
    seen = {k: np.zeros((h, w), "uint16") for k in seasons}
    green = {k: np.zeros((h, w), "uint16") for k in seasons}
    for i, d in enumerate(dates, 1):
        px = day_pixels(river, det, d, days.get(d, []), tf, w, h, mask)
        if px is None:
            print(f"  {d}: skipped")
            continue
        ok, hy = px
        for k, months in seasons.items():
            if months is None or int(d[5:7]) in months:
                seen[k] += ok
                green[k] += hy
        print(f"  {i}/{len(dates)} {d}")

    share = {k: green[k] / np.maximum(seen[k], 1) for k in seasons}
    enough = (seen["pre"] >= cfg["min_dates_per_season"]) & (seen["post"] >= cfg["min_dates_per_season"])
    if not enough[mask].any():
        raise SystemExit("  history does not span a monsoon yet; nothing written")
    fixed = mask & enough
    for k in seasons:
        fixed &= share[k] >= cfg["min_green_share"]

    path = bank_mask_path(river["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="GTiff", width=w, height=h, count=1, dtype="uint8", crs=UTM,
                   transform=tf, compress="deflate", nbits=1)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(fixed.astype("uint8"), 1)
        dst.update_tags(dates=f"{dates[0]}/{dates[-1]}", n_dates=len(dates),
                        rule=json.dumps(cfg, separators=(",", ":")))
    print(f"  {fixed.sum()} of {mask.sum()} river pixels ({fixed.sum() / mask.sum():.1%}) are "
          f"year-round vegetation; {(mask & ~enough).sum()} lacked seasonal data -> {path.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rivers", nargs="*", help="river ids (default: all)")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    for river in cfg["rivers"]:
        if args.rivers and river["id"] not in args.rivers:
            continue
        print(river["name"])
        build(river, cfg["detection"], cfg["bank_mask"])


if __name__ == "__main__":
    main()
