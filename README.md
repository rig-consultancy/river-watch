# River Watch

Automatic satellite monitoring of water hyacinth (floating vegetation) and muddy water on rivers,
starting with the Mula-Mutha in Pune. No manual steps: a scheduled job downloads new Sentinel-2
images, analyses the river, and republishes the dashboard.

## How it works

```
config/rivers.yaml          which rivers to watch + detection thresholds
pipeline/fetch_rivers.py    one-off: river outline + place names (English, Marathi) from OpenStreetMap -> data/rivers/
pipeline/run.py             scheduled: Sentinel-2 -> per-500 m stats -> data/history/ + web/data/
pipeline/build_river_mask.py after each monsoon: where the river really is, from the images -> data/masks/
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
- **River area, from the images:** OpenStreetMap's outline is missing in places (only a
  centreline, whose 30 m strip can run along one bank) and elsewhere covers wide vegetated riverbed.
  So every pixel within 150 m of the centreline counts as river only if Sentinel-2 saw open water
  there on at least 15% of clear dates. Hyacinth mats pass (the monsoon flood clears them for
  weeks); bank trees, islands and dry vegetated riverbed never show water and drop out. On the
  Mula-Mutha this gives 4.1 km² of river, against 5.3 km² in the OSM outline. Rebuild it after each
  monsoon with `uv run python pipeline/build_river_mask.py`; `run.py` notices and rebuilds history.
- **Green all year (needs a ground check):** inside the OSM outline, pixels that are green on most
  clear dates and never open water (0.8 km² here, mostly Wakad–Balewadi and the upper Mutha) are not
  counted. A channel permanently choked by weed and a riverbed overgrown with plants look the same
  from space, so these stretches are flagged rather than ranked.
- **Confidence per stretch:** high, medium or low from how much river area a stretch has, how much
  of it is on the river edge (mixed water/bank pixels), and how much of its channel is green all
  year. Low-confidence stretches are faded on the map and left out of the rankings.
- **Bad images:** a date is left out for heavy haze (aerosol optical thickness above 0.8, from the
  image's own AOT band; normal here is 0.1–0.6), or when cloud, thin cirrus or cloud shadow covers at
  least 10% of the river corridor *and* its river-wide figures disagree with images within 3 weeks by
  more than 3 robust standard deviations. So far: 3 Jan 2026 (haze), 17 Jun 2026 and 10 Oct 2025
  (cloud). They are listed, with the reason, in the dashboard's data table.
- **One-off jumps:** a stretch reading more than 15 points away from both neighbouring readings (each
  within 2 weeks, and agreeing with each other) is shown but not used (95 of 5,642 readings). A
  sudden jump in the latest image is kept out of the rankings until the next image confirms it.
- **Likely ranges:** measured, not assumed. Images taken 3 days or less apart differ by ±3.2 points
  river-wide and ±4.8 for a high-confidence stretch (80% range); the greenness cut-off is also tried
  at NDVI 0.25 and 0.35. Each figure's range spans both. Trends hold at either cut-off (correlation
  0.98); absolute levels move by about ±20%.
- **Check it yourself:** every date stores the Sentinel-2 image it came from; the dashboard shows
  that photo for each stretch and links to ESA's Copernicus Browser at the same place and date.
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
4. Once the history spans a monsoon, build its river area from the images:
   `uv run python pipeline/build_river_mask.py <river-id>`, rerun step 3's command without `--since`,
   and commit `data/masks/`. Until then the OSM outline is used and every stretch is rated low.

The dashboard's river selector picks it up automatically from `web/data/index.json`.

## Deploy (fully automatic)

1. Push this folder to a GitHub repository.
2. In the repo settings, set **Pages → Source** to **GitHub Actions**.
3. The workflow runs daily at 07:00 IST (or run it manually from the Actions tab).

## Limits

- These are satellite estimates, not lab measurements, and they have not yet been checked against
  photos or surveys on the ground. Seasonal weeds at the water's edge can still be counted, and a
  hyacinth mat that never clears, even in the monsoon, is reported as "green all year", not counted.
- Channels narrower than about 20 m always mix water and bank in 10 m pixels; such stretches are
  shown as not measurable.
- Rivers narrower than about 20 m, and anything smaller than a pixel (plastic, garbage), can't be seen.
- During the monsoon (June–September) most optical dates are cloudy; only the less precise radar
  estimate continues, and it doesn't measure muddiness.
- Sentinel-2 data is free for any use (credit "Contains modified Copernicus Sentinel data").
  OpenStreetMap tiles are for light use only; for heavy traffic switch to a hosted tile provider.
