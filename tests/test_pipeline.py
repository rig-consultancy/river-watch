"""Tests for the pipeline's calculations, on small made-up inputs with known answers.

Run with: uv run pytest
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import run  # noqa: E402

DET = {"hyacinth_ndvi": 0.3, "water_ndwi": 0.0, "turbid_ndti": 0.05, "min_valid_fraction": 0.6,
       "range_ndvi_delta": 0.05}
QUALITY = {"max_aot": 0.8, "cloud_share": 0.1, "max_z": 3, "spike_pts": 15}


def pixels(ndvi, ndwi, ndti):
    return {"ndvi": np.array(ndvi, "float32"), "ndwi": np.array(ndwi, "float32"), "ndti": np.array(ndti, "float32")}


def test_classify_separates_weed_water_and_unusable():
    px = pixels([0.6, -0.2, 0.1, np.nan], [-0.4, 0.3, -0.1, 0.2], [-0.1, 0.1, 0.0, 0.0])
    ok, hy, water = run.classify(px, DET)
    assert ok.tolist() == [True, True, True, False]
    assert hy.tolist() == [True, False, False, False]
    # Bare/mud pixel (NDWI below 0, not green) is neither weed nor open water.
    assert water.tolist() == [False, True, False, False]


def test_process_day_counts_per_segment_and_ranges():
    # Segment 0: 4 pixels, 2 clearly weed, 1 borderline (NDVI 0.27), 1 water. Segment 1: 4 water.
    px = pixels([0.6, 0.5, 0.27, -0.2] + [-0.3] * 4,
                [-0.4, -0.4, -0.2, 0.3] + [0.3] * 4,
                [0.0, 0.0, 0.0, 0.1] + [-0.1] * 4)
    keep = np.ones(8, bool)
    seg_idx = np.array([0] * 4 + [1] * 4)
    # Small segments: lower the "too cloudy" floor of 5 pixels by giving each segment 6 pixels.
    px = {k: np.concatenate([v, v[:2], v[4:6]]) for k, v in px.items()}
    keep = np.ones(12, bool)
    seg_idx = np.array([0] * 4 + [1] * 4 + [0, 0, 1, 1])
    res = run.process_day(px, keep, seg_idx, 2, DET)
    hy0, _, strict0, loose0 = res["segments"][0]
    assert hy0 == pytest.approx(4 / 6, abs=1e-3)       # 0.6, 0.5 and their copies
    assert strict0 == pytest.approx(4 / 6, abs=1e-3)   # NDVI > 0.35: same
    assert loose0 == pytest.approx(5 / 6, abs=1e-3)    # NDVI > 0.25 adds the borderline pixel
    assert res["segments"][1][0] == 0
    assert res["hyacinth_pct"] == pytest.approx(4 / 12 * 100, abs=0.01)
    assert res["hyacinth_pct_strict"] <= res["hyacinth_pct"] <= res["hyacinth_pct_loose"]


def test_process_day_skips_mostly_cloudy_dates():
    px = pixels([np.nan] * 8 + [0.1, 0.1], [0.2] * 10, [0.0] * 10)
    assert run.process_day(px, np.ones(10, bool), np.zeros(10, int), 1, DET) is None


def history(values, aot=None, cloud=None):
    dates = [f"2026-01-{d:02d}" for d in range(1, 3 * len(values) + 1, 3)]
    h = {"dates": {}}
    for i, (d, v) in enumerate(zip(dates, values)):
        h["dates"][d] = {"hyacinth_pct": v, "open_water_pct": 80 - v, "aot": (aot or {}).get(i, 0.3),
                         "cloud_share": (cloud or {}).get(i, 0.0)}
    return h, dates


def test_reject_dates_haze_alone_and_cloud_only_with_mismatch():
    values = [10, 11, 10, 12, 40, 11, 10, 12, 11]
    h, dates = history(values, aot={2: 1.1}, cloud={4: 0.2, 6: 0.3})
    rejected = run.reject_dates(h, dates, QUALITY)
    assert rejected[dates[2]] == "haze"          # heavy haze, even though the value looks normal
    assert rejected[dates[4]] == "cloud"         # cloud and far out of line with its neighbours
    assert dates[6] not in rejected              # cloud but in line with neighbours: kept


def test_one_off_jumps_and_unconfirmed_latest():
    dates = [f"2026-02-{d:02d}" for d in (1, 5, 9, 13, 17, 21)]
    hy = np.array([[2.0, 10.0], [3.0, 11.0], [30.0, 12.0], [4.0, 11.0], [3.0, 10.0], [5.0, 40.0]])
    spike, unconfirmed = run.one_off_jumps(dates, hy, QUALITY)
    assert spike[:, 0].tolist() == [False, False, True, False, False, False]
    assert not spike[:, 1].any()
    assert unconfirmed.tolist() == [False, True]  # stretch 1 jumped in the latest image only


def test_repeat_noise_matches_known_spread():
    rng = np.random.default_rng(1)
    dates = [f"2026-03-{d:02d}" for d in range(1, 29, 2)]  # every 2 days: all pairs count
    values = [np.array([20 + rng.normal(0, 2)]) for _ in dates]
    sigma, pairs = run.repeat_noise(dates, values)
    assert pairs == len(dates) - 1
    assert 1.0 < sigma < 3.5


def test_flood_verdict_thresholds():
    assert run.flood_verdict({"survived": 80, "cleared": 5, "unclear": 15})["verdict"] == "rooted"
    assert run.flood_verdict({"survived": 10, "cleared": 60, "unclear": 30})["verdict"] == "floating"
    assert run.flood_verdict({"survived": 10, "cleared": 10, "unclear": 80})["verdict"] == "unclear"
    assert run.flood_verdict({"survived": 40, "cleared": 30, "unclear": 30})["verdict"] == "mixed"
    assert run.flood_verdict({"survived": 5, "cleared": 5, "unclear": 5}) is None  # too small


def test_outlook_gives_no_number_when_monsoon_is_due():
    dates = [f"2026-05-{d:02d}" for d in range(1, 31, 3)] + ["2026-06-02"]
    hy = np.full((len(dates), 2), 20.0)
    look, segs = run.outlook(dates, hy, [20.0] * len(dates), {"horizon_days": 14, "alert_pct": 30})
    assert look["monsoon"] is True and "river" not in look
    assert segs == [None, None]


@pytest.mark.parametrize("path", sorted((ROOT / "web/data").glob("*.json")), ids=lambda p: p.name)
def test_published_dashboard_data_is_complete(path):
    """The files the dashboard reads have the fields it relies on."""
    data = json.loads(path.read_text())
    if path.name == "index.json":
        assert data["rivers"] and all({"id", "name", "latest_date"} <= set(r) for r in data["rivers"])
        return
    for key in ("river", "latest_date", "series", "segments", "segment_history", "regions", "uncertainty"):
        assert key in data, key
    assert all(s["range"][0] <= s["hyacinth_pct"] + 0.05 and s["hyacinth_pct"] <= s["range"][1] + 0.05
               for s in data["series"])
    sh = data["segment_history"]
    assert len(sh["dates"]) == len(sh["hyacinth_pct"]) == len(data["series"])
    for f in data["segments"]["features"]:
        p = f["properties"]
        if p["measurable"]:
            assert p["confidence"] in ("high", "medium", "low")
            assert p["range"][0] <= p["hyacinth_pct"] <= p["range"][1] + 0.05


CLEANUP = {"min_before": 20, "drop_pts": 15, "rain_mm": 25, "river_wide_share": 0.3}


def test_cleanup_events_label_cause_and_regrowth():
    dates = [f"2026-02-{d:02d}" for d in (1, 6, 11, 16, 21, 26)]
    hy = np.full((len(dates), 10), 30.0)  # 10 stretches steady at 30%: plenty with weed, none dropping
    hy[:, 0] = [40, 5, 4, 6, 25, 30]      # lasting drop, back to half its level after 15 days
    hy[:, 1] = [40, 5, 38, 39, 40, 41]    # dips and bounces straight back: not lasting
    dry = {"2026-02-03": 0.0}
    ev = run.cleanup_events(dates, hy, {}, dry, CLEANUP)
    assert [(e["segment"], e["from"], e["to"], e["cause"]) for e in ev] == [(0, "2026-02-01", "2026-02-06", "removal")]
    assert ev[0]["confirmed"] and ev[0]["regrowth_days"] == 15
    assert run.cleanup_events(dates, hy, {}, {"2026-02-03": 40.0}, CLEANUP)[0]["cause"] == "rain"
    hy[:, 2:6] = np.array([40, 5, 5, 5, 5, 5])[:, None]  # 5 of 10 drop together: river-wide
    assert {e["cause"] for e in run.cleanup_events(dates, hy, {}, dry, CLEANUP)} == {"river_wide"}


@pytest.mark.parametrize("script", sorted((ROOT / "web").glob("*.js")), ids=lambda p: p.name)
def test_dashboard_scripts_parse(script):
    """A stray quote in a translation once broke the whole page; catch that before deploy."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    r = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
