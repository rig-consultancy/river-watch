"""Fetch river geometry from OpenStreetMap and save it as GeoJSON.

River outlines rarely change, so this runs once per river (or on demand),
and the result is committed to data/rivers/. The scheduled pipeline never
calls Overpass, which keeps it working when the public servers are busy.
"""

import json
import sys
import time
from pathlib import Path

import requests
import yaml
from shapely.geometry import LineString, Polygon, mapping
from shapely.ops import linemerge, unary_union

ROOT = Path(__file__).resolve().parent.parent
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def overpass(query: str) -> dict:
    delay = 5
    for attempt in range(6):
        for url in MIRRORS:
            try:
                r = requests.post(url, data={"data": query}, timeout=90,
                                  headers={"User-Agent": "river-watch/0.1"})
                if r.ok and r.text.lstrip().startswith("{"):
                    return r.json()
                print(f"  {url}: HTTP {r.status_code}", file=sys.stderr)
            except requests.RequestException as e:
                print(f"  {url}: {type(e).__name__}", file=sys.stderr)
        print(f"  all mirrors failed, retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("Overpass unavailable")


def fetch(river: dict) -> dict:
    w, s, e, n = river["bbox"]
    bb = f"{s},{w},{n},{e}"
    names = river["osm_names"]
    query = f"""[out:json][timeout:80];
(
  way["waterway"="river"]["name"~"{names}",i]({bb});
  way["natural"="water"]["water"="river"]({bb});
  way["waterway"="riverbank"]({bb});
);
out tags geom;"""
    data = overpass(query)

    lines, polys = [], []
    for el in data["elements"]:
        coords = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        if len(coords) < 2:
            continue
        if el["tags"].get("waterway") == "river":
            lines.append(LineString(coords))
        elif len(coords) >= 4 and coords[0] == coords[-1]:
            polys.append(Polygon(coords))

    centreline = linemerge(unary_union(lines))
    # Keep only water polygons that touch the named river, not every pond.
    water = unary_union([p for p in polys if p.is_valid and p.distance(centreline) < 0.002])
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"kind": "centreline"}, "geometry": mapping(centreline)},
            {"type": "Feature", "properties": {"kind": "water"}, "geometry": mapping(water)},
        ],
    }


def fetch_places(river: dict) -> dict:
    """Neighbourhood and village names, used to label river stretches ("Near Bopodi")."""
    w, s, e, n = river["bbox"]
    data = overpass(f"""[out:json][timeout:60];
node["place"~"^(suburb|neighbourhood|quarter|village|town|hamlet)$"]["name"]({s},{w},{n},{e});
out;""")
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "properties": {"name": el["tags"].get("name:en") or el["tags"]["name"], "place": el["tags"]["place"]},
             "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]}}
            for el in data["elements"]
        ],
    }


def main():
    cfg = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
    out_dir = ROOT / "data/rivers"
    out_dir.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    for river in cfg["rivers"]:
        if only and river["id"] not in only:
            continue
        print(f"Fetching {river['name']}")
        gj = fetch(river)
        (out_dir / f"{river['id']}.geojson").write_text(json.dumps(gj))
        print(f"  saved data/rivers/{river['id']}.geojson")
        places = fetch_places(river)
        (out_dir / f"{river['id']}.places.geojson").write_text(json.dumps(places))
        print(f"  saved {len(places['features'])} place names")


if __name__ == "__main__":
    main()
