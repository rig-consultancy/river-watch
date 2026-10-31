# River Watch

Automatic satellite monitoring of water hyacinth (floating vegetation) and muddy water on Pune's
rivers: the Mula-Mutha, Pawana and Indrayani, from 2017. No manual steps: a scheduled job downloads
new Sentinel-2 images, analyses the rivers, and republishes the dashboard and monthly reports.

Copyright RIG Consultancy, all rights reserved (see LICENSE).

## How it works

```
config/rivers.yaml          which rivers to watch + detection thresholds
pipeline/fetch_rivers.py    one-off: river outline + place names (English, Marathi) from OpenStreetMap -> data/rivers/
pipeline/run.py             scheduled: Sentinel-2 -> per-500 m stats -> data/history/ + web/data/
pipeline/build_river_mask.py after each monsoon (CI runs it on 15 Oct): river area + flood test -> data/masks/
pipeline/crosscheck_landsat.py monthly (CI, 1st of the month, --new): independent Landsat 8/9 check -> data/validation/
pipeline/synthetic_test.py   yearly (CI, 15 Oct): planted-weed test on real clear images -> data/validation/
data/regions/               PMC and PCMC ward boundaries (see its README for sources)
web/                        dashboard and monthly report (report.html), English and Marathi (web/i18n.js)
tests/                      uv run pytest: calculations and the published data
.github/workflows/update.yml  tests, runs the pipeline daily and on every push, deploys web/ to GitHub Pages
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
- **Green all year, and the flood test:** inside the OSM outline, pixels that are green on most
  clear dates and never open water (0.8 km² here, mostly Wakad–Balewadi and the upper Mutha) are not
  counted. On one image a channel choked by weed and a riverbed overgrown with plants look the same;
  the monsoon tells them apart. Of 17,706 outline pixels green through the 2026 dry season, 7,838 were
  gone in the first clear images after the flood and 7,977 were still there (few in between): 90% of
  the hyacinth in the measured channel washed out, while 88% of the green-all-year area stayed. Floating
  weed doesn't survive these floods in place, so each stretch's green area gets a verdict: likely
  rooted plants (≥70% survived every flood), floating (≥50% washed out) or mixed. The other way an area
  survives is a pocket the floods don't reach; that can't be told apart from space.
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
- **Clean-up detection:** a stretch with at least 20% cover that drops 15+ points to half or less
  within 3 weeks, stays low, outside the monsoon, is labelled by cause: "rain" (25 mm or more in
  between, daily rainfall from NASA POWER, public domain), "river-wide" (30%+ of stretches with weed
  dropped together: flow or wind) or "probable clean-up". Days until it grew back to half are tracked.
- **Landsat check:** `crosscheck_landsat.py` measures the same pixels (at least 20 m inside the river)
  from Landsat 8/9 within a day of a Sentinel-2 image. On the Mula-Mutha (82 pairs, 2017-2026)
  stretches move together (correlation 0.78) but Landsat reads about 7 points higher at every level:
  its 30 m pixels still catch bank plants on a 40-60 m river. It confirms direction, not level.
  CI adds new Landsat images on the 1st of every month (`--new`; every pair again if the river
  area changed). The dashboard's "Cross-checked by other satellites" panel shows, for Landsat and
  for the Sentinel-1 radar, the days compared, how often they moved the same way as Sentinel-2
  when it changed by 5 points or more, the typical difference, and when Landsat was last checked;
  Landsat readings are also plotted on the hyacinth chart.
- **Landsat check of muddy water:** the same pairs also compare the mean NDTI of open water seen by
  both satellites (the share above the muddiness threshold is almost always 0 here, so it can't be
  compared). Shown in the dashboard's "How the figures are checked" panel.
- **Planted-weed test:** `synthetic_test.py` plants mats of 100 m² to 1 ha into 8 clear, almost
  weed-free images per river (grown over river pixels on a 2 m grid, mixed with the real pixel in
  reflectance, mat NDVI from the river's own dense mats) and measures them with the normal rule.
  Result: every mat is seen; mats of 1 ha are measured within about 1-6% of their area, but small
  ones are over-counted (100 m² reads as about twice its size), because a pixel about 20% covered
  by a dense mat already passes the threshold. Mat and water brightness are assumed (config
  `synthetic`) and also tried at half and double their ratio.
- **Muddier than the rest:** on dry-weather images (under 1 mm of rain in 7 days) each stretch's
  mean NDTI is compared with the river's middle stretch that day; one that is redder on at least
  90% of 30+ dry days by 0.04 or more is flagged (not low-confidence stretches, where bank pixels
  mix in). Possible causes: a drain or outlet upstream, or a shallow muddy bed.
- **Rain check of muddy water:** images after 10 mm of rain in 3 days against dry ones within 30
  days. Rain brings cloud, so each river has only 3-5 such images: no verdict yet, and the
  dashboard says so.
- **Alerts:** a stretch crossing the alert level, rising 15 points within 3 weeks, or a probable
  clean-up, in the last 120 days (not low-confidence stretches or unconfirmed jumps). Shown in the
  dashboard's Alerts tab and published as an Atom feed, `web/feed.xml`, for feed readers or
  feed-to-email services; no account or key is needed.
- **Monthly report:** `report.html?river=&area=&month=` is a printable page (save as PDF) with the
  month's average and range, change, river map, worst stretches, drops and data quality.
- **Downloads:** history backfills fetch 4 dates at a time into the cache (`prefetch` in run.py).
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

## Roadside watch (pilot): garbage dumps and long-parked vehicles

Along the roads outside a housing society (first: Mahindra Antheia, Pimpri), from very high
resolution images (30-50 cm per pixel; Sentinel-2's 10 m pixels can't see either). No free source
covers Pune at that resolution, so images come from a paid provider; the code is ready for them.

```
config/roadside.yaml        sites, detector settings, tracking distances
pipeline/fetch_roads.py     one-off: society boundary + public roads outside it + 15 m strip -> data/roadside/<site>.geojson
pipeline/roadside.py        scheduled: new GeoTIFFs in data/roadside/images/<site>/ (YYYY-MM-DD*.tif, not committed)
                            -> detections per date in data/roadside/<site>/ -> web/data/roadside-<site>.json
