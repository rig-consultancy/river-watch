"""Roadside watch: garbage dumps and parked vehicles along the roads outside a housing society.

Scheduled, like run.py. For every new very-high-resolution image (30-50 cm GeoTIFF in
data/roadside/images/<site>/, named YYYY-MM-DD*.tif) it finds vehicles and garbage inside the
road strip built by fetch_roads.py, follows each one from date to date, and publishes how long
it stayed: data/roadside/<site>/ (detections per date) and web/data/roadside-<site>.json.

Run with: uv run --group detect python pipeline/roadside.py [site-id ...]
Until real images exist, `--example` publishes clearly marked made-up data (the page shows a banner)
so the dashboard can be seen working; it goes through the same tracking and analytics code.
"""

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import rasterio
import yaml
from pyproj import Transformer
from rasterio.windows import Window
from shapely.geometry import Point, box, shape
from shapely.prepared import prep

from fetch_roads import UTM, to_ll, to_m, transform

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- detectors

def tiles(w: int, h: int, size: int, overlap: int):
    step = size - overlap
    for row in range(0, max(h - overlap, 1), step):
        for col in range(0, max(w - overlap, 1), step):
            yield Window(col, row, min(size, w - col), min(size, h - row))


def to_rgb8(a: np.ndarray) -> np.ndarray:
    """First three bands as 8-bit RGB (H, W, 3); 16-bit or float images are stretched 2-98%."""
    a = a[:3].astype("float32")
    if a.max() > 255 or a.dtype.kind == "f" and a.max() <= 1.5:
        lo, hi = np.nanpercentile(a, [2, 98])
        a = (a - lo) / max(hi - lo, 1e-6) * 255
    return np.clip(np.nan_to_num(a), 0, 255).astype("uint8").transpose(1, 2, 0)


class Vehicles:
    """YOLO oriented boxes trained on DOTA: centre, length and width (pixels) of each vehicle."""

    def __init__(self, det):
        from ultralytics import YOLO
        from ultralytics.utils.downloads import attempt_download_asset
        weights = ROOT / "data/cache/roadside/models" / det["vehicle_model"]
        weights.parent.mkdir(parents=True, exist_ok=True)
        self.model = YOLO(attempt_download_asset(weights))
        self.classes = [k for k, v in self.model.names.items() if v in det["vehicle_classes"]]
        self.min_score = det["vehicle_min_score"]

    def __call__(self, rgb):
        r = self.model.predict(rgb[..., ::-1], classes=self.classes, conf=self.min_score, verbose=False)[0]
        out = []
        for (cx, cy, w, h, _), s in zip(r.obb.xywhr.tolist(), r.obb.conf.tolist()):
            out.append({"x": cx, "y": cy, "length": max(w, h), "width": min(w, h), "score": s})
        return out


class Garbage:
    """Open-vocabulary detector (OWLv2) asked for piles of garbage; boxes in pixels."""

    def __init__(self, det):
        import torch
        from transformers import Owlv2ForObjectDetection, Owlv2Processor
        self.torch = torch
        self.dev = "mps" if torch.backends.mps.is_available() else "cpu"
        self.proc = Owlv2Processor.from_pretrained(det["garbage_model"])
        self.model = Owlv2ForObjectDetection.from_pretrained(det["garbage_model"]).to(self.dev).eval()
        self.queries = det["garbage_queries"]
        self.min_score = det["garbage_min_score"]

    def __call__(self, rgb):
        h, w = rgb.shape[:2]
        inp = self.proc(text=[self.queries], images=rgb, return_tensors="pt").to(self.dev)
        with self.torch.no_grad():
            res = self.model(**inp)
        # OWLv2 pads the image to a square; boxes come back relative to the padded side.
        side = max(h, w)
        r = self.proc.post_process_grounded_object_detection(res, threshold=self.min_score,
                                                    target_sizes=[(side, side)])[0]
        out = []
        for (x0, y0, x1, y1), s, q in zip(r["boxes"].tolist(), r["scores"].tolist(), r["labels"].tolist()):
            x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
            if x1 > x0 and y1 > y0:
                out.append({"x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "w": x1 - x0, "h": y1 - y0,
                            "score": s, "query": self.queries[q]})
        return out


def dedupe(dets, radius_m):
    """Tiles overlap, so one object can be found twice: keep the best-scoring within radius_m."""
    keep = []
    for d in sorted(dets, key=lambda d: -d["score"]):
        if all(np.hypot(d["x"] - k["x"], d["y"] - k["y"]) > radius_m for k in keep):
            keep.append(d)
    return keep


