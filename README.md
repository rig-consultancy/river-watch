# River Watch

Automatic satellite monitoring of water hyacinth (floating vegetation) and muddy water on rivers,
starting with the Mula-Mutha in Pune. No manual steps: a scheduled job downloads new Sentinel-2
images, analyses the river, and republishes the dashboard.

## How it works

```
config/rivers.yaml          which rivers to watch + detection thresholds
pipeline/fetch_rivers.py    one-off: river outline + place names from OpenStreetMap -> data/rivers/
pipeline/run.py             scheduled: Sentinel-2 -> per-500 m stats -> data/history/ + web/data/
web/                        static dashboard (Leaflet map, charts, hotspot list)
.github/workflows/update.yml  runs the pipeline daily and deploys web/ to GitHub Pages
```

- **Imagery:** Sentinel-2 L2A (10 m, every ~5 days), free via Microsoft Planetary Computer.
- **Clouds:** scenes with more than 40% cloud are skipped; remaining cloud and shadow pixels are
  masked using the scene classification layer. Dates where under 60% of the river is visible are skipped.
- **Hyacinth:** river pixels with NDVI > 0.30. **Muddy water:** NDTI (red vs green) > 0.05.
- **Banks:** the river area is shrunk by one pixel (10 m) to reduce mixed land/water pixels.
- **Change:** each stretch is compared against its value 30–60 days earlier.

## Run locally

```bash
uv run python pipeline/run.py
```

```bash
python3 -m http.server 8765 --directory web
```

Backfill history from a date:

```bash
uv run python pipeline/run.py --since 2025-10-01
```

## Add a river

1. Add an entry to `config/rivers.yaml` (id, name, bbox, OSM name pattern).
2. Fetch its outline once: `uv run python pipeline/fetch_rivers.py <river-id>` and commit `data/rivers/`.
3. Backfill: `uv run python pipeline/run.py --since 2025-10-01 <river-id>`.

The dashboard's river selector picks it up automatically from `web/data/index.json`.

## Deploy (fully automatic)

1. Push this folder to a GitHub repository.
2. In the repo settings, set **Pages → Source** to **GitHub Actions**.
3. The workflow runs daily at 07:00 IST (or run it manually from the Actions tab).

## Limits

- These are satellite estimates, not lab measurements. Bank vegetation and weeds on sandbars can
  be counted as hyacinth.
- Rivers narrower than about 20 m, and anything smaller than a pixel (plastic, garbage), can't be seen.
- During the monsoon (June–September) most dates are cloudy and skipped.
- Sentinel-2 data is free for any use (credit "Contains modified Copernicus Sentinel data").
  OpenStreetMap tiles are for light use only; for heavy traffic switch to a hosted tile provider.
