"""Heartbeats, the crashed sweep, exit statuses and ephemeral transport items."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pyrometer._transport import QUEUE_FILE, Spool, Transport
from pyrometer.server import auth, db
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
def server() -> TestClient:
    key = auth.create_key("sdk")[1]
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def status(server, rid) -> str:
    return server.get(f"/runs/{rid}").json()["data"]["status"]


def test_silent_run_becomes_crashed_and_heartbeat_revives_it(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    assert status(server, rid) == "running"
    assert db.db_mark_stale_runs(now=time.time() + 60) == []  # inside the 5 minute window
    assert db.db_mark_stale_runs(now=time.time() + 400) == [rid]
    assert status(server, rid) == "crashed"
    assert server.post(f"/runs/{rid}/heartbeat").status_code == 200
    run = server.get(f"/runs/{rid}").json()["data"]
    assert run["status"] == "running" and run["finished_at"] is None


def test_lazy_check_on_read(server, monkeypatch):
    monkeypatch.setenv("PYROMETER_HEARTBEAT_TIMEOUT", "1")
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    time.sleep(1.2)
    assert status(server, rid) == "crashed"
    assert [r["status"] for r in server.get("/runs").json()["data"]] == ["crashed"]


def test_heartbeat_keeps_run_alive_and_never_touches_finished_runs(server, monkeypatch):
    monkeypatch.setenv("PYROMETER_HEARTBEAT_TIMEOUT", "1")
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    time.sleep(0.8)
    server.post(f"/runs/{rid}/heartbeat")
    time.sleep(0.5)
    assert status(server, rid) == "running"
    server.patch(f"/runs/{rid}", json={"status": "completed"})
    server.post(f"/runs/{rid}/heartbeat")
    assert status(server, rid) == "completed"
    assert db.db_mark_stale_runs(now=time.time() + 1000) == []


def test_legacy_runs_without_heartbeat_are_not_crashed(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    db.get_conn().execute("UPDATE runs SET last_heartbeat = NULL")
    assert db.db_mark_stale_runs(now=time.time() + 10_000) == []
    assert status(server, rid) == "running"


def test_replayed_status_patch_wins_over_crashed(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    db.db_mark_stale_runs(now=time.time() + 400)
    assert status(server, rid) == "crashed"
    server.patch(f"/runs/{rid}", json={"status": "completed"})  # what `pyrometer sync` replays
    assert status(server, rid) == "completed"
    assert server.patch(f"/runs/{rid}", json={"status": "failed"}).status_code == 200
    assert status(server, rid) == "failed"


def test_heartbeat_unknown_run_404(server):
    assert server.post("/runs/nope/heartbeat").status_code == 404


# --- transport: heartbeats are ephemeral ------------------------------------------

def test_ephemeral_items_are_dropped_not_spooled(tmp_path):
    def down(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(base_url="http://srv", transport=httpx.MockTransport(down))
    t = Transport("r1", client, Spool("r1", tmp_path))
    t.submit("POST", "/runs/r1/heartbeat", None, ephemeral=True)
    t.submit("POST", "/runs/r1/heartbeat", None, ephemeral=True)  # coalesced
    t.close(timeout=1)
    assert not (tmp_path / "r1" / QUEUE_FILE).exists()
    assert t.dead == 0 and t.overflowed == 0


def test_ephemeral_items_not_saved_offline(tmp_path):
    t = Transport("r2", None, Spool("r2", tmp_path), offline=True)
    t.submit("POST", "/runs/r2/heartbeat", None, ephemeral=True)
    t.close(timeout=1)
    assert not (tmp_path / "r2" / QUEUE_FILE).exists()


def test_ephemeral_item_is_delivered_once_when_up(tmp_path):
    seen = []

    def ok(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    client = httpx.Client(base_url="http://srv", transport=httpx.MockTransport(ok))
    t = Transport("r3", client, Spool("r3", tmp_path))
    t.submit("POST", "/runs/r3/heartbeat", None, ephemeral=True)
    t.close(timeout=2)
    assert seen == ["/runs/r3/heartbeat"]
