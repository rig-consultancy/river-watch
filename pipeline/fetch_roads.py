"""Fetch the roads to watch around each roadside site from OpenStreetMap and save them as GeoJSON.

Also saves the building outlines around the roads (data/roadside/<site>-buildings.geojson), which
roadside_land.py uses to tell built-up pixels from open land. `--buildings` refreshes only those.

Runs once per site (or on demand); the result is committed to data/roadside/ so the scheduled
run never calls Overpass. Saves the society boundary, the road centrelines outside it, and the
strip either side of them where dumps and parked vehicles are looked for (all in WGS84).
"""

import json
import sys
from pathlib import Path

import yaml
from pyproj import Transformer
import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import unary_union

from fetch_rivers import overpass

ROOT = Path(__file__).resolve().parent.parent
UTM = "EPSG:32643"  # UTM 43N, metres, covers Pune
to_m = Transformer.from_crs("EPSG:4326", UTM, always_xy=True).transform
to_ll = Transformer.from_crs(UTM, "EPSG:4326", always_xy=True).transform


def transform(fn, geom):
    return shapely.transform(geom, lambda xy: np.column_stack(fn(xy[:, 0], xy[:, 1])), include_z=False)


def fetch(site: dict) -> dict:
    way = site["osm_boundary_way"]
    types = "|".join(site["road_types"])
    data = overpass(f"""[out:json][timeout:60];
way({way});out geom;
way({way})->.b;
way(around.b:{site['road_search_m']})[highway~"^({types})$"];
out tags geom;""")
    els = data["elements"]
    boundary = Polygon([(p["lon"], p["lat"]) for p in els[0]["geometry"]])
    roads = [LineString([(p["lon"], p["lat"]) for p in e["geometry"]]) for e in els[1:] if e["id"] != way]
    return strip(boundary, roads, site)


def strip(boundary, roads, site: dict) -> dict:
    b = transform(to_m, boundary)
    # Roads inside the society (its internal loop) are private; only the stretch outside is kept.
    outside = unary_union([transform(to_m, r) for r in roads]).difference(b.buffer(-2))
    band = outside.buffer(site["strip_half_width_m"], cap_style="flat").difference(b)
    feats = [("boundary", b), ("roads", outside), ("strip", band)]
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature",
                      "properties": {"kind": k, "length_m": round(g.length) if k == "roads" else None,
                                     "area_m2": round(g.area) if k != "roads" else None},
                      "geometry": mapping(transform(to_ll, g))} for k, g in feats],
    }


def buildings(geo: dict, within_m: float) -> dict:
    roads = shape(next(f for f in geo["features"] if f["properties"]["kind"] == "roads")["geometry"])
    w, s, e, n = transform(to_ll, transform(to_m, roads).buffer(within_m + 20).envelope).bounds
    data = overpass(f'[out:json][timeout:90];way["building"]({s},{w},{n},{e});out geom;')
    feats = []
    for el in data["elements"]:
        c = [(round(p["lon"], 6), round(p["lat"], 6)) for p in el.get("geometry", [])]
        if len(c) >= 4 and c[0] == c[-1]:
            feats.append({"type": "Feature", "properties": {}, "geometry": mapping(Polygon(c))})
    return {"type": "FeatureCollection", "features": feats}


def ward(geo: dict) -> str | None:
    """PCMC ward the watched roads mostly fall in (complaints go to that ward office)."""
    roads = shape(next(f for f in geo["features"] if f["properties"]["kind"] == "roads")["geometry"])
    wards = json.loads((ROOT / "data/regions/pcmc-wards.geojson").read_text())["features"]
    best = max(wards, key=lambda w: shape(w["geometry"]).buffer(0).intersection(roads).length)
    return best["properties"]["name"] if shape(best["geometry"]).buffer(0).intersects(roads) else None


def main():
    cfg = yaml.safe_load((ROOT / "config/roadside.yaml").read_text())
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_buildings = "--buildings" in sys.argv
    out = ROOT / "data/roadside"
    out.mkdir(parents=True, exist_ok=True)
    for site in cfg["sites"]:
        if only and site["id"] not in only:
            continue
        if only_buildings:
            geo = json.loads((out / f"{site['id']}.geojson").read_text())
        else:
            geo = fetch(site)
            geo["ward"] = ward(geo)
            (out / f"{site['id']}.geojson").write_text(json.dumps(geo))
        b = buildings(geo, cfg["land"]["corridor_m"])
        (out / f"{site['id']}-buildings.geojson").write_text(json.dumps(b))
        print(f"{site['id']}: {len(b['features'])} buildings")
        if only_buildings:
            continue
        p = {f["properties"]["kind"]: f["properties"] for f in geo["features"]}
        print(f"{site['id']}: {p['roads']['length_m']} m of road, strip {p['strip']['area_m2']} m², "
              f"ward {geo['ward']}")


if __name__ == "__main__":
    main()
