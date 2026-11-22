"""Phase 3 W4: activity sorting, run/project organisation (rename, archive, delete, move, bulk), groups,
pins, downsampling, media files, export, retention, WebSocket re-authorization."""

import base64
import io
import time
import zipfile

import duckdb
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pyrometer.server import auth, db
from pyrometer.server import app as server_app
from pyrometer.server.app import app

PW = "correct horse battery"
ORIGIN = {"Origin": "http://testserver"}
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image-bytes").decode()


@pytest.fixture(autouse=True)
def fresh_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    for var in ("PYROMETER_AUTH", "PYROMETER_TRUSTED_PROXIES", "PYROMETER_BOOTSTRAP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    auth._cache = None
    auth._setup_token = None
    auth._failures.clear()


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
def sdk(gkey) -> TestClient:
    return TestClient(app, headers=bearer(gkey))


@pytest.fixture
def admin() -> TestClient:
    return signed_in("root", "admin")


@pytest.fixture
def bob() -> TestClient:
    return signed_in("bob", "user")


def raw():
    """The DB connection for direct SQL, after background summary/alert threads have finished with it."""
    server_app.drain_background()
    return db.get_conn()


def new_run(sdk, name="r", project=None, **extra) -> str:
    body = {"name": name, **({"project": project} if project else {}), **extra}
    r = sdk.post("/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["run_id"]


def names(resp) -> list[str]:
    return [r["name"] for r in resp.json()["data"]]


def points(step, value, name="loss", ts=None):
    return {"step": step, "timestamp": ts if ts is not None else float(step), "name": name, "value": value}


# --- A: sorting / activity ---------------------------------------------------

def test_runs_default_sort_is_last_activity(sdk, admin):
    a, b = new_run(sdk, "alpha"), new_run(sdk, "beta")
    assert names(admin.get("/runs")) == ["beta", "alpha"]
    time.sleep(0.01)
    assert sdk.post(f"/runs/{a}/metrics", json=[points(1, 1.0)]).status_code == 201
    assert names(admin.get("/runs")) == ["alpha", "beta"]          # ingest bumped alpha
    assert names(admin.get("/runs?sort=created")) == ["beta", "alpha"]
    assert names(admin.get("/runs?sort=name")) == ["alpha", "beta"]
    assert admin.get("/runs?sort=nope").status_code == 400
    # a status / metadata change bumps too
    time.sleep(0.01)
    assert sdk.patch(f"/runs/{b}", json={"notes": "x"}).status_code == 200
    assert names(admin.get("/runs")) == ["beta", "alpha"]
    assert admin.get("/runs").json()["data"][0]["updated_at"] is not None


def test_duplicate_batch_does_not_bump_updated_at(sdk):
    rid = new_run(sdk)
    h = {"X-Pyrometer-Batch": "b1"}
    sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0)], headers=h)
    before = db.db_get_run(rid)["updated_at"]
    time.sleep(0.02)
    assert sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0)], headers=h).json()["data"]["duplicate"]
    assert db.db_get_run(rid)["updated_at"] == before


def test_updated_at_backfilled_from_newest_data(sdk):
    rid = new_run(sdk)
    sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0, ts=5000.0)])
    conn = raw()
    conn.execute("UPDATE runs SET updated_at = NULL, created_at = 100.0, finished_at = NULL, last_heartbeat = 200.0")
    db._backfill_updated_at(conn)
    assert conn.execute("SELECT updated_at FROM runs").fetchone()[0] == 5000.0


def test_projects_sorted_by_last_activity_with_last_activity_field(sdk, admin):
    new_run(sdk, "x", "older")
    new_run(sdk, "y", "newer")
    time.sleep(0.01)
    new_run(sdk, "z", "older")
    rows = admin.get("/projects").json()["data"]
    order = [p["name"] for p in rows]
    assert order.index("older") < order.index("newer")
    assert all("last_activity" in p for p in rows)
    assert next(p for p in rows if p["name"] == "default")["last_activity"] is None
    assert [p["name"] for p in admin.get("/projects?sort=name").json()["data"]] == sorted(order)


def test_api_keys_have_last_used_at_and_sort_by_it(monkeypatch):
    _, k1 = auth.create_key("first")
    _, k2 = auth.create_key("second")
    _, k3 = auth.create_key("never")
    c = TestClient(app)
    c.get("/runs", headers=bearer(k1))
    time.sleep(0.01)
    c.get("/runs", headers=bearer(k2))
    keys = auth.list_keys()
    assert [k["name"] for k in keys] == ["second", "first", "never"]
    assert keys[2]["last_used_at"] is None and keys[0]["last_used_at"] > keys[1]["last_used_at"]


def test_key_last_used_write_is_throttled(monkeypatch):
    key_id, key = auth.create_key("k")
    c = TestClient(app)
    c.get("/runs", headers=bearer(key))
    first = auth.list_keys()[0]["last_used_at"]
    time.sleep(0.02)
    c.get("/runs", headers=bearer(key))
    assert auth.list_keys()[0]["last_used_at"] == first      # within the interval: no rewrite
    monkeypatch.setattr(auth, "KEY_TOUCH_INTERVAL_S", 0.0)
    c.get("/runs", headers=bearer(key))
    assert auth.list_keys()[0]["last_used_at"] > first


