"""Is there a Sentinel-2 image the pipeline has not looked at yet? A few seconds' catalogue search,
run every 2 hours by .github/workflows/watch.yml, which starts the full update only when there is.

Prints the new dates and writes new=true|false to $GITHUB_OUTPUT when set.

  uv run python pipeline/check_new_images.py
"""

import json
import os
from datetime import date, datetime, timedelta, timezone

import yaml

from run import ROOT, search


def main():
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    det = cfg["detection"]
    today = datetime.now(timezone.utc).date()
    new = {}
    for river in cfg["rivers"]:
        if river.get("draft"):
            continue
        path = ROOT / f"data/history/{river['id']}.json"
        hist = json.loads(path.read_text()) if path.exists() else {"dates": {}, "skipped": []}
        seen = set(hist["dates"]) | set(hist.get("skipped", []))
        found = search(river, today - timedelta(days=det["lookback_days"]), today, det)
        new[river["id"]] = sorted(d for d in found if d not in seen and date.fromisoformat(d) <= today)
        print(f"{river['name']}: {', '.join(new[river['id']]) or 'nothing new'}")
    any_new = any(new.values())
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"new={'true' if any_new else 'false'}\n")


if __name__ == "__main__":
    main()
