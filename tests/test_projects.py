"""Projects, per-project API keys, user accounts, membership and visibility."""

import json

import pytest
from fastapi.testclient import TestClient

from pyrometer.server import auth
from pyrometer.server.app import app

PW = "correct horse battery"
ORIGIN = {"Origin": "http://testserver"}
RUN = {"name": "t", "hyperparams": {}, "tags": []}


@pytest.fixture(autouse=True)
def fresh_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    for var in ("PYROMETER_AUTH", "PYROMETER_TRUSTED_PROXIES", "PYROMETER_BOOTSTRAP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    auth._cache = None
    auth._setup_token = None
    auth._failures.clear()
    return tmp_path / "auth.json"


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def signed_in(username: str, role: str) -> TestClient:
    auth.create_user(username, PW, role)
    c = TestClient(app)
    assert c.post("/auth/login", json={"username": username, "password": PW}).status_code == 200
    return c


@pytest.fixture
def gkey() -> str:
    return auth.create_key("global")[1]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def post_run(client, key, project=None) -> dict:
    body = {**RUN, **({"project": project} if project else {})}
    r = client.post("/runs", json=body, headers=bearer(key))
    return r


def make_private(client, gkey, name) -> str:
    r = client.post("/projects", json={"name": name, "visibility": "private"}, headers=bearer(gkey))
    assert r.status_code == 201
    return r.json()["data"]["key"]


# --- defaults / registration ----------------------------------------------

def test_runs_default_to_public_default_project(client, gkey):
    r = post_run(client, gkey)
    assert r.status_code == 201 and r.json()["data"]["project"] == "default"
    listed = client.get("/runs", headers=bearer(gkey)).json()["data"]
    assert [x["project"] for x in listed] == ["default"]


def test_global_key_auto_registers_unknown_project_as_public(client, gkey):
    assert post_run(client, gkey, "newproj").status_code == 201
    projects = {p["name"]: p for p in client.get("/projects", headers=bearer(gkey)).json()["data"]}
    assert projects["newproj"]["visibility"] == "public" and projects["newproj"]["runs"] == 1


def test_register_public_project_needs_no_new_key(client, gkey):
    r = client.post("/projects", json={"name": "pub", "visibility": "public"}, headers=bearer(gkey))
    assert r.status_code == 201 and r.json()["data"]["key"] is None


def test_register_private_project_returns_its_own_key_once(client, gkey):
    r = client.post("/projects", json={"name": "priv", "visibility": "private"}, headers=bearer(gkey))
    data = r.json()["data"]
    assert data["project"]["visibility"] == "private" and data["key"].startswith("pyro_")
    listed = auth.list_keys()
    assert [k["project"] for k in listed] == [None, "priv"]
    assert "key" not in json.dumps(client.get("/projects", headers=bearer(gkey)).json())


@pytest.mark.parametrize("name", ["", "has space", "-lead", "a/b", "x" * 65])
def test_bad_project_names_rejected(client, gkey, name):
    r = client.post("/projects", json={"name": name, "visibility": "public"}, headers=bearer(gkey))
    assert r.status_code == 400


def test_duplicate_project_conflicts(client, gkey):
    client.post("/projects", json={"name": "dup"}, headers=bearer(gkey))
    assert client.post("/projects", json={"name": "dup"}, headers=bearer(gkey)).status_code == 409


def test_bad_visibility_rejected(client, gkey):
    r = client.post("/projects", json={"name": "v", "visibility": "secret"}, headers=bearer(gkey))
    assert r.status_code == 400


# --- who may register -------------------------------------------------------

def test_project_key_cannot_register_projects(client, gkey):
    pk = make_private(client, gkey, "p1")
    assert client.post("/projects", json={"name": "p2"}, headers=bearer(pk)).status_code == 403


def test_regular_user_cannot_register_projects():
    user = signed_in("bob", "user")
    assert user.post("/projects", json={"name": "p"}, headers=ORIGIN).status_code == 403


def test_admin_can_register_via_dashboard_with_same_origin():
    admin = signed_in("root", "admin")
    assert admin.post("/projects", json={"name": "p"}).status_code == 403  # no Origin
    assert admin.post("/projects", json={"name": "p"}, headers=ORIGIN).status_code == 201


# --- project keys ------------------------------------------------------------

def test_project_key_reads_and_writes_only_its_project(client, gkey):
    pk = make_private(client, gkey, "mine")
    make_private(client, gkey, "theirs")
    other = post_run(client, gkey, "theirs").json()["data"]["run_id"]

    own = post_run(client, pk)  # no project named: the key implies it
    assert own.status_code == 201 and own.json()["data"]["project"] == "mine"
    assert post_run(client, pk, "mine").status_code == 201
    assert post_run(client, pk, "theirs").status_code == 404
    assert post_run(client, pk, "nonexistent").status_code == 404

    visible = client.get("/runs", headers=bearer(pk)).json()["data"]
    assert {r["project"] for r in visible} == {"mine"}
    assert client.get(f"/runs/{other}", headers=bearer(pk)).status_code == 404
    assert client.get(f"/runs/{other}/metrics", headers=bearer(pk)).status_code == 404
    assert client.post(f"/runs/{other}/metrics", json=[], headers=bearer(pk)).status_code == 404
    assert [p["name"] for p in client.get("/projects", headers=bearer(pk)).json()["data"]] == ["mine"]


def test_project_key_cannot_manage_anything(client, gkey):
    pk = make_private(client, gkey, "mine")
    assert client.get("/admin/keys", headers=bearer(pk)).status_code == 403
    assert client.get("/admin/users", headers=bearer(pk)).status_code == 403


def test_global_key_reaches_private_projects(client, gkey):
    make_private(client, gkey, "priv")
    r = post_run(client, gkey, "priv")
    assert r.status_code == 201
    rid = r.json()["data"]["run_id"]
    assert client.get(f"/runs/{rid}", headers=bearer(gkey)).status_code == 200


def test_admin_can_mint_key_bound_to_project(client, gkey):
    admin = signed_in("root", "admin")
    client.post("/projects", json={"name": "pp"}, headers=bearer(gkey))
    r = admin.post("/admin/keys", json={"name": "ci", "project": "pp"}, headers=ORIGIN)
    assert r.status_code == 201 and r.json()["data"]["project"] == "pp"
    key = r.json()["data"]["key"]
    assert post_run(client, key).json()["data"]["project"] == "pp"
    assert admin.post("/admin/keys", json={"name": "x", "project": "nope"}, headers=ORIGIN).status_code == 404


# --- regular users ------------------------------------------------------------

def test_regular_user_sees_public_and_member_projects_only(client, gkey):
    make_private(client, gkey, "secret")
    make_private(client, gkey, "shared")
    pub = post_run(client, gkey, "open").json()["data"]["run_id"]
    sec = post_run(client, gkey, "secret").json()["data"]["run_id"]
    shr = post_run(client, gkey, "shared").json()["data"]["run_id"]
    admin = signed_in("root", "admin")
    user = signed_in("bob", "user")
    assert admin.patch("/admin/projects/shared", json={"members": ["bob"]}, headers=ORIGIN).status_code == 200

    names = {r["run_id"] for r in user.get("/runs").json()["data"]}
    assert pub in names and shr in names and sec not in names
    assert {p["name"] for p in user.get("/projects").json()["data"]} == {"default", "open", "shared"}
    assert user.get(f"/runs/{sec}").status_code == 404
    assert user.get(f"/runs/{sec}/metrics").status_code == 404
    assert user.get(f"/runs/{shr}").status_code == 200
    assert {r["run_id"] for r in admin.get("/runs").json()["data"]} == {pub, sec, shr}


def test_regular_user_cannot_write_or_admin():
    user = signed_in("bob", "user")
    assert user.post("/runs", json=RUN).status_code == 403
    assert user.get("/admin/keys").status_code == 403
    assert user.get("/admin/users").status_code == 403
    assert user.patch("/admin/projects/default", json={"visibility": "private"}, headers=ORIGIN).status_code == 403


def test_visibility_change_and_membership_take_effect_immediately(client, gkey):
    admin = signed_in("root", "admin")
    user = signed_in("bob", "user")
    rid = post_run(client, gkey, "proj").json()["data"]["run_id"]
    assert user.get(f"/runs/{rid}").status_code == 200
    admin.patch("/admin/projects/proj", json={"visibility": "private"}, headers=ORIGIN)
    assert user.get(f"/runs/{rid}").status_code == 404
    admin.patch("/admin/projects/proj", json={"members": ["bob"]}, headers=ORIGIN)
    assert user.get(f"/runs/{rid}").status_code == 200
    assert admin.get("/admin/projects/proj").json()["data"]["members"] == ["bob"]


def test_membership_rejects_unknown_users(client, gkey):
    admin = signed_in("root", "admin")
    post_run(client, gkey, "proj")
    r = admin.patch("/admin/projects/proj", json={"members": ["ghost"]}, headers=ORIGIN)
    assert r.status_code == 400


# --- user management -----------------------------------------------------------

def test_admin_manages_users():
    admin = signed_in("root", "admin")
    r = admin.post("/admin/users", json={"username": "Carol", "password": PW, "role": "user"}, headers=ORIGIN)
    assert r.status_code == 201 and r.json()["data"]["username"] == "carol"
    assert {u["username"] for u in admin.get("/admin/users").json()["data"]} == {"root", "carol"}
    assert "hash" not in json.dumps(admin.get("/admin/users").json())
    dup = admin.post("/admin/users", json={"username": "carol", "password": PW}, headers=ORIGIN)
    assert dup.status_code == 409
    weak = admin.post("/admin/users", json={"username": "dave", "password": "short"}, headers=ORIGIN)
    assert weak.status_code == 400
    badrole = admin.post("/admin/users", json={"username": "dave", "password": PW, "role": "god"}, headers=ORIGIN)
    assert badrole.status_code == 400
    assert TestClient(app).post("/auth/login", json={"username": "carol", "password": PW}).status_code == 200


def test_delete_user_ends_sessions_and_memberships(client, gkey):
    admin = signed_in("root", "admin")
    bob = signed_in("bob", "user")
    post_run(client, gkey, "proj")
    admin.patch("/admin/projects/proj", json={"members": ["bob"]}, headers=ORIGIN)
    assert admin.delete("/admin/users/bob", headers=ORIGIN).status_code == 200
    assert bob.get("/runs").status_code == 401
    assert admin.get("/admin/projects/proj").json()["data"]["members"] == []
    assert admin.delete("/admin/users/bob", headers=ORIGIN).status_code == 404


def test_cannot_delete_self_or_last_admin_or_demote_last_admin():
    admin = signed_in("root", "admin")
    assert admin.delete("/admin/users/root", headers=ORIGIN).status_code == 400
    assert admin.patch("/admin/users/root", json={"role": "user"}, headers=ORIGIN).status_code == 400
    auth.create_user("second", PW, "admin")
    assert admin.patch("/admin/users/root", json={"role": "user"}, headers=ORIGIN).status_code == 200


def test_role_change_applies_to_existing_session():
    admin = signed_in("root", "admin")
    bob = signed_in("bob", "user")
    assert bob.get("/admin/users").status_code == 403
    admin.patch("/admin/users/bob", json={"role": "admin"}, headers=ORIGIN)
    assert bob.get("/admin/users").status_code == 200


def test_change_own_password():
    bob = signed_in("bob", "user")
    assert bob.post("/auth/password", json={"current": "wrong wrong", "new": "brand new pass"}, headers=ORIGIN).status_code == 403
    assert bob.post("/auth/password", json={"current": PW, "new": "tiny"}, headers=ORIGIN).status_code == 400
    assert bob.post("/auth/password", json={"current": PW, "new": "brand new pass"}).status_code == 403  # no Origin
    assert bob.post("/auth/password", json={"current": PW, "new": "brand new pass"}, headers=ORIGIN).status_code == 200
    assert bob.get("/runs").status_code == 200  # this session was replaced, still signed in
    assert TestClient(app).post("/auth/login", json={"username": "bob", "password": PW}).status_code == 401
    assert TestClient(app).post("/auth/login", json={"username": "bob", "password": "brand new pass"}).status_code == 200


def test_username_is_case_insensitive_and_validated():
    auth.create_user("Alice", PW, "user")
    assert TestClient(app).post("/auth/login", json={"username": "ALICE", "password": PW}).status_code == 200
    with pytest.raises(ValueError):
        auth.create_user("alice", PW, "user")
    with pytest.raises(ValueError):
        auth.create_user("bad name!", PW, "user")


def test_session_reports_role():
    bob = signed_in("bob", "user")
    s = bob.get("/auth/session").json()["data"]
    assert s["role"] == "user" and s["user"] == "bob"


# --- WebSocket filtering ---------------------------------------------------------

def test_websocket_only_delivers_events_for_visible_runs(client, gkey):
    make_private(client, gkey, "secret")
    user = signed_in("bob", "user")
    with user.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        post_run(client, gkey, "secret")  # hidden from bob: must not be delivered
        visible = post_run(client, gkey, "open").json()["data"]["run_id"]
        msg = ws.receive_json()
        assert msg["run_id"] == visible


def test_websocket_admin_receives_everything(client, gkey):
    make_private(client, gkey, "secret")
    admin = signed_in("root", "admin")
    with admin.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        hidden = post_run(client, gkey, "secret").json()["data"]["run_id"]
        assert ws.receive_json()["run_id"] == hidden


# --- legacy auth file / local mode -------------------------------------------------

def test_legacy_single_admin_file_is_migrated(fresh_auth):
    fresh_auth.write_text(json.dumps({
        "admin": {"password_hash": auth._hash_password(PW), "updated_at": 1.0},
        "keys": [{"id": "abcd1234", "name": "old", "hash": auth._sha256("s"), "created_at": 1.0}],
        "sessions": [{"hash": "x", "created_at": 1.0, "expires_at": 9e12}],
    }))
    c = TestClient(app)
    assert c.get("/auth/session").json()["data"]["state"] == "login"
    assert c.post("/auth/login", json={"username": "admin", "password": PW}).status_code == 200
    assert [k["project"] for k in auth.list_keys()] == [None]  # old keys become global keys
    old = auth._verify_key(f"pyro_abcd1234_s")
    assert old is not None and old.role == "global"


def test_no_auth_mode_has_no_project_restrictions(client, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH", "off")
    assert client.post("/runs", json={**RUN, "project": "local-proj"}).status_code == 201
    r = client.post("/projects", json={"name": "p", "visibility": "private"})
    assert r.status_code == 201 and r.json()["data"]["key"] is None
    assert len(client.get("/runs").json()["data"]) == 1