```

```bash
uv run --group detect python pipeline/roadside.py
```

**Buying images automatically (UP42):** `pipeline/up42_fetch.py` runs in the daily workflow. It
looks for a new Pléiades Neo (30 cm) scene over the roads at most once a fortnight, takes only scenes
UP42 delivers within 10 minutes (charged as 0.1 km² instead of 5 km²), with under 10% cloud and
covering every watched road, checks UP42's price, and orders it only within the caps in
`config/roadside.yaml` (default about €12 per image, €20 per month). The delivered GeoTIFF goes to
`data/roadside/images/<site>/`, roadside.py analyses it the same run, and orders are kept in
`data/roadside/<site>/up42-orders.json`. One-time setup by the account owner: create the UP42 account
and buy or request credits, accept the Pléiades Neo licence once in the UP42 console, and add the
repository secrets `UP42_USERNAME` (account email) and `UP42_PASSWORD`. Use a UP42 account made for
this, since UP42 signs scripts in with the account password.

Satellite images are large and stay out of git; the detections they produce
(`data/roadside/<site>/YYYY-MM-DD.json`, a few KB each) are committed. So: put new GeoTIFFs in
`data/roadside/images/<site>/`, run the command above, commit `data/roadside/`, push. The daily
workflow then re-tracks every site from the committed detections and republishes
`roadside.html` (no detectors needed); a provider downloader running in the workflow can drop images
in the same folder and the detectors are installed for that run. Until a site has detections,
`uv run python pipeline/roadside.py --example` publishes made-up data and the page shows a banner.

- **Vehicles:** YOLO oriented boxes trained on DOTA aerial images. On 20 held-out DOTA images it found
  88% of labelled vehicles (75% of its boxes matched a label; DOTA leaves some cars unlabelled, so
  this is a floor). With pixels made 2x / 3x coarser, 78% / 66%: at 30-50 cm a car is only 9-15 px.
- **Garbage:** an open-vocabulary detector (OWLv2) asked for piles of garbage, rubble and dumped waste,
  no training needed. On 60 AerialWaste test images (dump sites in Italy, 30 with waste) it told dump
  sites from clean ones with AUC 0.86 (score 0.10: 80% of dump sites caught, 73% of alerts right), but
  its best box sat on the marked waste in only 11 of 30. Treat it as a starting point: once real images
  of the site exist, confirm its finds and train a detector on them.
- **How long:** each dump or vehicle is followed from image to image by position (vehicles also by
  length). Between images we can't know, so each stay has a minimum (first to last sighting) and a
  maximum (last image without it before, to the first without it after). An image that didn't cover
  a spot doesn't count as it being gone. A dump place that is cleared and refilled counts as coming back.
- **Land change (free, real data now):** `pipeline/roadside_land.py` takes the clearest Sentinel-2
  image of every month since 2017 within 300 m of the roads (10 m: too coarse for piles or cars).
  Leaving out buildings mapped in OSM (`data/roadside/<site>-buildings.geojson`, from
  `fetch_roads.py --buildings`), the roads and the society, it flags open land within 100 m of a road
  that was green after earlier monsoons (Oct-Dec) and is bare after the last two: dumping, debris or
  construction, to check on the ground. Around Antheia (82 images): a 5,600 m² patch 25 m from the
  road that stayed bare through the 2026 monsoon (covered ground), and a 2,700 m² cleared plot.
  Runs daily with the rest; only new months are downloaded.
- **Analytics:** dumps open now, days uncleared, cleared and how fast, places that come back,
  vehicles per image, share of kerb length taken by parking, vehicles seen in one place on images
  `long_parked_days` or more apart. Each spot gets its PCMC ward (the Antheia roads cross wards 27,
  28, 40 and 41), so a complaint can go to the right ward office.
- **Limits:** trees over the road hide whatever is under them; a satellite passes at about the
  same time each day, so a resident's car parked there every night looks the same as one never moved;
  two similar cars in one spot can't be told apart. Licences: the vehicle model (Ultralytics) is
  AGPL-3.0, which needs a commercial licence or a swap before this is offered as a closed service;
  AerialWaste is CC BY-NC-ND and was used only to score the detector, not to train it.

## PCMC land watch (free, automatic)

`city.html`: where open land across Pimpri-Chinchwad has turned bare, ward by ward, from free
Sentinel-2 images (10 m) - dumping grounds, debris or construction, which look the same from space.
Individual roadside piles are far below one pixel and can't be seen in free images.

```
config/city.yaml            cities, months used, thresholds
pipeline/city_land.py       scheduled: clearest image of each Feb-Apr and Oct-Dec month since 2017, stored as
                            int8 NDVI + bare-soil index (data/cache/city/, ~6 MB each) -> web/data/city-<id>.json
```

A patch counts when it was green after the earlier monsoons (Oct-Dec) and is bare after the last two
(same test as the Antheia land change), and is at least 500 m². For PCMC (57 images, 2017-2026):
2,221 patches, 552 ha, most turning bare in 2023-2025; wards 07, 52 and 06 have the most. Each ward
also gets the share of its land that is green after each monsoon. A fresh workflow cache refills 12
months a run; the published results are only replaced once every month is back.
`uv run python pipeline/city_land.py --all` fetches everything at once.

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
