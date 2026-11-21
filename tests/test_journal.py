"""Transport gaps: degraded init, auto-replay, write-ahead journal, media blobs, poison payloads."""

import base64
import json
import os
import subprocess
import sys
import textwrap
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import pyrometer
import pyrometer._transport as tr
from pyrometer._sync import find_spools, sync_run_dir
from pyrometer._transport import BATCH_HEADER, BLOB_DIR, QUEUE_FILE, Spool, Transport
from pyrometer.server import auth
from pyrometer.server.app import app

RUN_ID = "run-journal-0001"


@pytest.fixture(autouse=True)
def fresh_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    for var in ("PYROMETER_AUTH", "PYROMETER_TRUSTED_PROXIES", "PYROMETER_BOOTSTRAP_TOKEN", "PYROMETER_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    auth._cache = None
    auth._setup_token = None
    auth._failures.clear()
    monkeypatch.setattr(tr, "_BACKOFF_START", 0.01)


@pytest.fixture
def server() -> TestClient:
    key = auth.create_key("sdk")[1]
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make(handler, tmp_path, **kw) -> Transport:
    client = httpx.Client(base_url="http://srv", transport=httpx.MockTransport(handler))
    return Transport(RUN_ID, client, Spool(RUN_ID, tmp_path), **kw)


# --- 1. init with the server down ------------------------------------------------

def test_init_with_server_down_degrades_then_creates_run_and_drains(server, capsys):
    run = pyrometer.init(name="late", hyperparams={"lr": 1}, server="http://127.0.0.1:1")
    assert "not available" in capsys.readouterr().err
    run.log({"loss": 1.0}, step=1)
    run.event("hello")
    meta = Spool(run.run_id).read_meta()
    assert meta["created"] is False and meta["create"]["name"] == "late"
    assert "api_key" not in json.dumps(meta)

    run._transport._client = server  # the server "comes back"
    with run._transport._cv:
        run._transport._cv.notify_all()
    assert wait_for(lambda: server.get(f"/runs/{run.run_id}").status_code == 200)
    run.log({"loss": 0.5}, step=2)
    run.finish()
    data = server.get(f"/runs/{run.run_id}").json()["data"]
    assert data["name"] == "late" and data["status"] == "completed"
    assert len(server.get(f"/runs/{run.run_id}/metrics").json()["data"]) == 2
    assert len(server.get(f"/runs/{run.run_id}/events").json()["data"]) == 1
    assert find_spools(run.run_id) == []  # fully delivered: spool removed


def test_init_degrades_on_5xx_but_raises_on_auth_and_validation(monkeypatch):
    def fake(status):
        def post(url, **kw):
            return httpx.Response(status, json={"detail": "x"}, request=httpx.Request("POST", url))
        return post

    monkeypatch.setattr(httpx, "post", fake(503))
    run = pyrometer.init(name="a")  # no raise
    run._transport._halted = True
    run.finish()
    for status, exc in ((401, PermissionError), (403, PermissionError), (422, httpx.HTTPStatusError)):
        monkeypatch.setattr(httpx, "post", fake(status))
        with pytest.raises(exc):
            pyrometer.init(name="b")


def test_degraded_resume_creates_with_resume_semantics(server):
    server.post("/runs", json={"run_id": RUN_ID, "name": "orig", "created_at": 1.0})
    server.patch(f"/runs/{RUN_ID}", json={"status": "failed"})
    run = pyrometer.init(resume=RUN_ID, server="http://127.0.0.1:1")
    assert run.run_id == RUN_ID and run.resumed
    run._transport._client = server
    with run._transport._cv:
        run._transport._cv.notify_all()
    assert wait_for(lambda: server.get(f"/runs/{RUN_ID}").json()["data"]["status"] == "running")
    run.finish()
    data = server.get(f"/runs/{RUN_ID}").json()["data"]
    assert data["name"] == "orig" and data["status"] == "completed"
    events = server.get(f"/runs/{RUN_ID}/events").json()["data"]
    assert any(e["event_type"] == "run_resumed" for e in events)


# --- 2. automatic replay ----------------------------------------------------------

def test_backlog_replays_automatically_in_order_status_last(tmp_path):
    up = {"v": False}
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if not up["v"]:
            raise httpx.ConnectError("down")
        seen.append((req.method, json.loads(req.content)[0] if req.method == "POST" else "patch"))
        return httpx.Response(201, json={})

    t = make(handler, tmp_path, max_queue=3)
    for i in range(30):  # far beyond the memory cache: served from the journal
        t.submit("POST", "/m", [i])
    t.submit("PATCH", "/r", {"status": "completed"})
    assert t.overflowed >= 20
    up["v"] = True
    assert wait_for(lambda: len(seen) == 31)
    assert [v for m, v in seen[:30]] == list(range(30))
    assert seen[-1][0] == "PATCH"
    assert t.close(5) == 0
    assert not (tmp_path / RUN_ID).exists()


def test_batch_ids_stay_stable_across_retries_and_sync(tmp_path):
    ids: list[str] = []
    fail = {"n": 2}

    def handler(req):
        ids.append(req.headers[BATCH_HEADER])
        if fail["n"] > 0:
            fail["n"] -= 1
            return httpx.Response(503)
        return httpx.Response(201, json={})

    t = make(handler, tmp_path)
    t.submit("POST", "/m", [1])
    assert wait_for(lambda: len(ids) >= 3)
    assert len(set(ids)) == 1
    t.close(2)
    recorded = Spool(RUN_ID, tmp_path).pending()[0]
    assert not recorded  # delivered on the third attempt


# --- 3. write-ahead journal ------------------------------------------------------

def test_payload_is_on_disk_before_it_is_sent(tmp_path):
    seen = {}

    def handler(req):
        seen["journaled"] = [i["id"] for i in Spool(RUN_ID, tmp_path).pending()[0]]
        seen["sent"] = req.headers[BATCH_HEADER]
        return httpx.Response(201, json={})

    t = make(handler, tmp_path)
    t.submit("POST", "/m", [1], item_id="stable-id")
    assert wait_for(lambda: "sent" in seen)
    assert seen["journaled"] == ["stable-id"] == [seen["sent"]]
    t.close(2)


def test_next_transport_for_same_run_replays_unacknowledged_entries(tmp_path):
    spool = Spool(RUN_ID, tmp_path)
    spool.write_meta(server="http://srv", created=True)
    spool.append([{"id": f"old{i}", "method": "POST", "path": "/m", "json": [i]} for i in range(3)])
    (tmp_path / RUN_ID / QUEUE_FILE).open("ab").write(b'{"id": "torn", "meth')  # killed mid-write
    got: list = []

    def handler(req):
        got.append(json.loads(req.content)[0])
        return httpx.Response(201, json={})

    t = make(handler, tmp_path)
    t.submit("POST", "/m", [99])
    assert wait_for(lambda: len(got) == 4)
    assert got == [0, 1, 2, 99]
    t.close(2)


def test_sigkill_loses_nothing_that_was_logged(server, tmp_path, monkeypatch):
    spool_dir = tmp_path / "killspool"
    script = textwrap.dedent("""
        import os, signal, pyrometer
        run = pyrometer.init(name="killed", server="http://127.0.0.1:1")
        run.log({"loss": 1.0}, step=1)   # still in the SDK buffer: only in the write-ahead log
        run.event("before the kill")     # journaled
        run.log({"loss": 0.9}, step=2)
        os.kill(os.getpid(), signal.SIGKILL)
    """)
    env = {**os.environ, "PYROMETER_SPOOL_DIR": str(spool_dir), "PYROMETER_RUN_ID": ""}
    env.pop("PYROMETER_RUN_ID")
    proc = subprocess.run([sys.executable, "-c", script], env=env, timeout=120, capture_output=True)
    assert proc.returncode == -9

    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(spool_dir))
    dirs = find_spools(None)
    assert len(dirs) == 1
    res = sync_run_dir(dirs[0], client=server)
    assert res.complete, res
    rid = dirs[0].name
    assert server.get(f"/runs/{rid}").json()["data"]["name"] == "killed"
    assert sorted(m["step"] for m in server.get(f"/runs/{rid}/metrics").json()["data"]) == [1, 2]
    assert len(server.get(f"/runs/{rid}/events").json()["data"]) == 1
    assert not dirs[0].exists()


