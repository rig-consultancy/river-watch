"""Tests for the UP42 downloader against a stand-in for UP42's API (no account, no credits spent).

Run with: uv run pytest
"""

import io
import json
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
import yaml
from shapely.geometry import box, mapping

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import up42_fetch  # noqa: E402

SITE = "antheia-pimpri"
CFG = yaml.safe_load((ROOT / "config/roadside.yaml").read_text())
UP = CFG["up42"]
TODAY = date(2026, 10, 29)
GEO = json.loads((ROOT / f"data/roadside/{SITE}.geojson").read_text())
AREA = up42_fetch.order_area(GEO)
BIG = mapping(AREA.buffer(0.05))          # a scene footprint covering all the roads
SMALL = mapping(box(*AREA.bounds[:2], AREA.bounds[0] + 0.001, AREA.bounds[1] + 0.001))  # misses most of them


def scene(sid, when, delivery="MINUTES", cloud=2, footprint=BIG):
    return {"type": "Feature", "geometry": footprint,
            "properties": {"id": sid, "datetime": f"{when}T05:30:00Z", "deliveryTime": delivery, "cloudCoverage": cloud}}


class Resp:
    def __init__(self, body=None, content=b"", status=200):
        self.body, self.content, self.status_code = body, content, status
        self.ok, self.text = status < 400, json.dumps(body)

    def json(self):
        return self.body


def tif_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("IMG_PNEO/thumb.tif", b"small")
        z.writestr("IMG_PNEO/IMG_PNEO3_RGB.TIF", b"II*\x00" + b"x" * 100)  # the image: largest .tif
        z.writestr("meta.xml", b"<x/>")
    return buf.getvalue()


class FakeUp42:
    """Just enough of UP42's API: sign-in, collections, catalogue, estimate, orders, assets."""

    def __init__(self, scenes, credits=880, status="FULFILLED"):
        self.scenes, self.credits, self.status = scenes, credits, status
        self.orders, self.calls = [], []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/token"):
            return Resp({"access_token": "t"})
        if "/stac/search" in url and "/catalog/" in url:
            assert json["collections"] == ["pneo"] and json["query"]["cloudCoverage"]["LTE"] == UP["max_cloud"]
            lo, hi = json["datetime"].split("/")
            return Resp({"features": [s for s in self.scenes if lo[:10] <= s["properties"]["datetime"][:10] <= hi[:10]]})
        if url.endswith("/v2/orders/estimate"):
            return Resp({"summary": {"totalCredits": self.credits}, "results": [], "errors": []})
        if "/v2/orders?workspaceId=ws-1" in url:
            self.orders.append(json)
            return Resp({"results": [{"index": 0, "id": f"order-{len(self.orders)}"}], "errors": []})
        if url.endswith("/v2/assets/stac/search"):
            oid = json["filter"]["args"][1]
            return Resp({"features": [{"assets": {
                "thumb": {"href": f"{up42_fetch.API}/v2/assets/thumb-{oid}", "type": "image/jpeg"},
                "data": {"href": f"{up42_fetch.API}/v2/assets/data-{oid}", "type": "application/zip"}}}]})
        if url.endswith("/download-url"):
            assert "/data-" in url, "should download the image, not the thumbnail"
            return Resp({"url": "https://signed.example/file.zip"})
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None))
        if url.endswith("/userinfo"):
            return Resp({"sub": "ws-1"})
        if url.endswith("/v2/collections/pneo"):
            return Resp({"name": "pneo", "providers": [{"name": "producer-x", "roles": ["PRODUCER"]},
                                                       {"name": "oneatlas", "roles": ["HOST"]}],
                         "dataProducts": [{"id": "dp-analytic", "name": "pneo-analytic"},
                                          {"id": "dp-display", "name": "pneo-display"}]})
        if "/v2/orders/order-" in url:
            return Resp({"status": self.status})
        if url == "https://signed.example/file.zip":
            return Resp(content=tif_zip())
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "data/roadside").mkdir(parents=True)
    shutil.copy(ROOT / f"data/roadside/{SITE}.geojson", tmp_path / "data/roadside")
    monkeypatch.setattr(up42_fetch, "ROOT", tmp_path)
    monkeypatch.setattr(up42_fetch.time, "sleep", lambda s: None)
    return tmp_path


