"""FastAPI application — route handlers only. All DB access goes through db.py."""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pyrometer.server import alerts, auth
from pyrometer.server.auth import COOKIE_NAME, RateLimited, auth_enabled, require_auth
from pyrometer.server.db import (
    RUN_SORTS,
    clamp_max_points,
    db_apply_retention,
    db_delete_project,
    db_delete_run,
    db_export_media_files,
    db_export_parquet,
    db_get_media_file,
    db_is_pinned,
    db_list_groups,
    db_list_run_ids,
    db_move_run,
    db_pin_run,
    db_pinned_run_ids,
    db_set_project_archived,
    db_set_project_retention,
    db_set_run_archived,
    db_touch_run,
    db_unpin_run,
    db_claim_batch,
    db_compute_run_summary,
    db_create_project,
    db_create_run,
    get_lock,
    db_get_artifacts,
    db_get_events,
    db_get_logs,
    db_get_members,
    db_get_project,
    db_get_run_project,
    db_resume_run,
    db_release_batch,
    db_get_media,
    db_get_media_names,
    db_get_samples,
    db_get_sample_steps,
    db_get_sample_extremes,
    db_insert_samples,
    db_get_metric_names,
    db_get_metrics,
    db_get_param_stat_names,
    db_get_param_stats,
    db_get_profile_events,
    db_get_profile_steps,
    db_get_run,
    db_get_run_summary,
    db_get_system_snapshots,
    db_gpu_count,
    db_insert_artifact,
    db_insert_event,
    db_insert_logs,
    db_insert_media,
    db_insert_metrics,
    db_insert_param_stats,
    db_insert_profile_events,
    db_insert_system_snapshot,
    db_list_projects,
    db_list_runs,
    db_set_members,
    db_set_project_visibility,
    db_update_run,
    RUN_INFO_FIELDS,
    db_recent_metrics,
    db_heartbeat,
    db_get_run_status,
    db_mark_stale_runs,
    db_create_alert_rule,
    db_delete_alert_rule,
    db_get_alert_rule,
    db_list_alert_rules,
    db_update_alert_rule,
    set_status_hook,
    TERMINAL_STATUSES,
)

_log = logging.getLogger("pyrometer.server")

# Every path that turns a run crashed (sweeper, lazy checks on read) goes through db_mark_stale_runs.
set_status_hook(alerts.notify_run_status)


# Run summaries are computed off the request path, on one managed worker that shutdown (and the test
# fixtures) drain, so no summary thread can touch the database after it is closed.
_summary_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyrometer-summary")
_summary_lock = threading.Lock()


def _compute_summary_safely(run_id: str) -> None:
    try:
        db_compute_run_summary(run_id)
    except Exception:
        _log.exception("run summary failed for %s", run_id)


def _queue_summary(run_id: str) -> None:
    with _summary_lock:
        _summary_pool.submit(_compute_summary_safely, run_id)


