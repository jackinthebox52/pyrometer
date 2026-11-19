"""Auth behaviour: API keys, scopes, session cookie, trusted-proxy header, WebSocket, local mode."""

import json
import os
import stat
import tempfile

import pytest

# The DB path is read at import time, so point it at a temp dir before importing the app.
os.environ["PYROMETER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.duckdb")
os.environ.pop("PYROMETER_AUTH", None)

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from pyrometer.server import auth  # noqa: E402
from pyrometer.server.app import app  # noqa: E402

RUN = {"name": "t", "hyperparams": {}, "tags": []}


@pytest.fixture(autouse=True)
def keystore(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_KEYS_FILE", str(tmp_path / "keys.json"))
    monkeypatch.delenv("PYROMETER_AUTH", raising=False)
    monkeypatch.delenv("PYROMETER_TRUSTED_PROXIES", raising=False)
    return tmp_path / "keys.json"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def make_key(scopes: list[str]) -> str:
    return auth.create_key("test", scopes)[1]


def test_default_requires_auth(client):
    assert client.get("/runs").status_code == 401
    assert client.post("/runs", json=RUN).status_code == 401
    assert client.get("/runs").headers["www-authenticate"] == "Bearer"


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_docs_not_served_when_auth_required(client):
    assert client.get("/openapi.json").status_code in (401, 404)
    assert client.get("/docs").status_code in (401, 404)


def test_ingest_key_can_write_but_not_read(client):
    key = make_key(["ingest"])
    assert client.post("/runs", json=RUN, headers=bearer(key)).status_code == 201
    assert client.get("/runs", headers=bearer(key)).status_code == 403


def test_read_key_can_read_but_not_write(client):
    key = make_key(["read"])
    assert client.get("/runs", headers=bearer(key)).status_code == 200
    assert client.post("/runs", json=RUN, headers=bearer(key)).status_code == 403


@pytest.mark.parametrize("header", ["Bearer nope", "Bearer pyro_deadbeef_x", "Basic abc", "Bearer ", "bearer"])
def test_bad_credentials_rejected(client, header):
    assert client.get("/runs", headers={"Authorization": header}).status_code == 401


def test_wrong_secret_for_real_id_rejected(client):
    key_id, key = auth.create_key("t", ["read"])
    forged = f"pyro_{key_id}_{'A' * 43}"
    assert client.get("/runs", headers=bearer(forged)).status_code == 401
    assert client.get("/runs", headers=bearer(key)).status_code == 200


def test_revoked_key_rejected(client):
    key_id, key = auth.create_key("t", ["read"])
    assert client.get("/runs", headers=bearer(key)).status_code == 200
    assert auth.revoke_key(key_id)
    assert client.get("/runs", headers=bearer(key)).status_code == 401


def test_bad_bearer_does_not_fall_through_to_cookie(client):
    good = make_key(["read"])
    client.post("/auth/login", json={"key": good})
    assert client.get("/runs").status_code == 200
    assert client.get("/runs", headers=bearer("pyro_bad_bad")).status_code == 401


def test_keystore_stores_only_hash_with_private_mode(keystore):
    _, key = auth.create_key("t", ["ingest"])
    raw = keystore.read_text()
    secret = key.split("_", 2)[2]
    assert secret not in raw and key not in raw
    assert stat.S_IMODE(keystore.stat().st_mode) == 0o600
    assert "hash" not in json.dumps(auth.list_keys())


def test_create_key_rejects_bad_scopes():
    with pytest.raises(ValueError):
        auth.create_key("t", ["admin"])
    with pytest.raises(ValueError):
        auth.create_key("t", [])


def test_login_sets_httponly_strict_cookie_and_grants_read_only(client):
    key = make_key(["read"])
    r = client.post("/auth/login", json={"key": key})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/runs").status_code == 200
    assert client.post("/runs", json=RUN).status_code == 401  # cookie never grants writes
    assert client.get("/auth/session").json()["data"]["authenticated"] is True


def test_login_rejects_ingest_only_and_invalid_keys(client):
    assert client.post("/auth/login", json={"key": make_key(["ingest"])}).status_code == 401
    assert client.post("/auth/login", json={"key": "pyro_x_y"}).status_code == 401
    assert client.get("/auth/session").json()["data"]["authenticated"] is False


def test_logout_clears_session(client):
    client.post("/auth/login", json={"key": make_key(["read"])})
    client.post("/auth/logout")
    assert client.get("/runs").status_code == 401


def test_proxy_header_ignored_unless_peer_trusted(monkeypatch):
    headers = {"X-authentik-username": "alice"}
    untrusted = TestClient(app, client=("203.0.113.9", 5000))
    assert untrusted.get("/runs", headers=headers).status_code == 401

    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "127.0.0.1")
    assert untrusted.get("/runs", headers=headers).status_code == 401  # still wrong peer
    trusted = TestClient(app, client=("127.0.0.1", 5000))
    assert trusted.get("/runs", headers=headers).status_code == 200
    assert trusted.get("/runs").status_code == 401  # header absent
    assert trusted.post("/runs", json=RUN, headers=headers).status_code == 401  # read-only


def test_proxy_header_custom_name(monkeypatch):
    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setenv("PYROMETER_PROXY_USER_HEADER", "X-Remote-User")
    c = TestClient(app, client=("10.1.2.3", 1))
    assert c.get("/runs", headers={"X-Remote-User": "bob"}).status_code == 200
    assert c.get("/runs", headers={"X-authentik-username": "bob"}).status_code == 401


def test_websocket_requires_auth(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=bearer(make_key(["ingest"]))):
            pass
    with client.websocket_connect("/ws", headers=bearer(make_key(["read"]))):
        pass


def test_websocket_accepts_session_cookie(client):
    client.post("/auth/login", json={"key": make_key(["read"])})
    with client.websocket_connect("/ws"):
        pass


def test_no_auth_mode_opens_everything(client, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH", "off")
    assert client.get("/runs").status_code == 200
    assert client.post("/runs", json=RUN).status_code == 201
    assert client.get("/auth/session").json()["data"]["auth"] == "off"


@pytest.mark.parametrize("value", ["false", "0", "no", "OFFF", ""])
def test_typos_fail_closed(client, monkeypatch, value):
    monkeypatch.setenv("PYROMETER_AUTH", value)
    assert client.get("/runs").status_code == 401