def test_double_send_after_wal_recovery_is_deduped(server, tmp_path):
    """A crash between 'batch journaled' and 'wal cleared' leaves both copies; the server ignores one."""
    server.post("/runs", json={"run_id": RUN_ID})
    spool = Spool(RUN_ID, tmp_path)
    spool.write_meta(server="http://srv", created=True)
    entries = [{"step": 1, "timestamp": 1.0, "name": "loss", "value": 0.5}]
    spool.append([{"id": "b1", "method": "POST", "path": f"/runs/{RUN_ID}/metrics", "json": entries}])
    (tmp_path / RUN_ID / "metrics.wal").write_text(json.dumps({"b": "b1", "e": entries}) + "\n")
    assert sync_run_dir(tmp_path / RUN_ID, client=server).complete
    assert len(server.get(f"/runs/{RUN_ID}/metrics").json()["data"]) == 1


# --- 4. media as separate files ----------------------------------------------------

def test_media_is_stored_as_binary_file_not_base64_in_jsonl(tmp_path, server):
    raw = os.urandom(5000)
    t = Transport(RUN_ID, None, Spool(RUN_ID, tmp_path), offline=True)
    t.submit("POST", f"/runs/{RUN_ID}/media", {"step": 1, "timestamp": 1.0, "name": "im", "media_type": "image"},
             blob=("data", raw, "b64"))
    t.close(1)
    line = (tmp_path / RUN_ID / QUEUE_FILE).read_text()
    assert base64.b64encode(raw).decode() not in line
    assert "data" not in json.loads(line)["json"] and len(line) < 400
    (blob,) = list((tmp_path / RUN_ID / BLOB_DIR).iterdir())
    assert blob.read_bytes() == raw

    server.post("/runs", json={"run_id": RUN_ID})
    Spool(RUN_ID, tmp_path).write_meta(created=True)
    assert sync_run_dir(tmp_path / RUN_ID, client=server).complete
    media = server.get(f"/runs/{RUN_ID}/media").json()["data"]
    assert base64.b64decode(media[0]["data"]) == raw
    assert not (tmp_path / RUN_ID).exists()


