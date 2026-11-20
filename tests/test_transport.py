"""Durable SDK transport: retries, spool, offline mode, sync, duplicate-safe ingestion."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pyrometer.server import auth
from pyrometer._sync import find_spools, sync_run_dir
from pyrometer._transport import BATCH_HEADER, QUEUE_FILE, Spool, Transport
from pyrometer.server.app import app

RUN_ID = "run-test-0001"


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


def make_transport(handler, tmp_path, **kw) -> Transport:
    client = httpx.Client(base_url="http://srv", transport=httpx.MockTransport(handler))
    return Transport(RUN_ID, client, Spool(RUN_ID, tmp_path), **kw)


def queue_lines(tmp_path) -> list[dict]:
    return Spool(RUN_ID, tmp_path).pending()[0]


def dead_lines(tmp_path) -> list[dict]:
    return Spool(RUN_ID, tmp_path).dead()[0]


# --- server: idempotent ingestion ------------------------------------------------

def test_duplicate_batch_is_ignored(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    batch = [{"step": 1, "timestamp": 1.0, "name": "loss", "value": 0.5}]
    h = {BATCH_HEADER: "abc123"}
    assert server.post(f"/runs/{rid}/metrics", json=batch, headers=h).json()["data"]["inserted"] == 1
    again = server.post(f"/runs/{rid}/metrics", json=batch, headers=h).json()["data"]
    assert again == {"inserted": 0, "duplicate": True}
    assert len(server.get(f"/runs/{rid}/metrics").json()["data"]) == 1
    # a different id, or no id, still inserts
    nxt = lambda step: [{"step": step, "timestamp": 1.0, "name": "loss", "value": 0.5}]  # noqa: E731
    server.post(f"/runs/{rid}/metrics", json=nxt(2), headers={BATCH_HEADER: "other"})
    server.post(f"/runs/{rid}/metrics", json=nxt(3))
    assert len(server.get(f"/runs/{rid}/metrics").json()["data"]) == 3


def test_batch_ids_are_per_run_and_cover_events(server):
    a = server.post("/runs", json={}).json()["data"]["run_id"]
    b = server.post("/runs", json={}).json()["data"]["run_id"]
    ev = {"timestamp": 1.0, "event_type": "custom", "message": "hi"}
    h = {BATCH_HEADER: "same"}
    assert server.post(f"/runs/{a}/events", json=ev, headers=h).json()["data"] == {"inserted": True}
    assert server.post(f"/runs/{a}/events", json=ev, headers=h).json()["data"]["duplicate"] is True
    assert server.post(f"/runs/{b}/events", json=ev, headers=h).json()["data"] == {"inserted": True}
    assert len(server.get(f"/runs/{a}/events").json()["data"]) == 1


def test_create_run_with_client_id_is_idempotent(server):
    body = {"run_id": RUN_ID, "created_at": 1_700_000_000.0, "name": "n"}
    first = server.post("/runs", json=body).json()["data"]
    second = server.post("/runs", json=body).json()["data"]
    assert first["run_id"] == second["run_id"] == RUN_ID
    assert len(server.get("/runs").json()["data"]) == 1
    assert server.post("/runs", json={"run_id": "../x"}).status_code == 422


# --- transport -------------------------------------------------------------------

def test_delivers_in_order_with_batch_ids(tmp_path):
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers[BATCH_HEADER]))
        return httpx.Response(201, json={"data": {}})

    t = make_transport(handler, tmp_path)
    for i in range(3):
        t.submit("POST", f"/runs/{RUN_ID}/metrics", [{"i": i}])
    assert t.close(5) == 0
    assert [p for p, _ in seen] == [f"/runs/{RUN_ID}/metrics"] * 3
    assert len({b for _, b in seen}) == 3
    assert queue_lines(tmp_path) == []


def test_retries_then_succeeds_without_losing_data(tmp_path, monkeypatch):
    import pyrometer._transport as tr

    monkeypatch.setattr(tr, "_BACKOFF_START", 0.01)
    calls = {"n": 0}
    bodies: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise httpx.ConnectError("down")
        bodies.append(json.loads(req.content))
        return httpx.Response(201, json={})

    t = make_transport(handler, tmp_path)
    t.submit("POST", "/x", [1, 2])
    deadline = time.time() + 5
    while not bodies and time.time() < deadline:
        time.sleep(0.01)
    assert bodies == [[1, 2]]
    assert t.close(5) == 0


def test_close_spills_undelivered(tmp_path):
    def handler(req):
        return httpx.Response(503)

    t = make_transport(handler, tmp_path, meta_defaults={"server": "http://srv", "created": True})
    t.submit("POST", "/a", [1])
    t.submit("POST", "/b", [2])
    assert t.close(0.5) == 2
    assert [q["path"] for q in queue_lines(tmp_path)] == ["/a", "/b"]
    assert Spool(RUN_ID, tmp_path).read_meta()["server"] == "http://srv"


def test_queue_cap_spills_overflow_and_never_blocks(tmp_path):
    def handler(req):
        raise httpx.ConnectError("down")

    t = make_transport(handler, tmp_path, max_queue=3)
    start = time.time()
    for i in range(20):
        t.submit("POST", "/m", [i])
    assert time.time() - start < 1.0
    assert t.overflowed >= 17
    t.close(0.2)
    assert len(queue_lines(tmp_path)) == 20  # nothing lost: spilled + drained remainder


def test_permanent_rejection_is_spilled_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(404)

    t = make_transport(handler, tmp_path)
    t.submit("POST", "/x", [1])
    t.close(5)
    assert calls["n"] == 1
    assert len(dead_lines(tmp_path)) == 1 and queue_lines(tmp_path) == []


def test_offline_transport_never_touches_network(tmp_path):
    t = Transport(RUN_ID, None, Spool(RUN_ID, tmp_path), offline=True)
    t.submit("POST", "/m", [1])
    t.close(5)
    assert len(queue_lines(tmp_path)) == 1
    assert t.offline_saved == 1


# --- offline run + sync end to end ----------------------------------------------

def test_offline_run_then_sync(tmp_path, monkeypatch, server):
    import pyrometer

    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path))
    monkeypatch.setenv("PYROMETER_MODE", "offline")
    monkeypatch.setenv("PYROMETER_SERVER", "http://unreachable.invalid")
    run = pyrometer.init(name="off", hyperparams={"lr": 1})
    run.log({"loss": 1.0}, step=1)
    run.log({"loss": 0.5}, step=2)
    run.event("hello")
    run.finish()

    dirs = find_spools(None)
    assert [d.name for d in dirs] == [run.run_id]
    assert Spool(run.run_id).read_meta()["created"] is False

    result = sync_run_dir(dirs[0], client=server)
    assert result.complete and result.error is None
    assert not dirs[0].exists()

    data = server.get(f"/runs/{run.run_id}").json()["data"]
    assert data["name"] == "off" and data["status"] == "completed"
    assert len(server.get(f"/runs/{run.run_id}/metrics").json()["data"]) == 2
    assert len(server.get(f"/runs/{run.run_id}/events").json()["data"]) == 1

    # replaying a (re-created) copy of the same spool is a no-op thanks to batch ids
    assert find_spools(run.run_id) == []


def test_sync_keeps_spool_when_server_down(tmp_path):
    spool = Spool(RUN_ID, tmp_path)
    spool.write_meta(server="http://srv", created=True)
    spool.append([{"id": "1", "method": "POST", "path": "/x", "json": []}])

    def handler(req):
        raise httpx.ConnectError("down")

    client = httpx.Client(base_url="http://srv", transport=httpx.MockTransport(handler))
    result = sync_run_dir(tmp_path / RUN_ID, client=client)
    assert not result.complete and result.remaining == 1
    assert len(queue_lines(tmp_path)) == 1


def test_sync_is_safe_to_repeat_after_partial_failure(tmp_path, server):
    server.post("/runs", json={"run_id": RUN_ID})
    batch = [{"step": 1, "timestamp": 1.0, "name": "loss", "value": 0.5}]
    spool = Spool(RUN_ID, tmp_path)
    spool.write_meta(server="http://srv", created=True)
    item = {"id": "dup1", "method": "POST", "path": f"/runs/{RUN_ID}/metrics", "json": batch}
    server.post(item["path"], json=batch, headers={BATCH_HEADER: "dup1"})  # delivered, response "lost"
    spool.append([item])
    assert sync_run_dir(tmp_path / RUN_ID, client=server).complete
    assert len(server.get(f"/runs/{RUN_ID}/metrics").json()["data"]) == 1
