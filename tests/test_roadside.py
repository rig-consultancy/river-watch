"""Tests for the roadside watch: road strip, image plumbing and tracking, on made-up inputs with
known answers. The detectors themselves aren't loaded here (they're scored separately on open data).

Run with: uv run pytest
"""

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, Polygon, box, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import fetch_roads  # noqa: E402
import roadside  # noqa: E402

TRK = {"garbage_match_m": 6, "vehicle_match_m": 2.5, "vehicle_max_size_change": 0.3, "long_parked_days": 3}
DATES = ["2026-10-01", "2026-10-05", "2026-10-09", "2026-10-13", "2026-10-17"]
ALL = lambda i, x, y: True  # noqa: E731


def test_strip_keeps_public_road_outside_the_society():
    # Society: 200 m square near Antheia. One road runs past its south edge, one loops inside it.
    lon0, lat0 = 73.808, 18.629
    d, e = 200 / 111_320, 200 / (111_320 * np.cos(np.radians(lat0)))  # 200 m north-south, east-west
    soc = Polygon([(lon0, lat0), (lon0 + e, lat0), (lon0 + e, lat0 + d), (lon0, lat0 + d)])
    outside = LineString([(lon0 - e / 2, lat0 - 10 / 111_320), (lon0 + 1.5 * e, lat0 - 10 / 111_320)])
    inside = LineString([(lon0 + e / 4, lat0 + d / 2), (lon0 + 3 * e / 4, lat0 + d / 2)])
    geo = fetch_roads.strip(soc, [outside, inside], {"strip_half_width_m": 15})
    kinds = {f["properties"]["kind"]: f for f in geo["features"]}
    assert 395 < kinds["roads"]["properties"]["length_m"] < 425  # only the outside road (~ 400 m)
    band = shape(kinds["strip"]["geometry"])
    assert not band.intersects(shape(kinds["boundary"]["geometry"]).buffer(-1e-6))
    assert band.contains(Point(lon0 + e / 2, lat0 - 20 / 111_320))  # verge across the road


def test_episodes_minimum_and_maximum_duration():
    # Seen on 5 and 9 Oct, not on 1 or 13 Oct: at least 4 days, at most 12.
    eps = roadside.episodes(DATES, [1, 2], [0, 1, 2, 3, 4])
    assert eps == [{"first": "2026-10-05", "last": "2026-10-09", "min_days": 4, "max_days": 12, "open": False}]
    # Still there on the latest image: open, no maximum.
    eps = roadside.episodes(DATES, [3, 4], [0, 1, 2, 3, 4])
    assert eps[0]["open"] and eps[0]["min_days"] == 4 and eps[0]["max_days"] is None
    # There on the first image: we don't know when it started, so no maximum either.
    assert roadside.episodes(DATES, [0], [0, 1, 2, 3, 4])[0]["max_days"] is None


def test_episodes_skip_images_that_did_not_cover_the_spot():
    # Image 2 didn't reach this spot (outside its footprint): not a sign it was cleared.
    eps = roadside.episodes(DATES, [1, 3], [0, 1, 3, 4])
    assert len(eps) == 1 and eps[0]["min_days"] == 8 and eps[0]["max_days"] == 16


def test_track_follows_a_dump_and_counts_it_coming_back():
    g = lambda x, y: {"x": x, "y": y, "score": 0.5, "area_m2": 4}  # noqa: E731
    dets = [[], [g(0, 0)], [g(2, 1)], [], [g(1, -1), g(50, 0)]]
    places = roadside.track(DATES, dets, ALL, TRK["garbage_match_m"])
    assert len(places) == 2
    p = next(p for p in places if abs(p["x"]) < 5)
    assert p["seen"] == [1, 2, 4]
    eps = roadside.episodes(DATES, p["seen"], p["looked"])
    assert len(eps) == 2 and not eps[0]["open"] and eps[1]["open"]


def test_track_tells_a_different_vehicle_in_the_same_spot():
    v = lambda x, length: {"x": x, "y": 0, "score": 0.9, "length": length}  # noqa: E731
    same = lambda a, b: abs(a["length"] - b["length"]) <= 0.3 * b["length"]  # noqa: E731
    # A car (4.5 m), then a truck (8 m) at the same spot: two vehicles, not one parked for days.
    places = roadside.track(DATES, [[v(0, 4.5)], [v(0.5, 8.0)], [], [], []], ALL, TRK["vehicle_match_m"], same)
    assert sorted(p["seen"] for p in places) == [[0], [1]]


