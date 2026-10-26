# River Watch

Automatic satellite monitoring of water hyacinth (floating vegetation) and muddy water on rivers,
starting with the Mula-Mutha in Pune. No manual steps: a scheduled job downloads new Sentinel-2
images, analyses the river, and republishes the dashboard.

## How it works

```
config/rivers.yaml          which rivers to watch + detection thresholds
pipeline/fetch_rivers.py    one-off: river outline + place names (English, Marathi) from OpenStreetMap -> data/rivers/
pipeline/run.py             scheduled: Sentinel-2 -> per-500 m stats -> data/history/ + web/data/
pipeline/build_bank_mask.py once a year: year-round green pixels (trees, islands) -> data/masks/
data/regions/               PMC and PCMC ward boundaries (see its README for sources)
web/                        static dashboard in English and Marathi (web/i18n.js)
.github/workflows/update.yml  runs the pipeline daily and deploys web/ to GitHub Pages
```

- **Imagery:** Sentinel-2 L2A (10 m, every ~5 days), free via Microsoft Planetary Computer.
- **Clouds:** scenes with more than 40% cloud are skipped; remaining cloud and shadow pixels are
  masked using the scene classification layer. Dates where under 60% of the river is visible are skipped.
- **Hyacinth:** river pixels with NDVI > 0.30.
- **Muddy water:** share of *open water* (NDWI > 0, not hyacinth) with NDTI (red vs green) > 0.05.
  Measuring it over all river pixels was wrong: mud banks, sparse weed and brown, dying hyacinth
  also look "red", so the old figure mostly tracked those and rose with hyacinth in the dry season.
  When less than 5% of the river is open water (choked by weed, very low flow) no figure is given.
- **Banks:** the river area is shrunk by one pixel (10 m) to reduce mixed land/water pixels.
- **Fixed vegetation:** pixels green on at least 80% of clear dates, both before (Mar–Jun) and after
  (Oct–Dec) the monsoon, are left out entirely. The monsoon flood flushes floating hyacinth, so a
  pixel that stays green through it is a tree, a wooded island or a farmed sandbar. On the Mula-Mutha
  this is about 10% of the mapped river, mostly within 15 m of a bank. Rebuild the mask after each
  monsoon with `uv run python pipeline/build_bank_mask.py`; `run.py` notices the new mask and
  recomputes all history (optical and radar) automatically.
- **Change:** each stretch is compared against its value 30–60 days earlier, and the dashboard
  also compares with the same time last year.
- **Municipal areas:** each stretch is matched to the PMC and PCMC wards covering at least 20% of a
  150 m corridor around it. Where the Mula forms the boundary a stretch belongs to both.
- **2-week outlook:** the median of each stretch's last three clear readings. Backtests showed that
  extending the recent trend was less accurate than this. Replaying every past date gives the
  published error (±4.8 points river-wide, ±8.5 per stretch), the likely range, and each stretch's
  chance of being above 30%. No outlook is given when the horizon falls in the monsoon.
- **Radar (monsoon):** Sentinel-1 (every ~12 days, sees through clouds) gives a river-wide hyacinth
  estimate. Open water is dark in VH backscatter (~-20 dB), floating plants brighter (~-16 dB). The
  threshold is re-calibrated on every run against days when an optical image was taken within 2 days,
  and its leave-one-out error is published on the dashboard (about ±7 points on the Mula-Mutha). Radar
  is too noisy per 500 m stretch, so the map and hotspot list stay optical-only. It is hidden until
  at least 5 calibration pairs exist.

## Run locally

```bash
uv run python pipeline/run.py
```

```bash
python3 -m http.server 8765 --directory web
```

Per-pixel indices are cached in `data/cache/` (not committed), so after changing a threshold or
the method (bump `METHOD` in `run.py`) the history is rebuilt from the cache without downloading
images again. Refresh only the place names, e.g. for new Marathi names, with
`uv run python pipeline/fetch_rivers.py --places`.

Backfill history from a date:

```bash
uv run python pipeline/run.py --since 2025-10-01
```

## Add a river

1. Add an entry to `config/rivers.yaml` (id, name, bbox, OSM name pattern).
2. Fetch its outline once: `uv run python pipeline/fetch_rivers.py <river-id>` and commit `data/rivers/`.
3. Backfill: `uv run python pipeline/run.py --since 2025-10-01 <river-id>`.
4. Once the history spans a monsoon, build its fixed-vegetation mask:
   `uv run python pipeline/build_bank_mask.py <river-id>`, rerun step 3's command without `--since`,
   and commit `data/masks/`.

The dashboard's river selector picks it up automatically from `web/data/index.json`.

## Deploy (fully automatic)

1. Push this folder to a GitHub repository.
2. In the repo settings, set **Pages → Source** to **GitHub Actions**.
3. The workflow runs daily at 07:00 IST (or run it manually from the Actions tab).

## Limits

- These are satellite estimates, not lab measurements. Year-round bank trees are excluded, but
  seasonal weeds on banks and sandbars can still be counted as hyacinth, and a hyacinth mat that
  survives a whole monsoon in place would be mistaken for fixed vegetation.
- Rivers narrower than about 20 m, and anything smaller than a pixel (plastic, garbage), can't be seen.
- During the monsoon (June–September) most optical dates are cloudy; only the less precise radar
  estimate continues, and it doesn't measure muddiness.
- Sentinel-2 data is free for any use (credit "Contains modified Copernicus Sentinel data").
  OpenStreetMap tiles are for light use only; for heavy traffic switch to a hosted tile provider.
