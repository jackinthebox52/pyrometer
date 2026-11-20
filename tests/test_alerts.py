"""Webhook alerts: rule admin, triggers, crossing semantics, dedupe, rate limit, secrecy."""

import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pyrometer.server import alerts, auth, db
from pyrometer.server.app import app

PW = "correct horse battery"
ORIGIN = {"Origin": "http://testserver"}
SECRET = "https://hooks.example.com/services/SECRET-TOKEN"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    for var in ("PYROMETER_AUTH", "PYROMETER_TRUSTED_PROXIES", "PYROMETER_BOOTSTRAP_TOKEN", "PYROMETER_PUBLIC_URL", "PYROMETER_ALERT_RATE"):
        monkeypatch.delenv(var, raising=False)
    auth._cache = None
    auth._setup_token = None
    auth._failures.clear()
    alerts.flush()
    alerts._sent.clear()
    alerts._recent.clear()
    alerts._violating.clear()
    yield
    alerts.flush()
    time.sleep(0.2)  # let run-summary threads started by terminal PATCHes finish before the DB closes


@pytest.fixture
def posts(monkeypatch) -> list[dict]:
    """Captures webhook calls instead of sending them."""
    calls: list[dict] = []

    def fake_post(url: str, req: dict) -> httpx.Response:
        calls.append({"url": url, **req})
        return httpx.Response(200)

    monkeypatch.setattr(alerts, "_post", fake_post)
    return calls


@pytest.fixture
def sdk() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {auth.create_key('sdk')[1]}"})


@pytest.fixture
def admin() -> TestClient:
    auth.create_user("admin", PW, "admin")
    c = TestClient(app, headers=ORIGIN)
    assert c.post("/auth/login", json={"username": "admin", "password": PW}).status_code == 200
    return c


