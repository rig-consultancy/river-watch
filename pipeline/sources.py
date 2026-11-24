"""Where the images come from and how they are read: the two Sentinel-2 catalogues (Planetary
Computer, and Element 84 Earth Search for what it lacks), Landsat 8/9, and the local pixel cache
(data/cache/). run.py imports everything here under the same names.
"""

import json
import re
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

ROOT = Path(__file__).resolve().parent.parent
UTM = "EPSG:32643"  # UTM 43N covers all of Maharashtra
# Scene classification classes that are usable: dark area, vegetation, bare, water, unclassified.
SCL_OK = [2, 4, 5, 6, 7]
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
# The same Sentinel-2 images on Amazon (Element 84 Earth Search, free, no key): used for any day
# Planetary Computer lacks or when it is down. Its files are named differently and say their own scale.
# Earth Search lists file locations pystac can't parse for its storage extension; harmless.
warnings.filterwarnings("ignore", message="Could not parse bucket", module="pystac")
ES_URL, ES_COLLECTION = "https://earth-search.aws.element84.com/v1", "sentinel-2-c1-l2a"
S2_ASSETS = {"pc": {"green": "B03", "red": "B04", "nir": "B08", "scl": "SCL", "aot": "AOT"},
             "es": {"green": "green", "red": "red", "nir": "nir", "scl": "scl", "aot": "aot"}}


def read_band(href, tf, w, h, resampling):
    # Re-sign right before reading: links signed at search time expire (~1 h) during long backfills.
    if "blob.core.windows.net" in href:
        href = planetary_computer.sign_url(href.split("?")[0])
    with rasterio.open(href) as src, WarpedVRT(src, crs=UTM, transform=tf, width=w, height=h,
                                                resampling=resampling) as vrt:
        return vrt.read(1)


def s2_source(item):
    return "es" if item.collection_id == ES_COLLECTION else "pc"


def scene_id(item):
    """The image's name as stored with every figure: Planetary Computer's ID, which is ESA's product
    name without the processing-baseline part, whichever catalogue the image came from."""
    if s2_source(item) == "pc":
        return item.id
    return re.sub(r"_N\d{4}", "", item.properties["s2:product_uri"].removesuffix(".SAFE"))


def read_scene(item, tf, w, h):
    """Return reflectance arrays (green, red, nir) and a validity mask for one scene."""
    names = S2_ASSETS[s2_source(item)]
    scl = read_band(item.assets[names["scl"]].href, tf, w, h, Resampling.nearest)
    bands = [read_band(item.assets[names[b]].href, tf, w, h, Resampling.bilinear).astype("float32")
             for b in ("green", "red", "nir")]
    if s2_source(item) == "es":  # each file states its scale and offset
        rb = [item.assets[names[b]].extra_fields["raster:bands"][0] for b in ("green", "red", "nir")]
        green, red, nir = [b * r["scale"] + r["offset"] for b, r in zip(bands, rb)]
    else:
        # Processing baseline 04.00+ (2022 onwards) adds a +1000 offset to digital numbers.
        offset = 1000 if float(item.properties.get("s2:processing_baseline", "0")) >= 4.0 else 0
        green, red, nir = [(b - offset) / 10000 for b in bands]
    valid = np.isin(scl, SCL_OK) & (bands[0] > 0)
    return green, red, nir, valid


# ---------- Landsat 8/9 between Sentinel-2 images ----------
# QA_PIXEL bits: 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow.
QA_BAD = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)


def landsat_scenes(river, since, until):
    cat = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
    items = cat.search(collections=["landsat-c2-l2"], bbox=river["bbox"], datetime=f"{since}/{until}",
                       query={"platform": {"in": ["landsat-8", "landsat-9"]}, "eo:cloud_cover": {"lt": 60}}).item_collection()
    return sorted(items, key=lambda it: it.datetime)


def landsat_indices(item, tf, w, h, mask):
    """NDVI, NDWI and NDTI per river-corridor pixel from one Landsat scene (NaN where cloudy or
    no data), defined as for Sentinel-2."""
    green, red, nir = (read_band(item.assets[b].href, tf, w, h, Resampling.nearest)[mask].astype("float32")
                       * 0.0000275 - 0.2 for b in ("green", "red", "nir08"))  # Collection 2 reflectance scale
    qa = read_band(item.assets["qa_pixel"].href, tf, w, h, Resampling.nearest)[mask]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = {"ndvi": (nir - red) / (nir + red), "ndwi": (green - nir) / (green + nir),
               "ndti": (red - green) / (red + green)}
    bad = ((qa & QA_BAD) > 0) | (green <= 0) | (red <= 0) | (nir <= 0) | (qa == 1)  # qa 1 = fill
    for v in out.values():
        v[bad] = np.nan
    return out


