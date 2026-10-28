"""Tests of what the public site shows: translations, the published data, and that the data
is exactly what the code produces from the committed history.

  uv run pytest                      everything except the live site
  SITE_URL=https://… uv run pytest -k live   also checks the deployed site (CI does after deploy)
"""

import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CFG = yaml.safe_load((ROOT / "config/rivers.yaml").read_text())
RIVERS = [r["id"] for r in CFG["rivers"] if not r.get("draft")]

# Every value the pipeline can put in a field the page turns into text with t(`prefix_${value}`).
DYNAMIC_KEYS = {
    "cause_": ["removal", "rain", "river_wide", "unknown"],
    "conf_": ["high", "medium", "low"],
    "flood_": ["rooted", "floating", "mixed", "unclear"],
    "flood_why_": ["rooted", "floating", "mixed", "unclear"],
    "tt_flood_": ["rooted", "floating", "mixed", "unclear"],
    "rejected_": ["haze", "cloud"],
    "why_": ["small", "edges", "outline", "stale", "green_channel", "narrow", "no_clear_view"],
}


def strings():
    """The en and mr dictionaries of web/i18n.js, as {key: text}."""
    src = (WEB / "i18n.js").read_text()
    en_start, mr_start = src.index("\n  en: {"), src.index("\n  mr: {")
    out = {}
    for lang, block in (("en", src[en_start:mr_start]), ("mr", src[mr_start:])):
        out[lang] = {k: json.loads(f'"{v}"') for k, v in re.findall(r'^    (\w+): "((?:[^"\\]|\\.)*)",?$', block, re.M)}
    return out


def used_keys():
    keys = set()
    for js in ("app.js", "report.js", "roadside.js"):
        keys |= set(re.findall(r'\bt\("(\w+)"', (WEB / js).read_text()))
    for html in WEB.glob("*.html"):
        keys |= set(re.findall(r'data-i18n(?:-html)?="(\w+)"', html.read_text()))
    for prefix, values in DYNAMIC_KEYS.items():
        keys |= {prefix + v for v in values}
    return keys


def test_every_text_the_page_uses_exists_in_english_and_marathi():
    s = strings()
    missing = {lang: sorted(used_keys() - set(d)) for lang, d in s.items()}
    assert missing == {"en": [], "mr": []}


def test_marathi_has_every_english_string_with_the_same_placeholders():
    s = strings()
    assert set(s["en"]) == set(s["mr"]), set(s["en"]) ^ set(s["mr"])
    for k, en in s["en"].items():
        assert set(re.findall(r"\{(\w+)\}", en)) == set(re.findall(r"\{(\w+)\}", s["mr"][k])), k


def test_dynamic_text_keys_cover_every_value_the_pipeline_writes():
    """A new cause or verdict in the pipeline must get a translation before it ships."""
    for r in RIVERS:
        d = json.loads((WEB / f"data/{r}.json").read_text())
        feats = [f["properties"] for f in d["segments"]["features"]]
        seen = {
            "cause_": {e["cause"] for e in d["cleanup_events"]},
            "conf_": {p["confidence"] for p in feats if p["measurable"]},
            "flood_": {p["flood_test"]["verdict"] for p in feats if p.get("flood_test")},
            "rejected_": {x["reason"] for x in d["rejected_dates"]},
            "why_": {w for p in feats for w in p.get("confidence_why", [])} | {p["why"] for p in feats if not p["measurable"]},
        }
        for prefix, values in seen.items():
            assert values <= set(DYNAMIC_KEYS[prefix]), (r, prefix, values - set(DYNAMIC_KEYS[prefix]))


def test_index_lists_the_configured_rivers_in_config_order():
    index = json.loads((WEB / "data/index.json").read_text())
    assert [r["id"] for r in index["rivers"]] == RIVERS  # the first one is what the site opens on


def finite(x):
    return x is None or (isinstance(x, (int, float)) and math.isfinite(x))


@pytest.mark.parametrize("river", RIVERS)
def test_published_river_data_is_consistent(river):
    d = json.loads((WEB / f"data/{river}.json").read_text())
    series, dates = d["series"], [s["date"] for s in d["series"]]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    assert d["latest_date"] == dates[-1]
    assert not {r["date"] for r in d["rejected_dates"]} & set(dates), "a rejected image is still in the figures"
    for s in series:
        assert 0 <= s["hyacinth_pct"] <= 100 and finite(s["turbid_pct"]) and finite(s["open_water_pct"])
        assert s["range"][0] - 0.05 <= s["hyacinth_pct"] <= s["range"][1] + 0.05
        assert re.fullmatch(r"S2[ABC]_MSIL2A_\d{8}T\d{6}_\w+", s["scene"]), s["scene"]
        assert s["scene"][11:19] == s["date"].replace("-", ""), "photo link points at another day"

    sh = d["segment_history"]
    n = len(d["segments"]["features"])
    assert sh["dates"] == dates and all(len(row) == n for row in sh["hyacinth_pct"])
    assert all(v is None or 0 <= v <= 100 for row in sh["hyacinth_pct"] for v in row)
    assert all(0 <= i < len(dates) and 0 <= k < n for i, k in sh["one_off"])

    for f in d["segments"]["features"]:
        p = f["properties"]
        assert p["place"] or p["nearest"], f"stretch {p['id']} has no location"
        assert len(f["geometry"]["coordinates"]) >= 2
        if p["measurable"]:
            assert p["range"][0] <= p["hyacinth_pct"] <= p["range"][1] + 0.05
            assert p["date"] in dates and p["confidence"] in DYNAMIC_KEYS["conf_"]
            if p["outlook"]:
                assert 0 <= p["outlook"]["pct"] <= 100 and 0 <= p["outlook"]["p_alert"] <= 1
        else:
            assert p["why"] in DYNAMIC_KEYS["why_"]

    for r in d["regions"]:
        assert r["series"] and all(x["date"] in dates for x in r["series"])
        assert all(0 <= x["hyacinth_pct"] <= 100 and finite(x["turbid_pct"]) for x in r["series"])
    for e in d["cleanup_events"]:
        assert e["from"] < e["to"] and e["from"] in dates and e["to"] in dates and 0 <= e["segment"] < n
        assert e["after"] <= e["before"] / 2 + 0.05

    u = d["uncertainty"]
    assert u["river_noise_pts"] > 0 and u["one_off_jumps"] < 0.05 * u["readings"]
    look = d["outlook"]
    assert look["monsoon"] or (look["mae_river"] > 0 and look["backtests"] > 50)


