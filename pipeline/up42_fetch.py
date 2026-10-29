"""Order very high resolution images of the watched roads from UP42, with no one in the loop.

Each run (the daily workflow runs it before roadside.py):
  1. collects orders placed on earlier runs: once UP42 has delivered, the image goes into
     data/roadside/images/<site>/<acquisition date>-up42.tif, where roadside.py picks it up
  2. searches UP42's catalogue for a newer cloud-free scene over the roads, at least
     min_days_between after the last one bought, that UP42 delivers within 10 minutes (those have
     a 0.1 km² minimum charge; slower ones are charged as 5 km²)
  3. asks UP42 what it would cost and orders it only within the per-order and per-month caps
Orders are recorded in data/roadside/<site>/up42-orders.json (committed), so nothing is bought twice.

UP42 signs scripts in with the account's email and password: set the GitHub secrets UP42_USERNAME
and UP42_PASSWORD. Without them this does nothing. Credits are UP42's currency (100 = €1).

Run with: uv run python pipeline/up42_fetch.py [site-id ...]
"""

import io
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from shapely.geometry import mapping, shape

from fetch_roads import to_ll, to_m, transform

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.up42.com"
AUTH = "https://auth.up42.com/realms/public/protocol/openid-connect"
DONE, FAILED = {"FULFILLED"}, {"PLACEMENT_FAILED", "FAILED_PERMANENTLY", "CANCELED"}


class Up42:
    """Minimal UP42 API client. `http` is requests (or a stand-in in tests)."""

    def __init__(self, username, password, http=requests):
        self.user, self.password, self.http = username, password, http
        self.token, self.token_at = None, 0.0

    def headers(self):
        if not self.token or time.time() - self.token_at > 240:  # tokens last 5 minutes
            r = self.http.post(f"{AUTH}/token", data={"grant_type": "password", "client_id": "up42-api",
                                                       "username": self.user, "password": self.password}, timeout=60)
            check(r, "sign-in (check UP42_USERNAME / UP42_PASSWORD)")
            self.token, self.token_at = r.json()["access_token"], time.time()
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, url, **kw):
        r = self.http.get(url if url.startswith("http") else API + url, headers=self.headers(), timeout=120, **kw)
        return check(r, url).json()

    def post(self, url, body=None, **kw):
        r = self.http.post(url if url.startswith("http") else API + url, json=body, headers=self.headers(),
                           timeout=120, **kw)
        return check(r, url).json()

    def workspace_id(self):
        return self.get(f"{AUTH}/userinfo")["sub"]


def check(r, what):
    if not r.ok:
        raise RuntimeError(f"UP42 {what}: HTTP {r.status_code} {r.text[:500]}")
    return r


# ---------------------------------------------------------------- what to order

def order_area(geo):
    """One polygon around every watched road (lon/lat): the strip's convex hull, so a single image covers it."""
    strip = transform(to_m, shape(next(f for f in geo["features"] if f["properties"]["kind"] == "strip")["geometry"]))
    return transform(to_ll, strip.convex_hull.buffer(5))


def pick_scene(features, area, up, after):
    """Newest scene that covers the whole area, is clear enough, is delivered within minutes, after `after`."""
    ok = []
    for f in features:
        p = f["properties"]
        when = (p.get("datetime") or p.get("acquisitionDate") or "")[:10]
        if (p.get("deliveryTime") == up["delivery_time"] and when > after
                and (p.get("cloudCoverage") if p.get("cloudCoverage") is not None else 100) <= up["max_cloud"]
                and shape(f["geometry"]).buffer(1e-6).contains(area)):
            ok.append((when, f))
    return max(ok, key=lambda x: x[0], default=(None, None))[1]


def spent_this_month(state, today):
    return sum(o["credits"] for o in state["orders"] if o["placed"][:7] == today.isoformat()[:7]
               and o["status"] not in FAILED)


# ---------------------------------------------------------------- steps

def collect(client, state, out_dir):
    """Download any order UP42 has delivered since the last run."""
    for o in state["orders"]:
        if o["status"] in FAILED or o.get("file"):
            continue
        o["status"] = client.get(f"/v2/orders/{o['id']}")["status"]
        if o["status"] in FAILED:
            print(f"  order {o['id']} {o['status']}: no credits are charged for failed orders")
        if o["status"] not in DONE:
            continue
        items = client.post("/v2/assets/stac/search", {
            "filter": {"op": "=", "args": [{"property": "up42-order:id"}, o["id"]]}, "limit": 100})["features"]
        assets = [a for it in items for a in it["assets"].values() if a["href"].startswith(API)]
        # Prefer a GeoTIFF; Pléiades deliveries can also come as one ZIP with the GeoTIFF inside.
        assets.sort(key=lambda a: 0 if "tiff" in (a.get("type") or "") else 1 if "zip" in (a.get("type") or "") else 2)
        if not assets:
            print(f"  order {o['id']} fulfilled but no files yet")
            continue
        url = client.post(assets[0]["href"].rstrip("/") + "/download-url")["url"]
        data = client.http.get(url, timeout=600)
        check(data, "download")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{o['acquired']}-up42.tif"
        path.write_bytes(geotiff(data.content))
        o["file"] = path.name
        print(f"  downloaded {path.name} ({len(data.content) / 1e6:.1f} MB)")


