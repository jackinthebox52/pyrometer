"""Phase 3 W3: lifecycle (exit statuses, SIGTERM chaining, legacy runs, spool lock, write-ahead logs,
background fingerprint) and data correctness (last-write-wins, samples mode, detector seeding)."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import pyrometer
import pyrometer._hardware as hw
import pyrometer._run as run_mod
import pyrometer._transport as tr
from pyrometer._sync import find_spools, sync_run_dir
from pyrometer._transport import QUEUE_FILE, Spool, SpoolLock, Transport
from pyrometer.server import auth, db
from pyrometer.server.app import app


@pytest.fixture(autouse=True)
def fresh_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    for var in ("PYROMETER_AUTH", "PYROMETER_TRUSTED_PROXIES", "PYROMETER_BOOTSTRAP_TOKEN", "PYROMETER_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    auth._cache = None
    auth._setup_token = None
    auth._failures.clear()


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


def script(tmp_path, body: str, spool=None, timeout=120) -> subprocess.CompletedProcess:
    """Run ``body`` in a fresh interpreter with an offline-capable spool; returns the process."""
    spool = spool or tmp_path / "pspool"
    env = {**os.environ, "PYROMETER_SPOOL_DIR": str(spool)}
    env.pop("PYROMETER_RUN_ID", None)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(body)], env=env, timeout=timeout,
                          capture_output=True, text=True, cwd=str(tmp_path))


def final_status(spool) -> str | None:
    (d,) = [p for p in spool.iterdir() if p.is_dir()]
    items = [json.loads(l) for l in (d / QUEUE_FILE).read_text().splitlines() if l.strip()]
    statuses = [i["json"]["status"] for i in items if i["method"] == "PATCH" and "status" in (i["json"] or {})]
    return statuses[-1] if statuses else None


# --- 2. exit statuses ----------------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    ("sys.exit(3)", "failed"),
    ("sys.exit(0)", "completed"),
    ("sys.exit()", "completed"),
    ("sys.exit(None)", "completed"),
    ("sys.exit('boom')", "failed"),
    ("raise KeyboardInterrupt", "killed"),
    ("raise RuntimeError('x')", "failed"),
    ("pass", "completed"),
])
def test_exit_status_in_a_real_process(tmp_path, code, expected):
    proc = script(tmp_path, f"""
        import sys, pyrometer
        run = pyrometer.init(offline=True)
        run.log({{"loss": 1.0}}, step=1)
        {code}
    """)
    assert final_status(tmp_path / "pspool") == expected, proc.stderr


def test_caught_sys_exit_followed_by_normal_end_is_completed(tmp_path):
    script(tmp_path, """
        import sys, pyrometer
        run = pyrometer.init(offline=True)
        try:
            sys.exit(2)
        except SystemExit:
            pass
        run.log({"loss": 1.0}, step=1)   # the program carries on
    """)
    assert final_status(tmp_path / "pspool") == "completed"


# --- 1. SIGTERM chaining -------------------------------------------------------------------------

def test_sigterm_default_marks_killed_and_dies_by_sigterm(tmp_path):
    proc = script(tmp_path, """
        import os, signal, pyrometer
        run = pyrometer.init(offline=True)
        run.log({"loss": 1.0}, step=1)
        os.kill(os.getpid(), signal.SIGTERM)
        import time; time.sleep(30)
    """)
    assert proc.returncode == -signal.SIGTERM
    assert final_status(tmp_path / "pspool") == "killed"


def test_sigterm_chains_to_existing_application_handler(tmp_path):
    proc = script(tmp_path, """
        import os, signal, sys, pyrometer
        def app_handler(signum, frame):
            print("APP HANDLER RAN")
            sys.stdout.flush()
            os._exit(7)
        signal.signal(signal.SIGTERM, app_handler)
        run = pyrometer.init(offline=True)
        run.log({"loss": 1.0}, step=1)
        os.kill(os.getpid(), signal.SIGTERM)
        import time; time.sleep(30)
    """)
    assert proc.returncode == 7 and "APP HANDLER RAN" in proc.stdout
    assert final_status(tmp_path / "pspool") == "killed"  # ours ran first, then the application's


def test_sigterm_ignored_by_the_application_is_respected(tmp_path):
    proc = script(tmp_path, """
        import os, signal, pyrometer
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        run = pyrometer.init(offline=True)
        os.kill(os.getpid(), signal.SIGTERM)
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        run.log({"loss": 1.0}, step=1)
        print("STILL RUNNING")
    """)
    assert proc.returncode == 0 and "STILL RUNNING" in proc.stdout
    assert final_status(tmp_path / "pspool") == "completed"


def test_sigterm_install_chains_in_process_and_retries_after_non_main_thread(monkeypatch):
    seen = []
    prev = lambda signum, frame: seen.append("app")  # noqa: E731
    old = signal.signal(signal.SIGTERM, prev)
    monkeypatch.setattr(run_mod, "_our_sigterm", None)
    try:
        t = threading.Thread(target=run_mod._install_sigterm)
        t.start(); t.join()
        assert signal.getsignal(signal.SIGTERM) is prev  # not the main thread: nothing installed, nothing latched
        run_mod._install_sigterm()
        ours = signal.getsignal(signal.SIGTERM)
        assert ours is not prev and ours is run_mod._our_sigterm
        run_mod._install_sigterm()  # idempotent: does not wrap itself
        assert signal.getsignal(signal.SIGTERM) is ours
        ours(signal.SIGTERM, None)  # no live runs: just chains
        assert seen == ["app"]
    finally:
        signal.signal(signal.SIGTERM, old)


# --- 3. legacy stuck runs -------------------------------------------------------------------------

def test_legacy_running_runs_are_migrated_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_HEARTBEAT_TIMEOUT", "300")
    now = time.time()
    conn = db.get_conn()
    for rid, created in (("old-silent", now - 10_000), ("old-with-data", now - 10_000), ("recent", now - 20),
                         ("finished", now - 10_000)):
        conn.execute("INSERT INTO runs (run_id, status, created_at, project) VALUES (?, ?, ?, 'default')",
                     [rid, "completed" if rid == "finished" else "running", created])
    conn.execute("INSERT INTO metrics VALUES ('old-with-data', 1, ?, 'loss', 1.0)", [now - 100])  # recent data
    conn.execute("INSERT INTO logs VALUES ('old-silent', ?, 'stdout', 'x', 1)", [now - 9_000])  # old data only
    conn.execute("UPDATE runs SET last_heartbeat = NULL")
    alerts = []
    db.set_status_hook(lambda rid, st: alerts.append((rid, st)))
    try:
        db._migrate_legacy_running(conn)
    finally:
        db.set_status_hook(None)
    rows = {r[0]: r for r in conn.execute("SELECT run_id, status, finished_at, last_heartbeat FROM runs").fetchall()}
    assert rows["old-silent"][1] == "crashed" and abs(rows["old-silent"][2] - (now - 9_000)) < 1
    assert rows["old-with-data"][1] == "running" and abs(rows["old-with-data"][3] - (now - 100)) < 1
    assert rows["recent"][1] == "running"
    assert rows["finished"][1] == "completed" and rows["finished"][3] is None
    assert alerts == []  # history, not news
    # the normal sweep now owns the survivors: still alive within the timeout, crashed after it
    assert db.db_mark_stale_runs(now=now + 10) == []
    assert set(db.db_mark_stale_runs(now=now + 1_000)) == {"old-with-data", "recent"}


def test_migration_runs_when_the_database_is_opened(tmp_path, monkeypatch):
    import duckdb

    path = str(tmp_path / "legacy.duckdb")
    c = duckdb.connect(path)
    db._init_schema(c)
    c.execute("INSERT INTO runs (run_id, status, created_at, project) VALUES ('stuck', 'running', ?, 'default')",
              [time.time() - 99_999])
    c.execute("UPDATE runs SET last_heartbeat = NULL")
    c.close()
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "_DB_PATH", path)
    monkeypatch.setattr(db, "_conn", None)
    assert db.db_get_run_status("stuck") == "crashed"


# --- 4. PATCH running refreshes the heartbeat -----------------------------------------------------

def test_patch_running_refreshes_heartbeat(server, monkeypatch):
    monkeypatch.setenv("PYROMETER_HEARTBEAT_TIMEOUT", "60")
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    db.db_mark_stale_runs(now=time.time() + 100)
    assert server.get(f"/runs/{rid}").json()["data"]["status"] == "crashed"
    server.patch(f"/runs/{rid}", json={"status": "running"})
    run = server.get(f"/runs/{rid}").json()["data"]
    assert run["status"] == "running" and run["finished_at"] is None
    assert db.db_mark_stale_runs(now=time.time() + 10) == []  # not swept straight back


# --- 5. spool lock + sync -------------------------------------------------------------------------

OWNER = """
    import sys, time, pyrometer
    run = pyrometer.init(name="owner", offline=True)
    run.log({"loss": 1.0}, step=1)
    print(run.run_id, flush=True)
    time.sleep(60)
