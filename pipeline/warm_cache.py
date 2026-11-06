"""Download into data/cache the images the yearly jobs need, so GitHub's cache holds them.

The yearly river-area rebuild (build_river_mask.py) reads every clear image of the last
mask_years; the planted-weed test (synthetic_test.py) reads a few more. Both download what is
missing, but slowly in a single run; this fills the cache ahead of time. Run once from GitHub
(Actions > Update river data > Run workflow > warm cache); GitHub then keeps the cache.

  uv run python pipeline/warm_cache.py [river-id ...]
"""

import argparse
import json
from datetime import date, timedelta

import yaml

from run import ROOT, load_river, make_grid, prefetch, reject_dates, search


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rivers", nargs="*", help="river ids (default: all)")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    det = cfg["detection"]
    for river in cfg["rivers"]:
        if args.rivers and river["id"] not in args.rivers:
            continue
        hist = json.loads((ROOT / f"data/history/{river['id']}.json").read_text())
        all_dates = sorted(hist["dates"])
        rejected = reject_dates(hist, all_dates, cfg["quality"])
        since = (date.fromisoformat(all_dates[-1]) - timedelta(days=365 * cfg["river_mask"]["mask_years"])).isoformat()
        dates = [d for d in all_dates if d not in rejected and d > since]
        # The planted-weed test's clear dates come from the whole record: add its weediest ones too.
        dates += sorted((d for d in all_dates if d not in rejected),
                        key=lambda d: -hist["dates"][d]["hyacinth_pct"])[:6]
        _, _, corridor = load_river(river, det)
        tf, w, h, mask = make_grid(corridor)
        missing = sorted({d for d in dates if not (ROOT / f"data/cache/{river['id']}/{d}.npz").exists()})
        print(f"{river['name']}: {len(set(dates))} images needed, {len(missing)} not cached")
        if not missing:
            continue
        days = search(river, date.fromisoformat(missing[0]), date.fromisoformat(missing[-1]), det)
        # The images each stored date came from (the catalogue may list more for that day).
        found = {d: [it for it in days[d] if it.id in hist["dates"][d]["scenes"]] or days[d]
                 for d in missing if d in days}
        prefetch(river["id"], found, tf, w, h, mask)


if __name__ == "__main__":
    main()
