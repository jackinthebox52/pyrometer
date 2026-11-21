"""Phase 3 W4b: lock-free export, GET /runs paging, summary refresh after retention, sweeper updated_at,
resume of an archived run."""

import threading
import time

import duckdb
import pytest
from fastapi.testclient import TestClient

from pyrometer.server import auth, db
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


def new_run(sdk, name="r", **extra) -> str:
    r = sdk.post("/runs", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()["data"]["run_id"]


def pt(step, value=1.0, ts=None):
    return {"step": step, "timestamp": float(step) if ts is None else ts, "name": "loss", "value": value}


# --- export without the global lock -------------------------------------------

class _Cursor:
    """Wraps a DuckDB cursor; runs ``hook`` just before the metrics COPY (after the snapshot began)."""

    def __init__(self, cur, hook):
        self._cur, self._hook = cur, hook

    def execute(self, sql, *a):
        if sql.lstrip().startswith("COPY") and "metrics.parquet" in sql:
            self._hook()
        return self._cur.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Conn:
    def __init__(self, conn, hook):
        self._conn, self._hook = conn, hook

    def cursor(self):
        return _Cursor(self._conn.cursor(), self._hook)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_export_does_not_hold_the_db_lock(sdk, tmp_path, monkeypatch):
    rid = new_run(sdk)
    sdk.post(f"/runs/{rid}/metrics", json=[pt(1), pt(2)])
    server_app.drain_background()
    real = db.get_conn()
    during: list[int] = []

    def ingest_while_copying():
        done = threading.Event()

        def work():
            r = sdk.post(f"/runs/{rid}/metrics", json=[pt(100 + len(during), 5.0)])
            assert r.status_code == 201, r.text
            done.set()

        t = threading.Thread(target=work)
        t.start()
        # With the lock held by the exporter this would block forever; give it generous time.
        assert done.wait(15), "ingest blocked while the export was running"
        t.join()
        during.append(1)

    dest = tmp_path / "out"
    dest.mkdir()
    with monkeypatch.context() as m:
        m.setattr(db, "get_conn", lambda: _Conn(real, ingest_while_copying))
        counts = db.db_export_parquet(dest, run_id=rid)
    assert during, "the hook never ran"
    # One transaction = one snapshot: every table agrees on the pre-export state, ingestion landed after it.
    assert counts["metrics"] == 2
    assert duckdb.connect().execute(f"SELECT count(*) FROM '{dest / 'metrics.parquet'}'").fetchone()[0] == 2
    assert len(db.db_get_metrics(rid)) == 2 + len(during)


def test_large_export_with_concurrent_ingest(sdk, tmp_path):
    rid = new_run(sdk)
    server_app.drain_background()
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO metrics (run_id, step, timestamp, name, value) "
            "SELECT ?, i, i * 0.5, 'm' || (i % 50), 1.0 FROM range(1500000) t(i)", [rid]
        )
    dest = tmp_path / "big"
    dest.mkdir()
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(db.db_export_parquet(dest, run_id=rid)))
    t.start()
    ingested = 0
    while t.is_alive():
        assert sdk.post(f"/runs/{rid}/metrics", json=[pt(2_000_000 + ingested)]).status_code == 201
        ingested += 1
    t.join()
    assert result["metrics"] == 1500000
    assert duckdb.connect().execute(f"SELECT count(*) FROM '{dest / 'metrics.parquet'}'").fetchone()[0] == 1500000


# --- GET /runs paging ------------------------------------------------------------

def test_runs_paging_and_total(sdk):
    ids = [new_run(sdk, name=f"run{i}") for i in range(7)]
    full = sdk.get("/runs").json()
    assert "meta" not in full and len(full["data"]) == 7          # backward compatible
    for sort in ("updated", "created", "name"):
        everything = [r["run_id"] for r in sdk.get(f"/runs?sort={sort}").json()["data"]]
        paged: list[str] = []
        for off in range(0, 7, 3):
            body = sdk.get(f"/runs?sort={sort}&limit=3&offset={off}").json()
            assert body["meta"]["total"] == 7 and body["meta"]["limit"] == 3 and body["meta"]["offset"] == off
            assert body["meta"]["returned"] == len(body["data"])
            paged += [r["run_id"] for r in body["data"]]
        assert paged == everything and sorted(paged) == sorted(ids)
    beyond = sdk.get("/runs?limit=3&offset=50").json()
    assert beyond["data"] == [] and beyond["meta"]["total"] == 7
    assert len(sdk.get("/runs?offset=5").json()["data"]) == 2
    assert sdk.get("/runs?limit=0").status_code == 400
    assert sdk.get("/runs?offset=-1").status_code == 400