def drain_background() -> None:
    """Wait for queued run summaries and alert jobs to finish (server shutdown, test teardown)."""
    global _summary_pool
    with _summary_lock:
        pool, _summary_pool = _summary_pool, ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyrometer-summary")
    pool.shutdown(wait=True)
    alerts.flush()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    if not auth_enabled():
        _log.warning("AUTH DISABLED (PYROMETER_AUTH=off) — local mode only, do not expose this server")
    else:
        token = auth.setup_token()
        if token is not None:
            if os.environ.get("PYROMETER_BOOTSTRAP_TOKEN"):
                _log.warning("Pyrometer is not set up. Open the dashboard and enter PYROMETER_BOOTSTRAP_TOKEN.")
            else:
                _log.warning(
                    "Pyrometer is not set up. Open the dashboard and enter this one-time setup token: %s "
                    "(or run `pyrometer admin set-password` on the host)",
                    token,
                )
    tasks = [asyncio.create_task(_crash_sweeper()), asyncio.create_task(_retention_sweeper())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.to_thread(drain_background)


async def _crash_sweeper() -> None:
    """Periodically mark runs that stopped sending heartbeats as ``crashed`` and tell dashboards."""
    while True:
        await asyncio.sleep(30)
        try:
            for run_id in db_mark_stale_runs():
                _queue_summary(run_id)
                await _ws.broadcast({"type": "run_updated", "run_id": run_id})
        except Exception:
            _log.exception("crash sweep failed")


async def _retention_sweeper() -> None:
    """Apply per-project retention policies (off unless an admin set one) shortly after start, then daily."""
    await asyncio.sleep(60)
    while True:
        try:
            deleted = await asyncio.to_thread(db_apply_retention)
            if deleted:
                _log.info("retention sweep removed rows: %s", deleted)
        except Exception:
            _log.exception("retention sweep failed")
        await asyncio.sleep(float(os.environ.get("PYROMETER_RETENTION_INTERVAL", 86400)))


# Every API route requires auth by default (see auth.require_auth). Interactive docs expose the
# schema without credentials, so they are only served in local no-auth mode.
_local = not auth_enabled()
app = FastAPI(
    title="Pyrometer API",
    version="0.1.0",
    lifespan=_lifespan,
    dependencies=[Depends(require_auth)],
    docs_url="/docs" if _local else None,
    redoc_url="/redoc" if _local else None,
    openapi_url="/openapi.json" if _local else None,
)


# ---------------------------------------------------------------------------
# WebSocket connection manager — push events to connected dashboard clients
# ---------------------------------------------------------------------------

_THROTTLE_MAX_ENTRIES = 1024


def _ws_recheck_s() -> float:
    """How often an idle WebSocket re-validates its credential (``PYROMETER_WS_RECHECK``, default 30 s)."""
    try:
        return max(0.05, float(os.environ.get("PYROMETER_WS_RECHECK", "30")))
    except ValueError:
        return 30.0


class _WsManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._last_broadcast: dict[tuple[str, str], float] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)

    def forget_run(self, run_id: str) -> None:
        """Drop throttle state of a deleted run."""
        for key in [k for k in self._last_broadcast if k[0] == run_id]:
            del self._last_broadcast[key]

    def _throttled(self, key: tuple[str, str], throttle_ms: int) -> bool:
        now = time.monotonic()
        if (now - self._last_broadcast.get(key, 0.0)) * 1000 < throttle_ms:
            return True
        if len(self._last_broadcast) >= _THROTTLE_MAX_ENTRIES:  # bound the map: stale entries no longer throttle
            horizon = max(throttle_ms, 1000) / 1000
            self._last_broadcast = {k: t for k, t in self._last_broadcast.items() if now - t < horizon}
            if len(self._last_broadcast) >= _THROTTLE_MAX_ENTRIES:  # still full of fresh entries: hard cap
                newest = sorted(self._last_broadcast.items(), key=lambda kv: kv[1])[_THROTTLE_MAX_ENTRIES // 2:]
                self._last_broadcast = dict(newest)
        self._last_broadcast[key] = now
        return False

    async def _deliver(self, choose: Callable[[auth.Principal | None], str | None]) -> None:
        """Send each connection what ``choose(principal)`` returns for it (``None`` = nothing).

        The credential is re-validated on every delivery: a revoked key, ended session, deleted user or
        changed role stops (closes) the socket instead of keeping a stale principal alive.
        """
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                principal = None
                if auth_enabled():
                    principal = auth.authenticate(ws)
                    if principal is None:
                        dead.append(ws)
                        await ws.close(code=1008)
                        continue
                    ws.state.principal = principal
                text = choose(principal)
                if text is not None:
                    await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast(
        self,
        payload: dict,
        throttle_key: tuple[str, str] | None = None,
        throttle_ms: int = 500,
        project: str | None = None,
        readable: Callable[[auth.Principal | None], bool] | None = None,
    ) -> None:
        """Tell connected dashboards, but only those whose user may see the subject.

        The subject's project is looked up from ``payload["run_id"]`` unless ``project`` is given (events
        about a run or project that no longer exists); ``readable`` overrides the check entirely (a
        deleted project's former visibility).
        """
        if throttle_key and self._throttled(throttle_key, throttle_ms):
            return
        if not self._connections:
            return
        text = json.dumps(payload)
        run_id = payload.get("run_id")
        if project is None and run_id:
            project = db_get_run_project(run_id)

        def choose(principal: auth.Principal | None) -> str | None:
            if readable is not None:
                return text if readable(principal) else None
            if project is not None and not auth.can_read_project(principal, project):
                return None
            return text

        await self._deliver(choose)

    async def broadcast_move(self, run_id: str, old: str, new: str) -> None:
        """A run changed project: readers of both see ``run_moved``; of only the old project, the run is
        gone (``run_deleted``); of only the new one, it appeared (``run_created``). No project name leaks."""
        if not self._connections:
            return

        def choose(principal: auth.Principal | None) -> str | None:
            was, now = auth.can_read_project(principal, old), auth.can_read_project(principal, new)
            if was and now:
                return json.dumps({"type": "run_moved", "run_id": run_id, "project": new})
            if was:
                return json.dumps({"type": "run_deleted", "run_id": run_id, "reason": "moved"})
            if now:
                return json.dumps({"type": "run_created", "run_id": run_id})
            return None

        await self._deliver(choose)


_ws = _WsManager()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await _ws.connect(websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=_ws_recheck_s())  # keep-alive; ignore client messages
            except asyncio.TimeoutError:
                # Idle: still make sure the key/session behind this socket has not been revoked.
                if auth_enabled() and auth.authenticate(websocket) is None:
                    await websocket.close(code=1008)
                    break
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws.disconnect(websocket)


# No CORS middleware: the dashboard is served same-origin, and a wildcard CORS policy would let
# any website a user visits read this API from their browser.


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def ok(data) -> dict:
    return {"data": data, "error": None}


def ok_series(rows: list, total: int, max_points: int | None, windowed: bool) -> dict:
    """Envelope for the downsample-capable reads. With ``max_points`` or a window requested, a top-level
    ``meta`` says what happened (``total`` rows in the window, ``returned``, ``downsampled``)."""
    out = ok(rows)
    if windowed:
        out["meta"] = {
            "total": total, "returned": len(rows), "downsampled": len(rows) < total,
            "max_points": clamp_max_points(max_points) if max_points is not None else None,
        }
    return out


def _check_window(max_points: int | None, *bounds: float | None) -> None:
    if max_points is not None and max_points < 1:
        err("max_points must be at least 1", 400)
    lo, hi = bounds[0::2], bounds[1::2]
    if any(a is not None and b is not None and a > b for a, b in zip(lo, hi)):
        err("range minimum is greater than its maximum", 400)


def err(message: str, status_code: int = 400):
    raise HTTPException(status_code=status_code, detail={"data": None, "error": message})


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CreateRunRequest(BaseModel):
    # Set by the SDK so run creation is idempotent (offline sync, retries); the server mints one if absent.
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    created_at: float | None = None
    name: str | None = None
    hyperparams: dict = {}
    tags: list[str] = []
    notes: str | None = None
    git_hash: str | None = None
    git_dirty: bool = False
    gpu_model: str | None = None
    gpu_memory_total: int | None = None
    cuda_version: str | None = None
    pytorch_version: str | None = None
    python_version: str | None = None
    hostname: str | None = None
    project: str | None = None
    group: str | None = Field(default=None, max_length=128)  # sweep / experiment group (init(group=...))
    # Resume an existing run: "must" (404 if unknown) or "auto" (create it when unknown). Needs run_id.
    resume: Literal["must", "auto"] | None = None


class UpdateRunRequest(BaseModel):
    status: str | None = None
    notes: str | None = None
    # Organisation fields: also editable by dashboard admins (see docs/auth.md).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=50)
    group: str | None = Field(default=None, max_length=128)  # "" or null clears it
    # Hardware/environment fields the SDK fills in after init (probing torch takes seconds).
    git_hash: str | None = None
    git_dirty: bool | None = None
    gpu_model: str | None = None
    gpu_memory_total: int | None = None
    cuda_version: str | None = None
    pytorch_version: str | None = None
    python_version: str | None = None
    hostname: str | None = None


class MetricEntry(BaseModel):
    step: int
    timestamp: float
    name: str
    value: float


class ProfileEvent(BaseModel):
    step: int
    timestamp: float
    name: str
    category: str
    duration_us: float
    cuda_time_us: float = 0.0
    cpu_time_us: float = 0.0
    input_shapes: list = []
    memory_bytes: int | None = None
    device_id: int = 0


class SystemSnapshot(BaseModel):
    timestamp: float
    gpu_utilization: float | None = None
    memory_used: int | None = None
    memory_total: int | None = None
    temperature: float | None = None
    power_draw: float | None = None
    fan_speed: float | None = None
    sm_clock_mhz: int | None = None
    memory_clock_mhz: int | None = None
    process_memory_used: int | None = None
    process_exclusive: bool | None = None
    attributed_utilization: float | None = None
    attributed_power: float | None = None
    gpu_index: int | None = None  # CUDA ordinal within the run's visible GPUs; None = GPU 0


class ParamStatEntry(BaseModel):
    step: int
    timestamp: float
    param_name: str
    grad_norm: float
    grad_min: float
    grad_max: float
    grad_mean: float
    weight_norm: float
    weight_min: float
    weight_max: float
    weight_mean: float
    grad_histogram: list[float] = []
    weight_histogram: list[float] = []


class EventEntry(BaseModel):
    timestamp: float
    step: int | None = None
    event_type: str
    severity: str = "info"
    message: str
    metadata: dict = {}


class LogEntry(BaseModel):
    timestamp: float
    stream: str = "stdout"
    line: str
    step: int | None = None


class ArtifactEntry(BaseModel):
    timestamp: float
    step: int | None = None
    artifact_type: str = "checkpoint"
    path: str
    size_bytes: int | None = None
    metadata: dict = {}


class MediaEntry(BaseModel):
    step: int
    timestamp: float
    name: str
    media_type: str  # "image" | "histogram" | "figure"
    data: str        # base64 PNG for images/figures, JSON string for histograms


MAX_SAMPLES_PER_CALL = 500
MAX_SAMPLE_BYTES = 64 * 1024        # serialized JSON per sample
MAX_SAMPLES_CALL_BYTES = 4 * 1024 * 1024


class SamplesEntry(BaseModel):
    name: str = "samples"
    step: int
    timestamp: float
    samples: list[dict]
    # "replace" (default) overwrites what is stored for (run, name, step); "append" adds to it.
    mode: Literal["append", "replace"] = "replace"


# ---------------------------------------------------------------------------
# Routes — health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return ok({"status": "ok"})


# ---------------------------------------------------------------------------
# Routes — auth (public; they bootstrap the dashboard's session)
# ---------------------------------------------------------------------------

class SetupRequest(BaseModel):
    token: str
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current: str
    new: str


class CreateKeyRequest(BaseModel):
    name: str
    project: str | None = None  # bind the key to one project


class AlertTrigger(BaseModel):
    kind: str
    min_severity: str | None = None     # kind "event"
    metric: str | None = None           # kind "metric_threshold"
    op: str | None = None
    threshold: float | None = None


class AlertRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(max_length=2048)
    format: str = "generic"
    project: str | None = None          # None = every project
    enabled: bool = True
    triggers: list[AlertTrigger] | None = Field(default=None, max_length=20)
    # Single-trigger shorthand from before rules had several triggers; used when ``triggers`` is omitted.
    kind: str | None = None
    min_severity: str | None = None
    metric: str | None = None
    op: str | None = None
    threshold: float | None = None


class UpdateAlertRuleRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, max_length=2048)
    format: str | None = None
    project: str | None = None
    enabled: bool | None = None
    triggers: list[AlertTrigger] | None = Field(default=None, max_length=20)


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    password: str | None = None
    role: str | None = None


