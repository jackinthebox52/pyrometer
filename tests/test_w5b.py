"""Phase 3 W5b: GET /runs filters (q, tags, created range), run facets, throttled run_activity event."""

import time

import pytest
from fastapi.testclient import TestClient

from pyrometer.server import auth
from pyrometer.server import app as server_app
from pyrometer.server.app import app


@pytest.fixture(autouse=True)
def fresh_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    for var in ("PYROMETER_AUTH", "PYROMETER_TRUSTED_PROXIES", "PYROMETER_BOOTSTRAP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    auth._cache = None
    auth._setup_token = None
    auth._failures.clear()


@pytest.fixture
def sdk() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {auth.create_key('global')[1]}"})


def mk(sdk, name, **extra) -> str:
    r = sdk.post("/runs", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()["data"]["run_id"]


def names(sdk, **params) -> set[str]:
    r = sdk.get("/runs", params=params)
    assert r.status_code == 200, r.text
    return {x["name"] for x in r.json()["data"]}


def test_q_matches_name_group_and_tag_substrings(sdk):
    mk(sdk, "Resnet-Baseline", tags=["vision"])
    mk(sdk, "gpt", tags=["Nightly-Eval"], group="sweepA")
    mk(sdk, "plain")
    assert names(sdk, q="resnet") == {"Resnet-Baseline"}
    assert names(sdk, q="nightly") == {"gpt"}
    assert names(sdk, q="SWEEP") == {"gpt"}
    assert names(sdk, q="") == {"Resnet-Baseline", "gpt", "plain"}


def test_q_is_literal_and_injection_safe(sdk):
    mk(sdk, "a_b")
    mk(sdk, "axb")
    mk(sdk, "100%")
    assert names(sdk, q="a_b") == {"a_b"}
    assert names(sdk, q="%") == {"100%"}
    assert names(sdk, q="'; DROP TABLE runs; --") == set()
    assert names(sdk, q="\\") == set()
    assert len(names(sdk)) == 3


def test_tags_all_of_exact(sdk):
    mk(sdk, "one", tags=["a", "b"])
    mk(sdk, "two", tags=["a"])
    mk(sdk, "three", tags=["ab"])
    assert names(sdk, tags="a") == {"one", "two"}
    assert names(sdk, tags="a,b") == {"one"}
    assert names(sdk, tags=" a , b ") == {"one"}
    assert names(sdk, tags="zzz") == set()


def test_created_range_and_paging_total(sdk):
    now = time.time()
    for i, age in enumerate((10 * 86400, 5 * 86400, 86400)):
        mk(sdk, f"r{i}", created_at=now - age)
    assert names(sdk, created_after=now - 6 * 86400) == {"r1", "r2"}
    assert names(sdk, created_before=now - 6 * 86400) == {"r0"}
    assert names(sdk, created_after=now - 6 * 86400, created_before=now - 2 * 86400) == {"r1"}
    iso = time.strftime("%Y-%m-%d", time.gmtime(now - 2 * 86400))
    assert names(sdk, created_after=iso) == {"r2"}
    r = sdk.get("/runs", params={"created_after": now - 6 * 86400, "limit": 1})
    assert r.json()["meta"]["total"] == 2
    assert sdk.get("/runs", params={"created_after": "yesterday-ish"}).status_code == 400


def test_filters_respect_visibility(sdk):
    sdk.post("/projects", json={"name": "priv", "visibility": "private"})
    key = auth.create_key("p", project="priv")[1]
    mk(sdk, "secret-run", project="priv", tags=["t"])
    other = auth.create_key("p2", project="default")[1]
    c = TestClient(app, headers={"Authorization": f"Bearer {other}"})
    assert names(c, q="secret") == set()
    assert c.get("/runs/facets").json()["data"]["tags"] == []
    del key


def test_facets(sdk):
    a = mk(sdk, "a", tags=["x", "y"], group="g1")
    mk(sdk, "b", tags=["x"], group="g1")
    mk(sdk, "c", tags=["x"], group="g2")
    mk(sdk, "d")
    sdk.patch(f"/runs/{a}", json={"status": "completed"})
    d = sdk.get("/runs/facets").json()["data"]
    g1 = next(g for g in d["groups"] if g["group"] == "g1")
    assert (g1["runs"], g1["running"], g1["statuses"], g1["project"]) == (2, 1, {"completed": 1, "running": 1}, "default")
    assert d["tags"][0] == {"tag": "x", "runs": 3}
    assert {t["tag"] for t in d["tags"]} == {"x", "y"}
    assert d["statuses"] == {"running": 3, "completed": 1}


def test_run_activity_event_is_throttled(sdk, monkeypatch):
    sent: list[dict] = []

    async def fake(self, payload, throttle_key=None, throttle_ms=500, **kw):
        if throttle_key and self._throttled(throttle_key, throttle_ms):
            return
        sent.append(payload)

    monkeypatch.setattr(server_app._WsManager, "broadcast", fake)
    rid = mk(sdk, "live")
    sent.clear()
    m = [{"step": 0, "timestamp": 1.0, "name": "loss", "value": 1.0}]
    for _ in range(3):
        assert sdk.post(f"/runs/{rid}/metrics", json=m).status_code == 201
    acts = [p for p in sent if p["type"] == "run_activity"]
    assert len(acts) == 1 and acts[0]["run_id"] == rid
    assert sdk.post(f"/runs/{rid}/events", json={"timestamp": 1.0, "message": "x", "event_type": "custom", "severity": "info", "metadata": {}}).status_code == 201
    assert sdk.post("/runs/nope12345/metrics", json=m).status_code == 404
    assert not [p for p in sent if p.get("run_id") == "nope12345"]