def test_runs_paging_ties_are_stable(sdk):
    ids = [new_run(sdk, name="same", run_id=f"tie-run-{i:04d}", created_at=100.0) for i in range(6)]
    with db._lock:
        db.get_conn().execute("UPDATE runs SET updated_at = 500.0")
    for sort in ("updated", "created", "name"):
        paged: list[str] = []
        for off in range(0, 6, 2):
            paged += [r["run_id"] for r in sdk.get(f"/runs?sort={sort}&limit=2&offset={off}").json()["data"]]
        assert sorted(paged) == sorted(ids) and len(set(paged)) == 6
        assert paged == sorted(paged)


# --- retention refreshes summaries -------------------------------------------------

def test_retention_recomputes_stored_summary(sdk):
    rid = new_run(sdk)
    now = time.time()
    sdk.post(f"/runs/{rid}/metrics", json=[pt(5, 9.0, ts=now - 90 * 86400), pt(2, 1.0, ts=now)])
    server_app.drain_background()
    db.db_compute_run_summary(rid)
    assert db.db_get_run_summary(rid)["total_steps"] == 5
    db.db_set_project_retention("default", {"older_than_days": {"metrics": 30}})
    assert db.db_apply_retention() == {"default": {"metrics": 1}}
    summary = db.db_get_run_summary(rid)
    assert summary["total_steps"] == 2


def test_retention_does_not_create_summaries_that_never_existed(sdk):
    rid = new_run(sdk)
    sdk.post(f"/runs/{rid}/metrics", json=[pt(1, ts=1.0)])
    server_app.drain_background()
    with db._lock:
        db.get_conn().execute("DELETE FROM run_summaries")
    db.db_set_project_retention("default", {"older_than_days": {"metrics": 1}})
    db.db_apply_retention()
    assert db.db_get_run_summary(rid) is None


# --- sweeper bumps updated_at ------------------------------------------------------

def test_crash_sweep_bumps_updated_at(sdk):
    rid = new_run(sdk)
    with db._lock:
        db.get_conn().execute("UPDATE runs SET last_heartbeat = 10.0, updated_at = 10.0 WHERE run_id = ?", [rid])
    now = time.time()
    assert db.db_mark_stale_runs(now=now) == [rid]
    row = db.get_conn().execute("SELECT status, updated_at FROM runs WHERE run_id = ?", [rid]).fetchone()
    assert row[0] == "crashed" and abs(row[1] - now) < 5


# --- resume unarchives ------------------------------------------------------------

def test_resume_unarchives_but_plain_ingest_does_not(sdk, monkeypatch):
    rid = new_run(sdk, run_id="arch-run-0001")
    assert db.db_set_run_archived(rid, True)
    assert sdk.post(f"/runs/{rid}/metrics", json=[pt(1)]).status_code == 201      # still accepted
    assert db.db_get_run(rid)["archived"] is True                                  # ...and still archived
    events: list[dict] = []

    async def record(msg, *a, **k):
        events.append(msg)

    monkeypatch.setattr(server_app._ws, "broadcast", record)
    r = sdk.post("/runs", json={"run_id": rid, "resume": "must"})
    assert r.status_code == 201 and r.json()["data"]["unarchived"] is True
    assert db.db_get_run(rid)["archived"] is False
    assert {"type": "run_updated", "run_id": rid} in events
    assert [x["run_id"] for x in sdk.get("/runs").json()["data"]] == [rid]
    again = sdk.post("/runs", json={"run_id": rid, "resume": "must"}).json()["data"]
    assert again["unarchived"] is False