class CreateProjectRequest(BaseModel):
    name: str
    visibility: str = "public"


class UpdateProjectRequest(BaseModel):
    visibility: str | None = None
    members: list[str] | None = None
    # {"older_than_days": {"system_snapshots": 30, ...}}; null or {} switches retention off.
    retention: dict | None = None


class MoveRunRequest(BaseModel):
    project: str
    force: bool = False  # also move a run that is still running


class BulkRunsRequest(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["tag", "archive", "unarchive", "move", "delete"]
    add_tags: list[str] = Field(default=[], max_length=50)       # action "tag"
    remove_tags: list[str] = Field(default=[], max_length=50)    # action "tag"
    project: str | None = None                                   # action "move"
    force: bool = False                                          # action "move": running runs too


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=auth.is_https(request),
        path="/",
    )


@app.get("/auth/session")
def auth_session(request: Request):
    """What the dashboard needs to pick a screen: ``setup_required`` | ``login`` | ``ready``."""
    if not auth_enabled():
        return ok({"auth": "off", "state": "ready", "authenticated": True, "user": None, "role": "admin"})
    principal = auth.authenticate(request)
    if principal is not None:
        return ok({
            "auth": "required", "state": "ready", "authenticated": True,
            "user": principal.name, "role": principal.role,
        })
    state = "login" if auth.admin_configured() else "setup_required"
    return ok({"auth": "required", "state": state, "authenticated": False, "user": None, "role": None})


@app.post("/auth/setup")
def auth_setup(req: SetupRequest, request: Request, response: Response):
    """First run: exchange the one-time setup token (from the server log) for the first admin account."""
    try:
        username = auth.claim_setup(req.token.strip(), req.username, req.password)
        session = auth.login(username, req.password)
    except RateLimited:
        err("Too many failed attempts; try again later", 429)
    except PermissionError as e:
        err(str(e), 401)
    except ValueError as e:
        err(str(e), 400)
    _set_session_cookie(response, request, session)
    return ok({"user": username})


@app.post("/auth/login")
def auth_login(req: LoginRequest, request: Request, response: Response):
    """Username/password login; sets an HttpOnly, SameSite=Strict, read-only session cookie."""
    try:
        session = auth.login(req.username, req.password)
    except RateLimited:
        err("Too many failed attempts; try again later", 429)
    except PermissionError as e:
        err(str(e), 401)
    _set_session_cookie(response, request, session)
    return ok({"user": req.username.strip().lower()})


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return ok({"logged_out": True})


@app.post("/auth/password")
def auth_change_password(req: ChangePasswordRequest, request: Request, response: Response):
    """Change your own password. Ends all your sessions; this response signs you back in."""
    principal = _principal(request)
    if principal is None:
        err("Not available in local no-auth mode", 400)
    try:
        auth.change_own_password(principal.name, req.current, req.new)
        session = auth.login(principal.name, req.new)
    except RateLimited:
        err("Too many failed attempts; try again later", 429)
    except PermissionError as e:
        err(str(e), 403)
    except ValueError as e:
        err(str(e), 400)
    _set_session_cookie(response, request, session)
    return ok({"changed": True})


# ---------------------------------------------------------------------------
# Routes — projects
# ---------------------------------------------------------------------------

def _principal(request: Request):
    """The authenticated caller, or ``None`` when auth is off."""
    return getattr(request.state, "principal", None)


def _validated_project_name(name: str) -> str:
    name = name.strip()
    if not auth.PROJECT_RE.match(name):
        err("Project name must be 1-64 characters: letters, digits, '.', '_' or '-'", 400)
    return name


@app.get("/projects")
def list_projects(request: Request, sort: str = "activity", include_archived: bool = False):
    """Projects the caller can see (all of them for admins and global keys), most recently active first
    (``last_activity``); ``sort=name`` for a-z. Archived projects need ``include_archived=true``."""
    if sort not in ("activity", "name"):
        err("sort must be 'activity' or 'name'", 400)
    return ok(db_list_projects(auth.visible_projects(_principal(request)), sort, include_archived))


def _readable_project(request: Request, name: str) -> dict:
    """The project if it exists and the caller may read it; otherwise a 404 that reveals nothing."""
    project = db_get_project(name)
    if project is None or not auth.can_read_project(_principal(request), name):
        err("No such project", 404)
    return project


@app.get("/projects/{name}/groups")
def list_project_groups(name: str, request: Request, include_archived: bool = False):
    """Run groups (``init(group=...)``) in a project, most recently active first."""
    _readable_project(request, name)
    return ok(db_list_groups(name, include_archived))


@app.get("/projects/{name}/export")
def export_project(name: str, request: Request):
    """Zip of Parquet files (every table, all runs of the project incl. archived) plus media files."""
    _readable_project(request, name)
    return _export_response(name, project=name, run_ids=db_list_run_ids(name))


@app.post("/projects", status_code=201)
def create_project(req: CreateProjectRequest, request: Request):
    """Register a project. A private project also gets its own API key, returned once."""
    name = _validated_project_name(req.name)
    if req.visibility not in ("public", "private"):
        err("visibility must be 'public' or 'private'", 400)
    principal = _principal(request)
    project = db_create_project(name, req.visibility, principal.name if principal else "local")
    if project is None:
        err(f"Project {name!r} already exists", 409)
    key = None
    if req.visibility == "private" and auth_enabled():
        _, key = auth.create_key(f"project:{name}", project=name)
    return ok({"project": project, "key": key})


# ---------------------------------------------------------------------------
# Routes — admin: users, projects, API keys (admin only; see auth.require_auth)
# ---------------------------------------------------------------------------

@app.get("/admin/keys")
def admin_list_keys():
    return ok(auth.list_keys())


@app.post("/admin/keys", status_code=201)
def admin_create_key(req: CreateKeyRequest):
    name = req.name.strip()
    if not 1 <= len(name) <= 64:
        err("Name must be 1-64 characters", 400)
    if req.project is not None and db_get_project(req.project) is None:
        err("No such project", 404)
    key_id, key = auth.create_key(name, project=req.project)
    return ok({"id": key_id, "name": name, "project": req.project, "key": key})  # the only time the key is shown


@app.delete("/admin/keys/{key_id}")
def admin_revoke_key(key_id: str):
    if not auth.revoke_key(key_id):
        err("No such key", 404)
    return ok({"revoked": key_id})


@app.get("/admin/users")
def admin_list_users():
    return ok(auth.list_users())


@app.post("/admin/users", status_code=201)
def admin_create_user(req: CreateUserRequest):
    try:
        auth.create_user(req.username, req.password, req.role)
    except ValueError as e:
        err(str(e), 409 if "already exists" in str(e) else 400)
    return ok({"username": req.username.strip().lower(), "role": req.role})


@app.patch("/admin/users/{username}")
def admin_update_user(username: str, req: UpdateUserRequest):
    if auth.get_user(username) is None:
        err("No such user", 404)
    try:
        if req.role is not None:
            auth.update_user_role(username, req.role)
        if req.password is not None:
            auth.set_password(username, req.password)
    except ValueError as e:
        err(str(e), 400)
    return ok({"updated": username})


@app.delete("/admin/users/{username}")
def admin_delete_user(username: str, request: Request):
    me = _principal(request)
    if me is not None and me.name == username.strip().lower():
        err("You cannot delete your own account", 400)
    try:
        deleted = auth.delete_user(username)
    except ValueError as e:
        err(str(e), 400)
    if not deleted:
        err("No such user", 404)
    return ok({"deleted": username})