def detect_image(path, strip_m, det, vehicles, garbage, trk):
    """Vehicles and garbage inside the strip on one image, in UTM metres, plus the image footprint."""
    with rasterio.open(path) as src:
        to_utm = Transformer.from_crs(src.crs, UTM, always_xy=True).transform
        px_m = abs(src.transform.a) * (1 if src.crs.is_projected else 111_320 * np.cos(np.radians(18.6)))
        footprint = transform(to_utm, box(*src.bounds))
        area = prep(strip_m)
        found = {"vehicle": [], "garbage": []}
        for win in tiles(src.width, src.height, det["tile_px"], det["tile_overlap_px"]):
            wt = src.window_transform(win)
            if not strip_m.intersects(transform(to_utm, box(*rasterio.windows.bounds(win, src.transform)))):
                continue
            rgb = to_rgb8(src.read(window=win))
            for kind, fn in (("vehicle", vehicles), ("garbage", garbage)):
                for d in fn(rgb):
                    x, y = to_utm(*(wt @ (d["x"], d["y"])))
                    if not area.contains(Point(x, y)):
                        continue
                    rec = {"x": round(x, 2), "y": round(y, 2), "score": round(d["score"], 3)}
                    if kind == "vehicle":
                        rec |= {"length": round(d["length"] * px_m, 2), "width": round(d["width"] * px_m, 2)}
                    else:
                        rec["area_m2"] = round(d["w"] * d["h"] * px_m * px_m, 1)
                        if rec["area_m2"] > det["garbage_max_area_m2"]:
                            continue
                    found[kind].append(rec)
    return {"pixel_m": round(px_m, 3), "footprint": footprint.wkt,
            "vehicle": dedupe(found["vehicle"], trk["vehicle_match_m"] / 2),
            "garbage": dedupe(found["garbage"], trk["garbage_match_m"] / 2)}


# ---------------------------------------------------------------- tracking (no detectors needed)

def track(dates, dets, covered, match_m, same=lambda a, b: True):
    """Follow objects across dates.

    dates: sorted ISO dates; dets[i]: detections on dates[i] ({x, y, ...} in metres);
    covered(i, x, y): whether image i saw that spot (inside its footprint).
    Returns places: {x, y, seen: [date index], looked: [date index]} - a place keeps its spot after
    it's gone, so garbage dumped again there counts as the same place coming back.
    """
    places = []
    for i, ds in enumerate(dates):
        pairs = sorted((np.hypot(d["x"] - p["x"], d["y"] - p["y"]), j, k)
                       for j, d in enumerate(dets[i]) for k, p in enumerate(places))
        used_d, used_p = set(), set()
        for dist, j, k in pairs:
            if dist <= match_m and j not in used_d and k not in used_p and same(dets[i][j], places[k]["last"]):
                used_d.add(j), used_p.add(k)
                p = places[k]
                p["seen"].append(i)
                p["last"] = dets[i][j]
        for j, d in enumerate(dets[i]):
            if j not in used_d:
                places.append({"x": d["x"], "y": d["y"], "seen": [i], "last": d})
    for p in places:
        p["looked"] = [i for i in range(len(dates)) if covered(i, p["x"], p["y"])]
    return places


def days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def episodes(dates, seen, looked):
    """Unbroken runs of sightings at one place, with how long each lasted.

    Between images we can't know, so each run has a minimum (first to last sighting) and a
    maximum (last image without it before, to first image without it after; open if none after).
    """
    seen, out, run = set(seen), [], []
    for n, i in enumerate(looked):
        if i in seen:
            run.append(n)
        if run and (i not in seen or n == len(looked) - 1):
            first, last = looked[run[0]], looked[run[-1]]
            before = looked[run[0] - 1] if run[0] > 0 else None
            after = looked[run[-1] + 1] if run[-1] + 1 < len(looked) else None
            out.append({
                "first": dates[first], "last": dates[last],
                "min_days": days(dates[first], dates[last]),
                "max_days": days(dates[before], dates[after]) if before is not None and after is not None else None,
                "open": after is None,
            })
            run = []
    return out