def add_rule(admin, **kw) -> dict:
    body = {"name": "r", "kind": "run_failed", "url": SECRET, "format": "generic", **kw}
    r = admin.post("/admin/alerts", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def new_run(sdk, project=None, name="my-run") -> str:
    return sdk.post("/runs", json={"name": name, "project": project}).json()["data"]["run_id"]


def test_rule_admin_masks_url_and_is_admin_only(admin, sdk):
    rule = add_rule(admin)
    assert "SECRET" not in json.dumps(rule)
    listed = admin.get("/admin/alerts").json()["data"]
    assert len(listed) == 1 and "SECRET" not in json.dumps(listed)
    assert db.db_get_alert_rule(rule["id"])["url"] == SECRET  # stored intact
    assert sdk.get("/admin/alerts").status_code == 403
    assert sdk.post("/admin/alerts", json={"name": "x", "kind": "run_failed", "url": SECRET}).status_code == 403
    user_client = TestClient(app, headers=ORIGIN)
    auth.create_user("bob", PW, "user")
    user_client.post("/auth/login", json={"username": "bob", "password": PW})
    assert user_client.get("/admin/alerts").status_code == 403


def test_rule_validation_update_delete(admin):
    bad = lambda **kw: admin.post("/admin/alerts", json={"name": "r", "kind": "run_failed", "url": SECRET, **kw}).status_code
    assert bad(kind="nope") == 400
    assert bad(format="irc") == 400
    assert bad(url="ftp://x") == 400
    assert bad(project="ghost") == 404
    assert bad(kind="metric_threshold") == 400
    rule = add_rule(admin)
    r = admin.patch(f"/admin/alerts/{rule['id']}", json={"enabled": False, "name": "z"})
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False
    assert db.db_get_alert_rule(rule["id"])["url"] == SECRET
    assert admin.delete(f"/admin/alerts/{rule['id']}").status_code == 200
    assert admin.delete(f"/admin/alerts/{rule['id']}").status_code == 404


def test_failed_run_alerts_once_with_link(admin, sdk, posts, monkeypatch):
    monkeypatch.setenv("PYROMETER_PUBLIC_URL", "https://pyrometer.example.com/")
    add_rule(admin)
    rid = new_run(sdk)
    sdk.patch(f"/runs/{rid}", json={"status": "failed"})
    sdk.patch(f"/runs/{rid}", json={"status": "failed"})  # replay: not a transition
    alerts.flush()
    assert len(posts) == 1
    p = posts[0]
    assert p["url"] == SECRET
    assert p["json"]["link"] == f"https://pyrometer.example.com/#/runs/{rid}"
    assert "my-run" in p["json"]["message"] and "default" in p["json"]["message"]


def test_no_link_without_public_url_and_completed_does_not_alert(admin, sdk, posts):
    add_rule(admin)
    rid = new_run(sdk)
    sdk.patch(f"/runs/{rid}", json={"status": "completed"})
    rid2 = new_run(sdk, name="b")
    sdk.patch(f"/runs/{rid2}", json={"status": "failed"})
    alerts.flush()
    assert len(posts) == 1 and posts[0]["json"]["link"] is None


def test_crash_alerts_once_regardless_of_detecting_path(admin, sdk, posts):
    add_rule(admin, kind="run_crashed")
    rid = new_run(sdk)
    sdk.post(f"/runs/{rid}/heartbeat")
    later = time.time() + 400
    assert db.db_mark_stale_runs(now=later) == [rid]
    assert db.db_mark_stale_runs(now=later) == []  # already crashed
    db.db_list_runs()
    alerts.flush()
    assert len(posts) == 1


def test_project_scoping(admin, sdk, posts):
    sdk.post("/projects", json={"name": "a"})
    sdk.post("/projects", json={"name": "b"})
    add_rule(admin, project="a")
    ra, rb = new_run(sdk, "a"), new_run(sdk, "b")
    sdk.patch(f"/runs/{rb}", json={"status": "failed"})
    alerts.flush()
    assert posts == []
    sdk.patch(f"/runs/{ra}", json={"status": "failed"})
    alerts.flush()
    assert len(posts) == 1


def test_event_severity_and_anomaly_and_dedupe_batch(admin, sdk, posts):
    add_rule(admin, name="ev", kind="event", min_severity="warning")
    add_rule(admin, name="an", kind="anomaly")
    rid = new_run(sdk)
    ev = lambda **kw: {"timestamp": 1.0, "event_type": "custom", "severity": "info", "message": "m", **kw}
    sdk.post(f"/runs/{rid}/events", json=ev())  # below threshold
    alerts.flush()
    assert posts == []
    hdr = {"X-Pyrometer-Batch": "b1"}
    sdk.post(f"/runs/{rid}/events", json=ev(severity="error", message="boom"), headers=hdr)
    sdk.post(f"/runs/{rid}/events", json=ev(severity="error", message="boom"), headers=hdr)  # duplicate batch
    alerts.flush()
    assert len(posts) == 1
    sdk.post(f"/runs/{rid}/events", json=ev(event_type="anomaly_detected", severity="warning", message="NaN loss"))
    alerts.flush()
    assert len(posts) == 3  # event rule (warning >= warning) and anomaly rule both fire


def test_metric_threshold_fires_on_crossing_only(admin, sdk, posts):
    add_rule(admin, kind="metric_threshold", metric="loss", op=">", threshold=5.0)
    rid = new_run(sdk)
    pt = lambda step, v, name="loss": {"step": step, "timestamp": 1.0, "name": name, "value": v}
    sdk.post(f"/runs/{rid}/metrics", json=[pt(1, 1.0), pt(2, 6.0), pt(3, 7.0), pt(1, 99.0, "other")])
    alerts.flush()
    assert len(posts) == 1  # step 2 only
    sdk.post(f"/runs/{rid}/metrics", json=[pt(4, 8.0)])
    alerts.flush()
    assert len(posts) == 1  # still above
    sdk.post(f"/runs/{rid}/metrics", json=[pt(5, 1.0)])
    alerts._recent.clear()  # skip the dedupe window to observe a second crossing
    sdk.post(f"/runs/{rid}/metrics", json=[pt(6, 9.0)])
    alerts.flush()
    assert len(posts) == 2


def test_rate_limit_and_dedupe(admin, sdk, posts, monkeypatch):
    monkeypatch.setenv("PYROMETER_ALERT_RATE", "2")
    add_rule(admin, kind="event", min_severity="info")
    rid = new_run(sdk)
    for i in range(5):
        sdk.post(f"/runs/{rid}/events", json={"timestamp": 1.0, "event_type": "c", "severity": "info", "message": f"m{i}"})
    sdk.post(f"/runs/{rid}/events", json={"timestamp": 1.0, "event_type": "c", "severity": "info", "message": "m0"})
    alerts.flush()
    assert len(posts) == 2


def test_formats(admin, sdk, posts):
    for fmt in ("slack", "discord", "ntfy"):
        add_rule(admin, name=fmt, format=fmt)
    rid = new_run(sdk)
    sdk.patch(f"/runs/{rid}", json={"status": "failed"})
    alerts.flush()
    by = {i: p for i, p in enumerate(posts)}
    assert len(by) == 3
    assert any("text" in p.get("json", {}) for p in posts)
    assert any("content" in p.get("json", {}) for p in posts)
    assert any("Title" in p.get("headers", {}) for p in posts)


def test_test_endpoint_and_failures_never_leak_url(admin, monkeypatch, caplog):
    rule = add_rule(admin)

    def boom(url, req):
        raise httpx.ConnectError(f"cannot connect to {url}")

    monkeypatch.setattr(alerts, "_post", boom)
    r = admin.post(f"/admin/alerts/{rule['id']}/test")
    assert r.status_code == 502 and "SECRET" not in r.text
    monkeypatch.setattr(alerts, "_post", lambda url, req: httpx.Response(500))
    with caplog.at_level(logging.DEBUG):
        r = admin.post(f"/admin/alerts/{rule['id']}/test")
    assert r.status_code == 502 and "SECRET" not in r.text and "SECRET" not in caplog.text
    monkeypatch.setattr(alerts, "_post", lambda url, req: httpx.Response(204))
    assert admin.post(f"/admin/alerts/{rule['id']}/test").status_code == 200
