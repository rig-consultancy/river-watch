"""Land change near the watched roads, from free Sentinel-2 images (10 m).

Too coarse for a garbage pile or a car, but it shows where open land beside the roads has changed:
after the monsoon (Oct-Dec) open land here is normally green, so a patch that was green after
earlier monsoons and is bare after the last two is dumping, debris or construction - something to
check on the ground. Also the share of open land near the roads that is green, year by year.

Each run adds the clearest image of any new month (cached in data/cache/roadside/s2/<site>/), then
writes web/data/roadside-<site>-land.json for roadside.html.

Run with: uv run python pipeline/roadside_land.py [site-id ...]
"""

import json
import sys
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import shapely
import yaml
from affine import Affine
from rasterio.enums import Resampling
from rasterio.features import rasterize, shapes
from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

import run

ROOT = Path(__file__).resolve().parent.parent
BANDS = ("B02", "B03", "B04", "B08", "B11")  # blue, green, red, near infrared, short-wave infrared
POST, DRY = (10, 11, 12), (2, 3, 4)


# ---------------------------------------------------------------- images

def fetch(site_id, aoi, land):
    """Clearest image of every month not yet cached. Returns the grid (transform, w, h, mask) and cache."""
    cache = ROOT / "data/cache/roadside/s2" / site_id
    tf, w, h, mask = run.make_grid(aoi)
    fetch_months(aoi, cache, land, lambda stack: {"bands": stack}, label=site_id)
    return tf, w, h, mask, cache


def read_stack(its, tf, w, h):
    """Reflectance of BANDS for one day, mosaicking that day's tiles; clouds and shadows are NaN."""
    stack = np.full((len(BANDS), h, w), np.nan, "float32")
    for it in its:  # first clear pixel wins
        scl = run.read_band(it.assets["SCL"].href, tf, w, h, Resampling.nearest)
        off = 1000 if float(it.properties.get("s2:processing_baseline", "0")) >= 4.0 else 0
        bs = [run.read_band(it.assets[b].href, tf, w, h, Resampling.bilinear).astype("float32") for b in BANDS]
        ok = np.isin(scl, run.SCL_OK) & (bs[0] > 0) & np.isnan(stack[0])
        for k, b in enumerate(bs):
            stack[k][ok] = (b[ok] - off) / 10000
    return stack


def fetch_months(aoi, cache, land, save, months=None, max_new=None, label=""):
    """Cache the clearest image of each month (only `months`, if given) as cache/<date>.npz.

    save(stack) -> dict of arrays to store. At most `max_new` new months per call, so a long backfill
    is spread over several runs. Returns how many months are still to fetch.
    """
    cache.mkdir(parents=True, exist_ok=True)
    tf, w, h, mask = run.make_grid(aoi)
    items = run.search({"bbox": list(run.to_wgs(aoi.envelope).bounds)}, date.fromisoformat(land["start"]),
                       date.today(), {"max_scene_cloud": land["max_scene_cloud"]})
    by_month = defaultdict(list)
    for d, its in items.items():
        if months is None or int(d[5:7]) in months:
            by_month[d[:7]].append((np.mean([i.properties["eo:cloud_cover"] for i in its]), d, its))
    this_month = date.today().isoformat()[:7]
    # The current month stays open until it's over: a clearer day may still come.
    todo = [m for m in sorted(by_month)
            if not any(cache.glob(f"{m}-*.npz")) and not (m != this_month and (cache / f"{m}.none").exists())]

    def month(m):
        for _, d, its in sorted(by_month[m], key=lambda x: x[0])[:3]:
            try:
                stack = read_stack(its, tf, w, h)
            except Exception as e:  # noqa: BLE001 - an unreadable tile: try the month's next clearest day
                print(f"  {m} {d}: {type(e).__name__}", flush=True)
                continue
            if (~np.isnan(stack[0]))[mask].mean() >= land["min_clear"]:
                np.savez_compressed(cache / f"{d}.npz", **save(stack))
                print(f"  {label} {d}", flush=True)
                return
        if m != this_month:
            (cache / f"{m}.none").touch()

    batch = todo if max_new is None else todo[-max_new:]  # newest first matters most for the page
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(month, batch))
    return len(todo) - len(batch)


