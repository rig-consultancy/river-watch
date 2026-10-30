"""City-wide land watch from free Sentinel-2 images (10 m): where open land has turned bare, by ward.

Too coarse for a roadside pile, but large enough changes show: land that was green after earlier
monsoons (Oct-Dec) and is bare after the last two is a dumping ground, debris, or construction. Also
how much of each ward is green after the monsoon, year by year. Fully automatic and free.

Each run caches the clearest image of any new dry-season or post-monsoon month (as two small
indices, data/cache/city/<city>/) and writes web/data/city-<city>.json for city.html.

Run with: uv run python pipeline/city_land.py [city-id ...] [--all]   (--all: fetch every missing month now)
"""

import json
import sys
from pathlib import Path

import numpy as np
import shapely
import yaml
from rasterio.features import rasterize, shapes
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

import run
from roadside_land import POST, DRY, bare_since, fetch_months, indices, seasonal, turned_bare

ROOT = Path(__file__).resolve().parent.parent


def pack(stack):
    """NDVI and bare-soil index as int8 hundredths (-128 = no data): ~4 MB per image for the whole city."""
    out = {}
    for name, v in zip(("ndvi", "bsi"), indices(stack)):
        q = np.round(np.clip(v, -1, 1) * 100)
        out[name] = np.where(np.isnan(q), -128, q).astype("int8")
    return out


def unpack(a):
    return np.where(a == -128, np.nan, a.astype("float32") / 100)


def load(cache):
    """Dates and per-year medians (post-monsoon NDVI and BSI, dry-season NDVI), loading a year at a time."""
    files = sorted(cache.glob("????-??-??.npz"))
    dates = [p.stem for p in files]
    post, post_bsi, dry = {}, {}, {}
    for y in sorted({int(d[:4]) for d in dates}):
        mine = [(d, p) for d, p in zip(dates, files) if int(d[:4]) == y]
        arr = [np.load(p) for _, p in mine]
        ys = [d for d, _ in mine]
        nd = np.stack([unpack(a["ndvi"]) for a in arr])
        bs = np.stack([unpack(a["bsi"]) for a in arr])
        post |= seasonal(ys, nd, POST)
        post_bsi |= seasonal(ys, bs, POST)
        dry |= seasonal(ys, nd, DRY)
    return dates, post, post_bsi, dry


def analyse(city, wards, tf, w, h, inside, dates, post, post_bsi, land):
    """Changed patches and ward figures. `wards`: [(name, geometry in UTM)]."""
    ward_id = rasterize([(g, i + 1) for i, (_, g) in enumerate(wards)], out_shape=(h, w), transform=tf,
                        dtype="int16")
    lost, before = turned_bare(post, post_bsi, inside, land)
    years = sorted(post)
    patches = []
    for poly in (shape(s) for s, v in shapes(lost.astype("uint8"), mask=lost, connectivity=8, transform=tf) if v):
        m = rasterize([(poly, 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool) & lost
        n = int(m.sum())
        if n < land["min_pixels"]:
            continue
        c = run.to_wgs(poly.centroid)
        ids = ward_id[m]
        wid = int(np.bincount(ids[ids > 0]).argmax()) if (ids > 0).any() else 0
        with np.errstate(invalid="ignore"):
            series = {y: round(float(np.nanmedian(post[y][m])), 2) for y in years}
        first = bare_since(series, land["bare_ndvi"])
        patches.append({"area_m2": n * 100, "lat": round(c.y, 6), "lon": round(c.x, 6),
                        "ward": wards[wid - 1][0] if wid else None, "first_bare_year": first,
                        "ndvi_before": round(float(np.nanmedian(np.stack([post[y][m] for y in before]))), 2),
                        "ndvi_now": round(float(np.nanmedian(np.stack([post[y][m] for y in years[-2:]]))), 2),
                        "series": [series[y] for y in years],
                        "geometry": mapping(shapely.set_precision(run.to_wgs(poly), 1e-6))})
    patches.sort(key=lambda p: -p["area_m2"])

    def green_pct(sel):
        with np.errstate(invalid="ignore"):
            return [round(100 * float((post[y][sel] > land["green_ndvi"]).sum() / max(np.isfinite(post[y][sel]).sum(), 1)), 1)
                    for y in years]

    ward_rows = []
    for i, (name, g) in enumerate(wards, 1):
        sel = ward_id == i
        mine = [p for p in patches if p["ward"] == name]
        ward_rows.append({"name": name, "area_km2": round(g.area / 1e6, 2), "patches": len(mine),
                          "changed_m2": sum(p["area_m2"] for p in mine), "green_pct": green_pct(sel),
                          "geometry": mapping(shapely.set_precision(run.to_wgs(g).simplify(0.0002), 1e-5))})
    for i, p in enumerate(patches, 1):
        p["id"] = i
    return {"city": city["id"], "name": city["name"], "name_mr": city["name_mr"],
            "dates": dates, "first": dates[0], "last": dates[-1], "images": len(dates), "years": years,
            "before_years": [before[0], before[-1]] if before else None,
            "post_images": [sum(1 for d in dates if int(d[:4]) == y and int(d[5:7]) in POST) for y in years],
            "area_km2": round(float(inside.sum()) / 1e4, 1), "green_pct": green_pct(inside),
            "patches_total": len(patches), "changed_m2_total": sum(p["area_m2"] for p in patches),
            "patches": patches[:land["max_patches"]], "wards": ward_rows}


def run_city(city, land, fetch_all=False):
    feats = json.loads((ROOT / city["wards"]).read_text())["features"]
    wards = [(f["properties"]["name"], run.to_utm(shape(f["geometry"]).buffer(0))) for f in feats]
    area = unary_union([g for _, g in wards])
    tf, w, h, _ = run.make_grid(area)
    inside = rasterize([(area, 1)], out_shape=(h, w), transform=tf, dtype="uint8").astype(bool)
    cache = ROOT / "data/cache/city" / city["id"]
    left = fetch_months(area, cache, land, pack, months=set(land["months"]), max_new=None if fetch_all else land["max_new_per_run"],
                        label=city["id"])
    out = ROOT / f"web/data/city-{city['id']}.json"
    if left and out.exists():
        # Still filling the cache (a fresh CI cache fills 12 months a run): keep the published results,
        # which were made from the full record, until every month is back.
        print(f"{city['id']}: {left} months still to fetch; keeping the published results until then")
        return
    dates, post, post_bsi, _ = load(cache)
    if len(post) < 3:
        print(f"{city['id']}: {len(dates)} images so far, {left} months still to fetch; need three post-monsoon "
              f"seasons before comparing")
        return
    res = analyse(city, wards, tf, w, h, inside, dates, post, post_bsi, land)
    res["months_to_fetch"] = left
    out.write_text(json.dumps(res))
    print(f"{city['id']}: {res['images']} images {res['first']} to {res['last']} ({left} months still to fetch), "
          f"{res['patches_total']} patches turned bare, {res['changed_m2_total'] / 1e4:.1f} ha")


def main():
    cfg = yaml.safe_load((ROOT / "config/city.yaml").read_text())
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for city in cfg["cities"]:
        if not only or city["id"] in only:
            run_city(city, cfg["land"], fetch_all="--all" in sys.argv)
    (ROOT / "web/data/city-index.json").write_text(json.dumps(
        [{"id": c["id"], "name": c["name"], "name_mr": c["name_mr"]} for c in cfg["cities"]
         if (ROOT / f"web/data/city-{c['id']}.json").exists()]))


if __name__ == "__main__":
    main()