def summarise(dates, garbage, vehicles, road_m, trk):
    latest = dates[-1]
    spots = []
    for p in garbage:
        eps = episodes(dates, p["seen"], p["looked"])
        spots.append({"x": p["x"], "y": p["y"], "episodes": eps, "open": bool(eps and eps[-1]["open"]),
                      "area_m2": p["last"].get("area_m2")})
    open_now = [s for s in spots if s["open"]]
    uncleared = [s["episodes"][-1]["min_days"] for s in open_now]
    cleared = [e for s in spots for e in s["episodes"] if not e["open"]]

    cars = []
    for p in vehicles:
        for e in episodes(dates, p["seen"], p["looked"]):
            if e["min_days"] >= trk["long_parked_days"]:
                cars.append({"x": p["x"], "y": p["y"], "length": p["last"].get("length")} | e)
    per_date = []
    for i, d in enumerate(dates):
        here = [v for v in vehicles if i in v["seen"]]
        # Parked end to end, how much of the road's kerb length they take up (both sides count).
        per_date.append({"date": d, "vehicles": len(here), "dumps": sum(i in p["seen"] for p in garbage),
                         "kerb_used_pct": round(100 * sum(v["last"].get("length", 4.5) for v in here)
                                                / (2 * road_m), 1) if road_m else None})
    return {
        "latest": latest, "images": len(dates),
        "garbage": {
            "open_now": len(open_now), "places": len(spots),
            "open_area_m2": round(sum(s["area_m2"] or 0 for s in open_now)),
            "days_uncleared_median": float(np.median(uncleared)) if uncleared else None,
            "days_uncleared_max": max(uncleared) if uncleared else None,
            "came_back": sum(len(s["episodes"]) > 1 for s in spots),
            "cleared": len(cleared),
            "days_to_clear_median": float(np.median([e["min_days"] for e in cleared])) if cleared else None,
            "spots": spots,
        },
        "vehicles": {"per_date": per_date, "long_parked": cars,
                     "long_parked_now": sum(c["open"] for c in cars)},
    }


# ---------------------------------------------------------------- run

def run_site(site, cfg, detectors):
    det, trk = cfg["detection"], cfg["tracking"]
    geo = json.loads((ROOT / f"data/roadside/{site['id']}.geojson").read_text())
    kinds = {f["properties"]["kind"]: f for f in geo["features"]}
    strip_m = transform(to_m, shape(kinds["strip"]["geometry"]))

    out = ROOT / "data/roadside" / site["id"]
    out.mkdir(parents=True, exist_ok=True)
    images = sorted((ROOT / cfg["images_dir"] / site["id"]).glob("*.tif"))
    for img in images:
        cached = out / f"{img.name[:10]}.json"
        if not cached.exists():
            if detectors is None:
                detectors = Vehicles(det), Garbage(det)
            print(f"  {site['id']} {img.name}", flush=True)
            cached.write_text(json.dumps(detect_image(img, strip_m, det, *detectors, trk)))
    found = {p.stem: json.loads(p.read_text()) for p in sorted(out.glob("????-??-??.json"))}
    if not found:
        print(f"{site['id']}: no images yet in {cfg['images_dir']}/{site['id']}/")
        return detectors
    publish(site, trk, geo, found)
    return detectors


def publish(site, trk, geo, found, example=False):
    kinds = {f["properties"]["kind"]: f for f in geo["features"]}
    road_m = kinds["roads"]["properties"]["length_m"]
    dates = list(found)
    from shapely import wkt
    feet = [prep(wkt.loads(found[d]["footprint"])) for d in dates]
    covered = lambda i, x, y: feet[i].contains(Point(x, y))  # noqa: E731
    same_car = lambda a, b: abs(a["length"] - b["length"]) <= trk["vehicle_max_size_change"] * b["length"]  # noqa: E731
    garbage = track(dates, [found[d]["garbage"] for d in dates], covered, trk["garbage_match_m"])
    vehicles = track(dates, [found[d]["vehicle"] for d in dates], covered, trk["vehicle_match_m"], same_car)
    summary = summarise(dates, garbage, vehicles, road_m, trk)
    # Each spot gets its map position and PCMC ward: the roads around a society can cross wards,
    # and a complaint goes to the ward office responsible for that spot.
    wards = [(w["properties"]["name"], prep(shape(w["geometry"]).buffer(0)))
             for w in json.loads((ROOT / "data/regions/pcmc-wards.geojson").read_text())["features"]]
    for s in summary["garbage"]["spots"] + summary["vehicles"]["long_parked"]:
        lon, lat = to_ll(s["x"], s["y"])
        s |= {"lon": round(lon, 6), "lat": round(lat, 6),
              "ward": next((n for n, w in wards if w.contains(Point(lon, lat))), None)}
    roads = kinds["roads"]["geometry"]
    lines = roads["coordinates"] if roads["type"] == "MultiLineString" else [roads["coordinates"]]
    summary |= {"site": site["id"], "name": site["name"], "name_mr": site["name_mr"], "road_m": road_m,
                "example": example, "long_parked_days": trk["long_parked_days"], "roads": [[[round(x, 6), round(y, 6)] for x, y in ln] for ln in lines]}
    (ROOT / f"web/data/roadside-{site['id']}.json").write_text(json.dumps(summary))
    g = summary["garbage"]
    print(f"{site['id']}: {len(dates)} images{' (EXAMPLE DATA)' if example else ''}, {g['open_now']} dumps open now "
          f"(median {g['days_uncleared_median']} days), {summary['vehicles']['long_parked_now']} long-parked vehicles")


