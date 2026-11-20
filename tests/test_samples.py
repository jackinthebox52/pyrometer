"""log_samples: server storage/pagination/limits/visibility, SDK truncation, spool + sync replay."""

import json

import pytest
from fastapi.testclient import TestClient

import pyrometer
from pyrometer._sync import find_spools, sync_run_dir
from pyrometer._transport import BATCH_HEADER
from pyrometer.server import auth
from pyrometer.server.app import MAX_SAMPLE_BYTES, MAX_SAMPLES_PER_CALL, app


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


def body(step=1, samples=None, name="samples"):
    return {"name": name, "step": step, "timestamp": 1.0,
            "samples": samples if samples is not None else [{"prompt": "p", "completion": "c", "reward": 1.0}]}


def test_store_page_and_steps(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    smp = [{"prompt": f"p{i}", "reward": float(i)} for i in range(5)]
    assert server.post(f"/runs/{rid}/samples", json=body(3, smp)).json()["data"]["inserted"] == 5
    server.post(f"/runs/{rid}/samples", json=body(7, smp[:2]))
    steps = server.get(f"/runs/{rid}/samples/steps").json()["data"]
    assert [(s["name"], s["step"], s["count"]) for s in steps] == [("samples", 3, 5), ("samples", 7, 2)]
    page = server.get(f"/runs/{rid}/samples", params={"step": 3, "limit": 2, "offset": 2}).json()["data"]
    assert page["total"] == 5 and [i["sample"]["prompt"] for i in page["items"]] == ["p2", "p3"]


def test_duplicate_batch_ignored(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    h = {BATCH_HEADER: "b1"}
    server.post(f"/runs/{rid}/samples", json=body(), headers=h)
    assert server.post(f"/runs/{rid}/samples", json=body(), headers=h).json()["data"]["duplicate"]
    assert server.get(f"/runs/{rid}/samples/steps").json()["data"][0]["count"] == 1


def test_size_limits(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    big = [{"completion": "x" * (MAX_SAMPLE_BYTES + 1)}]
    r = server.post(f"/runs/{rid}/samples", json=body(samples=big))
    assert r.status_code == 413 and "limit" in r.json()["detail"]["error"]
    r = server.post(f"/runs/{rid}/samples", json=body(samples=[{"a": 1}] * (MAX_SAMPLES_PER_CALL + 1)))
    assert r.status_code == 413


def test_private_project_visibility(server):
    key = server.post("/projects", json={"name": "secret", "visibility": "private"}).json()["data"]["key"]
    rid = server.post("/runs", json={"project": "secret"}).json()["data"]["run_id"]
    server.post(f"/runs/{rid}/samples", json=body())
    anon = TestClient(app)
    assert anon.get(f"/runs/{rid}/samples/steps").status_code == 401
    other = server.post("/projects", json={"name": "other", "visibility": "private"}).json()["data"]["key"]
    theirs = TestClient(app, headers={"Authorization": f"Bearer {other}"})
    assert theirs.get(f"/runs/{rid}/samples/steps").status_code == 404
    assert theirs.post(f"/runs/{rid}/samples", json=body()).status_code == 404
    mine = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    assert mine.get(f"/runs/{rid}/samples/steps").status_code == 200


def test_llm_summary_includes_extremes(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    smp = [{"prompt": f"p{i}", "completion": "y" * 1000, "reward": float(i)} for i in range(10)]
    server.post(f"/runs/{rid}/samples", json=body(5, smp))
    text = server.get(f"/runs/{rid}/llm-summary").text
    assert "SAMPLES 'samples'" in text and "[top reward]" in text and "p9" in text and "p0" in text
    assert "y" * 200 not in text


def test_sdk_truncates_and_offline_sync_replays(server, tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_SPOOL_DIR", str(tmp_path / "spool"))
    run = pyrometer.init(offline=True)
    run.log_samples(2, [{"prompt": "p", "completion": "z" * 200_000, "reward": 0.5}, "bad"], name="gen")
    run.log_samples(3, [{"prompt": str(i)} for i in range(450)], name="gen")
    run.finish()
    lines = [json.loads(l) for d in find_spools(None) for l in (d / "queue.jsonl").read_text().splitlines()]
    posts = [l for l in lines if l["path"].endswith("/samples")]
    assert len(posts) == 4  # 1 + 3 chunks of <=200
    assert len(posts[0]["json"]["samples"]) == 1
    assert len(posts[0]["json"]["samples"][0]["completion"]) < 64 * 1024

    class Client:  # route the replay through the TestClient
        def post(self, *a, **k):
            return server.post(*a, **k)

        def request(self, method, path, **k):
            return server.request(method, path, **k)

    res = sync_run_dir(find_spools(None)[0], client=Client())
    assert res.complete, res.messages
    steps = server.get(f"/runs/{run.run_id}/samples/steps").json()["data"]
    assert {(s["step"], s["count"]) for s in steps} == {(2, 1), (3, 450)}