def geotiff(content):
    """The image itself: the file as delivered, or the largest GeoTIFF inside a ZIP delivery."""
    if not zipfile.is_zipfile(io.BytesIO(content)):
        return content
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        tifs = [i for i in z.infolist() if i.filename.lower().endswith((".tif", ".tiff"))]
        if not tifs:
            raise RuntimeError(f"no GeoTIFF in the delivery: {[i.filename for i in z.infolist()][:10]}")
        return z.read(max(tifs, key=lambda i: i.file_size))


def buy(client, state, geo, up, today, workspace):
    """Order the newest suitable scene, if any, within the caps. Returns the order record or None."""
    area = order_area(geo)
    col = client.get(f"/v2/collections/{up['collection']}")
    host = next(p["name"] for p in col["providers"] if "HOST" in p["roles"])
    product = next(d["id"] for d in col["dataProducts"] if d["name"] == up["data_product"])
    bought = [o["acquired"] for o in state["orders"] if o["status"] not in FAILED]
    after = (date.fromisoformat(max(bought)) + timedelta(days=up["min_days_between"] - 1)).isoformat() if bought \
        else (today - timedelta(days=up["look_back_days"])).isoformat()
    w, s, e, n = area.bounds
    feats = client.post(f"/catalog/hosts/{host}/stac/search", {
        "collections": [col["name"]], "bbox": [w, s, e, n], "limit": 500,
        "datetime": f"{after}T00:00:00Z/{today.isoformat()}T23:59:59Z",
        "query": {"cloudCoverage": {"LTE": up["max_cloud"]}}})["features"]
    scene = pick_scene(feats, area, up, after)
    if scene is None:
        print(f"  no new scene: {len(feats)} found since {after}, none clear, covering the roads and delivered "
              f"within minutes")
        return None
    sid, when = scene["properties"]["id"], (scene["properties"].get("datetime") or "")[:10]
    body = {"displayName": f"Roadside watch {when}", "dataProduct": product, "params": {"id": sid},
            "featureCollection": {"type": "FeatureCollection",
                                  "features": [{"type": "Feature", "properties": {}, "geometry": mapping(area)}]},
            "tags": ["roadside-watch"]}
    est = client.post("/v2/orders/estimate", body)
    if est["errors"]:
        print(f"  scene {when} can't be ordered: {est['errors']}")
        return None
    credits = est["summary"]["totalCredits"]
    month = spent_this_month(state, today)
    if credits > up["max_credits_per_order"] or month + credits > up["max_credits_per_month"]:
        print(f"  scene {when} would cost {credits:.0f} credits; caps are {up['max_credits_per_order']} per order "
              f"and {up['max_credits_per_month']} per month ({month:.0f} used): not ordered")
        return None
    res = client.post(f"/v2/orders?workspaceId={workspace}", body)
    if res["errors"] or not res["results"]:
        # A data product's licence (EULA) must be accepted once in the UP42 console by the account owner.
        raise RuntimeError(f"UP42 refused the order for {when}: {res['errors']}")
    rec = {"id": res["results"][0]["id"], "scene": sid, "acquired": when, "credits": credits,
           "placed": today.isoformat(), "status": "CREATED"}
    state["orders"].append(rec)
    print(f"  ordered the {when} scene for {credits:.0f} credits (order {rec['id']})")
    return rec


def run_site(site, cfg, client, workspace, today, wait_s=900):
    up = cfg["up42"]
    geo = json.loads((ROOT / f"data/roadside/{site['id']}.geojson").read_text())
    path = ROOT / "data/roadside" / site["id"] / "up42-orders.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(path.read_text()) if path.exists() else {"orders": []}
    out = ROOT / cfg["images_dir"] / site["id"]
    try:
        collect(client, state, out)
        new = buy(client, state, geo, up, today, workspace)
        # Fast-delivery scenes arrive within minutes: wait for this one so roadside.py can use it today.
        deadline = time.time() + wait_s
        while new and not new.get("file") and new["status"] not in FAILED and time.time() < deadline:
            time.sleep(30)
            collect(client, state, out)
    finally:
        path.write_text(json.dumps(state, indent=1))


def main():
    cfg = yaml.safe_load((ROOT / "config/roadside.yaml").read_text())
    user, password = os.environ.get("UP42_USERNAME"), os.environ.get("UP42_PASSWORD")
    if not cfg.get("up42", {}).get("enabled") or not user or not password:
        print("UP42: not set up (needs up42.enabled in config/roadside.yaml and the UP42_USERNAME / "
              "UP42_PASSWORD secrets); skipping")
        return
    client = Up42(user, password)
    workspace = client.workspace_id()
    today = datetime.now(timezone.utc).date()
    only = sys.argv[1:]
    for site in cfg["sites"]:
        if not only or site["id"] in only:
            print(f"UP42 {site['id']}:")
            run_site(site, cfg, client, workspace, today)


if __name__ == "__main__":
    main()