# ---------------------------------------------------------------- analysis (no downloads)

def indices(bands):
    b, _, r, n, s = bands
    with np.errstate(invalid="ignore", divide="ignore"):
        return (n - r) / (n + r), ((s + r) - (n + b)) / ((s + r) + (n + b))  # NDVI, bare soil index


def seasonal(dates, values, months):
    """Per-year median of the images taken in `months`."""
    out = {}
    for y in sorted({int(d[:4]) for d in dates}):
        idx = [k for k, d in enumerate(dates) if int(d[:4]) == y and int(d[5:7]) in months]
        if idx:
            with warnings.catch_warnings():  # pixels clouded on every image of a season stay NaN
                warnings.simplefilter("ignore", RuntimeWarning)
                out[y] = np.nanmedian(values[idx], axis=0)
    return out


def turned_bare(post_ndvi, post_bsi, candidates, land):
    """Pixels green after earlier monsoons and bare after the last two.

    post_ndvi / post_bsi: {year: array} of post-monsoon medians. Needs at least three seasons.
    """
    years = sorted(post_ndvi)
    last = years[-1]
    before = [y for y in years if y <= last - 2]
    if not before:
        return np.zeros_like(candidates), before
    base = np.nanmedian(np.stack([post_ndvi[y] for y in before]), axis=0)
    now = np.nanmedian(np.stack([post_ndvi[y] for y in years if y >= last - 1]), axis=0)
    now_bsi = np.nanmedian(np.stack([post_bsi[y] for y in years if y >= last - 1]), axis=0)
    with np.errstate(invalid="ignore"):
        lost = (candidates & (base > land["green_ndvi"]) & (base - now >= land["drop_ndvi"])
                & (now < land["bare_ndvi"]) & (now_bsi > -0.05))
    return lost, before


def bare_since(by_year, bare):
    """First year of the bare stretch that runs up to the latest season (an earlier dry year doesn't count)."""
    first = None
    for y in sorted(by_year, reverse=True):
        if not by_year[y] < bare:
            break
        first = y
    return first


