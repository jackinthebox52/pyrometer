"""Phase 3 W9a: run_backfill signal, summary significant digits."""

import asyncio

import pytest
from fastapi.testclient import TestClient

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
def sdk() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {auth.create_key('global')[1]}"})


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    got: list[dict] = []

    async def fake(self, payload, throttle_key=None, throttle_ms=500, **kw):
        if throttle_key and self._throttled(throttle_key, throttle_ms):
            return
        got.append(payload)

    monkeypatch.setattr(server_app._WsManager, "broadcast", fake)
    return got


def mk(sdk, name) -> str:
    return sdk.post("/runs", json={"name": name}).json()["data"]["run_id"]


def pt(step, ts, v=1.0):
    return {"step": step, "timestamp": ts, "name": "loss", "value": v}


def backfills(sent):
    return [p for p in sent if p["type"] == "run_backfill"]


def test_late_metrics_batch_signals_backfill(sdk, sent):
    rid = mk(sdk, "bf")
    assert sdk.post(f"/runs/{rid}/metrics", json=[pt(10, 1000.0)]).status_code == 201
    assert sdk.post(f"/runs/{rid}/metrics", json=[pt(11, 1001.0)]).status_code == 201  # live: in order
    assert backfills(sent) == []
    assert sdk.post(f"/runs/{rid}/metrics", json=[pt(1, 500.0)]).status_code == 201  # replay of old data
    assert backfills(sent) == [{"type": "run_backfill", "run_id": rid, "kind": "metrics"}]


def test_within_tail_margin_is_not_a_backfill(sdk, sent):
    rid = mk(sdk, "bf-margin")
    sdk.post(f"/runs/{rid}/metrics", json=[pt(10, 1000.0)])
    sdk.post(f"/runs/{rid}/metrics", json=[pt(9, 997.0)])  # 3 s older: inside the 5 s tail refetch margin
    assert backfills(sent) == []


def test_first_batch_of_a_run_and_duplicates_do_not_signal(sdk, sent):
    rid = mk(sdk, "bf-first")
    sdk.post(f"/runs/{rid}/metrics", json=[pt(5, 900.0), pt(1, 100.0)])  # unordered inside the first batch
    assert backfills(sent) == []
    hdr = {server_app.BATCH_HEADER: "b1"}
    sdk.post(f"/runs/{rid}/metrics", json=[pt(0, 50.0)], headers=hdr)
    n = len(backfills(sent))
    sdk.post(f"/runs/{rid}/metrics", json=[pt(0, 50.0)], headers=hdr)  # retried duplicate: nothing new
    assert len(backfills(sent)) == n


def test_backfill_state_is_seeded_from_the_database_and_covers_system_and_stats(sdk, sent):
    rid = mk(sdk, "bf-other")
    sdk.post(f"/runs/{rid}/system", json={"timestamp": 2000.0})
    server_app._ws._newest.clear()  # as after a server restart: newest comes from the stored rows
    sdk.post(f"/runs/{rid}/system", json={"timestamp": 1000.0})
    stat = {"step": 1, "timestamp": 100.0, "param_name": "w", "grad_norm": 1.0, "grad_min": 0.0, "grad_max": 1.0,
            "grad_mean": 0.5, "weight_norm": 1.0, "weight_min": 0.0, "weight_max": 1.0, "weight_mean": 0.1}
    sdk.post(f"/runs/{rid}/param-stats", json=[{**stat, "timestamp": 300.0}])
    sdk.post(f"/runs/{rid}/param-stats", json=[stat])
    assert [p["kind"] for p in backfills(sent)] == ["system", "param_stats"]


def test_backfill_burst_ends_with_a_trailing_event(monkeypatch):
    monkeypatch.setattr(server_app, "BACKFILL_THROTTLE_MS", 30)
    got: list[dict] = []

    async def fake(self, payload, **kw):
        got.append(payload)

    monkeypatch.setattr(server_app._WsManager, "broadcast", fake)
    ws = server_app._WsManager()

    async def go():
        for i in range(4):  # replay burst: only the first goes out at once
            await ws.note_ingest("r1", "metrics", [10.0 + i], 1000.0)
        assert len(got) == 1
        await asyncio.sleep(0.15)
        assert len(got) == 2  # one trailing event after the burst, not one per batch

    asyncio.run(go())


def test_summary_uses_significant_digits(sdk):
    rid = mk(sdk, "sig")
    sdk.post(f"/runs/{rid}/metrics", json=[
        {"step": 1, "timestamp": 1.0, "name": "loss", "value": 3.1234567891e-9},
        {"step": 2, "timestamp": 2.0, "name": "acc", "value": 0.123456789},
    ])
    server_app.drain_background()
    db.db_compute_run_summary(rid)
    s = db.db_get_run_summary(rid)
    assert s["final_metrics"]["loss"] == 3.12346e-9
    assert s["final_metrics"]["acc"] == 0.123457
    assert s["best_metrics"]["loss"]["value"] == 3.12346e-9