def test_summarise_open_dumps_and_long_parked():
    g = lambda x: {"x": x, "y": 0, "score": 0.5, "area_m2": 10}  # noqa: E731
    v = lambda x: {"x": x, "y": 30, "score": 0.9, "length": 4.5}  # noqa: E731
    gd = [[g(0)], [g(0)], [g(0), g(100)], [g(100)], [g(100)]]  # dump A cleared after 2026-10-09, B open
    vd = [[v(0)], [v(0), v(20)], [v(0)], [v(0)], []]           # car at 0 parked 12+ days, car at 20 once
    garbage = roadside.track(DATES, gd, ALL, 6)
    vehicles = roadside.track(DATES, vd, ALL, 2.5)
    s = roadside.summarise(DATES, garbage, vehicles, road_m=500, trk=TRK)
    assert s["garbage"]["open_now"] == 1 and s["garbage"]["cleared"] == 1
    assert s["garbage"]["days_uncleared_median"] == 8.0  # B: 2026-10-09 to 2026-10-17
    assert s["garbage"]["days_to_clear_median"] == 8.0   # A: at least 2026-10-01 to 2026-10-09
    assert [c["min_days"] for c in s["vehicles"]["long_parked"]] == [12]
    assert s["vehicles"]["long_parked_now"] == 0  # gone on the latest image
    assert s["vehicles"]["per_date"][1] == {"date": "2026-10-05", "vehicles": 2, "dumps": 1, "kerb_used_pct": 0.9}


def test_detect_image_maps_pixels_and_keeps_only_the_strip(tmp_path):
    # 2000 x 400 px GeoTIFF at 0.5 m in UTM 43N; strip covers the lower half (y < 100 m from origin).
    x0, y0, px = 370_000.0, 2_060_200.0, 0.5
    path = tmp_path / "2026-10-01.tif"
    with rasterio.open(path, "w", driver="GTiff", width=2000, height=400, count=3, dtype="uint8",
                       crs=fetch_roads.UTM, transform=from_origin(x0, y0, px, px)) as dst:
        dst.write(np.full((3, 400, 2000), 120, "uint8"))
    strip_m = box(x0, y0 - 200, x0 + 1000, y0 - 100)

    class Fake:
        """Finds one object at a fixed spot on the whole image, in the pixels of whichever tile."""
        def __init__(self, at, **extra):
            self.at, self.extra, self.off = at, extra, None

        def __call__(self, rgb):
            col, row = self.off
            x, y = self.at[0] - col, self.at[1] - row
            return [{"x": x, "y": y, "score": 0.8, **self.extra}] if 0 <= x < rgb.shape[1] and 0 <= y < rgb.shape[0] else []

    real_tiles = roadside.tiles
    car = Fake((1000, 300), length=9, width=4)    # in the strip (150 m down), 4.5 m long
    pile = Fake((500, 100), w=6, h=6)             # 50 m down: outside the strip
    big = Fake((1500, 350), w=60, h=60)           # in the strip but 900 m²: a yard, not a dump

    def tiles(w, h, size, overlap):
        for win in real_tiles(w, h, size, overlap):
            for f in (car, pile, big):
                f.off = (win.col_off, win.row_off)
            yield win

    roadside.tiles = tiles
    try:
        det = {"tile_px": 1024, "tile_overlap_px": 128, "garbage_max_area_m2": 400}
        garbage = lambda rgb: pile(rgb) + big(rgb)  # noqa: E731
        out = roadside.detect_image(path, strip_m, det, car, garbage, TRK)
    finally:
        roadside.tiles = real_tiles
    assert out["pixel_m"] == 0.5
    # The car sits where two tiles overlap but is counted once, at the right place and size.
    assert len(out["vehicle"]) == 1
    v = out["vehicle"][0]
    assert (v["x"], v["y"], v["length"]) == (x0 + 500, y0 - 150, 4.5)
    assert out["garbage"] == []


def test_published_roadside_data_has_what_the_page_reads():
    index = json.loads((ROOT / "web/data/roadside-index.json").read_text())
    assert index and all({"id", "name", "name_mr"} <= set(s) for s in index)
    for site in index:
        d = json.loads((ROOT / f"web/data/roadside-{site['id']}.json").read_text())
        assert {"latest", "images", "road_m", "roads", "example", "long_parked_days", "garbage", "vehicles"} <= set(d)
        for s in d["garbage"]["spots"]:
            assert {"lat", "lon", "ward", "open", "area_m2"} <= set(s) and s["episodes"]
            assert 18.4 < s["lat"] < 18.8 and 73.6 < s["lon"] < 74.0
        for p in d["vehicles"]["long_parked"]:
            assert {"lat", "lon", "ward", "open", "first", "last", "min_days"} <= set(p)
        assert all({"date", "vehicles", "dumps", "kerb_used_pct"} <= set(r) for r in d["vehicles"]["per_date"])