@app.get("/admin/projects/{name}")
def admin_get_project(name: str):
    project = db_get_project(name)
    if project is None:
        err("No such project", 404)
    return ok({**project, "members": db_get_members(name)})


@app.patch("/admin/projects/{name}")
def admin_update_project(name: str, req: UpdateProjectRequest):
    if db_get_project(name) is None:
        err("No such project", 404)
    if req.visibility is not None:
        if req.visibility not in ("public", "private"):
            err("visibility must be 'public' or 'private'", 400)
        db_set_project_visibility(name, req.visibility)
    if req.members is not None:
        unknown = [u for u in req.members if auth.get_user(u) is None]
        if unknown:
            err(f"Unknown users: {', '.join(unknown)}", 400)
        db_set_members(name, [u.strip().lower() for u in req.members])
    if "retention" in req.model_fields_set:
        try:
            db_set_project_retention(name, req.retention)
        except ValueError as e:
            err(str(e), 400)
    return ok({**db_get_project(name), "members": db_get_members(name)})


@app.post("/admin/projects/{name}/archive")
async def admin_archive_project(name: str):
    """Hide a project and its runs from lists (data stays; ``include_archived=true`` shows it)."""
    return await _set_project_archived(name, True)


@app.post("/admin/projects/{name}/unarchive")
async def admin_unarchive_project(name: str):
    return await _set_project_archived(name, False)


async def _set_project_archived(name: str, archived: bool):
    if name == "default":
        err("The default project cannot be archived", 400)
    if not db_set_project_archived(name, archived):
        err("No such project", 404)
    await _ws.broadcast({"type": "project_updated", "project": name}, project=name)
    return ok(db_get_project(name))


@app.delete("/admin/projects/{name}")
async def admin_delete_project(name: str, cascade: bool = False):
    """Hard-delete a project. Refused (409) while it still has runs unless ``cascade=true``, which
    deletes every run with all of its data and media files. Also removes the membership list, alert rules
    scoped to the project and the API keys bound to it."""
    if name == "default":
        err("The default project cannot be deleted", 400)
    project = db_get_project(name)
    if project is None:
        err("No such project", 404)
    run_ids = db_list_run_ids(name)
    if run_ids and not cascade:
        err(f"Project {name!r} has {len(run_ids)} runs; pass cascade=true to delete them with it", 409)
    visibility, members = project["visibility"], db_get_members(name)
    with get_lock():
        for run_id in run_ids:
            db_delete_run(run_id)
        removed = db_delete_project(name)
    for run_id in run_ids:
        _ws.forget_run(run_id)
    keys = auth.revoke_project_keys(name) if auth_enabled() else 0
    await _ws.broadcast(
        {"type": "project_deleted", "project": name},
        readable=lambda p: auth.can_read_snapshot(p, name, visibility, members),
    )
    return ok({"deleted": name, "runs_deleted": len(run_ids), "keys_revoked": keys,
               "alert_rules_removed": removed["alert_rules"]})


@app.post("/admin/projects/{name}/retention/run")
async def admin_run_retention(name: str):
    """Apply the project's retention policy now (the daily sweeper does the same). Returns rows deleted per table."""
    if db_get_project(name) is None:
        err("No such project", 404)
    deleted = await asyncio.to_thread(db_apply_retention, name)
    return ok({"deleted": deleted.get(name, {})})


# ---------------------------------------------------------------------------
# Routes — admin: webhook alert rules (URLs are secrets: always masked in responses)
# ---------------------------------------------------------------------------

def _check_alert_rule(rule: dict) -> None:
    """Validate a complete rule dict, answering 400/404 through ``err``."""
    if rule["format"] not in alerts.FORMATS:
        err(f"format must be one of {', '.join(alerts.FORMATS)}", 400)
    bad_url = alerts.validate_url(rule["url"])
    if bad_url:
        err(bad_url, 400)
    if rule.get("project") is not None and db_get_project(rule["project"]) is None:
        err("No such project", 404)
    if not rule["triggers"]:
        err("A rule needs at least one trigger", 400)
    for t in rule["triggers"]:
        if t["kind"] not in alerts.KINDS:
            err(f"kind must be one of {', '.join(alerts.KINDS)}", 400)
        if t["kind"] == "event" and (t.get("min_severity") or "error") not in alerts.SEVERITIES:
            err(f"min_severity must be one of {', '.join(alerts.SEVERITIES)}", 400)
        if t["kind"] == "metric_threshold" and (
            not t.get("metric") or t.get("op") not in alerts.OPS or t.get("threshold") is None
        ):
            err("metric_threshold triggers need metric, op (>, >=, <, <=) and threshold", 400)


def _clean_triggers(triggers: list[dict]) -> list[dict]:
    """Keep only the fields that apply to each trigger kind (event severity defaults to error)."""
    out = []
    for t in triggers:
        if t["kind"] == "event":
            out.append({"kind": "event", "min_severity": t.get("min_severity") or "error"})
        elif t["kind"] == "metric_threshold":
            out.append({k: t.get(k) for k in ("kind", "metric", "op", "threshold")})
        else:
            out.append({"kind": t["kind"]})
    return out


@app.get("/admin/alerts")
def admin_list_alerts():
    return ok([alerts.public_rule(r) for r in db_list_alert_rules()])


@app.post("/admin/alerts", status_code=201)
def admin_create_alert(req: AlertRuleRequest, request: Request):
    rule = req.model_dump(exclude={"kind", "min_severity", "metric", "op", "threshold"})
    rule["name"] = rule["name"].strip()
    if req.triggers is None:
        if req.kind is None:
            err("A rule needs triggers", 400)
        rule["triggers"] = [{"kind": req.kind, "min_severity": req.min_severity, "metric": req.metric,
                             "op": req.op, "threshold": req.threshold}]
    _check_alert_rule(rule)
    rule["triggers"] = _clean_triggers(rule["triggers"])
    me = _principal(request)
    return ok(alerts.public_rule(db_create_alert_rule(rule, me.name if me else "local")))


@app.patch("/admin/alerts/{rule_id}")
def admin_update_alert(rule_id: str, req: UpdateAlertRuleRequest):
    current = db_get_alert_rule(rule_id)
    if current is None:
        err("No such alert rule", 404)
    patch = req.model_dump(exclude_unset=True)
    if patch.get("triggers") is None:
        patch.pop("triggers", None)
    _check_alert_rule({**current, **patch})
    if "triggers" in patch:
        patch["triggers"] = _clean_triggers(patch["triggers"])
    db_update_alert_rule(rule_id, patch)
    return ok(alerts.public_rule(db_get_alert_rule(rule_id)))


@app.delete("/admin/alerts/{rule_id}")
def admin_delete_alert(rule_id: str):
    if not db_delete_alert_rule(rule_id):
        err("No such alert rule", 404)
    return ok({"deleted": rule_id})


@app.post("/admin/alerts/{rule_id}/test")
def admin_test_alert(rule_id: str):
    """Send a test message to the rule's webhook now."""
    rule = db_get_alert_rule(rule_id)
    if rule is None:
        err("No such alert rule", 404)
    error = alerts.send_test(rule)
    if error:
        err(f"Delivery failed: {error}", 502)
    return ok({"sent": True})


# ---------------------------------------------------------------------------
# Idempotent ingestion
# ---------------------------------------------------------------------------

BATCH_HEADER = "X-Pyrometer-Batch"