def test_alert_rules_listed_by_updated_and_users_alphabetical(admin):
    def mk(name):
        r = admin.post("/admin/alerts", headers=ORIGIN, json={
            "name": name, "url": "https://example.com/hook", "format": "generic", "triggers": [{"kind": "run_failed"}]})
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"]

    a, _b = mk("a"), mk("b")
    assert [r["name"] for r in admin.get("/admin/alerts").json()["data"]] == ["b", "a"]
    time.sleep(0.01)
    assert admin.patch(f"/admin/alerts/{a}", headers=ORIGIN, json={"enabled": False}).status_code == 200
    rules = admin.get("/admin/alerts").json()["data"]
    assert [r["name"] for r in rules] == ["a", "b"] and rules[0]["updated_at"] > rules[1]["updated_at"]
    for u in ("zed", "amy", "mid"):
        auth.create_user(u, PW, "user")
    assert [u["username"] for u in admin.get("/admin/users").json()["data"]] == sorted(
        u["username"] for u in admin.get("/admin/users").json()["data"])


# --- B: rename / tags / notes ------------------------------------------------

def test_patch_org_fields_by_key_and_admin_but_not_user(sdk, admin, bob):
    rid = new_run(sdk, "old", tags=["a"])
    r = sdk.patch(f"/runs/{rid}", json={"name": " new name ", "tags": ["x", " y ", "x", ""], "notes": "n", "group": "g1"})
    assert r.status_code == 200
    run = admin.get(f"/runs/{rid}").json()["data"]
    assert (run["name"], run["tags"], run["notes"], run["group"]) == ("new name", ["x", "y"], "n", "g1")
    assert admin.patch(f"/runs/{rid}", headers=ORIGIN, json={"name": "by admin", "group": ""}).status_code == 200
    run = admin.get(f"/runs/{rid}").json()["data"]
    assert run["name"] == "by admin" and run["group"] is None
    # an admin session cannot change status or hardware fields; needs same-origin
    assert admin.patch(f"/runs/{rid}", headers=ORIGIN, json={"status": "failed"}).status_code == 403
    assert admin.patch(f"/runs/{rid}", headers=ORIGIN, json={"gpu_model": "x"}).status_code == 403
    assert admin.patch(f"/runs/{rid}", json={"name": "csrf"}).status_code == 403
    # regular users never write
    assert bob.patch(f"/runs/{rid}", headers=ORIGIN, json={"name": "nope"}).status_code == 403
    assert sdk.patch(f"/runs/{rid}", json={"name": ""}).status_code == 422
    assert sdk.patch("/runs/does-not-exist", json={"name": "x"}).status_code == 404


def test_project_key_cannot_edit_other_projects_run(sdk):
    rid = new_run(sdk, "r", "team-a")
    r = sdk.post("/projects", json={"name": "team-b", "visibility": "private"})
    other_key = r.json()["data"]["key"]
    assert TestClient(app).patch(f"/runs/{rid}", json={"name": "x"}, headers=bearer(other_key)).status_code == 404


# --- B: archive --------------------------------------------------------------

def test_archive_run_admin_only_and_hidden_by_default(sdk, admin, bob):
    rid = new_run(sdk, "r")
    assert sdk.post(f"/runs/{rid}/archive").status_code == 403         # keys cannot archive
    assert bob.post(f"/runs/{rid}/archive", headers=ORIGIN).status_code == 403
    assert admin.post(f"/runs/{rid}/archive").status_code == 403       # same-origin required
    assert admin.post(f"/runs/{rid}/archive", headers=ORIGIN).status_code == 200
    assert admin.get("/runs").json()["data"] == []
    listed = admin.get("/runs?include_archived=true").json()["data"]
    assert listed[0]["archived"] is True
    assert admin.get(f"/runs/{rid}").json()["data"]["archived"] is True      # direct access still works
    assert sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0)]).status_code == 201  # and so does ingestion
    assert admin.post(f"/runs/{rid}/unarchive", headers=ORIGIN).status_code == 200
    assert len(admin.get("/runs").json()["data"]) == 1
    assert admin.post("/runs/nope-nope/archive", headers=ORIGIN).status_code == 404


def test_archive_project_hides_its_runs_and_default_is_protected(sdk, admin, bob):
    new_run(sdk, "a", "proj")
    new_run(sdk, "b")
    assert bob.post("/admin/projects/proj/archive", headers=ORIGIN).status_code == 403
    assert sdk.post("/admin/projects/proj/archive").status_code == 403
    assert admin.post("/admin/projects/proj/archive", headers=ORIGIN).status_code == 200
    assert names(admin.get("/runs")) == ["b"]
    assert "proj" not in [p["name"] for p in admin.get("/projects").json()["data"]]
    shown = admin.get("/projects?include_archived=true").json()["data"]
    assert next(p for p in shown if p["name"] == "proj")["archived"] is True
    assert names(admin.get("/runs?include_archived=true")) == ["b", "a"]
    assert admin.post("/admin/projects/default/archive", headers=ORIGIN).status_code == 400
    admin.post("/admin/projects/proj/unarchive", headers=ORIGIN)
    assert sorted(names(admin.get("/runs"))) == ["a", "b"]


# --- B: hard delete ----------------------------------------------------------

def all_run_rows(run_id: str) -> dict[str, int]:
    """Row count for ``run_id`` in every table that has a run_id column."""
    conn = raw()
    tables = [r[0] for r in conn.execute(
        "SELECT DISTINCT table_name FROM information_schema.columns WHERE column_name = 'run_id'").fetchall()]
    return {t: n for t in tables if (n := conn.execute(f"SELECT count(*) FROM {t} WHERE run_id = ?", [run_id]).fetchone()[0])}