def run(root, fake, wait_s=60):
    client = up42_fetch.Up42("me@example.com", "pw", http=fake)
    up42_fetch.run_site({"id": SITE}, CFG, client, client.workspace_id(), TODAY, wait_s=wait_s)
    return json.loads((root / f"data/roadside/{SITE}/up42-orders.json").read_text())


def test_orders_the_newest_clear_fast_scene_covering_the_roads_and_downloads_it(root):
    fake = FakeUp42([
        scene("old", "2026-09-01"),
        scene("best", "2026-10-20"),
        scene("slow", "2026-10-25", delivery="DAYS"),      # newer, but charged as 5 km²
        scene("partial", "2026-10-26", footprint=SMALL),  # newer, but doesn't cover the roads
        scene("cloudy", "2026-10-27", cloud=40),
    ])
    state = run(root, fake)
    (order,) = fake.orders
    assert order["params"] == {"id": "best"} and order["dataProduct"] == "dp-display"
    assert order["featureCollection"]["features"][0]["geometry"]["type"] == "Polygon"
    (rec,) = state["orders"]
    assert rec | {"id": None} == {"id": None, "scene": "best", "acquired": "2026-10-20", "credits": 880,
                                  "placed": "2026-10-29", "status": "FULFILLED", "file": "2026-10-20-up42.tif"}
    # The image from inside the ZIP, named so roadside.py dates it.
    img = root / f"data/roadside/images/{SITE}/2026-10-20-up42.tif"
    assert img.read_bytes().startswith(b"II*\x00") and len(img.read_bytes()) == 104


def test_nothing_is_bought_twice_and_the_next_waits_a_fortnight(root):
    fake = FakeUp42([scene("a", "2026-10-20")])
    run(root, fake)
    fake.scenes.append(scene("b", "2026-10-28"))  # only 8 days later: too soon
    run(root, fake)
    assert len(fake.orders) == 1
    fake.scenes.append(scene("c", "2026-11-03"))
    run(root, fake)  # TODAY is still 29 Oct, so 3 Nov isn't searched yet either
    assert len(fake.orders) == 1


def test_budget_caps_stop_the_order(root):
    run(root, FakeUp42([scene("a", "2026-10-01")], credits=900))
    # Same month: 900 already used, another 1,200 would pass the 2,000 monthly cap.
    fake = FakeUp42([scene("b", "2026-10-20")], credits=1200)
    state = run(root, fake)
    assert fake.orders == [] and len(state["orders"]) == 1
    # One order above the per-order cap is never placed either.
    fake = FakeUp42([scene("x", "2026-10-25")], credits=UP["max_credits_per_order"] + 1)
    assert fake.orders == [] and len(run(root, fake)["orders"]) == 1


def test_an_order_still_being_delivered_is_collected_on_a_later_run(root):
    fake = FakeUp42([scene("a", "2026-10-20")], status="BEING_FULFILLED")
    state = run(root, fake, wait_s=0)  # no time to wait: the run ends with the order just placed
    assert state["orders"][0]["status"] == "CREATED" and "file" not in state["orders"][0]
    run(root, fake)  # next run: UP42 is still delivering
    assert json.loads((root / f"data/roadside/{SITE}/up42-orders.json").read_text())["orders"][0]["status"] == "BEING_FULFILLED"
    fake.status = "FULFILLED"
    state = run(root, fake)
    assert state["orders"][0]["file"] == "2026-10-20-up42.tif" and len(fake.orders) == 1


def test_without_credentials_nothing_happens(monkeypatch, capsys):
    monkeypatch.delenv("UP42_USERNAME", raising=False)
    monkeypatch.delenv("UP42_PASSWORD", raising=False)
    monkeypatch.setattr(up42_fetch, "Up42", lambda *a: pytest.fail("must not contact UP42"))
    up42_fetch.main()
    assert "skipping" in capsys.readouterr().out
