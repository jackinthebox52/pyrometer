"""FastAPI application — route handlers only. All DB access goes through db.py."""

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pyrometer.server.db import (
    db_compute_run_summary,
    db_create_run,
    db_get_artifacts,
    db_get_events,
    db_get_logs,
    db_get_media,
    db_get_media_names,
    db_get_metric_names,
    db_get_metrics,
    db_get_param_stat_names,
    db_get_param_stats,
    db_get_profile_events,
    db_get_profile_steps,
    db_get_run,
    db_get_run_summary,
    db_get_system_snapshots,
    db_insert_artifact,
    db_insert_event,
    db_insert_logs,
    db_insert_media,
    db_insert_metrics,
    db_insert_param_stats,
    db_insert_profile_events,
    db_insert_system_snapshot,
    db_list_runs,
    db_update_run,
)

app = FastAPI(title="Pyrometer API", version="0.1.0")


# ---------------------------------------------------------------------------
# WebSocket connection manager — push events to connected dashboard clients
# ---------------------------------------------------------------------------

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

    async def broadcast(
        self,
        payload: dict,
        throttle_key: tuple[str, str] | None = None,
        throttle_ms: int = 500,
    ) -> None:
        if throttle_key:
            now = time.monotonic()
            last = self._last_broadcast.get(throttle_key, 0.0)
            if (now - last) * 1000 < throttle_ms:
                return
            self._last_broadcast[throttle_key] = now
        if not self._connections:
            return
        text = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_ws = _WsManager()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await _ws.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive; ignore client messages
    except (WebSocketDisconnect, Exception):
        _ws.disconnect(websocket)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def ok(data) -> dict:
    return {"data": data, "error": None}


def err(message: str, status_code: int = 400):
    raise HTTPException(status_code=status_code, detail={"data": None, "error": message})


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CreateRunRequest(BaseModel):
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


class UpdateRunRequest(BaseModel):
    status: str | None = None
    notes: str | None = None


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


# ---------------------------------------------------------------------------
# Routes — health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return ok({"status": "ok"})


# ---------------------------------------------------------------------------
# Routes — runs
# ---------------------------------------------------------------------------

@app.post("/runs", status_code=201)
async def create_run(req: CreateRunRequest):
    run = db_create_run(
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
    )
    await _ws.broadcast({"type": "run_created", "run_id": run["run_id"]})
    return ok(run)


@app.get("/runs")
def list_runs():
    return ok(db_list_runs())


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run = db_get_run(run_id)
    if run is None:
        err("Run not found", status_code=404)
    return ok(run)


@app.patch("/runs/{run_id}")
async def update_run(run_id: str, req: UpdateRunRequest):
    if req.status not in (None, "running", "completed", "crashed", "killed"):
        err(f"Invalid status: {req.status!r}")
    db_update_run(run_id, req.status, req.notes)
    # Compute derived summary stats when a run reaches a terminal state.
    if req.status in ("completed", "crashed", "killed"):
        import threading as _threading
        _threading.Thread(
            target=db_compute_run_summary, args=(run_id,), daemon=True
        ).start()
    await _ws.broadcast({"type": "run_updated", "run_id": run_id})
    return ok({"updated": True})


# ---------------------------------------------------------------------------
# Routes — metrics
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/metrics", status_code=201)
async def ingest_metrics(run_id: str, metrics: list[MetricEntry]):
    inserted = db_insert_metrics(run_id, [m.model_dump() for m in metrics])
    await _ws.broadcast({"type": "metrics", "run_id": run_id}, throttle_key=(run_id, "metrics"))
    return ok({"inserted": inserted})


@app.get("/runs/{run_id}/metrics")
def get_metrics(run_id: str, name: str | None = None):
    return ok(db_get_metrics(run_id, name))


@app.get("/runs/{run_id}/metric-names")
def get_metric_names(run_id: str):
    return ok(db_get_metric_names(run_id))


# ---------------------------------------------------------------------------
# Routes — profile events
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/profile", status_code=201)
def ingest_profile(run_id: str, events: list[ProfileEvent]):
    inserted = db_insert_profile_events(run_id, [e.model_dump() for e in events])
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
async def ingest_system_snapshot(run_id: str, snapshot: SystemSnapshot):
    db_insert_system_snapshot(run_id, snapshot.model_dump())
    await _ws.broadcast({"type": "system", "run_id": run_id}, throttle_key=(run_id, "system"))
    return ok({"inserted": True})