def populate(sdk, rid: str) -> None:
    h = {"X-Pyrometer-Batch": "batch-1"}
    sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0), points(2, 2.0)], headers=h)
    sdk.post(f"/runs/{rid}/profile", json=[{"step": 1, "timestamp": 1.0, "name": "op", "category": "cpu", "duration_us": 5.0}])
    sdk.post(f"/runs/{rid}/system", json={"timestamp": 1.0, "gpu_utilization": 50.0})
    sdk.post(f"/runs/{rid}/param-stats", json=[{
        "step": 1, "timestamp": 1.0, "param_name": "w", "grad_norm": 1, "grad_min": 0, "grad_max": 1, "grad_mean": 0,
        "weight_norm": 1, "weight_min": 0, "weight_max": 1, "weight_mean": 0}])
    sdk.post(f"/runs/{rid}/events", json={"timestamp": 1.0, "event_type": "x", "message": "m"})
    sdk.post(f"/runs/{rid}/logs", json=[{"timestamp": 1.0, "line": "hello"}])
    sdk.post(f"/runs/{rid}/artifacts", json={"timestamp": 1.0, "path": "/tmp/x"})
    sdk.post(f"/runs/{rid}/media", json={"step": 1, "timestamp": 1.0, "name": "img", "media_type": "image", "data": PNG})
    sdk.post(f"/runs/{rid}/samples", json={"step": 1, "timestamp": 1.0, "samples": [{"prompt": "p", "reward": 1}]})
    db.db_compute_run_summary(rid)
    db.db_pin_run("someone", rid)
    db.db_alert_swap_violating("rule", "t", rid, True)
    db.db_alert_acquire("rule", "dk", 60.0, 100, run_id=rid)


def test_delete_run_removes_every_row_and_media_files(sdk, admin, bob):
    rid, keep = new_run(sdk, "victim"), new_run(sdk, "keeper")
    populate(sdk, rid)
    populate(sdk, keep)
    assert len(all_run_rows(rid)) >= 13                       # data really is in every table
    media_dir = db.media_root() / rid
    assert media_dir.is_dir() and any(media_dir.iterdir())
    assert sdk.delete(f"/runs/{rid}").status_code == 403
    assert bob.delete(f"/runs/{rid}", headers=ORIGIN).status_code == 403
    assert admin.delete(f"/runs/{rid}").status_code == 403    # same-origin
    assert admin.delete(f"/runs/{rid}", headers=ORIGIN).status_code == 200
    assert all_run_rows(rid) == {}
    assert not media_dir.exists()
    assert admin.get(f"/runs/{rid}").status_code == 404
    assert sdk.post(f"/runs/{rid}/metrics", json=[points(3, 1.0)]).status_code == 404   # no orphan rows
    assert all_run_rows(rid) == {}
    assert all_run_rows(keep)                                # neighbours untouched
    assert (db.media_root() / keep).is_dir()
    assert admin.delete(f"/runs/{rid}", headers=ORIGIN).status_code == 404


def test_summary_computed_after_delete_leaves_no_row(sdk):
    rid = new_run(sdk)
    db.db_delete_run(rid)
    assert db.db_compute_run_summary(rid) is None and all_run_rows(rid) == {}


def test_delete_project_refuses_unless_empty_or_cascade(sdk, admin, bob):
    key = sdk.post("/projects", json={"name": "priv", "visibility": "private"}).json()["data"]["key"]
    rid = new_run(sdk, "r", "priv")
    populate(sdk, rid)
    assert bob.delete("/admin/projects/priv", headers=ORIGIN).status_code == 403
    assert sdk.delete("/admin/projects/priv").status_code == 403
    assert admin.delete("/admin/projects/priv", headers=ORIGIN).status_code == 409
    assert admin.get(f"/runs/{rid}").status_code == 200
    r = admin.delete("/admin/projects/priv?cascade=true", headers=ORIGIN)
    assert r.status_code == 200
    out = r.json()["data"]
    assert out["runs_deleted"] == 1 and out["keys_revoked"] == 1
    assert all_run_rows(rid) == {}
    assert db.db_get_project("priv") is None
    assert auth._verify_key(key) is None                       # a re-created project cannot inherit the key
    assert admin.delete("/admin/projects/priv", headers=ORIGIN).status_code == 404
    # empty projects delete without cascade; default never
    sdk.post("/projects", json={"name": "empty", "visibility": "public"})
    assert admin.delete("/admin/projects/empty", headers=ORIGIN).status_code == 200
    assert admin.delete("/admin/projects/default?cascade=true", headers=ORIGIN).status_code == 400


def test_delete_project_removes_scoped_alert_rules(sdk, admin):
    new_run(sdk, "r", "p")
    admin.post("/admin/alerts", headers=ORIGIN, json={
        "name": "scoped", "url": "https://example.com/h", "project": "p", "triggers": [{"kind": "run_failed"}]})
    out = admin.delete("/admin/projects/p?cascade=true", headers=ORIGIN).json()["data"]
    assert out["alert_rules_removed"] == 1 and admin.get("/admin/alerts").json()["data"] == []


# --- B: move -----------------------------------------------------------------

