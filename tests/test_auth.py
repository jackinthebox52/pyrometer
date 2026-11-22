"""Auth behaviour: first-run setup, admin login, API keys, proxy identity, roles, CSRF, WebSocket."""

import json
import stat

import pytest

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from pyrometer.server import auth  # noqa: E402
from pyrometer.server.app import app  # noqa: E402

RUN = {"name": "t", "hyperparams": {}, "tags": []}
PASSWORD = "correct horse battery"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    for var in ("PYROMETER_AUTH", "PYROMETER_TRUSTED_PROXIES", "PYROMETER_BOOTSTRAP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    auth._cache = None
    auth._setup_token = None
    auth._failures.clear()
    return tmp_path / "auth.json"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(client) -> TestClient:
    """A client logged in as admin (browser session)."""
    auth.create_user("admin", PASSWORD, "admin")
    assert client.post("/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
    return client


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def make_key() -> str:
    return auth.create_key("test")[1]


# --- defaults -------------------------------------------------------------

def test_default_requires_auth(client):
    assert client.get("/runs").status_code == 401
    assert client.post("/runs", json=RUN).status_code == 401
    assert client.get("/admin/keys").status_code == 401
    assert client.get("/runs").headers["www-authenticate"] == "Bearer"


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_docs_not_served_when_auth_required(client):
    assert client.get("/openapi.json").status_code in (401, 404)
    assert client.get("/docs").status_code in (401, 404)


# --- API keys -------------------------------------------------------------

def test_api_key_reads_and_writes_data(client):
    key = make_key()
    assert client.post("/runs", json=RUN, headers=bearer(key)).status_code == 201
    assert client.get("/runs", headers=bearer(key)).status_code == 200


def test_api_key_cannot_manage_keys(client):
    key = make_key()
    assert client.get("/admin/keys", headers=bearer(key)).status_code == 403
    r = client.post("/admin/keys", json={"name": "evil"}, headers={**bearer(key), **ORIGIN})
    assert r.status_code == 403
    assert len(auth.list_keys()) == 1


@pytest.mark.parametrize("header", ["Bearer nope", "Bearer pyro_deadbeef_x", "Basic abc", "Bearer ", "bearer"])
def test_bad_credentials_rejected(client, header):
    assert client.get("/runs", headers={"Authorization": header}).status_code == 401


def test_wrong_secret_for_real_id_rejected(client):
    key_id, key = auth.create_key("t")
    assert client.get("/runs", headers=bearer(f"pyro_{key_id}_{'A' * 43}")).status_code == 401
    assert client.get("/runs", headers=bearer(key)).status_code == 200


def test_revoked_key_rejected(client):
    key_id, key = auth.create_key("t")
    assert client.get("/runs", headers=bearer(key)).status_code == 200
    assert auth.revoke_key(key_id)
    assert client.get("/runs", headers=bearer(key)).status_code == 401


def test_bad_bearer_does_not_fall_through_to_session(admin):
    assert admin.get("/runs").status_code == 200
    assert admin.get("/runs", headers=bearer("pyro_bad_bad")).status_code == 401


# --- storage --------------------------------------------------------------

def test_auth_file_has_only_hashes_and_private_mode(fresh_state):
    _, key = auth.create_key("t")
    auth.create_user("admin", PASSWORD, "admin")
    token = auth.login("admin", PASSWORD)
    raw = fresh_state.read_text()
    for secret in (key.split("_", 2)[2], key, PASSWORD, token):
        assert secret not in raw
    assert stat.S_IMODE(fresh_state.stat().st_mode) == 0o600
    assert "hash" not in json.dumps(auth.list_keys())


def test_password_hash_roundtrip():
    stored = auth._hash_password("hunter2 hunter2")
    assert stored.startswith("scrypt$")
    assert auth._verify_password("hunter2 hunter2", stored)
    assert not auth._verify_password("hunter2 hunter3", stored)
    assert not auth._verify_password("x", "garbage")


def test_set_password_enforces_length_and_ends_only_that_users_sessions():
    with pytest.raises(ValueError):
        auth.create_user("admin", "short", "admin")
    auth.create_user("admin", PASSWORD, "admin")
    auth.create_user("bob", PASSWORD, "user")
    admin_token, bob_token = auth.login("admin", PASSWORD), auth.login("bob", PASSWORD)
    auth.set_password("admin", "a different long password")
    assert not auth._session_valid(admin_token)
    assert auth._session_valid(bob_token)


# --- first-run setup ------------------------------------------------------

def test_session_state_progression(client):
    assert client.get("/auth/session").json()["data"]["state"] == "setup_required"
    auth.create_user("admin", PASSWORD, "admin")
    s = client.get("/auth/session").json()["data"]
    assert s["state"] == "login" and s["authenticated"] is False


def test_setup_with_token_creates_admin_and_logs_in(client):
    token = auth.setup_token()
    assert token
    r = client.post("/auth/setup", json={"token": token, "username": "admin", "password": PASSWORD})
    assert r.status_code == 200
    assert client.get("/auth/session").json()["data"]["authenticated"] is True
    assert client.get("/runs").status_code == 200


def test_setup_rejects_wrong_token_and_weak_password(client):
    token = auth.setup_token()
    assert client.post("/auth/setup", json={"token": "wrong", "username": "admin", "password": PASSWORD}).status_code == 401
    assert client.post("/auth/setup", json={"token": token, "username": "admin", "password": "short"}).status_code == 400
    assert not auth.admin_configured()


def test_setup_token_is_single_use(client):
    token = auth.setup_token()
    assert client.post("/auth/setup", json={"token": token, "username": "admin", "password": PASSWORD}).status_code == 200
    again = client.post("/auth/setup", json={"token": token, "username": "mallory", "password": "attacker password!"})
    assert again.status_code == 401
    assert auth.setup_token() is None
    assert auth.login("admin", PASSWORD)  # original password still valid


def test_pinned_bootstrap_token(client, monkeypatch):
    monkeypatch.setenv("PYROMETER_BOOTSTRAP_TOKEN", "from-secret-manager")
    r = client.post("/auth/setup", json={"token": "from-secret-manager", "username": "admin", "password": PASSWORD})
    assert r.status_code == 200


def test_setup_unavailable_once_admin_set_via_cli(client):
    token = auth.setup_token()
    auth.create_user("admin", PASSWORD, "admin")  # e.g. `pyrometer admin set-password` while the server runs
    assert client.post("/auth/setup", json={"token": token, "username": "admin", "password": "another long password"}).status_code == 401


# --- admin login / session ------------------------------------------------

def test_login_sets_httponly_strict_cookie(client):
    auth.create_user("admin", PASSWORD, "admin")
    r = client.post("/auth/login", json={"username": "admin", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/auth/session").json()["data"]["authenticated"] is True


def test_wrong_password_rejected(client):
    auth.create_user("admin", PASSWORD, "admin")
    assert client.post("/auth/login", json={"username": "admin", "password": "nope nope nope"}).status_code == 401
    assert client.get("/runs").status_code == 401


def test_login_throttled_after_repeated_failures(client):
    auth.create_user("admin", PASSWORD, "admin")
    for _ in range(auth._MAX_FAILURES):
        assert client.post("/auth/login", json={"username": "admin", "password": "wrong wrong wrong"}).status_code == 401
    assert client.post("/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 429


def test_logout_invalidates_session_server_side(client):
    auth.create_user("admin", PASSWORD, "admin")
    client.post("/auth/login", json={"username": "admin", "password": PASSWORD})
    stolen = client.cookies.get(auth.COOKIE_NAME)
    client.post("/auth/logout")
    assert client.get("/runs").status_code == 401
    other = TestClient(app)
    assert other.get("/runs", cookies={auth.COOKIE_NAME: stolen}).status_code == 401


def test_session_cookie_is_read_only_for_run_data(admin):
    assert admin.get("/runs").status_code == 200
    assert admin.post("/runs", json=RUN).status_code == 403


# --- admin key management (+ CSRF origin check) ---------------------------

def test_admin_can_create_list_and_revoke_keys(admin):
    r = admin.post("/admin/keys", json={"name": "my-project"}, headers=ORIGIN)
    assert r.status_code == 201
    created = r.json()["data"]
    assert created["key"].startswith("pyro_")
    listed = admin.get("/admin/keys").json()["data"]
    assert [k["name"] for k in listed] == ["my-project"] and "key" not in listed[0]
    assert TestClient(app).get("/runs", headers=bearer(created["key"])).status_code == 200
    assert admin.delete(f"/admin/keys/{created['id']}", headers=ORIGIN).status_code == 200
    assert admin.delete(f"/admin/keys/{created['id']}", headers=ORIGIN).status_code == 404
    assert TestClient(app).get("/runs", headers=bearer(created["key"])).status_code == 401


def test_admin_state_change_needs_same_origin(admin):
    assert admin.post("/admin/keys", json={"name": "x"}).status_code == 403  # no Origin
    evil = {"Origin": "http://evil.example"}
    assert admin.post("/admin/keys", json={"name": "x"}, headers=evil).status_code == 403
    assert auth.list_keys() == []


def test_admin_key_name_validated(admin):
    assert admin.post("/admin/keys", json={"name": "  "}, headers=ORIGIN).status_code == 400
    assert admin.post("/admin/keys", json={"name": "x" * 65}, headers=ORIGIN).status_code == 400


# --- trusted proxy (authentik) --------------------------------------------

def test_proxy_header_ignored_unless_peer_trusted(monkeypatch):
    headers = {"X-authentik-username": "alice"}
    untrusted = TestClient(app, client=("203.0.113.9", 5000))
    assert untrusted.get("/runs", headers=headers).status_code == 401

    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "127.0.0.1")
    assert untrusted.get("/runs", headers=headers).status_code == 401  # still the wrong peer
    trusted = TestClient(app, client=("127.0.0.1", 5000))
    assert trusted.get("/runs", headers=headers).status_code == 200
    assert trusted.get("/runs").status_code == 401  # header absent


def test_proxy_user_is_admin_but_still_cannot_write_run_data(monkeypatch):
    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "127.0.0.1")
    c = TestClient(app, client=("127.0.0.1", 5000))
    h = {"X-authentik-username": "alice"}
    s = c.get("/auth/session", headers=h).json()["data"]
    assert s["state"] == "ready" and s["user"] == "alice"  # no setup screen needed
    assert c.post("/runs", json=RUN, headers=h).status_code == 403
    assert c.post("/admin/keys", json={"name": "k"}, headers={**h, **ORIGIN}).status_code == 201
    assert c.post("/admin/keys", json={"name": "k"}, headers=h).status_code == 403  # CSRF check applies


def test_forwarded_host_honoured_only_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "127.0.0.1")
    h = {"X-authentik-username": "a", "Origin": "https://pyro.example.com", "X-Forwarded-Host": "pyro.example.com"}
    assert TestClient(app, client=("127.0.0.1", 1)).post("/admin/keys", json={"name": "k"}, headers=h).status_code == 201


def test_proxy_header_custom_name(monkeypatch):
    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setenv("PYROMETER_PROXY_USER_HEADER", "X-Remote-User")
    c = TestClient(app, client=("10.1.2.3", 1))
    assert c.get("/runs", headers={"X-Remote-User": "bob"}).status_code == 200
    assert c.get("/runs", headers={"X-authentik-username": "bob"}).status_code == 401


# --- WebSocket ------------------------------------------------------------

def test_websocket_requires_auth(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    with client.websocket_connect("/ws", headers=bearer(make_key())):
        pass


def test_websocket_accepts_admin_session(admin):
    with admin.websocket_connect("/ws", headers=ORIGIN):
        pass


def _ws_rejected(client, headers) -> bool:
    try:
        with client.websocket_connect("/ws", headers=headers):
            return False
    except WebSocketDisconnect as e:
        assert e.code == 1008
        return True


def test_websocket_cross_origin_session_rejected(admin):
    assert _ws_rejected(admin, {"Origin": "https://evil.example.com"})
    assert _ws_rejected(admin, {})  # a browser-credential socket must carry a same-origin Origin
    assert not _ws_rejected(admin, ORIGIN)


def test_websocket_api_key_without_origin_ok_but_wrong_key_not(client):
    assert not _ws_rejected(client, bearer(make_key()))
    assert _ws_rejected(client, bearer("pyro_bad_key"))


def test_websocket_origin_behind_trusted_proxy(monkeypatch):
    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "127.0.0.1")
    proxied = {"X-authentik-username": "alice", "Origin": "https://pyro.example.com", "X-Forwarded-Host": "pyro.example.com"}
    trusted = TestClient(app, client=("127.0.0.1", 1))
    assert not _ws_rejected(trusted, proxied)
    assert _ws_rejected(trusted, {**proxied, "Origin": "https://evil.example.com"})
    assert _ws_rejected(trusted, {"X-authentik-username": "alice"})  # proxy identity, no Origin
    # X-Forwarded-Host is ignored from an untrusted peer (and the identity header is too)
    untrusted = TestClient(app, client=("203.0.113.9", 1))
    assert _ws_rejected(untrusted, proxied)


# --- login throttle isolation ---------------------------------------------

def _login(ip, user, password="wrong wrong wrong", headers=None):
    c = TestClient(app, client=(ip, 1000))
    return c.post("/auth/login", json={"username": user, "password": password}, headers=headers or {}).status_code


def test_lockout_is_per_user_and_ip():
    auth.create_user("alice", PASSWORD, "user")
    auth.create_user("bob", PASSWORD, "user")
    for _ in range(auth._MAX_FAILURES):
        assert _login("203.0.113.1", "alice") == 401
    assert _login("203.0.113.1", "alice", PASSWORD) == 429           # locked for this pair
    assert _login("203.0.113.2", "alice", PASSWORD) == 200           # same user, other IP
    assert _login("203.0.113.1", "bob", PASSWORD) == 200             # other user, same IP


def test_success_clears_only_that_pair():
    auth.create_user("alice", PASSWORD, "user")
    for _ in range(3):
        _login("203.0.113.1", "alice")
    assert _login("203.0.113.1", "alice", PASSWORD) == 200
    assert ("u", "alice", "203.0.113.1") not in auth._failures
    assert len(auth._failures[("ip", "203.0.113.1")]) == 3


def test_per_ip_cap_blocks_username_spraying(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_FAILURES_PER_IP", 12)
    auth.create_user("alice", PASSWORD, "user")
    for i in range(12):
        assert _login("203.0.113.1", f"ghost{i}") == 401   # each pair is far below its own cap
    assert _login("203.0.113.1", "alice", PASSWORD) == 429
    assert _login("203.0.113.2", "alice", PASSWORD) == 200


def test_throttle_uses_forwarded_for_only_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("PYROMETER_TRUSTED_PROXIES", "127.0.0.1")
    auth.create_user("alice", PASSWORD, "user")

    def via_proxy(client_addr, password="wrong wrong wrong"):
        return _login("127.0.0.1", "alice", password, {"X-Forwarded-For": f"198.51.100.250, {client_addr}"})

    for _ in range(auth._MAX_FAILURES):
        assert via_proxy("203.0.113.1") == 401
    assert via_proxy("203.0.113.1", PASSWORD) == 429
    assert via_proxy("203.0.113.2", PASSWORD) == 200   # other real client behind the same proxy
    # an untrusted peer cannot pick its bucket with a forged header
    for _ in range(auth._MAX_FAILURES):
        assert _login("192.0.2.7", "alice", headers={"X-Forwarded-For": "203.0.113.77"}) == 401
    assert ("u", "alice", "192.0.2.7") in auth._failures
    assert ("u", "alice", "203.0.113.77") not in auth._failures


def test_throttle_prunes_old_entries_and_bounds_memory(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_THROTTLE_KEYS", 50)
    auth._record_failure("old", "203.0.113.1")
    for key in list(auth._failures):
        auth._failures[key] = [auth.time.monotonic() - auth._FAILURE_WINDOW - 1]
    auth._check_throttle("anyone", "203.0.113.9")
    assert not auth._failures                     # expired buckets are dropped
    for i in range(500):
        auth._record_failure(f"u{i}", f"198.51.100.{i % 250}")
    assert len(auth._failures) <= 50


def test_password_change_failures_use_same_throttle(admin):
    for _ in range(auth._MAX_FAILURES):
        r = admin.post("/auth/password", json={"current": "bad bad bad", "new": "another long one"}, headers=ORIGIN)
        assert r.status_code == 403
    assert admin.post("/auth/password", json={"current": PASSWORD, "new": "another long one"}, headers=ORIGIN).status_code == 429
    assert _login("203.0.113.5", "admin", PASSWORD) == 200  # other IP unaffected


# --- local mode / fail-closed ---------------------------------------------

def test_no_auth_mode_opens_everything(client, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH", "off")
    assert client.get("/runs").status_code == 200
    assert client.post("/runs", json=RUN).status_code == 201
    assert client.get("/auth/session").json()["data"]["auth"] == "off"


@pytest.mark.parametrize("value", ["false", "0", "no", "OFFF", ""])
def test_typos_fail_closed(client, monkeypatch, value):
    monkeypatch.setenv("PYROMETER_AUTH", value)
    assert client.get("/runs").status_code == 401
