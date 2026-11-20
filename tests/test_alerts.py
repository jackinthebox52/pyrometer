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
    monkeypatch.setattr(alerts, "RETRY_BACKOFF_S", (0.0, 0.0, 0.0))
    alerts.flush()
    alerts.reset_state()
    yield


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
    db.db_alert_clear_state()  # skip the dedupe window to observe a second crossing
    db.db_alert_swap_violating(db.db_list_alert_rules()[0]["id"], "loss>5.0", rid, False)
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


# --- multi-trigger rules, persisted state, delivery retries ------------------------------------

def test_rule_with_several_triggers_fires_once_per_event(admin, sdk, posts):
    rule = add_rule(admin, triggers=[{"kind": "run_failed"}, {"kind": "event", "min_severity": "warning"}, {"kind": "anomaly"}])
    assert [t["kind"] for t in rule["triggers"]] == ["run_failed", "event", "anomaly"]
    rid = new_run(sdk)
    sdk.patch(f"/runs/{rid}", json={"status": "failed"})
    # a warning anomaly matches both the event and anomaly triggers: still one message for the rule
    sdk.post(f"/runs/{rid}/events", json={"timestamp": 1.0, "event_type": "anomaly_detected", "severity": "warning", "message": "NaN"})
    sdk.post(f"/runs/{rid}/events", json={"timestamp": 1.0, "event_type": "custom", "severity": "info", "message": "quiet"})
    alerts.flush()
    assert len(posts) == 2


def test_trigger_validation_and_update(admin):
    body = {"name": "r", "url": SECRET}
    post = lambda **kw: admin.post("/admin/alerts", json={**body, **kw}).status_code
    assert post(triggers=[]) == 400
    assert post() == 400  # neither triggers nor kind
    assert post(triggers=[{"kind": "nope"}]) == 400
    assert post(triggers=[{"kind": "run_failed"}, {"kind": "metric_threshold"}]) == 400
    rule = add_rule(admin, triggers=[{"kind": "run_failed"}])
    r = admin.patch(f"/admin/alerts/{rule['id']}", json={"triggers": [{"kind": "run_crashed"}, {"kind": "event"}]})
    assert r.status_code == 200
    assert r.json()["data"]["triggers"] == [{"kind": "run_crashed"}, {"kind": "event", "min_severity": "error"}]
    assert admin.patch(f"/admin/alerts/{rule['id']}", json={"triggers": []}).status_code == 400


def test_legacy_single_trigger_rows_are_migrated(admin):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO alert_rules (id, name, kind, url, format, min_severity, enabled, created_at, created_by) "
        "VALUES ('old1', 'legacy', 'event', ?, 'generic', 'warning', true, 1.0, 'x')", [SECRET]
    )
    db._init_schema(conn)  # what a restart onto an old database does
    rule = db.db_get_alert_rule("old1")
    assert rule["triggers"] == [{"kind": "event", "min_severity": "warning", "metric": None, "op": None, "threshold": None}]
    assert [r["id"] for r in admin.get("/admin/alerts").json()["data"]] == ["old1"]


def restart_db() -> None:
    """Close and reopen the database file, as a server restart would."""
    db._conn.close()
    db._conn = None


def test_dedupe_and_rate_limit_survive_restart(admin, sdk, posts, monkeypatch):
    monkeypatch.setenv("PYROMETER_ALERT_RATE", "2")
    add_rule(admin, kind="event", min_severity="info")
    rid = new_run(sdk)
    ev = lambda m: {"timestamp": 1.0, "event_type": "c", "severity": "info", "message": m}
    sdk.post(f"/runs/{rid}/events", json=ev("m0"))
    sdk.post(f"/runs/{rid}/events", json=ev("m1"))
    alerts.flush()
    assert len(posts) == 2
    restart_db()
    sdk.post(f"/runs/{rid}/events", json=ev("m0"))  # same alert as before the restart: deduped
    sdk.post(f"/runs/{rid}/events", json=ev("m2"))  # new alert: over the per-minute rate
    alerts.flush()
    assert len(posts) == 2


def test_threshold_state_survives_restart(admin, sdk, posts):
    add_rule(admin, kind="metric_threshold", metric="loss", op=">", threshold=5.0)
    rid = new_run(sdk)
    pt = lambda step, v: {"step": step, "timestamp": 1.0, "name": "loss", "value": v}
    sdk.post(f"/runs/{rid}/metrics", json=[pt(1, 6.0)])
    alerts.flush()
    assert len(posts) == 1
    restart_db()
    sdk.post(f"/runs/{rid}/metrics", json=[pt(2, 7.0)])  # still above the threshold: no new crossing
    alerts.flush()
    assert len(posts) == 1


def test_delivery_is_retried_then_succeeds_without_leaking_url(admin, sdk, monkeypatch, caplog):
    add_rule(admin)
    calls: list[int] = []

    def flaky(url, req):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError(f"cannot connect to {url}")
        return httpx.Response(200)

    monkeypatch.setattr(alerts, "_post", flaky)
    rid = new_run(sdk)
    with caplog.at_level(logging.DEBUG):
        sdk.patch(f"/runs/{rid}", json={"status": "failed"})
        alerts.flush()
    assert len(calls) == 3
    assert "SECRET" not in caplog.text


def test_permanent_failure_gives_up_and_allows_a_later_retry(admin, sdk, monkeypatch, caplog):
    add_rule(admin)
    calls: list[int] = []
    monkeypatch.setattr(alerts, "_post", lambda url, req: (calls.append(1), httpx.Response(500))[1])
    rid = new_run(sdk)
    with caplog.at_level(logging.WARNING):
        sdk.patch(f"/runs/{rid}", json={"status": "failed"})
        alerts.flush()
    assert len(calls) == 1 + len(alerts.RETRY_BACKOFF_S)  # bounded
    assert "SECRET" not in caplog.text and "failed for rule" in caplog.text
    # a client error is not retried
    calls.clear()
    monkeypatch.setattr(alerts, "_post", lambda url, req: (calls.append(1), httpx.Response(404))[1])
    rid2 = new_run(sdk, name="b")
    sdk.patch(f"/runs/{rid2}", json={"status": "failed"})
    alerts.flush()
    assert len(calls) == 1
