"""init(resume=...): server-side reattach, project checks, SDK helpers and offline mode."""

import httpx
import pytest
from fastapi.testclient import TestClient

import pyrometer
from pyrometer import _diff_config
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


RID = "abcdef1234567890"


def create(server, **kw):
    return server.post("/runs", json={"run_id": RID, "hyperparams": {"lr": 1}, "tags": ["a"], **kw})


def test_resume_reattaches_and_keeps_config(server):
    create(server, name="orig")
    server.post(f"/runs/{RID}/metrics", json=[{"step": s, "timestamp": 1.0, "name": "loss", "value": 1.0} for s in (3, 9)])
    server.patch(f"/runs/{RID}", json={"status": "completed"})
    r = server.post("/runs", json={"run_id": RID, "resume": "must", "name": "other", "hyperparams": {"lr": 5}})
    assert r.status_code == 200 or r.status_code == 201
    d = r.json()["data"]
    assert d["resumed"] and d["previous_status"] == "completed" and d["last_step"] == 9
    assert d["name"] == "orig" and d["hyperparams"] == {"lr": 1}
    run = server.get(f"/runs/{RID}").json()["data"]
    assert run["status"] == "running" and run["finished_at"] is None


def test_resume_must_404_auto_creates(server):
    assert server.post("/runs", json={"run_id": RID, "resume": "must"}).status_code == 404
    d = server.post("/runs", json={"run_id": RID, "resume": "auto", "name": "n"}).json()["data"]
    assert d["run_id"] == RID and not d.get("resumed")
    assert server.post("/runs", json={"run_id": RID, "resume": "auto"}).json()["data"]["resumed"]
    assert server.post("/runs", json={"resume": "auto"}).status_code == 400


def test_resume_without_flag_does_not_change_status(server):
    create(server)
    server.patch(f"/runs/{RID}", json={"status": "completed"})
    create(server)
    assert server.get(f"/runs/{RID}").json()["data"]["status"] == "completed"


def test_resume_project_checks(server):
    create(server, project="alpha")
    bad = server.post("/runs", json={"run_id": RID, "resume": "must", "project": "beta"})
    assert bad.status_code == 409
    ok = server.post("/runs", json={"run_id": RID, "resume": "must", "project": "alpha"})
    assert ok.status_code in (200, 201)
    # no project given: the run's own project is used (not "default")
    assert server.post("/runs", json={"run_id": RID, "resume": "must"}).json()["data"]["project"] == "alpha"


def test_project_key_cannot_resume_foreign_run(server):
    create(server, project="alpha")
    server.post("/projects", json={"name": "beta", "visibility": "private"})
    info = server.post("/projects", json={"name": "gamma", "visibility": "private"}).json()["data"]
    pk = TestClient(app, headers={"Authorization": f"Bearer {info['key']}"})
    r = pk.post("/runs", json={"run_id": RID, "resume": "must"})
    assert r.status_code == 404
    assert server.get(f"/runs/{RID}").json()["data"]["status"] == "running"


def test_resume_revives_crashed_and_sdk_helpers(server):
    create(server)
    db.db_mark_stale_runs(now=1e12)
    assert server.get(f"/runs/{RID}").json()["data"]["status"] == "crashed"
    assert server.post("/runs", json={"run_id": RID, "resume": "must"}).json()["data"]["previous_status"] == "crashed"


def test_diff_config_only_compares_passed_values():
    stored = {"hyperparams": {"lr": 1, "bs": 8}, "tags": ["a"]}
    assert _diff_config(stored, None, None) == {}
    assert _diff_config(stored, {"lr": 1}, ["a"]) == {}
    ch = _diff_config(stored, {"lr": 2, "new": 1}, ["b"])
    assert set(ch) == {"hyperparams.lr", "hyperparams.new", "tags"}


def test_init_validation(monkeypatch):
    with pytest.raises(ValueError):
        pyrometer.init(resume="auto", offline=True)
    with pytest.raises(ValueError):
        pyrometer.init(resume="short", offline=True)


def test_offline_resume_spools_resume_create_and_event(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("PYROMETER_RUN_ID", RID)  # env alone means resume="auto"
    run = pyrometer.init(offline=True)
    assert run.run_id == RID and run.resumed and run.last_step is None
    run.log({"loss": 1.0}, step=4)
    assert run.last_step == 4
    run.finish()
    import json
    meta = json.loads((tmp_path / "spool" / RID / "meta.json").read_text())
    assert meta["create"]["resume"] == "auto"
    queue = (tmp_path / "spool" / RID / "queue.jsonl").read_text()
    assert "run_resumed" in queue


def test_online_resume_end_to_end(server, monkeypatch, tmp_path):
    """Drive init(resume=...) against the in-process app by routing httpx through TestClient."""
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "spool"))
    create(server, project="alpha")
    server.post(f"/runs/{RID}/metrics", json=[{"step": 41, "timestamp": 1.0, "name": "loss", "value": 1.0}])
    server.patch(f"/runs/{RID}", json={"status": "failed"})

    def fake_post(url, json=None, headers=None, timeout=None):
        r = server.post(url.replace("http://test", ""), json=json)
        return httpx.Response(r.status_code, json=r.json(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "Client", lambda **kw: _Proxy(server))
    run = pyrometer.init(resume=RID, server="http://test", hyperparams={"lr": 9})
    assert run.resumed and run.last_step == 41 and run.run_id == RID
    run.finish()
    events = server.get(f"/runs/{RID}/events").json()["data"]
    ev = [e for e in events if e["event_type"] == "run_resumed"]
    assert ev and ev[0]["metadata"]["previous_status"] == "failed"
    assert "hyperparams.lr" in ev[0]["metadata"]["config_changes"]
    with pytest.raises(LookupError):
        pyrometer.init(resume="deadbeef00000000", server="http://test")


class _Proxy:
    """Minimal httpx.Client stand-in that forwards to a TestClient."""

    def __init__(self, tc: TestClient) -> None:
        self._tc = tc

    def request(self, method, path, json=None, headers=None, **kw):
        r = self._tc.request(method, path, json=json, headers=headers)
        return httpx.Response(r.status_code, json=r.json(), request=httpx.Request(method, path))

    def close(self) -> None:
        pass