@pytest.mark.parametrize("river", RIVERS)
def test_published_data_is_what_the_code_makes_from_the_committed_history(river, tmp_path, monkeypatch):
    """Regenerate the dashboard file from data/history with the current code; it must match the
    published one exactly (apart from the timestamp). Catches stale data and non-determinism."""
    if not (ROOT / f"data/cache/{river}/rain.json").exists():
        pytest.skip("rainfall cache not present (it is filled by the first pipeline run)")
    sys.path.insert(0, str(ROOT / "pipeline"))
    import numpy as np
    import rasterio
    import run
    from shapely import STRtree, points

    river_cfg = next(r for r in CFG["rivers"] if r["id"] == river)
    det = CFG["detection"]
    line, aoi, corridor = run.load_river(river_cfg, det)
    segments = run.make_segments(line, det["segment_length_m"])
    tf, w, h, mask = run.make_grid(corridor)
    river_px, green_ch, from_images = run.river_area(river, tf, w, h, mask, aoi)
    keep = river_px[mask]
    inner = river_px.copy()
    inner[1:] &= river_px[:-1]; inner[:-1] &= river_px[1:]; inner[:, 1:] &= river_px[:, :-1]; inner[:, :-1] &= river_px[:, 1:]
    edge = (river_px & ~inner)[mask]
    rows, cols = np.nonzero(mask)
    xs, ys = rasterio.transform.xy(tf, rows, cols)
    seg_idx = STRtree(segments).query_nearest(points(xs, ys), return_distance=False, all_matches=False)[1]
    k = len(segments)
    area = {"px": np.bincount(seg_idx[keep], minlength=k), "edge_px": np.bincount(seg_idx[keep & edge], minlength=k),
            "green_px": np.bincount(seg_idx[green_ch[mask] > 0], minlength=k),
            "flood_px": {n: np.bincount(seg_idx[green_ch[mask] == v], minlength=k)
                         for n, v in (("survived", 2), ("cleared", 3), ("unclear", 4))},
            "from_images": from_images}

    # Write into a scratch web/ (inputs still come from the real data/).
    (tmp_path / "web/data").mkdir(parents=True)
    (tmp_path / "data").symlink_to(ROOT / "data")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    history = json.loads((ROOT / f"data/history/{river}.json").read_text())
    run.write_web(river_cfg, CFG, segments, history, area)

    def comparable(o):
        return {**o, "updated": None, "segments": [f["properties"] for f in o["segments"]["features"]]}
    fresh = json.loads((tmp_path / f"web/data/{river}.json").read_text())
    published = json.loads((WEB / f"data/{river}.json").read_text())
    assert comparable(fresh) == comparable(published)


@pytest.mark.skipif(not os.environ.get("SITE_URL"), reason="set SITE_URL to check the deployed site")
def test_live_site_serves_this_commit():
    """After a deploy: every page, script and data file is served, and the data is this commit's."""
    import requests
    base = os.environ["SITE_URL"].rstrip("/") + "/"
    for page in ("", "report.html", "roadside.html", "app.js", "report.js", "roadside.js", "i18n.js", "style.css", "report.css", "roadside.css"):
        r = requests.get(base + page, timeout=30)
        assert r.status_code == 200, (page, r.status_code)
        if page.endswith(".js"):
            assert r.text == (WEB / page).read_text(), f"{page} on the site differs from this commit"
    index = requests.get(base + "data/index.json", timeout=30).json()
    assert [r["id"] for r in index["rivers"]] == RIVERS
    for river in RIVERS:
        live = requests.get(base + f"data/{river}.json", timeout=60).json()
        local = json.loads((WEB / f"data/{river}.json").read_text())
        assert live["latest_date"] == local["latest_date"] and live["series"] == local["series"], river
        days_old = (date.today() - date.fromisoformat(live["latest_date"])).days
        assert days_old <= 30 or date.today().month in (7, 8, 9), f"{river}: newest image is {days_old} days old"