def _ingest_once(run_id: str, request: Request, insert):
    """Run ``insert()`` unless this client batch id was already ingested for the run.

    The SDK retries undelivered batches, possibly after the server stored them but the response was
    lost. Returns ``insert()``'s result, or ``None`` for a duplicate (nothing inserted).
    """
    # One lock hold for existence check, claim, insert and the activity stamp: a retry that arrives while
    # the first attempt is still inserting waits for it (instead of being told "duplicate" and then losing
    # the data if the first attempt fails), and a hard delete cannot slip in between the check and the
    # insert and leave orphan rows (the lock is re-entrant, the db_* helpers take it again).
    with get_lock():
        if db_get_run_project(run_id) is None:
            err("Run not found", 404)  # never create orphan rows (or claim batch ids) for an unknown run
        batch_id = request.headers.get(BATCH_HEADER)
        if not batch_id:
            result = insert()
        else:
            if not db_claim_batch(run_id, batch_id[:64]):
                return None
            try:
                result = insert()
            except Exception:
                db_release_batch(run_id, batch_id[:64])
                raise
        db_touch_run(run_id)
        return result


def _dup() -> dict:
    return ok({"inserted": 0, "duplicate": True})


# ---------------------------------------------------------------------------
# Routes — runs
# ---------------------------------------------------------------------------

@app.post("/runs", status_code=201)
async def create_run(req: CreateRunRequest, request: Request):
    principal = _principal(request)
    if req.resume is not None:
        if req.run_id is None:
            err("resume needs a run_id", 400)
        existing = db_get_run_project(req.run_id)
        if existing is not None:
            if not auth.can_write_project(principal, existing):
                err("No such run", 404)  # same answer as a missing run: do not reveal other projects
            if req.project is not None and req.project != existing:
                err(f"Run {req.run_id} belongs to project {existing!r}, not {req.project!r}", 409)
            info = db_resume_run(req.run_id)
            await _ws.broadcast({"type": "run_updated", "run_id": req.run_id})
            return ok(info)
        if req.resume == "must":
            err("No such run", 404)
    if req.run_id is not None:
        existing = db_get_run_project(req.run_id)
        if existing is not None:  # repeat of an earlier create (lost response, re-synced spool)
            if not auth.can_write_project(principal, existing):
                err("No such project", 404)
            if req.project is not None and req.project != existing:
                err(f"Run {req.run_id} already exists in project {existing!r}, not {req.project!r}", 409)
            run = db_get_run(req.run_id)
            return ok({k: run[k] for k in ("run_id", "name", "status", "created_at", "project")})
    # A project key implies its project; everyone else lands in "default" unless they name one.
    default = principal.project if principal and principal.role == "project" else "default"
    project = _validated_project_name(req.project or default)
    if db_get_project(project) is None:
        if principal is not None and principal.role == "project":
            err("No such project", 404)
        # First use by a global key (or local mode) registers it as a public project.
        db_create_project(project, "public", principal.name if principal else "local")
    if not auth.can_write_project(principal, project):
        err("No such project", 404)  # same answer as a missing one: do not reveal private names
    try:
        run = db_create_run(
            run_id=req.run_id,
            created_at=req.created_at,
            name=req.name,
            hyperparams=req.hyperparams,
            tags=req.tags,
            notes=req.notes,
            git_hash=req.git_hash,
            git_dirty=req.git_dirty,
            gpu_model=req.gpu_model,
            gpu_memory_total=req.gpu_memory_total,
            cuda_version=req.cuda_version,
            pytorch_version=req.pytorch_version,
            python_version=req.python_version,
            hostname=req.hostname,
            project=project,
            group=(req.group or "").strip() or None,
        )
    except duckdb.ConstraintException:
        # A concurrent identical create (original + retry) won the race: answer like the repeat it is.
        existing = db_get_run_project(req.run_id) if req.run_id else None
        if existing is None:
            raise
        if not auth.can_write_project(principal, existing):
            err("No such project", 404)
        run = db_get_run(req.run_id)
        return ok({k: run[k] for k in ("run_id", "name", "status", "created_at", "project")})
    await _ws.broadcast({"type": "run_created", "run_id": run["run_id"]})
    return ok(run)


def _pin_user(principal) -> str | None:
    """Whose pins apply: the dashboard user, ``local`` without auth, nobody for API keys."""
    if principal is None:
        return "local"
    return None if principal.via == "key" else principal.name


@app.get("/runs")
def list_runs(
    request: Request,
    sort: str = "updated",
    include_archived: bool = False,
    group: str | None = None,
    project: str | None = None,
    status: str | None = None,
):
    """Visible runs. ``sort``: ``updated`` (last activity, default), ``created`` or ``name``. Archived runs
    (and runs of archived projects) are left out unless ``include_archived=true``. Optional ``group``,
    ``project`` and ``status`` filters. Each run carries ``updated_at``, ``group``, ``archived`` and the
    caller's ``pinned`` flag."""
    if sort not in RUN_SORTS:
        err(f"sort must be one of {', '.join(RUN_SORTS)}", 400)
    principal = _principal(request)
    return ok(db_list_runs(
        auth.visible_projects(principal), sort, include_archived, group, project, status, pinned_by=_pin_user(principal),
    ))


@app.get("/pins")
def list_pins(request: Request, include_archived: bool = False):
    """The signed-in user's pinned runs (visible ones only), most recently pinned first."""
    user = _pin_user(_principal(request))
    if user is None:
        err("API keys cannot pin runs", 403)
    order = {rid: i for i, rid in enumerate(db_pinned_run_ids(user))}
    runs = db_list_runs(
        auth.visible_projects(_principal(request)), "updated", include_archived, pinned_by=user, run_ids=list(order),
    )
    return ok(sorted(runs, key=lambda r: order[r["run_id"]]))


@app.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    run = db_get_run(run_id)
    if run is None:
        err("Run not found", status_code=404)
    user = _pin_user(_principal(request))
    run["pinned"] = bool(user) and db_is_pinned(user, run_id)
    return ok(run)


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = t.strip()
        if not t:
            continue
        if len(t) > 64:
            err("Tags are at most 64 characters", 400)
        if t not in out:
            out.append(t)
    return out


_HW_FIELDS = ("status", *RUN_INFO_FIELDS)


@app.patch("/runs/{run_id}")
async def update_run(run_id: str, req: UpdateRunRequest, request: Request):
    """SDK keys: status, notes, hardware info and the organisation fields. Dashboard admins: only the
    organisation fields (``name``, ``tags``, ``notes``, ``group``)."""
    if req.status not in (None, "running", *TERMINAL_STATUSES):
        err(f"Invalid status: {req.status!r}")
    principal = _principal(request)
    if principal is not None and principal.via != "key" and any(f in req.model_fields_set for f in _HW_FIELDS):
        err("Dashboard sessions can only edit name, tags, notes and group", 403)
    meta: dict = {}
    if req.name is not None:
        if not req.name.strip():
            err("Name cannot be empty", 400)
        meta["name"] = req.name.strip()
    if req.tags is not None:
        meta["tags"] = _clean_tags(req.tags)
    if "group" in req.model_fields_set:
        meta["run_group"] = (req.group or "").strip() or None
    # An explicit status always wins, including a replayed one after the sweeper marked the run crashed.
    before = db_get_run_status(run_id)
    if before is None:
        err("Run not found", 404)
    db_update_run(run_id, req.status, req.notes, req.model_dump(include=set(RUN_INFO_FIELDS), exclude_none=True), meta)
    if req.status is not None and req.status != before:  # a replayed PATCH must not alert twice
        alerts.notify_run_status(run_id, req.status)
    # Compute derived summary stats when a run reaches a terminal state.
    if req.status in TERMINAL_STATUSES:
        _queue_summary(run_id)
    await _ws.broadcast({"type": "run_updated", "run_id": run_id})
    return ok({"updated": True})


# --- organisation: archive / move / delete / bulk (admins; see docs/auth.md) ---------------------

def _require_project_for_move(target: str) -> str:
    target = _validated_project_name(target)
    project = db_get_project(target)
    if project is None:
        err("No such project", 404)
    if project["archived"]:
        err(f"Project {target!r} is archived; unarchive it first", 409)
    return target