def test_move_run(sdk, admin, bob):
    rid = new_run(sdk, "r", "from")
    sdk.post("/projects", json={"name": "to", "visibility": "public"})
    body = {"project": "to"}
    assert sdk.post(f"/runs/{rid}/move", json=body).status_code == 403
    assert bob.post(f"/runs/{rid}/move", json=body, headers=ORIGIN).status_code == 403
    assert admin.post(f"/runs/{rid}/move", json={"project": "missing"}, headers=ORIGIN).status_code == 404
    assert admin.post(f"/runs/{rid}/move", json=body, headers=ORIGIN).status_code == 409     # still running
    assert admin.post(f"/runs/{rid}/move", json={**body, "force": True}, headers=ORIGIN).status_code == 200
    assert admin.get(f"/runs/{rid}").json()["data"]["project"] == "to"
    sdk.patch(f"/runs/{rid}", json={"status": "completed"})
    admin.post("/admin/projects/from/archive", headers=ORIGIN)
    assert admin.post(f"/runs/{rid}/move", json={"project": "from"}, headers=ORIGIN).status_code == 409  # archived target


def test_moved_run_locks_out_old_project_key(sdk):
    old_key = sdk.post("/projects", json={"name": "old", "visibility": "private"}).json()["data"]["key"]
    rid = new_run(sdk, "r", "old")
    sdk.post("/projects", json={"name": "new", "visibility": "public"})
    sdk.patch(f"/runs/{rid}", json={"status": "completed"})
    db.db_move_run(rid, "new")
    assert TestClient(app).post(f"/runs/{rid}/metrics", json=[points(1, 1.0)], headers=bearer(old_key)).status_code == 404


# --- B: bulk -----------------------------------------------------------------

def test_bulk_tag_archive_move_delete_with_per_item_results(sdk, admin, bob):
    a, b, c = new_run(sdk, "a", tags=["t"]), new_run(sdk, "b"), new_run(sdk, "c")
    sdk.post("/projects", json={"name": "dst", "visibility": "public"})
    for rid in (a, b, c):
        sdk.patch(f"/runs/{rid}", json={"status": "completed"})
    r = admin.post("/runs/bulk", headers=ORIGIN, json={"run_ids": [a, b, "ghost"], "action": "tag", "add_tags": ["new"], "remove_tags": ["t"]})
    out = r.json()["data"]
    assert out["succeeded"] == 2 and out["failed"] == 1
    assert [x["ok"] for x in out["results"]] == [True, True, False] and out["results"][2]["error"] == "Run not found"
    assert admin.get(f"/runs/{a}").json()["data"]["tags"] == ["new"]
    # keys may tag but nothing else; users nothing
    assert sdk.post("/runs/bulk", json={"run_ids": [b], "action": "tag", "add_tags": ["k"]}).json()["data"]["succeeded"] == 1
    assert sdk.post("/runs/bulk", json={"run_ids": [b], "action": "delete"}).status_code == 403
    assert bob.post("/runs/bulk", headers=ORIGIN, json={"run_ids": [b], "action": "tag", "add_tags": ["k"]}).status_code == 403
    assert admin.post("/runs/bulk", headers=ORIGIN, json={"run_ids": [a], "action": "tag"}).status_code == 400
    assert admin.post("/runs/bulk", headers=ORIGIN, json={"run_ids": [a, b], "action": "archive"}).json()["data"]["succeeded"] == 2
    assert names(admin.get("/runs")) == ["c"]
    assert admin.post("/runs/bulk", headers=ORIGIN, json={"run_ids": [a, b], "action": "move", "project": "dst"}).json()["data"]["succeeded"] == 2
    assert admin.post("/runs/bulk", headers=ORIGIN, json={"run_ids": [a], "action": "move", "project": "nope"}).status_code == 404
    done = admin.post("/runs/bulk", headers=ORIGIN, json={"run_ids": [a, b, c], "action": "delete"}).json()["data"]
    assert done["succeeded"] == 3 and all(all_run_rows(x) == {} for x in (a, b, c))


def test_bulk_does_not_reveal_or_touch_invisible_runs_to_keys(sdk):
    key_b = sdk.post("/projects", json={"name": "pb", "visibility": "private"}).json()["data"]["key"]
    rid = new_run(sdk, "secret", "other")
    out = TestClient(app).post("/runs/bulk", headers=bearer(key_b),
                               json={"run_ids": [rid], "action": "tag", "add_tags": ["x"]}).json()["data"]
    assert out["failed"] == 1 and db.db_get_run(rid)["tags"] == []


# --- C: groups ---------------------------------------------------------------

def test_groups_filter_and_list(sdk, admin, bob):
    new_run(sdk, "a", "p", group="sweep-1")
    new_run(sdk, "b", "p", group=" sweep-1 ")
    time.sleep(0.01)
    new_run(sdk, "c", "p", group="sweep-2")
    new_run(sdk, "d", "p")
    assert sorted(names(admin.get("/runs?group=sweep-1"))) == ["a", "b"]
    groups = admin.get("/projects/p/groups").json()["data"]
    assert [(g["group"], g["runs"]) for g in groups] == [("sweep-2", 1), ("sweep-1", 2)]
    assert bob.get("/projects/p/groups").status_code == 200
    assert bob.get("/projects/nope/groups").status_code == 404
    sdk.post("/projects", json={"name": "hidden", "visibility": "private"})
    assert bob.get("/projects/hidden/groups").status_code == 404
    assert sdk.post("/runs", json={"name": "x", "group": "g" * 200}).status_code == 422


def test_sdk_init_sends_group(monkeypatch):
    import pyrometer
    from pyrometer._transport import Spool

    created: list[dict] = []
    real = Spool.write_meta

    def capture(self, **kw):
        if kw.get("create"):
            created.append(kw["create"])
        return real(self, **kw)

    monkeypatch.setattr(Spool, "write_meta", capture)
    monkeypatch.delenv("PYROMETER_GROUP", raising=False)
    for kwargs, env, expect in (
        ({"group": "abc"}, None, "abc"),
        ({}, "from-env", "from-env"),
        ({"group": "explicit"}, "from-env", "explicit"),
        ({}, None, None),
    ):
        if env:
            monkeypatch.setenv("PYROMETER_GROUP", env)
        else:
            monkeypatch.delenv("PYROMETER_GROUP", raising=False)
        run = pyrometer.init(name="x", offline=True, **kwargs)
        run.finish()
        assert created[-1]["group"] == expect


