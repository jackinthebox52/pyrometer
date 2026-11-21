"""W9c final-review regressions."""

import sys

import pytest

import pyrometer
from pyrometer._run import Run


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    monkeypatch.setattr(Run, "_start_hardware_probe", lambda self, known: None)


def test_overlapping_runs_restore_the_original_streams():
    """Finishing runs out of order must leave sys.stdout/stderr exactly as the program had them."""
    out, err = sys.stdout, sys.stderr
    a = pyrometer.init(name="a", offline=True)
    b = pyrometer.init(name="b", offline=True)
    a.finish()
    b.finish()
    assert sys.stdout is out and sys.stderr is err


def test_stream_replaced_by_the_app_is_not_clobbered_on_finish():
    import io

    out = sys.stdout
    run = pyrometer.init(name="c", offline=True)
    mine = io.StringIO()
    sys.stdout = mine  # the application (or a redirect helper) swapped the stream meanwhile
    try:
        run.finish()
        assert sys.stdout is mine
    finally:
        sys.stdout = out


def test_masked_webhook_url_cannot_overwrite_the_secret():
    from pyrometer.server import alerts

    assert alerts.validate_url(alerts.mask_url("https://hooks.example.com/secret/token")) is not None
    assert alerts.validate_url("https://hooks.example.com/secret/token") is None


def test_nan_and_inf_metrics_survive_the_wire_and_are_readable(tmp_path, monkeypatch):
    """A diverged loss (NaN/Inf) must neither fail the SDK's batch (httpx refuses NaN) nor 500 the dashboard."""
    from fastapi.testclient import TestClient

    from pyrometer._transport import wire_json
    from pyrometer.server import auth
    from pyrometer.server.app import app

    assert wire_json({"v": [float("nan"), float("inf"), -float("inf"), 1.5]}) == {"v": ["NaN", "Infinity", "-Infinity", 1.5]}

    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    auth._cache = None
    auth._setup_token = None
    c = TestClient(app, headers={"Authorization": f"Bearer {auth.create_key('sdk')[1]}"})
    rid = "abcdef1234567890"
    assert c.post("/runs", json={"run_id": rid}).status_code == 201
    batch = wire_json([{"step": s, "timestamp": float(s), "name": "loss", "value": v}
                       for s, v in enumerate([1.0, float("nan"), float("inf")], 1)])
    assert c.post(f"/runs/{rid}/metrics", json=batch).json()["data"]["inserted"] == 3
    for path in ("metrics", "metrics/recent", "summary"):
        assert c.get(f"/runs/{rid}/{path}").status_code == 200, path
    vals = [m["value"] for m in c.get(f"/runs/{rid}/metrics").json()["data"]]
    assert vals[0] == 1.0 and None in vals