async def _archive_run(run_id: str, archived: bool) -> None:
    if not db_set_run_archived(run_id, archived):
        err("Run not found", 404)
    await _ws.broadcast({"type": "run_updated", "run_id": run_id})


async def _move_run(run_id: str, target: str, force: bool) -> str:
    old = db_get_run_project(run_id)
    if old is None:
        err("Run not found", 404)
    target = _require_project_for_move(target)
    if old == target:
        return old
    if db_get_run_status(run_id) == "running" and not force:
        err("Run is still running; a key bound to its project loses access when it moves. Pass force=true", 409)
    db_move_run(run_id, target)
    await _ws.broadcast_move(run_id, old, target)
    return old


async def _delete_run(run_id: str) -> None:
    project = db_get_run_project(run_id)
    if project is None or not db_delete_run(run_id):
        err("Run not found", 404)
    _ws.forget_run(run_id)
    await _ws.broadcast({"type": "run_deleted", "run_id": run_id, "project": project}, project=project)


async def _retag_run(run_id: str, add: list[str], remove: list[str]) -> None:
    run = db_get_run(run_id)
    if run is None:
        err("Run not found", 404)
    tags = [t for t in run["tags"] if t not in remove]
    tags += [t for t in add if t not in tags]
    db_update_run(run_id, None, None, None, {"tags": tags})
    await _ws.broadcast({"type": "run_updated", "run_id": run_id})


@app.post("/runs/{run_id}/archive")
async def archive_run(run_id: str):
    """Admin: hide a run from lists (data is kept; direct access and ingestion keep working)."""
    await _archive_run(run_id, True)
    return ok({"archived": True})


@app.post("/runs/{run_id}/unarchive")
async def unarchive_run(run_id: str):
    await _archive_run(run_id, False)
    return ok({"archived": False})


@app.post("/runs/{run_id}/move")
async def move_run(run_id: str, req: MoveRunRequest):
    """Admin: move a run to another (existing, unarchived) project. A running run needs ``force=true``."""
    old = await _move_run(run_id, req.project, req.force)
    return ok({"moved": True, "from": old, "project": req.project})


@app.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    """Admin: permanently delete a run and everything stored for it, media files included."""
    await _delete_run(run_id)
    return ok({"deleted": run_id})


@app.post("/runs/bulk")
async def bulk_runs(req: BulkRunsRequest, request: Request):
    """Apply one action to many runs; the answer lists the outcome per run (one failure does not stop the rest).

    ``tag`` (add/remove tags): dashboard admins and API keys that can write the run's project.
    ``archive`` / ``unarchive`` / ``move`` / ``delete``: dashboard admins only.
    """
    principal = _principal(request)
    is_key = principal is not None and principal.via == "key"
    if req.action != "tag" and is_key:
        err("Admin access required", 403)
    add, remove = _clean_tags(req.add_tags), _clean_tags(req.remove_tags)
    if req.action == "tag" and not (add or remove):
        err("tag needs add_tags and/or remove_tags", 400)
    if req.action == "move":
        if not req.project:
            err("move needs a project", 400)
        _require_project_for_move(req.project)
    results: list[dict] = []
    for run_id in dict.fromkeys(req.run_ids):
        try:
            project = db_get_run_project(run_id)
            allowed = project is not None and (
                auth.can_write_project(principal, project) if is_key else auth.can_read_project(principal, project)
            )
            if not allowed:
                err("Run not found", 404)  # same answer for missing and invisible
            if req.action == "tag":
                await _retag_run(run_id, add, remove)
            elif req.action in ("archive", "unarchive"):
                await _archive_run(run_id, req.action == "archive")
            elif req.action == "move":
                await _move_run(run_id, req.project or "", req.force)
            else:
                await _delete_run(run_id)
            results.append({"run_id": run_id, "ok": True, "error": None})
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {}
            results.append({"run_id": run_id, "ok": False, "error": detail.get("error") or str(e.detail)})
    done = sum(1 for r in results if r["ok"])
    return ok({"results": results, "succeeded": done, "failed": len(results) - done})


# --- pins (per dashboard user; API keys cannot pin) ------------------------------------------------

@app.put("/runs/{run_id}/pin")
def pin_run(run_id: str, request: Request):
    user = _pin_user(_principal(request))
    if user is None:
        err("API keys cannot pin runs", 403)
    if db_get_run_project(run_id) is None:
        err("Run not found", 404)
    db_pin_run(user, run_id)
    return ok({"pinned": True})


@app.delete("/runs/{run_id}/pin")
def unpin_run(run_id: str, request: Request):
    user = _pin_user(_principal(request))
    if user is None:
        err("API keys cannot pin runs", 403)
    db_unpin_run(user, run_id)
    return ok({"pinned": False})


@app.post("/runs/{run_id}/heartbeat")
async def heartbeat(run_id: str):
    """Liveness ping from the SDK. Revives a run the sweeper marked ``crashed``."""
    before = db_get_run_status(run_id)
    if before is None:
        err("Run not found", status_code=404)
    db_heartbeat(run_id)
    if before == "crashed":
        await _ws.broadcast({"type": "run_updated", "run_id": run_id})
    return ok({"alive": True})


# ---------------------------------------------------------------------------
# Routes — metrics
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/metrics", status_code=201)
async def ingest_metrics(run_id: str, metrics: list[MetricEntry], request: Request):
    points = [m.model_dump() for m in metrics]
    inserted = _ingest_once(run_id, request, lambda: db_insert_metrics(run_id, points))
    if inserted is None:
        return _dup()
    alerts.check_metrics(run_id, points)
    await _ws.broadcast({"type": "metrics", "run_id": run_id}, throttle_key=(run_id, "metrics"))
    return ok({"inserted": inserted})


@app.get("/runs/{run_id}/metrics")
def get_metrics(
    run_id: str,
    name: str | None = None,
    max_points: int | None = None,
    step_min: int | None = None,
    step_max: int | None = None,
    t_min: float | None = None,
    t_max: float | None = None,
):
    """Metric points ordered by step; the full history unless asked otherwise. ``step_min/step_max`` or
    ``t_min/t_max`` return only that window (zoom). ``max_points`` downsamples each metric to about that
    many points (8..100000) keeping each bucket's min, max, first and last point, so spikes survive."""
    _check_window(max_points, step_min, step_max, t_min, t_max)
    rows, total = db_get_metrics(run_id, name, max_points, step_min, step_max, t_min, t_max, with_total=True)
    return ok_series(rows, total, max_points, any(v is not None for v in (max_points, step_min, step_max, t_min, t_max)))


@app.get("/runs/{run_id}/metrics/recent")
def get_recent_metrics(run_id: str, limit: int = 100):
    """Newest ``limit`` values per metric, oldest first: ``{name: [values]}`` (SDK detector seeding on resume)."""
    return ok(db_recent_metrics(run_id, max(1, min(limit, 1000))))


@app.get("/runs/{run_id}/metric-names")
def get_metric_names(run_id: str):
    return ok(db_get_metric_names(run_id))


# ---------------------------------------------------------------------------
# Routes — profile events
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/profile", status_code=201)
def ingest_profile(run_id: str, events: list[ProfileEvent], request: Request):
    inserted = _ingest_once(
        run_id, request, lambda: db_insert_profile_events(run_id, [e.model_dump() for e in events])
    )
    if inserted is None:
        return _dup()
    return ok({"inserted": inserted})


@app.get("/runs/{run_id}/profile")
def get_profile(run_id: str, step: int | None = None):
    """Return profile events for a run, optionally filtered to a single step.

    Results are sorted by duration_us descending — most expensive operators first.
    """
    return ok(db_get_profile_events(run_id, step))


@app.get("/runs/{run_id}/profile/steps")
def get_profile_steps(run_id: str):
    """Return the sorted list of steps for which profile data exists."""
    return ok(db_get_profile_steps(run_id))