@app.get("/runs/{run_id}/system")
def get_system_snapshots(run_id: str):
    return ok(db_get_system_snapshots(run_id))


# ---------------------------------------------------------------------------
# Routes — param stats (gradient & weight monitoring)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/param-stats", status_code=201)
async def ingest_param_stats(run_id: str, stats: list[ParamStatEntry]):
    inserted = db_insert_param_stats(run_id, [s.model_dump() for s in stats])
    await _ws.broadcast({"type": "param_stats", "run_id": run_id}, throttle_key=(run_id, "param_stats"))
    return ok({"inserted": inserted})


@app.get("/runs/{run_id}/param-stats")
def get_param_stats(run_id: str, name: str | None = None):
    return ok(db_get_param_stats(run_id, name))


@app.get("/runs/{run_id}/param-stats/names")
def get_param_stat_names_route(run_id: str):
    return ok(db_get_param_stat_names(run_id))


# ---------------------------------------------------------------------------
# Routes — events (anomaly flags, custom milestones)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/events", status_code=201)
def ingest_event(run_id: str, event: EventEntry):
    db_insert_event(run_id, event.model_dump())
    return ok({"inserted": True})


@app.get("/runs/{run_id}/events")
def get_events(run_id: str):
    return ok(db_get_events(run_id))


# ---------------------------------------------------------------------------
# Routes — console logs
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/logs", status_code=201)
async def ingest_logs(run_id: str, entries: list[LogEntry]):
    inserted = db_insert_logs(run_id, [e.model_dump() for e in entries])
    await _ws.broadcast({"type": "logs", "run_id": run_id}, throttle_key=(run_id, "logs"))
    return ok({"inserted": inserted})


@app.get("/runs/{run_id}/logs")
def get_logs(run_id: str, stream: str | None = None):
    return ok(db_get_logs(run_id, stream))


# ---------------------------------------------------------------------------
# Routes — artifacts (checkpoint / file tracking)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/artifacts", status_code=201)
def ingest_artifact(run_id: str, artifact: ArtifactEntry):
    db_insert_artifact(run_id, artifact.model_dump())
    return ok({"inserted": True})


@app.get("/runs/{run_id}/artifacts")
def get_artifacts(run_id: str):
    return ok(db_get_artifacts(run_id))


# ---------------------------------------------------------------------------
# Routes — run summaries (derived stats + LLM export)
# ---------------------------------------------------------------------------

@app.get("/runs/{run_id}/summary")
def get_run_summary(run_id: str):
    """Return derived stats for the run.

    If no summary has been computed yet (e.g., run still in progress),
    computes one on-the-fly and caches it for future requests.
    """
    summary = db_get_run_summary(run_id)
    if summary is None:
        summary = db_compute_run_summary(run_id)
    if summary is None:
        err("Run not found", status_code=404)
    return ok(summary)


@app.get("/runs/{run_id}/llm-summary", response_class=PlainTextResponse)
def get_llm_summary(run_id: str):
    """Return a plain-text summary of the run formatted for pasting into an LLM."""
    run = db_get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"data": None, "error": "Run not found"})

    summary = db_get_run_summary(run_id)
    if summary is None:
        summary = db_compute_run_summary(run_id)

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
            if run.get("gpu_memory_total"):
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

    # Notes
    if run.get("notes"):
        lines.append("NOTES")
        lines.append(f"  {run['notes']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes — media (images, histograms, figures)
# ---------------------------------------------------------------------------

@app.post("/runs/{run_id}/media", status_code=201)
def ingest_media(run_id: str, item: MediaEntry):
    db_insert_media(run_id, item.model_dump())
    return ok({"inserted": True})


@app.get("/runs/{run_id}/media/names")
def get_media_names(run_id: str):
    return ok(db_get_media_names(run_id))


@app.get("/runs/{run_id}/media")
def get_media(run_id: str, name: str | None = None):
    return ok(db_get_media(run_id, name))


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