def search(river, since: date, until: date, det):
    """Sentinel-2 images by day: from Planetary Computer, plus any day only Earth Search has (it
    publishes some images sooner), or all from Earth Search when Planetary Computer is down."""
    def find(url, collection, modifier=None):
        cat = pystac_client.Client.open(url, modifier=modifier)
        items = cat.search(collections=[collection], bbox=river["bbox"],
                           datetime=f"{since.isoformat()}/{until.isoformat()}",
                           query={"eo:cloud_cover": {"lt": det["max_scene_cloud"]}}).item_collection()
        by_day = defaultdict(list)
        for it in items:
            by_day[it.datetime.date().isoformat()].append(it)
        return by_day

    found, failed = {}, []
    for name, url, collection, modifier in (("Planetary Computer", STAC_URL, "sentinel-2-l2a", planetary_computer.sign_inplace),
                                            ("Earth Search", ES_URL, ES_COLLECTION, None)):
        try:
            for d, items in find(url, collection, modifier).items():
                if d not in found:
                    found[d] = items
                    if name == "Earth Search":
                        print(f"  {d}: from Earth Search (not on Planetary Computer)")
        except Exception as e:
            failed.append(name)
            print(f"  {name}: search failed ({type(e).__name__}), using the other catalogue")
    if len(failed) == 2:
        raise RuntimeError("both image catalogues are unavailable")
    return dict(sorted(found.items()))


def read_day(items, tf, w, h):
    green = np.full((h, w), np.nan, "float32")
    red, nir = green.copy(), green.copy()
    for it in items:  # mosaic tiles for the same day; first valid pixel wins
        g, r, n, v = read_scene(it, tf, w, h)
        fill = v & np.isnan(green)
        green[fill], red[fill], nir[fill] = g[fill], r[fill], n[fill]
    return green, red, nir


def day_pixels(river_id: str, d: str, items, tf, w, h, mask):
    """NDVI, NDTI and NDWI of every river pixel on one date (NaN where unusable).

    Cached locally (data/cache/, gitignored) as float16 so methods and thresholds can be
    changed and the history rebuilt without downloading the images again.
    """
    path = ROOT / f"data/cache/{river_id}/{d}.npz"
    n = int(mask.sum())
    if path.exists():
        z = np.load(path)
        if "ndvi" in z and z["ndvi"].shape == (n,):
            return {k: z[k].astype("float32") for k in ("ndvi", "ndti", "ndwi")}
    if not items:  # would otherwise cache an all-empty date and poison every later rebuild
        raise ValueError(f"{d} is not cached and no images were given")
    green, red, nir = (b[mask] for b in read_day(items, tf, w, h))
    with np.errstate(invalid="ignore", divide="ignore"):
        px = {"ndvi": (nir - red) / (nir + red),
              "ndti": (red - green) / (red + green),
              "ndwi": (green - nir) / (green + nir)}
    # Zero or negative reflectance (sensor saturation, nodata edges) gives meaningless indices.
    bad = ~((green > 0) & (red > 0) & (nir > 0))
    for a in px.values():
        a[bad | ~np.isfinite(a)] = np.nan
    px = {k: v.astype("float16") for k, v in px.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **px)
    # Use the stored precision now too, so a rebuild from cache gives identical numbers.
    return {k: v.astype("float32") for k, v in px.items()}


def day_quality(river_id: str, d: str, items, tf, w, h, mask):
    """Haze and cloud around the river on one date, as reported with the image itself.

    aot: mean aerosol optical thickness (haze; usually 0.1-0.6 here). cloud_share: share of the
    river corridor flagged as cloud, thin cirrus or cloud shadow; their edges leak into nearby
    pixels. Cached in data/cache/<river>/quality.json.
    """
    path = ROOT / f"data/cache/{river_id}/quality.json"
    q = json.loads(path.read_text()) if path.exists() else {}
    if d not in q:
        q[d] = read_quality(items, tf, w, h, mask)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(q, indent=0, sort_keys=True))
    return q[d]


def read_quality(items, tf, w, h, mask):
    names = S2_ASSETS[s2_source(items[0])]
    aot = read_band(items[0].assets[names["aot"]].href, tf, w, h, Resampling.nearest)[mask].astype("float32")
    scl = read_band(items[0].assets[names["scl"]].href, tf, w, h, Resampling.nearest)[mask]
    return {"aot": round(float(aot[aot > 0].mean()) / 1000, 3) if (aot > 0).any() else None,
            "cloud_share": round(float(np.isin(scl, [3, 8, 9, 10]).mean()), 3)}


def prefetch(river_id: str, todo: dict, tf, w, h, mask, workers=4):
    """Download many dates at once into the local cache (backfills); the main loop then reads it.

    Downloads are network-bound, so a few threads cut a multi-hour backfill to a fraction.
    """
    qpath = ROOT / f"data/cache/{river_id}/quality.json"
    q = json.loads(qpath.read_text()) if qpath.exists() else {}

    def one(d, items):
        for attempt in range(3):  # Planetary Computer reads fail now and then
            try:
                day_pixels(river_id, d, items, tf, w, h, mask)
                return d, q.get(d) or read_quality(items, tf, w, h, mask)
            except Exception as e:
                err = e
        print(f"  {d}: download failed ({type(err).__name__}); will retry in the main loop")
        return d, None

    with ThreadPoolExecutor(workers) as pool:
        for i, (d, qd) in enumerate(pool.map(lambda kv: one(*kv), todo.items()), 1):
            if qd:
                q[d] = qd
            if i % 10 == 0 or i == len(todo):
                print(f"  downloaded {i}/{len(todo)}", flush=True)
                qpath.parent.mkdir(parents=True, exist_ok=True)
                qpath.write_text(json.dumps(q, indent=0, sort_keys=True))