# ---------------------------------------------------------------------------
# Routes — system snapshots
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/system", status_code=201)
async def ingest_system_snapshot(run_id: str, snapshot: SystemSnapshot, request: Request):
    if _ingest_once(run_id, request, lambda: db_insert_system_snapshot(run_id, snapshot.model_dump()) or True) is None:
        return _dup()
    await _ws.broadcast({"type": "system", "run_id": run_id}, throttle_key=(run_id, "system"))
    return ok({"inserted": True})


@app.get("/runs/{run_id}/system")
def get_system_snapshots(
    run_id: str, max_points: int | None = None, t_min: float | None = None, t_max: float | None = None
):
    """System snapshots (all GPUs) ordered by time. ``t_min/t_max`` select a window; ``max_points``
    downsamples each GPU's series keeping the first/last snapshot and the utilization, memory and
    temperature peaks of every time bucket."""
    _check_window(max_points, t_min, t_max)
    rows, total = db_get_system_snapshots(run_id, max_points, t_min, t_max, with_total=True)
    return ok_series(rows, total, max_points, any(v is not None for v in (max_points, t_min, t_max)))


# ---------------------------------------------------------------------------
# Routes — param stats (gradient & weight monitoring)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/param-stats", status_code=201)
async def ingest_param_stats(run_id: str, stats: list[ParamStatEntry], request: Request):
    inserted = _ingest_once(run_id, request, lambda: db_insert_param_stats(run_id, [s.model_dump() for s in stats]))
    if inserted is None:
        return _dup()
    await _ws.broadcast({"type": "param_stats", "run_id": run_id}, throttle_key=(run_id, "param_stats"))
    return ok({"inserted": inserted})


@app.get("/runs/{run_id}/param-stats")
def get_param_stats(
    run_id: str,
    name: str | None = None,
    max_points: int | None = None,
    step_min: int | None = None,
    step_max: int | None = None,
    t_min: float | None = None,
    t_max: float | None = None,
):
    """Gradient/weight stats ordered by parameter, then step. Window and ``max_points`` as for metrics;
    downsampling is per parameter (first/last step plus the peak gradient-norm and weight-norm steps)."""
    _check_window(max_points, step_min, step_max, t_min, t_max)
    rows, total = db_get_param_stats(run_id, name, max_points, step_min, step_max, t_min, t_max, with_total=True)
    return ok_series(rows, total, max_points, any(v is not None for v in (max_points, step_min, step_max, t_min, t_max)))


@app.get("/runs/{run_id}/param-stats/names")
def get_param_stat_names_route(run_id: str):
    return ok(db_get_param_stat_names(run_id))


# ---------------------------------------------------------------------------
# Routes — events (anomaly flags, custom milestones)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/events", status_code=201)
def ingest_event(run_id: str, event: EventEntry, request: Request):
    if _ingest_once(run_id, request, lambda: db_insert_event(run_id, event.model_dump()) or True) is None:
        return _dup()
    alerts.notify_event(run_id, event.model_dump())
    return ok({"inserted": True})


@app.get("/runs/{run_id}/events")
def get_events(run_id: str):
    return ok(db_get_events(run_id))


# ---------------------------------------------------------------------------
# Routes — console logs
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/logs", status_code=201)
async def ingest_logs(run_id: str, entries: list[LogEntry], request: Request):
    inserted = _ingest_once(run_id, request, lambda: db_insert_logs(run_id, [e.model_dump() for e in entries]))
    if inserted is None:
        return _dup()
    await _ws.broadcast({"type": "logs", "run_id": run_id}, throttle_key=(run_id, "logs"))
    return ok({"inserted": inserted})


@app.get("/runs/{run_id}/logs")
def get_logs(run_id: str, stream: str | None = None):
    return ok(db_get_logs(run_id, stream))


# ---------------------------------------------------------------------------
# Routes — artifacts (checkpoint / file tracking)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/artifacts", status_code=201)
def ingest_artifact(run_id: str, artifact: ArtifactEntry, request: Request):
    if _ingest_once(run_id, request, lambda: db_insert_artifact(run_id, artifact.model_dump()) or True) is None:
        return _dup()
    return ok({"inserted": True})


@app.get("/runs/{run_id}/artifacts")
def get_artifacts(run_id: str):
    return ok(db_get_artifacts(run_id))


# ---------------------------------------------------------------------------
# Routes — run summaries (derived stats + LLM export)
# ---------------------------------------------------------------------------

def _current_summary(run_id: str) -> dict | None:
    """The stored summary for a finished run; in-progress (or resumed) runs are recomputed, never served stale."""
    summary = db_get_run_summary(run_id)
    if summary is None or db_get_run_status(run_id) not in TERMINAL_STATUSES:
        summary = db_compute_run_summary(run_id)
    return summary


@app.get("/runs/{run_id}/summary")
def get_run_summary(run_id: str):
    """Return derived stats for the run.

    If no summary has been computed yet (e.g., run still in progress),
    computes one on-the-fly and caches it for future requests.
    """
    summary = _current_summary(run_id)
    if summary is None:
        err("Run not found", status_code=404)
    return ok(summary)