# --- D: pins -----------------------------------------------------------------

def test_pins_are_per_user_and_keys_cannot_pin(sdk, admin, bob):
    rid = new_run(sdk, "r")
    other = new_run(sdk, "o")
    assert sdk.put(f"/runs/{rid}/pin").status_code == 403
    assert sdk.get("/pins").status_code == 403
    assert bob.put(f"/runs/{rid}/pin").status_code == 403                    # same-origin
    assert bob.put(f"/runs/{rid}/pin", headers=ORIGIN).status_code == 200
    assert bob.put(f"/runs/{other}/pin", headers=ORIGIN).status_code == 200
    assert [r["name"] for r in bob.get("/pins").json()["data"]] == ["o", "r"]
    assert {r["name"]: r["pinned"] for r in bob.get("/runs").json()["data"]} == {"r": True, "o": True}
    assert admin.get(f"/runs/{rid}").json()["data"]["pinned"] is False
    assert bob.get(f"/runs/{rid}").json()["data"]["pinned"] is True
    assert admin.get("/pins").json()["data"] == []
    assert bob.delete(f"/runs/{rid}/pin", headers=ORIGIN).status_code == 200
    assert [r["name"] for r in bob.get("/pins").json()["data"]] == ["o"]
    assert bob.put("/runs/ghost-run/pin", headers=ORIGIN).status_code == 404
    admin.delete(f"/runs/{other}", headers=ORIGIN)
    assert bob.get("/pins").json()["data"] == []                              # delete clears pins


def test_cannot_pin_invisible_run(sdk, bob):
    sdk.post("/projects", json={"name": "hid", "visibility": "private"})
    rid = new_run(sdk, "s", "hid")
    assert bob.put(f"/runs/{rid}/pin", headers=ORIGIN).status_code == 404


# --- E: downsampling ---------------------------------------------------------

def test_metrics_downsample_keeps_spikes_and_is_full_by_default(sdk, admin):
    rid = new_run(sdk)
    pts = [points(i, 1.0) for i in range(2000)] + [points(i, 0.5, name="acc") for i in range(10)]
    pts[777] = points(777, 99.0)
    pts[1500] = points(1500, -99.0)
    sdk.post(f"/runs/{rid}/metrics", json=pts)
    full = admin.get(f"/runs/{rid}/metrics").json()
    assert len(full["data"]) == 2010 and "meta" not in full
    r = admin.get(f"/runs/{rid}/metrics?max_points=100").json()
    loss = [p for p in r["data"] if p["name"] == "loss"]
    assert len(loss) <= 100 and {p["value"] for p in loss} >= {99.0, -99.0}
    assert len([p for p in r["data"] if p["name"] == "acc"]) == 10            # short series untouched
    assert [p["step"] for p in r["data"]] == sorted(p["step"] for p in r["data"])
    assert loss[0]["step"] == 0 and loss[-1]["step"] == 1999                  # ends preserved
    assert r["meta"]["downsampled"] and r["meta"]["total"] == 2010 and r["meta"]["returned"] == len(r["data"])
    z = admin.get(f"/runs/{rid}/metrics?name=loss&step_min=700&step_max=800&max_points=20").json()
    assert all(700 <= p["step"] <= 800 for p in z["data"]) and 99.0 in {p["value"] for p in z["data"]}
    t = admin.get(f"/runs/{rid}/metrics?name=loss&t_min=10&t_max=19").json()
    assert [p["step"] for p in t["data"]] == list(range(10, 20)) and not t["meta"]["downsampled"]
    assert admin.get(f"/runs/{rid}/metrics?max_points=0").status_code == 400
    assert admin.get(f"/runs/{rid}/metrics?step_min=5&step_max=1").status_code == 400


def test_system_and_param_stats_downsample(sdk, admin):
    rid = new_run(sdk)
    for i in range(300):
        for gpu in (0, 1):
            sdk.post(f"/runs/{rid}/system", json={
                "timestamp": 1000.0 + i, "gpu_index": gpu, "gpu_utilization": 100.0 if (i == 123 and gpu == 1) else 10.0,
                "memory_used": 5, "temperature": 50.0})
    stat = lambda i, g: {"step": i, "timestamp": float(i), "param_name": "w", "grad_norm": 50.0 if i == 40 else 1.0,  # noqa: E731
                         "grad_min": 0, "grad_max": 1, "grad_mean": 0, "weight_norm": 1, "weight_min": 0,
                         "weight_max": 1, "weight_mean": 0, "grad_histogram": [1.0, 2.0]}
    sdk.post(f"/runs/{rid}/param-stats", json=[stat(i, 0) for i in range(200)])
    sysr = admin.get(f"/runs/{rid}/system?max_points=60").json()
    assert len(sysr["data"]) < 600 and sysr["meta"]["total"] == 600
    assert any(p["gpu_utilization"] == 100.0 and p["gpu_index"] == 1 for p in sysr["data"])
    assert {p["gpu_index"] for p in sysr["data"]} == {0, 1}
    assert len(admin.get(f"/runs/{rid}/system").json()["data"]) == 600
    win = admin.get(f"/runs/{rid}/system?t_min=1000&t_max=1009").json()["data"]
    assert len(win) == 20
    ps = admin.get(f"/runs/{rid}/param-stats?max_points=40").json()
    assert len(ps["data"]) <= 40 and 40 in {p["step"] for p in ps["data"]} and ps["data"][0]["grad_histogram"] == [1.0, 2.0]
    assert len(admin.get(f"/runs/{rid}/param-stats").json()["data"]) == 200
    z = admin.get(f"/runs/{rid}/param-stats?name=w&step_min=10&step_max=19").json()["data"]
    assert [p["step"] for p in z] == list(range(10, 20))