def test_log_image_and_large_histogram_use_blobs(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    np = pytest.importorskip("numpy")
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path))
    run = pyrometer.init(name="m", offline=True)
    run.log_image("im", np.zeros((8, 8, 3), dtype=np.uint8), step=1)
    run.log_histogram("h", np.arange(100.0), step=1)
    run.finish()
    lines = [json.loads(l) for l in (tmp_path / run.run_id / QUEUE_FILE).read_text().splitlines()]
    media = [l for l in lines if l["path"].endswith("/media")]
    assert len(media) == 2
    image = next(m for m in media if m["json"]["media_type"] == "image")
    assert "data" not in image["json"] and image["blob"]["enc"] == "b64"
    hist = next(m for m in media if m["json"]["media_type"] == "histogram")
    assert "data" in hist["json"]  # small: stays inline


# --- 5. head-of-line blocking ------------------------------------------------------

def test_poison_payload_is_dead_lettered_and_newer_data_flows(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_POISON_ATTEMPTS", 3)
    delivered: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={})
        if req.url.path == "/poison":
            return httpx.Response(500)
        delivered.append(json.loads(req.content)[0])
        return httpx.Response(201, json={})

    t = make(handler, tmp_path)
    t.submit("POST", "/m", [1])
    t.submit("POST", "/poison", [2], item_id="poison-1")
    t.submit("POST", "/m", [3])
    assert wait_for(lambda: delivered == [1, 3])
    assert t.dead == 1
    t.close(2)
    dead = Spool(RUN_ID, tmp_path).dead()[0]
    assert [d["id"] for d in dead] == ["poison-1"]

    # still replayable by sync (here the "server" has been fixed)
    spool = Spool(RUN_ID, tmp_path)
    spool.write_meta(created=True)
    ok = httpx.Client(base_url="http://srv", transport=httpx.MockTransport(lambda r: httpx.Response(201, json={})))
    res = sync_run_dir(tmp_path / RUN_ID, client=ok)
    assert res.complete and res.sent == 1


def test_outage_is_not_mistaken_for_a_poison_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_POISON_ATTEMPTS", 2)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(502)  # a proxy answering for a dead backend, /health included

    t = make(handler, tmp_path)
    t.submit("POST", "/m", [1])
    assert wait_for(lambda: calls["n"] >= 12)
    assert t.dead == 0
    t.close(0.2)
    assert len(Spool(RUN_ID, tmp_path).pending()[0]) == 1