def analyse(site, geo, buildings, tf, w, h, aoi, cache, land, wards):
    kinds = {f["properties"]["kind"]: shape(f["geometry"]) for f in geo["features"]}
    roads, society = run.to_utm(kinds["roads"]), run.to_utm(kinds["boundary"])
    bld = [run.to_utm(shape(f["geometry"])) for f in buildings["features"]]

    # Share of each pixel under mapped buildings (rasterised at 1 m, averaged to 10 m).
    fine = rasterize([(b, 1) for b in bld], out_shape=(h * 10, w * 10), transform=tf * Affine.scale(0.1),
                     dtype="uint8") if bld else np.zeros((h * 10, w * 10), "uint8")
    built = fine.reshape(h, 10, w, 10).mean(axis=(1, 3))
    ys, xs = np.mgrid[0:h, 0:w]
    cx, cy = tf * (xs + 0.5, ys + 0.5)
    dist = shapely.distance(shapely.points(cx.ravel(), cy.ravel()), roads).reshape(h, w)
    road_px = rasterize([(roads.buffer(4), 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool)
    in_soc = rasterize([(society, 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool)
    open_land = aoi & (built < land["max_built_share"]) & ~road_px & ~in_soc
    near = open_land & (dist <= land["near_road_m"])

    files = sorted(cache.glob("????-??-??.npz"))
    dates = [p.stem for p in files]
    ndvi = np.full((len(files), h, w), np.nan, "float32")
    bsi = ndvi.copy()
    for k, p in enumerate(files):
        ndvi[k], bsi[k] = indices(np.load(p)["bands"])
    post, post_bsi = seasonal(dates, ndvi, POST), seasonal(dates, bsi, POST)
    dry, dry_bsi = seasonal(dates, ndvi, DRY), seasonal(dates, bsi, DRY)
    # A season counts once it has an image from its last month or two (Oct-only is too early to judge).
    with np.errstate(invalid="ignore"):
        trend = [{"year": y, "green_pct": round(100 * float((post[y][near] > land["green_ndvi"]).mean()), 1),
                  "images": sum(1 for d in dates if int(d[:4]) == y and int(d[5:7]) in POST)} for y in post]

    lost, before = turned_bare(post, post_bsi, open_land & (dist <= land["patch_road_m"]), land)
    blobs = [shape(s) for s, v in shapes(lost.astype("uint8"), mask=lost, connectivity=8, transform=tf) if v]
    patches = []
    for poly in blobs:
        m = rasterize([(poly, 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool) & lost
        if m.sum() < land["min_pixels"]:
            continue
        c = run.to_wgs(poly.centroid)
        last = sorted(post)[-1]
        with np.errstate(invalid="ignore"):
            series = [round(float(np.nanmean(ndvi[k][m])), 3) for k in range(len(dates))]
            first = bare_since({y: np.nanmedian(post[y][m]) for y in post}, land["bare_ndvi"])
        series = [None if np.isnan(v) else v for v in series]  # cloud over the patch that month
        # Still bare through the latest monsoon (Jul-Oct): covered ground (construction, paving, a heap),
        # not land that greens up again.
        monsoon = [v for d, v in zip(dates, series) if f"{last}-07" <= d < f"{last}-11" and v is not None]
        patches.append({
            "area_m2": int(m.sum() * 100), "lat": round(c.y, 6), "lon": round(c.x, 6),
            "road_m": int(round(float(dist[m].min()))),
            "ward": next((n for n, wp in wards if wp.contains(Point(c.x, c.y))), None),
            "first_bare_year": first,
            "ndvi_before": round(float(np.nanmedian(np.stack([post[y][m] for y in before]))), 2),
            "ndvi_now": round(float(np.nanmedian(np.stack([post[y][m] for y in sorted(post)[-2:]]))), 2),
            "stayed_bare_in_monsoon": bool(monsoon) and max(monsoon) < land["bare_ndvi"],
            "geometry": mapping(shapely.set_precision(run.to_wgs(poly), 1e-6)),
            "series": series,
        })
    patches.sort(key=lambda p: -p["area_m2"])
    for i, p in enumerate(patches, 1):
        p["id"] = i

    ly = max(dry)
    with np.errstate(invalid="ignore"):
        bare_dry = near & (dry[ly] < 0.15) & (dry_bsi[ly] > 0)
    return {
        "site": site["id"], "dates": dates, "first": dates[0], "last": dates[-1], "images": len(dates),
        "open_land_ha": round(open_land.sum() / 100, 1), "near_roads_ha": round(near.sum() / 100, 2),
        "near_road_m": land["near_road_m"], "patch_road_m": land["patch_road_m"],
        "dry_year": ly, "bare_near_roads_dry_ha": round(bare_dry.sum() / 100, 2),
        "before_years": [min(before), max(before)] if before else None,
        "trend": trend, "patches": patches,
    }


def run_site(site, cfg, wards):
    land = cfg["land"]
    geo = json.loads((ROOT / f"data/roadside/{site['id']}.geojson").read_text())
    buildings = json.loads((ROOT / f"data/roadside/{site['id']}-buildings.geojson").read_text())
    roads = run.to_utm(shape(next(f for f in geo["features"] if f["properties"]["kind"] == "roads")["geometry"]))
    tf, w, h, mask, cache = fetch(site["id"], roads.buffer(land["corridor_m"]), land)
    res = analyse(site, geo, buildings, tf, w, h, mask, cache, land, wards)
    (ROOT / f"web/data/roadside-{site['id']}-land.json").write_text(json.dumps(res))
    print(f"{site['id']}: {res['images']} images {res['first']} to {res['last']}, "
          f"{len(res['patches'])} patches turned bare near the roads")


def main():
    cfg = yaml.safe_load((ROOT / "config/roadside.yaml").read_text())
    only = sys.argv[1:]
    wards = [(f["properties"]["name"], prep(shape(f["geometry"]).buffer(0)))
             for f in json.loads((ROOT / "data/regions/pcmc-wards.geojson").read_text())["features"]]
    for site in cfg["sites"]:
        if not only or site["id"] in only:
            run_site(site, cfg, wards)


if __name__ == "__main__":
    main()