# --- E: media files ----------------------------------------------------------

def test_media_lives_in_files_and_is_served_with_visibility(sdk, admin, bob):
    key = sdk.post("/projects", json={"name": "priv", "visibility": "private"}).json()["data"]["key"]
    rid = new_run(sdk, "r", "priv")
    sdk.post(f"/runs/{rid}/media", json={"step": 3, "timestamp": 1.0, "name": "gen/img 1", "media_type": "image", "data": PNG})
    sdk.post(f"/runs/{rid}/media", json={"step": 3, "timestamp": 1.0, "name": "hist", "media_type": "histogram", "data": "[1,2]"})
    row = raw().execute("SELECT data, path FROM media WHERE name = 'gen/img 1'").fetchone()
    assert row[0] is None and row[1].startswith(rid) and (db.media_root() / row[1]).is_file()
    items = {m["name"]: m for m in admin.get(f"/runs/{rid}/media").json()["data"]}
    assert items["gen/img 1"]["data"] == PNG and items["gen/img 1"]["url"]
    assert items["hist"]["data"] == "[1,2]" and items["hist"]["url"] is None
    lean = {m["name"]: m for m in admin.get(f"/runs/{rid}/media?inline=false").json()["data"]}
    assert lean["gen/img 1"]["data"] is None and lean["gen/img 1"]["url"] == items["gen/img 1"]["url"]
    f = admin.get(items["gen/img 1"]["url"])
    assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content == base64.b64decode(PNG)
    assert admin.get(f"/runs/{rid}/media/file?name=nope&step=3").status_code == 404
    assert bob.get(items["gen/img 1"]["url"]).status_code == 404               # private project: invisible
    assert TestClient(app).get(items["gen/img 1"]["url"]).status_code == 401
    assert TestClient(app).get(items["gen/img 1"]["url"], headers=bearer(key)).status_code == 200
    # re-logging replaces the file's content
    other = base64.b64encode(b"second").decode()
    sdk.post(f"/runs/{rid}/media", json={"step": 3, "timestamp": 2.0, "name": "gen/img 1", "media_type": "image", "data": other})
    assert admin.get(items["gen/img 1"]["url"]).content == b"second"


def test_legacy_inline_media_is_migrated_to_files(sdk):
    rid = new_run(sdk)
    conn = raw()
    conn.execute("INSERT INTO media (run_id, step, timestamp, name, media_type, data) VALUES (?, 1, 1.0, 'a', 'image', ?)", [rid, PNG])
    conn.execute("INSERT INTO media (run_id, step, timestamp, name, media_type, data) VALUES (?, 1, 1.0, 'bad', 'figure', 'not base64!')", [rid])
    conn.execute("INSERT INTO media (run_id, step, timestamp, name, media_type, data) VALUES (?, 1, 1.0, 'h', 'histogram', '[1]')", [rid])
    db._migrate_media_to_files(conn)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT name, data, path FROM media").fetchall()}
    assert rows["a"][0] is None and (db.media_root() / rows["a"][1]).read_bytes() == base64.b64decode(PNG)
    assert rows["bad"] == ("not base64!", None) and rows["h"] == ("[1]", None)
    db._migrate_media_to_files(conn)                                         # idempotent


# --- E: export ---------------------------------------------------------------

def test_export_run_and_project_zip(sdk, admin, bob, tmp_path):
    rid = new_run(sdk, "exp run", "p1")
    populate(sdk, rid)
    r = admin.get(f"/runs/{rid}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    nm = zf.namelist()
    assert {"runs.parquet", "metrics.parquet", "system_snapshots.parquet", "samples.parquet"} <= set(nm)
    assert any(n.startswith("media/") for n in nm)
    zf.extractall(tmp_path / "x")
    con = duckdb.connect()
    assert con.execute(f"SELECT count(*) FROM '{tmp_path / 'x' / 'metrics.parquet'}'").fetchone()[0] == 2
    assert con.execute(f"SELECT name FROM '{tmp_path / 'x' / 'runs.parquet'}'").fetchone()[0] == "exp run"
    other = new_run(sdk, "o", "p2")
    sdk.post(f"/runs/{other}/metrics", json=[points(1, 1.0)])
    pz = admin.get("/projects/p1/export")
    assert pz.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(pz.content))
    zf.extractall(tmp_path / "p")
    assert con.execute(f"SELECT count(*) FROM '{tmp_path / 'p' / 'runs.parquet'}'").fetchone()[0] == 1
    assert admin.get("/projects/ghost/export").status_code == 404
    assert admin.get("/runs/ghost-run/export").status_code == 404
    # visibility: a user cannot export a private project / its runs
    sdk.post("/projects", json={"name": "hid", "visibility": "private"})
    hid = new_run(sdk, "h", "hid")
    assert bob.get("/projects/hid/export").status_code == 404
    assert bob.get(f"/runs/{hid}/export").status_code == 404
    assert bob.get("/projects/p1/export").status_code == 200
    assert TestClient(app).get(f"/runs/{rid}/export").status_code == 401


