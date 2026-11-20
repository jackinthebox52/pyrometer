"""Regression tests for the phase 1-2 review: batch claim races, unknown runs, SIGTERM locks, summaries."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from pyrometer import _run
from pyrometer._transport import BATCH_HEADER
from pyrometer.server import app as server_app
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
    return TestClient(app, headers={"Authorization": f"Bearer {auth.create_key('sdk')[1]}"})


def test_ingest_to_unknown_run_is_404_and_claims_nothing(server):
    pt = [{"step": 1, "timestamp": 1.0, "name": "loss", "value": 1.0}]
    assert server.post("/runs/no-such-run/metrics", json=pt, headers={BATCH_HEADER: "b"}).status_code == 404
    assert server.post("/runs/no-such-run/events", json={"timestamp": 1.0, "event_type": "x", "message": "m"}).status_code == 404
    assert db.get_conn().execute("SELECT COUNT(*) FROM seen_batches").fetchone()[0] == 0
    assert db.get_conn().execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0


def test_retry_during_inflight_insert_waits_and_failed_insert_is_retried(server):
    """A duplicate arriving mid-insert must not be told 'duplicate' if the first attempt then fails."""
    rid = server.post("/runs", json={}).json()["data"]["run_id"]

    class Req:
        headers = {BATCH_HEADER: "same"}

    started, release = threading.Event(), threading.Event()
    results: dict[str, object] = {}

    def first() -> None:
        def insert():
            started.set()
            release.wait(5)
            raise RuntimeError("insert failed")
        try:
            server_app._ingest_once(rid, Req, insert)
        except RuntimeError:
            results["first"] = "failed"

    def second() -> None:
        started.wait(5)
        results["second"] = server_app._ingest_once(rid, Req, lambda: 7)

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start(); t2.start()
    time.sleep(0.2)
    release.set()
    t1.join(5); t2.join(5)
    assert results == {"first": "failed", "second": 7}  # the retry inserted instead of being dropped as a dup


def test_concurrent_identical_creates_do_not_500(server, monkeypatch):
    body = {"run_id": "run-race-0001", "name": "a"}
    assert server.post("/runs", json=body).status_code == 201
    # Simulate losing the race: the pre-check says "free", the insert then hits the primary key.
    real = server_app.db_get_run_project
    calls = {"n": 0}

    def flaky(run_id):
        calls["n"] += 1
        return None if calls["n"] <= 1 else real(run_id)

    monkeypatch.setattr(server_app, "db_get_run_project", flaky)
    r = server.post("/runs", json=body)
    assert r.status_code == 200 or r.status_code == 201
    assert r.json()["data"]["run_id"] == "run-race-0001"


def test_in_progress_summary_is_not_served_stale(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    pt = lambda step: {"step": step, "timestamp": float(step), "name": "loss", "value": 1.0}
    server.post(f"/runs/{rid}/metrics", json=[pt(1)])
    assert server.get(f"/runs/{rid}/summary").json()["data"]["total_steps"] == 1
    server.post(f"/runs/{rid}/metrics", json=[pt(9)])
    assert server.get(f"/runs/{rid}/summary").json()["data"]["total_steps"] == 9


def test_terminal_patch_summary_is_drained_before_teardown(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    server.patch(f"/runs/{rid}", json={"status": "completed"})
    server_app.drain_background()
    assert db.db_get_run_summary(rid) is not None


def test_finish_from_signal_handler_does_not_deadlock_on_held_locks(tmp_path, monkeypatch):
    """SIGTERM runs finish() on the thread that may be inside log() holding the buffer lock."""
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path))
    run = _run.Run("run-signal-0001", "http://localhost:1", offline=True)
    done = threading.Event()

    def in_main_thread_like() -> None:
        with run._lock, run._log_lock, run._param_stats_lock:  # as if interrupted inside log()/print()
            run._finish("killed")
        done.set()

    t = threading.Thread(target=in_main_thread_like, daemon=True)
    t.start()
    assert done.wait(10), "finish() deadlocked on a lock held by its own thread"