@app.get("/runs/{run_id}/llm-summary", response_class=PlainTextResponse)
def get_llm_summary(run_id: str):
    """Return a plain-text summary of the run formatted for pasting into an LLM."""
    run = db_get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"data": None, "error": "Run not found"})

    summary = _current_summary(run_id)

    lines: list[str] = []

    # Header
    lines.append(f"=== {run['name']} ===")
    status_line = f"Status: {run['status']}"
    if summary and summary.get("total_training_time_s"):
        secs = int(summary["total_training_time_s"])
        status_line += f"  |  Duration: {secs // 60}m {secs % 60}s"
    if summary and summary.get("efficiency_score"):
        status_line += f"  |  Efficiency: {summary['efficiency_score']}"
    lines.append(status_line)
    if run.get("created_at"):
        lines.append(f"Started: {run['created_at']}")
    lines.append("")

    # Hyperparameters
    hp = run.get("hyperparams") or {}
    if hp:
        lines.append("HYPERPARAMETERS")
        for k, v in hp.items():
            lines.append(f"  {k}: {v}")
        lines.append("")

    # Hardware
    hw_parts = []
    if run.get("gpu_model"):
        vram = f" ({run['gpu_memory_total'] / 1024**3:.1f} GB)" if run.get("gpu_memory_total") else ""
        hw_parts.append(f"  GPU: {run['gpu_model']}{vram}")
    env_parts = []
    if run.get("python_version"):
        env_parts.append(f"Python {run['python_version']}")
    if run.get("pytorch_version"):
        env_parts.append(f"PyTorch {run['pytorch_version']}")
    if run.get("cuda_version"):
        env_parts.append(f"CUDA {run['cuda_version']}")
    if hw_parts or env_parts:
        lines.append("HARDWARE")
        lines.extend(hw_parts)
        if env_parts:
            lines.append("  " + "  |  ".join(env_parts))
        lines.append("")

    # Git
    if run.get("git_hash"):
        lines.append("GIT")
        dirty = " (dirty)" if run.get("git_dirty") else " (clean)"
        lines.append(f"  Commit: {run['git_hash'][:8]}{dirty}")
        lines.append("")

    if summary:
        # Final metric values
        final = summary.get("final_metrics") or {}
        if final:
            lines.append("METRICS — Final values")
            for name, val in sorted(final.items()):
                lines.append(f"  {name:<28} {val:.6g}")
            lines.append("")

        # Best metric values
        best = summary.get("best_metrics") or {}
        if best:
            lines.append("BEST VALUES")
            for name, info in sorted(best.items()):
                step_str = f"  (step {info['step']})" if info.get("step") is not None else ""
                lines.append(f"  {name:<28} {info['value']:.6g}  ({info['direction']}){step_str}")
            lines.append("")

        # System
        sys_parts = []
        if summary.get("total_steps"):
            throughput = f"  ({summary['steps_per_sec']:.1f} steps/sec)" if summary.get("steps_per_sec") else ""
            sys_parts.append(f"  Steps: {summary['total_steps']}{throughput}")
        if summary.get("avg_gpu_utilization") is not None:
            sys_parts.append(f"  Avg GPU utilization: {summary['avg_gpu_utilization']:.1f}%")
        if summary.get("peak_process_memory_bytes"):
            peak_gb = summary["peak_process_memory_bytes"] / 1024**3
            total_str = ""
            n_gpus = db_gpu_count(run_id)
            if n_gpus > 1:
                total_str = f" (summed over {n_gpus} GPUs)"
            elif run.get("gpu_memory_total"):
                total_gb = run["gpu_memory_total"] / 1024**3
                total_str = f" / {total_gb:.1f} GB"
            sys_parts.append(f"  Peak VRAM: {peak_gb:.2f} GB{total_str}")
        if sys_parts:
            lines.append("SYSTEM")
            lines.extend(sys_parts)
            lines.append("")

    # Events (all + anomalies)
    events = db_get_events(run_id)
    if events:
        anomaly_n = sum(1 for e in events if e.get("event_type") == "anomaly_detected")
        lines.append(f"EVENTS ({len(events)} total, {anomaly_n} anomalies)")
        for e in events:
            step_str = f"step {e['step']}" if e.get("step") is not None else "no step"
            lines.append(f"  [{step_str}] {e['event_type']} ({e['severity']}): {e['message']}")
        lines.append("")

    # Samples: latest step's best and worst by reward, truncated
    def _clip(smp: dict) -> str:
        return "; ".join(f"{k}={str(v)[:160]!r}" for k, v in list(smp.items())[:6])

    for ex in db_get_sample_extremes(run_id):
        lines.append(f"SAMPLES '{ex['name']}' ({ex['n_steps']} steps with samples; latest step {ex['latest_step']})")
        for label, key in (("top reward", "top"), ("bottom reward", "bottom")):
            for smp in ex[key]:
                lines.append(f"  [{label}] {_clip(smp)}")
        lines.append("")

    # Notes
    if run.get("notes"):
        lines.append("NOTES")
        lines.append(f"  {run['notes']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes — export
# ---------------------------------------------------------------------------

def _export_response(label: str, run_ids: list[str], run_id: str | None = None, project: str | None = None):
    """Build the Parquet zip in a temp dir and stream it; the dir is removed once the response is sent."""
    tmp = Path(tempfile.mkdtemp(prefix="pyrometer-export-"))
    try:
        parquet = tmp / "parquet"
        parquet.mkdir()
        db_export_parquet(parquet, run_id=run_id, project=project)
        archive = tmp / "export.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:  # Parquet is already compressed
            for f in sorted(parquet.iterdir()):
                zf.write(f, f.name)
            for disk, rel in db_export_media_files(run_ids):
                zf.write(disk, f"media/{rel}")
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:60].strip("_") or "export"
    return FileResponse(
        archive, media_type="application/zip", filename=f"{filename}-parquet.zip",
        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
    )


@app.get("/runs/{run_id}/export")
def export_run(run_id: str):
    """Zip of Parquet files (``runs``, ``metrics``, ``system_snapshots``, ``param_stats``, ``profile_events``,
    ``events``, ``logs``, ``artifacts``, ``media``, ``samples``, ``run_summaries``) plus ``media/`` image files."""
    run = db_get_run(run_id)
    if run is None:
        err("Run not found", 404)
    return _export_response(f"{run['name']}-{run_id[:8]}", [run_id], run_id=run_id)


# ---------------------------------------------------------------------------
# Routes — media (images, histograms, figures)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/media", status_code=201)
def ingest_media(run_id: str, item: MediaEntry, request: Request):
    if _ingest_once(run_id, request, lambda: db_insert_media(run_id, item.model_dump()) or True) is None:
        return _dup()
    return ok({"inserted": True})


@app.get("/runs/{run_id}/media/names")
def get_media_names(run_id: str):
    return ok(db_get_media_names(run_id))


@app.get("/runs/{run_id}/media")
def get_media(run_id: str, name: str | None = None, inline: bool = True):
    """Media items. Images/figures are stored as files: each has a ``url`` (``GET /runs/{id}/media/file``) and,
    unless ``inline=false``, its base64 ``data`` too (kept for older clients). Histograms are always inline."""
    return ok(db_get_media(run_id, name, inline))


@app.get("/runs/{run_id}/media/file")
def get_media_file(run_id: str, name: str, step: int):
    """The PNG of one image/figure. Same visibility as the run; browsers can use it directly in ``<img src>``."""
    path = db_get_media_file(run_id, name, step)
    if path is None:
        err("Media not found", 404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------------
# Routes — samples (prompt / completion / reward dicts per step)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/samples", status_code=201)
def ingest_samples(run_id: str, item: SamplesEntry, request: Request):
    import json as _json

    if len(item.samples) > MAX_SAMPLES_PER_CALL:
        err(f"Too many samples in one call ({len(item.samples)} > {MAX_SAMPLES_PER_CALL})", 413)
    total = 0
    for i, smp in enumerate(item.samples):
        size = len(_json.dumps(smp, default=str))
        if size > MAX_SAMPLE_BYTES:
            err(f"Sample {i} is {size} bytes; the limit is {MAX_SAMPLE_BYTES}", 413)
        total += size
    if total > MAX_SAMPLES_CALL_BYTES:
        err(f"Samples payload is {total} bytes; the limit is {MAX_SAMPLES_CALL_BYTES}", 413)
    n = _ingest_once(
        run_id, request,
        lambda: db_insert_samples(run_id, item.name[:200], item.step, item.timestamp, item.samples, item.mode),
    )
    if n is None:
        return _dup()
    return ok({"inserted": n})


@app.get("/runs/{run_id}/samples/steps")
def get_sample_steps(run_id: str):
    return ok(db_get_sample_steps(run_id))


@app.get("/runs/{run_id}/samples")
def get_samples(
    run_id: str,
    name: str = "samples",
    step: int = 0,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
    order: str = "asc",
    q: str | None = None,
    prompt: str | None = None,
):
    """Page of samples at (name, step). Optional whole-step ``sort`` key + ``order`` (asc|desc),
    ``q`` substring filter and exact (whitespace-normalized) ``prompt`` filter."""
    limit = max(1, min(limit, 200))
    if order not in ("asc", "desc"):
        err("order must be 'asc' or 'desc'", 400)
    sort = sort[:200] if sort else None
    q = q[:500] if q else None
    return ok(db_get_samples(run_id, name, step, limit, max(0, offset), sort, order == "desc", q, prompt))


# ---------------------------------------------------------------------------
# Serve React frontend (static build)
# Mount after all API routes so the API always takes precedence.
# Uses HashRouter in the frontend, so the server only ever serves GET /
# for page loads — no SPA fallback conflicts with API paths.
#
# _SPAMount overrides matches() so it returns NONE for non-HTTP connections.
# Without this, the catch-all "/" Mount would also match WebSocket upgrade
# requests (scope["type"] == "websocket") and StaticFiles would crash with
# an AssertionError since it only handles HTTP.
# ---------------------------------------------------------------------------

from starlette.routing import Match, Mount as _StarletteMount  # noqa: E402
from starlette.types import Scope as _Scope  # noqa: E402


class _SPAMount(_StarletteMount):
    def matches(self, scope: _Scope) -> tuple:
        if scope.get("type") != "http":
            return Match.NONE, {}
        return super().matches(scope)


_FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.router.routes.append(
        _SPAMount("/", app=StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend"),
    )