# --- E: retention ------------------------------------------------------------

def test_retention_policy_deletes_only_chosen_tables(sdk, admin, bob):
    rid = new_run(sdk, "r", "keepers")
    now = time.time()
    old, fresh = now - 40 * 86400, now - 1 * 86400
    sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0, ts=old), points(2, 1.0, ts=fresh)])
    for ts in (old, fresh):
        sdk.post(f"/runs/{rid}/system", json={"timestamp": ts, "gpu_utilization": 1.0})
        sdk.post(f"/runs/{rid}/logs", json=[{"timestamp": ts, "line": "l"}])
    sdk.post(f"/runs/{rid}/media", json={"step": 1, "timestamp": old, "name": "i", "media_type": "image", "data": PNG})
    assert db.db_apply_retention() == {}                                      # off by default: nothing happens
    assert bob.patch("/admin/projects/keepers", headers=ORIGIN, json={"retention": {}}).status_code == 403
    bad = admin.patch("/admin/projects/keepers", headers=ORIGIN, json={"retention": {"older_than_days": {"runs": 5}}})
    assert bad.status_code == 400
    assert admin.patch("/admin/projects/keepers", headers=ORIGIN, json={"retention": {"older_than_days": {"system_snapshots": 0}}}).status_code == 400
    ok = admin.patch("/admin/projects/keepers", headers=ORIGIN,
                     json={"retention": {"older_than_days": {"system_snapshots": 30, "media": 30}}})
    assert ok.json()["data"]["retention"] == {"older_than_days": {"system_snapshots": 30, "media": 30}}
    assert sdk.post("/admin/projects/keepers/retention/run").status_code == 403
    out = admin.post("/admin/projects/keepers/retention/run", headers=ORIGIN).json()["data"]["deleted"]
    assert out == {"system_snapshots": 1, "media": 1}
    assert len(db.db_get_system_snapshots(rid)) == 1
    assert len(db.db_get_metrics(rid)) == 2 and len(db.db_get_logs(rid)) == 2   # not named -> untouched
    assert db.db_get_media(rid) == [] and not any((db.media_root() / rid).glob("*"))
    assert admin.patch("/admin/projects/keepers", headers=ORIGIN, json={"retention": None}).json()["data"]["retention"] is None
    assert db.db_apply_retention() == {}


def test_retention_can_explicitly_include_metrics(sdk):
    rid = new_run(sdk, "r")
    sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0, ts=1.0), points(2, 1.0, ts=time.time())])
    db.db_set_project_retention("default", {"older_than_days": {"metrics": 1}})
    assert db.db_apply_retention() == {"default": {"metrics": 1}}
    assert db.db_get_run(rid) is not None


def test_seen_batches_are_not_pruned_by_age(sdk):
    rid = new_run(sdk)
    conn = raw()
    conn.execute("INSERT INTO seen_batches VALUES (?, 'old', 1.0)", [rid])
    db._init_schema(conn)
    assert conn.execute("SELECT count(*) FROM seen_batches WHERE batch_id = 'old'").fetchone()[0] == 1
    # a very late replay of that batch is still recognised
    assert sdk.post(f"/runs/{rid}/metrics", json=[points(1, 1.0)], headers={"X-Pyrometer-Batch": "old"}).json()["data"]["duplicate"]


# --- F: routed items ---------------------------------------------------------

def test_idempotent_create_with_different_project_is_409(sdk):
    body = {"run_id": "abcdefgh-1234", "name": "r", "project": "one"}
    assert sdk.post("/runs", json=body).status_code == 201
    assert sdk.post("/runs", json=body).status_code == 201                      # same project: idempotent
    assert sdk.post("/runs", json={**body, "project": "two"}).status_code == 409
    assert db.db_get_project("two") is None                                     # and no side-effect project
    assert sdk.post("/runs", json={"run_id": "abcdefgh-1234", "name": "r"}).status_code == 201   # implicit project: fine


def test_last_write_wins_compares_client_timestamps(sdk):
    rid = new_run(sdk)
    sdk.post(f"/runs/{rid}/metrics", json=[points(5, 10.0, ts=100.0)])
    sdk.post(f"/runs/{rid}/metrics", json=[points(5, 1.0, ts=50.0)])            # very late replay of an older value
    assert [p["value"] for p in db.db_get_metrics(rid)] == [10.0]
    sdk.post(f"/runs/{rid}/metrics", json=[points(5, 20.0, ts=100.0)])          # equal timestamp: replaces
    sdk.post(f"/runs/{rid}/metrics", json=[points(5, 30.0, ts=200.0)])
    assert [p["value"] for p in db.db_get_metrics(rid)] == [30.0]
    # inside one batch the newest timestamp wins, not the last position
    sdk.post(f"/runs/{rid}/metrics", json=[points(6, 2.0, ts=9.0), points(6, 1.0, ts=3.0)])
    assert [p["value"] for p in db.db_get_metrics(rid, step_min=6)] == [2.0]
    # samples and media too
    s = lambda t, text: {"step": 1, "timestamp": t, "samples": [{"x": text}]}  # noqa: E731
    sdk.post(f"/runs/{rid}/samples", json=s(10.0, "new"))
    sdk.post(f"/runs/{rid}/samples", json=s(5.0, "stale"))
    assert db.db_get_samples(rid, "samples", 1)["items"][0]["sample"]["x"] == "new"
    m = lambda t, d: {"step": 1, "timestamp": t, "name": "i", "media_type": "histogram", "data": d}  # noqa: E731
    sdk.post(f"/runs/{rid}/media", json=m(10.0, "new"))
    sdk.post(f"/runs/{rid}/media", json=m(5.0, "stale"))
    assert db.db_get_media(rid)[0]["data"] == "new"