def example_found(geo, seed=7):
    """Made-up detections on the real road, for showing the dashboard before real images exist.

    Ten images about a week apart. Dumps with known stories (left for weeks, cleared, coming back)
    and vehicles that come and go, a few parked for days.
    """
    rng = np.random.default_rng(seed)
    kinds = {f["properties"]["kind"]: f for f in geo["features"]}
    road = transform(to_m, shape(kinds["roads"]["geometry"]))
    foot = transform(to_m, shape(kinds["strip"]["geometry"])).envelope.buffer(50).wkt
    dates = [str(date.fromordinal(date(2026, 8, 20).toordinal() + 7 * i)) for i in range(10)]

    def along(f, side):
        """A point at fraction f along the road, side * metres to its left or right."""
        a = road.interpolate(f, normalized=True)
        b = road.interpolate(min(f + 0.002, 1), normalized=True)
        dx, dy = b.x - a.x, b.y - a.y
        n = np.hypot(dx, dy) or 1
        return a.x - dy / n * side, a.y + dx / n * side

    dumps = [(0.08, [range(0, 10)]), (0.21, [range(2, 5), range(7, 10)]), (0.35, [range(1, 3)]),
             (0.47, [range(5, 10)]), (0.62, [range(8, 10)]), (0.74, [range(0, 4)]), (0.9, [range(6, 7)])]
    parked = [(0.15, range(0, 6)), (0.4, range(3, 10)), (0.55, range(6, 10)), (0.81, range(1, 3))]
    found = {d: {"pixel_m": 0.3, "footprint": foot, "vehicle": [], "garbage": []} for d in dates}
    for k, (f, runs) in enumerate(dumps):
        x, y = along(f, 9)
        for run in runs:
            for i in run:
                found[dates[i]]["garbage"].append({"x": round(x + rng.normal(0, 1), 2), "y": round(y + rng.normal(0, 1), 2),
                                                   "score": 0.15, "area_m2": round(4 + 3 * k + 2 * (i - run[0]), 1)})
    for f, run in parked:
        x, y = along(f, -6)
        for i in run:
            found[dates[i]]["vehicle"].append({"x": round(x, 2), "y": round(y, 2), "score": 0.8, "length": 4.6, "width": 1.9})
    for d in dates:
        for _ in range(rng.integers(18, 34)):
            x, y = along(rng.uniform(0, 1), rng.choice([-6, 6]))
            found[d]["vehicle"].append({"x": round(x, 2), "y": round(y, 2), "score": 0.7,
                                        "length": round(rng.uniform(3.8, 7), 1), "width": 1.8})
    return found


def main():
    cfg = yaml.safe_load((ROOT / "config/roadside.yaml").read_text())
    args = sys.argv[1:]
    example = "--example" in args
    only = [a for a in args if not a.startswith("--")]
    detectors = None
    for site in cfg["sites"]:
        if only and site["id"] not in only:
            continue
        if example:
            geo = json.loads((ROOT / f"data/roadside/{site['id']}.geojson").read_text())
            publish(site, cfg["tracking"], geo, example_found(geo), example=True)
        else:
            detectors = run_site(site, cfg, detectors)
    (ROOT / "web/data/roadside-index.json").write_text(json.dumps(
        [{"id": s["id"], "name": s["name"], "name_mr": s["name_mr"]} for s in cfg["sites"]]))


if __name__ == "__main__":
    main()