"""


def start_owner(tmp_path):
    env = {**os.environ, "PYROMETER_SPOOL_DIR": str(tmp_path / "pspool")}
    env.pop("PYROMETER_RUN_ID", None)
    p = subprocess.Popen([sys.executable, "-c", textwrap.dedent(OWNER)], env=env, stdout=subprocess.PIPE, text=True)
    rid = p.stdout.readline().strip()
    assert rid
    return p, rid


def test_sync_refuses_while_owner_is_alive_and_recovers_after_sigkill(server, tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "pspool"))
    p, rid = start_owner(tmp_path)
    try:
        d = tmp_path / "pspool" / rid
        res = sync_run_dir(d, client=server)
        assert res.skipped and not res.complete and str(p.pid) in res.error
        assert (d / "owner.lock").exists() and (d / "meta.json").exists()  # untouched
        assert server.get(f"/runs/{rid}").status_code == 404
        p.kill()
        p.wait()
        assert (d / "owner.lock").exists()  # a stale lock file is left behind...
        assert SpoolLock.held_by_other(d) is None  # ...and recognised as stale
        res = sync_run_dir(d, client=server)
        assert res.complete, (res.error, res.messages)
        assert not d.exists()
        assert [m["value"] for m in server.get(f"/runs/{rid}/metrics").json()["data"]] == [1.0]
    finally:
        p.kill()


def test_cli_sync_skips_active_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "pspool"))
    p, rid = start_owner(tmp_path)
    try:
        from pyrometer import cli

        monkeypatch.setattr(sys, "argv", ["pyrometer", "sync", "--server", "http://127.0.0.1:1"])
        cli.main()  # skipped, not a failure: no SystemExit
        assert "skipped" in capsys.readouterr().out
    finally:
        p.kill()


def test_lock_basics_in_process(tmp_path):
    d = tmp_path / "r1"
    a = SpoolLock(d)
    assert a.acquire() and a.owner_pid() == os.getpid()
    assert SpoolLock.held_by_other(d) == os.getpid()  # flock is per open file: a second handle sees it held
    assert not SpoolLock(d).acquire()
    a.release()
    assert SpoolLock.held_by_other(d) is None
    assert SpoolLock(d).acquire()


def test_transport_holds_lock_until_close(tmp_path):
    t = Transport("r-lock-0001", None, Spool("r-lock-0001", tmp_path), offline=True)
    d = tmp_path / "r-lock-0001"
    assert SpoolLock.held_by_other(d) is not None
    t.close(1)
    assert SpoolLock.held_by_other(d) is None and not (d / "owner.lock").exists()


def test_pid_file_fallback_without_fcntl(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_fcntl", None)
    d = tmp_path / "w"
    lock = SpoolLock(d)
    assert lock.acquire()
    assert SpoolLock.held_by_other(d) is None  # our own pid does not count as "another" process
    (d / "owner.lock").write_text(str(os.getppid()))  # a live process that is not us
    assert SpoolLock.held_by_other(d) == os.getppid()
    assert not SpoolLock(d).acquire()
    (d / "owner.lock").write_text("2147483646")  # no such process: stale
    assert SpoolLock.held_by_other(d) is None
    assert SpoolLock(d).acquire()


# --- 6. write-ahead coverage for log lines and gradient stats ---------------------------------------

def test_sigkill_keeps_unflushed_stdout_lines(server, tmp_path, monkeypatch):
    spool = tmp_path / "killspool"
    proc = script(tmp_path, """
        import os, signal, pyrometer
        run = pyrometer.init(name="logs", server="http://127.0.0.1:1")
        print("line one")
        print("line two")
        os.kill(os.getpid(), signal.SIGKILL)
    """, spool=spool)
    assert proc.returncode == -9
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(spool))
    (d,) = find_spools(None)
    assert (d / "logs.wal").stat().st_size > 0
    assert sync_run_dir(d, client=server).complete
    lines = [l["line"] for l in server.get(f"/runs/{d.name}/logs").json()["data"]]
    assert "line one" in lines and "line two" in lines


def test_sigkill_keeps_unflushed_param_stats(server, tmp_path, monkeypatch):
    pytest.importorskip("torch")
    spool = tmp_path / "killspool"
    proc = script(tmp_path, """
        import os, signal, torch, pyrometer
        run = pyrometer.init(name="grads", server="http://127.0.0.1:1")
        model = torch.nn.Linear(4, 2)
        run.watch(model, log_freq=1)
        run.log({"loss": 1.0}, step=3)
        model(torch.randn(8, 4)).sum().backward()
        os.kill(os.getpid(), signal.SIGKILL)
    """, spool=spool)
    assert proc.returncode == -9
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(spool))
    (d,) = find_spools(None)
    assert (d / "param_stats.wal").stat().st_size > 0
    assert sync_run_dir(d, client=server).complete
    names = server.get(f"/runs/{d.name}/param-stats/names").json()["data"]
    assert set(names) == {"weight", "bias"}


def test_wal_cleared_after_flush_and_never_loses_a_concurrent_batch(tmp_path):
    t = Transport("r-wal-0001", None, Spool("r-wal-0001", tmp_path), offline=True)
    wal = tmp_path / "r-wal-0001" / "logs.wal"
    t.wal_append("b1", [{"line": "a"}], "logs")
    t.wal_append("b2", [{"line": "b"}], "logs")  # a new batch started while b1 was being flushed
    t.wal_clear("logs", "b1")
    assert wal.stat().st_size > 0  # b1 is journaled but b2 is not: nothing may be dropped
    t.wal_clear("logs", "b2")
    assert wal.stat().st_size == 0
    t.close(1)
    assert not wal.exists()


def test_recover_wal_handles_all_kinds_with_stable_ids(tmp_path):
    spool = Spool("r-rec-0001", tmp_path)
    spool.dir.mkdir(parents=True)
    for fname, bid in (("metrics.wal", "m"), ("logs.wal", "l"), ("param_stats.wal", "p")):
        (spool.dir / fname).write_text(json.dumps({"b": bid, "e": [{"x": 1}]}) + "\n" + json.dumps({"b": bid, "e": [{"x": 2}]}) + "\n")
    assert spool.recover_wal() == 3
    items, _ = spool.pending()
    assert {(i["id"], i["path"].rsplit("/", 1)[1], len(i["json"])) for i in items} == {
        ("m", "metrics", 2), ("l", "logs", 2), ("p", "param-stats", 2)}


# --- 7. fingerprint does not block init -------------------------------------------------------------

def test_init_does_not_wait_for_torch_and_patches_run_later(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "s"))
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(hw, "fingerprint_slow",
                        lambda: (time.sleep(0.8), {"pytorch_version": "9.9", "cuda_version": "12.1",
                                                   "gpu_model": "Fake GPU", "gpu_memory_total": 123})[1])
    t0 = time.time()
    run = pyrometer.init(name="fp", offline=True)
    assert time.time() - t0 < 0.7  # the probe takes 0.8 s: init must not have waited for it
    run.finish()  # waits (bounded) for the probe so a short run still gets its hardware info
    items = [json.loads(l) for l in (tmp_path / "s" / run.run_id / QUEUE_FILE).read_text().splitlines()]
    patches = [i["json"] for i in items if i["method"] == "PATCH" and "gpu_model" in i["json"]]
    assert patches == [{"pytorch_version": "9.9", "cuda_version": "12.1", "gpu_model": "Fake GPU",
                        "gpu_memory_total": 123}]
    create = json.loads((tmp_path / "s" / run.run_id / "meta.json").read_text())["create"]
    assert create["python_version"] and create["hostname"]  # the cheap part is in the create payload


def test_fast_fingerprint_is_fast():
    t0 = time.time()
    info = hw.fingerprint_fast()
    assert time.time() - t0 < 1.0
    assert info["python_version"] and info["hostname"]


def test_patch_run_hardware_fields(server):
    rid = server.post("/runs", json={"gpu_model": None}).json()["data"]["run_id"]
    server.patch(f"/runs/{rid}", json={"gpu_model": "A100", "cuda_version": "12.4", "gpu_memory_total": 80 * 2**30})
    run = server.get(f"/runs/{rid}").json()["data"]
    assert run["gpu_model"] == "A100" and run["cuda_version"] == "12.4" and run["status"] == "running"
    server.patch(f"/runs/{rid}", json={"notes": "n"})  # absent fields are left alone
    assert server.get(f"/runs/{rid}").json()["data"]["gpu_model"] == "A100"


# --- 8. last write wins -----------------------------------------------------------------------------

def pt(step, value, name="loss", ts=1.0):
    return {"step": step, "timestamp": ts, "name": name, "value": value}


def test_metrics_last_write_wins_per_name_and_step(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    server.post(f"/runs/{rid}/metrics", json=[pt(s, 1.0) for s in range(5)] + [pt(2, 7.0, "acc")])
    # resumed run re-logs steps 3-6 with new values (3,4 already exist)
    r = server.post(f"/runs/{rid}/metrics", json=[pt(s, 2.0) for s in range(3, 7)])
    assert r.json()["data"]["inserted"] == 4
    got = {(m["name"], m["step"]): m["value"] for m in server.get(f"/runs/{rid}/metrics").json()["data"]}
    assert len(got) == 8 and got[("loss", 2)] == 1.0 and got[("loss", 3)] == 2.0 and got[("loss", 6)] == 2.0
    assert got[("acc", 2)] == 7.0  # another metric at a re-logged step is untouched
    # duplicates inside one batch: the later one wins
    server.post(f"/runs/{rid}/metrics", json=[pt(9, 1.0), pt(9, 5.0)])
    assert [m["value"] for m in server.get(f"/runs/{rid}/metrics", params={"name": "loss"}).json()["data"]
            if m["step"] == 9] == [5.0]
    # another run is never affected
    other = server.post("/runs", json={}).json()["data"]["run_id"]
    server.post(f"/runs/{other}/metrics", json=[pt(3, 42.0)])
    server.post(f"/runs/{rid}/metrics", json=[pt(3, 3.0)])
    assert server.get(f"/runs/{other}/metrics").json()["data"][0]["value"] == 42.0


def test_param_stats_and_media_replace_per_name_and_step(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    stat = lambda step, norm, p="w": {  # noqa: E731
        "step": step, "timestamp": 1.0, "param_name": p, "grad_norm": norm, "grad_min": 0, "grad_max": 1,
        "grad_mean": 0, "weight_norm": 1, "weight_min": 0, "weight_max": 1, "weight_mean": 0}
    server.post(f"/runs/{rid}/param-stats", json=[stat(1, 1.0), stat(1, 1.0, "b"), stat(2, 1.0)])
    server.post(f"/runs/{rid}/param-stats", json=[stat(2, 9.0), stat(3, 1.0)])
    rows = server.get(f"/runs/{rid}/param-stats").json()["data"]
    assert len(rows) == 4 and [r["grad_norm"] for r in rows if r["step"] == 2] == [9.0]

    media = lambda step, data, name="img": {"step": step, "timestamp": 1.0, "name": name,  # noqa: E731
                                            "media_type": "image", "data": data}
    server.post(f"/runs/{rid}/media", json=media(1, "AAA"))
    server.post(f"/runs/{rid}/media", json=media(1, "BBB", "other"))
    server.post(f"/runs/{rid}/media", json=media(1, "CCC"))
    got = {(m["name"], m["step"]): m["data"] for m in server.get(f"/runs/{rid}/media").json()["data"]}
    assert got == {("img", 1): "CCC", ("other", 1): "BBB"}


def test_samples_replace_by_default_and_append_on_request(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    body = lambda samples, **kw: {"name": "g", "step": 4, "timestamp": 1.0, "samples": samples, **kw}  # noqa: E731
    server.post(f"/runs/{rid}/samples", json=body([{"prompt": "a"}, {"prompt": "b"}]))
    server.post(f"/runs/{rid}/samples", json=body([{"prompt": "c"}]))  # default: replaces
    page = server.get(f"/runs/{rid}/samples", params={"name": "g", "step": 4}).json()["data"]
    assert [i["sample"]["prompt"] for i in page["items"]] == ["c"]
    server.post(f"/runs/{rid}/samples", json=body([{"prompt": "d"}], mode="append"))
    page = server.get(f"/runs/{rid}/samples", params={"name": "g", "step": 4}).json()["data"]
    assert [i["sample"]["prompt"] for i in page["items"]] == ["c", "d"]
    # other steps/names untouched by a replace
    server.post(f"/runs/{rid}/samples", json={**body([{"prompt": "z"}]), "step": 5})
    server.post(f"/runs/{rid}/samples", json=body([{"prompt": "e"}]))
    assert {(s["step"], s["count"]) for s in server.get(f"/runs/{rid}/samples/steps").json()["data"]} == {(4, 1), (5, 1)}
    assert server.post(f"/runs/{rid}/samples", json=body([], mode="bogus")).status_code == 422


def test_sdk_log_samples_modes_and_chunking(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "s"))
    run = pyrometer.init(offline=True)
    run.log_samples(1, [{"p": i} for i in range(450)])  # 3 chunks: only the first one may replace
    run.log_samples(2, [{"p": 1}], mode="append")
    with pytest.raises(ValueError):
        run.log_samples(3, [{"p": 1}], mode="nope")
    run.finish()
    items = [json.loads(l) for l in (tmp_path / "s" / run.run_id / QUEUE_FILE).read_text().splitlines()]
    modes = [i["json"]["mode"] for i in items if i["path"].endswith("/samples")]
    assert modes == ["replace", "append", "append", "append"]


def test_sdk_resume_relogged_steps_do_not_duplicate(server, tmp_path, monkeypatch):
    """End to end through the journal: the same steps logged by two processes end up once."""
    spool = tmp_path / "sp"
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(spool))
    rid = None
    for value in (1.0, 2.0):
        if rid:
            monkeypatch.setenv("PYROMETER_RUN_ID", rid)  # resume="auto"
        run = pyrometer.init(name="r", offline=True)
        rid = run.run_id
        for step in range(5):
            run.log({"loss": value}, step=step)
        run.finish()
    assert sync_run_dir(spool / rid, client=server).complete
    vals = [m["value"] for m in server.get(f"/runs/{rid}/metrics").json()["data"]]
    assert vals == [2.0] * 5


# --- 9. seeding detectors on resume ------------------------------------------------------------------

def test_recent_metrics_endpoint(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    server.post(f"/runs/{rid}/metrics", json=[pt(s, float(s)) for s in range(10)] + [pt(1, 5.0, "val")])
    d = server.get(f"/runs/{rid}/metrics/recent", params={"limit": 3}).json()["data"]
    assert d == {"loss": [7.0, 8.0, 9.0], "val": [5.0]}


class _Client:
    """httpx.Client stand-in forwarding to a TestClient (GET and the rest)."""

    def __init__(self, tc):
        self._tc = tc

    def request(self, method, path, json=None, headers=None, **kw):
        r = self._tc.request(method, path, json=json, headers=headers)
        return httpx.Response(r.status_code, json=r.json(), request=httpx.Request(method, path))

    def get(self, path, params=None, **kw):
        r = self._tc.get(path, params=params)
        return httpx.Response(r.status_code, json=r.json(), request=httpx.Request("GET", path))

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json)

    def close(self):
        pass


def test_resume_seeds_anomaly_detectors_from_server_history(server, tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "sp"))
    rid = "seeded-run-0001"
    server.post("/runs", json={"run_id": rid})
    server.post(f"/runs/{rid}/metrics", json=[pt(s, 1.0 + 0.01 * (s % 5)) for s in range(200)])
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None: httpx.Response(
        201, json=server.post("/runs", json=json).json(), request=httpx.Request("POST", url)))
    monkeypatch.setattr(httpx, "Client", lambda **kw: _Client(server))
    run = pyrometer.init(resume=rid, server="http://test")
    assert run.resumed
    assert wait_for(lambda: len(run._metric_detector._hist.get("loss", [])) >= 100)
    assert run._metric_detector._hist["loss"][-1] == pytest.approx(1.0 + 0.01 * (199 % 5))
    assert run._metric_detector.observe("loss", 90.0, 5000) is not None  # first value after resume: no cold warmup
    run.finish()


def test_seeding_failure_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "sp"))
    run = pyrometer.init(offline=True, resume="auto-run-00001")  # offline: nothing to seed from
    assert run._metric_detector._hist == {}
    run.finish()