def test_one_time_repair_of_legacy_duplicates(sdk):
    rid = new_run(sdk)
    conn = raw()
    conn.execute("DELETE FROM schema_meta")
    for ts, v in ((1.0, 1.0), (3.0, 3.0), (2.0, 2.0)):
        conn.execute("INSERT INTO metrics VALUES (?, 7, ?, 'loss', ?)", [rid, ts, v])
    conn.execute("INSERT INTO metrics VALUES (?, 8, 1.0, 'loss', 8.0)", [rid])
    for ts, d in ((1.0, "old"), (2.0, "newest")):
        conn.execute("INSERT INTO media (run_id, step, timestamp, name, media_type, data) VALUES (?, 1, ?, 'h', 'histogram', ?)", [rid, ts, d])
    for ts, g in ((5.0, 1.0), (9.0, 9.0)):
        conn.execute("INSERT INTO param_stats VALUES (?, 1, ?, 'w', ?, 0, 0, 0, 0, 0, 0, 0, '[]', '[]')", [rid, ts, g])
    db._repair_duplicates(conn)
    assert sorted((r["step"], r["value"]) for r in db.db_get_metrics(rid)) == [(7, 3.0), (8, 8.0)]
    assert [m["data"] for m in db.db_get_media(rid)] == ["newest"]
    assert [p["grad_norm"] for p in db.db_get_param_stats(rid)] == [9.0]
    # recorded: a second pass is skipped even if duplicates reappear
    conn.execute("INSERT INTO metrics VALUES (?, 8, 0.0, 'loss', 0.0)", [rid])
    db._repair_duplicates(conn)
    assert len(db.db_get_metrics(rid, step_min=8)) == 2


def test_last_broadcast_map_is_bounded_and_forgets_deleted_runs():
    m = server_app._WsManager()
    for i in range(3000):
        m._throttled((f"run{i}", "metrics"), 0)      # throttle_ms=0: never throttled, entries still recorded
    assert len(m._last_broadcast) <= server_app._THROTTLE_MAX_ENTRIES + 1
    m._last_broadcast[("gone", "metrics")] = time.monotonic()
    m.forget_run("gone")
    assert ("gone", "metrics") not in m._last_broadcast


# --- F: WebSocket re-authorization & events --------------------------------------------

def test_websocket_closes_when_key_is_revoked(sdk):
    key_id, key = auth.create_key("watcher")
    c = TestClient(app)
    with c.websocket_connect("/ws", headers=bearer(key)) as ws:
        rid = new_run(sdk, "first")
        assert ws.receive_json()["run_id"] == rid
        auth.revoke_key(key_id)
        new_run(sdk, "second")                                                  # triggers a broadcast
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_websocket_closes_when_session_ends(sdk):
    signed_in("root", "admin")
    bob = signed_in("bob", "user")
    with bob.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        auth.delete_user("bob")
        new_run(sdk, "x")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_idle_websocket_is_rechecked(sdk, monkeypatch):
    monkeypatch.setenv("PYROMETER_WS_RECHECK", "0.1")
    key_id, key = auth.create_key("idle")
    with TestClient(app).websocket_connect("/ws", headers=bearer(key)) as ws:
        auth.revoke_key(key_id)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()                                                   # closed with no broadcast at all


def test_websocket_events_for_delete_archive_move(sdk, admin, bob):
    sdk.post("/projects", json={"name": "priv", "visibility": "private"})
    sdk.post("/projects", json={"name": "pub", "visibility": "public"})
    pub, priv = new_run(sdk, "a", "pub"), new_run(sdk, "b", "priv")
    with bob.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws, admin.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws_admin:
        admin.post(f"/runs/{pub}/archive", headers=ORIGIN)
        assert ws.receive_json() == {"type": "run_updated", "run_id": pub}
        assert ws_admin.receive_json()["type"] == "run_updated"
        # bob cannot see priv; moving priv -> pub looks like a new run to him, and admin sees a move
        sdk.patch(f"/runs/{priv}", json={"status": "completed"})
        admin.post(f"/runs/{priv}/move", json={"project": "pub"}, headers=ORIGIN)
        msgs_admin = [ws_admin.receive_json() for _ in range(2)]
        assert {"type": "run_moved", "run_id": priv, "project": "pub"} in msgs_admin
        # bob heard nothing about priv until the move: first message is run_created
        assert ws.receive_json() == {"type": "run_created", "run_id": priv}
        admin.delete(f"/runs/{priv}", headers=ORIGIN)
        assert ws.receive_json() == {"type": "run_deleted", "run_id": priv, "project": "pub"}
        # deleting a private project: only people who could see it hear about it
        admin.delete("/admin/projects/priv", headers=ORIGIN)
        sdk.post("/runs", json={"name": "marker", "project": "pub"})
        assert ws.receive_json()["type"] == "run_created"                       # bob skipped project_deleted for priv


def test_websocket_project_delete_event_reaches_readers(sdk, admin, bob):
    sdk.post("/projects", json={"name": "pub2", "visibility": "public"})
    with bob.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        admin.delete("/admin/projects/pub2", headers=ORIGIN)
        assert ws.receive_json() == {"type": "project_deleted", "project": "pub2"}
