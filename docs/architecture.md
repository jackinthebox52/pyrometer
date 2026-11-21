# Pyrometer Architecture

System architecture, data model, and design decisions.

---

## Table of Contents

- [System Architecture](#system-architecture)
- [Data Model](#data-model)
  - [Schema Overview](#schema-overview)
  - [Table Details](#table-details)
- [Design Decisions](#design-decisions)
  - [Why DuckDB?](#why-duckdb)
  - [Why Hash Routing?](#why-hash-routing)
  - [Why Polling + WebSocket?](#why-polling--websocket)
  - [Why key_averages()?](#why-key_averages)
- [Component Details](#component-details)
  - [SDK Internals](#sdk-internals)
  - [Server Internals](#server-internals)
  - [Frontend Architecture](#frontend-architecture)
- [Threading Model](#threading-model)
- [Error Handling](#error-handling)

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Training Script (any machine)                     │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                      pyrometer SDK                                │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────┐  │   │
│  │  │ run.log()   │  │ run.watch() │  │ run.profile()│  │  Tee    │  │   │
│  │  │ metric buf  │  │ grad hooks  │  │ torch.profiler│  │ stdout  │  │   │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └────┬────┘  │   │
│  │         │                │                │               │       │   │
│  │  ┌──────┴────────────────┴────────────────┴───────────────┴────┐  │   │
│  │  │                    Flush Thread (5s / 100 entries)           │  │   │
│  │  └─────────────────────────────┬───────────────────────────────┘  │   │
│  │                                  │                                  │   │
│  │  ┌───────────────────────────────┴───────────────────────────────┐ │   │
│  │  │              System Monitor Thread (pynvml, 2s)                │ │   │
│  │  └───────────────────────────────┬───────────────────────────────┘ │   │
│  └───────────────────────────────────┼─────────────────────────────────┘   │
│                                      │                                      │
│                                      ▼ HTTP POST (batched)                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           FastAPI Server (localhost:8321)                    │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                         REST API Endpoints                            │   │
│  │  POST /runs              GET  /runs/{id}/metrics                      │   │
│  │  GET  /runs              POST /runs/{id}/profile                      │   │
│  │  GET  /runs/{id}         GET  /runs/{id}/profile                      │   │
│  │  PATCH /runs/{id}        POST /runs/{id}/system                       │   │
│  │  POST /runs/{id}/metrics GET  /runs/{id}/param-stats                  │   │
│  │  ...                         ...                                      │   │
│  └─────────────────────────────┬────────────────────────────────────────┘   │
│                                │                                             │
│  ┌─────────────────────────────┴────────────────────────────────────────┐   │
│  │                         WebSocket Endpoint                            │   │
│  │  GET /ws → broadcast events to connected dashboard clients            │   │
│  └─────────────────────────────┬────────────────────────────────────────┘   │
│                                │                                             │
│  ┌─────────────────────────────┴────────────────────────────────────────┐   │
│  │                         DuckDB Storage                                │   │
│  │  ~/.pyrometer/data.duckdb                                             │   │
│  │  ┌─────────┐ ┌──────────┐ ┌──────────────┐ ┌─────────────────┐       │   │
│  │  │ runs    │ │ metrics  │ │ profile_events│ │ system_snapshots│       │   │
│  │  │ param_stats │ events │ │ logs         │ │ artifacts       │       │   │
│  │  │ media   │ │ run_summaries │                                      │   │
│  │  └─────────┘ └──────────┘ └──────────────┘ └─────────────────┘       │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                                │                                             │
│  ┌─────────────────────────────┴────────────────────────────────────────┐   │
│  │                    Static File Serving                                │   │
│  │  frontend/dist/ → / (React SPA)                                       │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         React Dashboard (SPA)                                │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  Run Sidebar                                                          │   │
│  │  - Run list (newest first)                                           │   │
│  │  - Search/filter by name, tag                                        │   │
│  │  - Checkbox selection for comparison                                 │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                │                                             │
│  ┌─────────────────────────────┴────────────────────────────────────────┐   │
│  │  Main Content (Hash Router)                                           │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                   │   │
│  │  │ EmptyState  │  │ RunDetail   │  │ CompareView │                   │   │
│  │  │ /           │  │ /runs/:id   │  │ /compare    │                   │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘                   │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                                │                                             │
│  ┌─────────────────────────────┴────────────────────────────────────────┐   │
│  │  WebSocket Hook (useWebSocket)                                        │   │
│  │  - Singleton connection                                               │   │
│  │  - Auto-reconnect with backoff                                        │   │
│  │  - Event dispatch to mounted components                               │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Model

### Schema Overview

The DuckDB database contains 9 tables:

```
┌─────────────────────┐     ┌─────────────────────┐
│ runs                │     │ metrics             │
│─────────────────────│     │─────────────────────│
│ run_id (PK)         │◀────│ run_id (FK)         │
│ name                │     │ step                │
│ status              │     │ timestamp           │
│ created_at          │     │ name                │
│ finished_at         │     │ value               │
│ hyperparams (JSON)  │     └─────────────────────┘
│ git_hash            │
│ git_dirty           │     ┌─────────────────────┐
│ gpu_model           │     │ profile_events      │
│ gpu_memory_total    │     │─────────────────────│
│ cuda_version        │     │ run_id (FK)         │
│ pytorch_version     │     │ step                │
│ python_version      │     │ timestamp           │
│ hostname            │     │ name                │
│ tags (JSON)         │     │ category            │
│ notes               │     │ duration_us         │
└─────────────────────┘     │ cuda_time_us        │
                            │ cpu_time_us         │
┌─────────────────────┐     │ input_shapes (JSON) │
│ system_snapshots    │     │ memory_bytes        │
│─────────────────────│     │ device_id           │
│ run_id (FK)         │     └─────────────────────┘
│ timestamp           │
│ gpu_utilization     │     ┌─────────────────────┐
│ memory_used         │     │ param_stats         │
│ memory_total        │     │─────────────────────│
│ temperature         │     │ run_id (FK)         │
│ power_draw          │     │ step                │
│ fan_speed           │     │ timestamp           │
│ sm_clock_mhz        │     │ param_name          │
│ memory_clock_mhz    │     │ grad_norm           │
│ process_memory_used │     │ grad_min/max/mean   │
│ process_exclusive   │     │ weight_norm         │
│ attributed_*        │     │ weight_min/max/mean │
└─────────────────────┘     │ grad_histogram (JSON)│
                            │ weight_histogram    │
┌─────────────────────┐     └─────────────────────┘
│ events              │
│─────────────────────│     ┌─────────────────────┐
│ run_id (FK)         │     │ logs                │
│ timestamp           │     │─────────────────────│
│ step                │     │ run_id (FK)         │
│ event_type          │     │ timestamp           │
│ severity            │     │ stream              │
│ message             │     │ line                │
│ metadata (JSON)     │     │ step                │
└─────────────────────┘     └─────────────────────┘

┌─────────────────────┐     ┌─────────────────────┐
│ artifacts           │     │ media               │
│─────────────────────│     │─────────────────────│
│ run_id (FK)         │     │ run_id (FK)         │
│ timestamp           │     │ step                │
│ step                │     │ timestamp           │
│ artifact_type       │     │ name                │
│ path                │     │ media_type          │
│ size_bytes          │     │ data (base64/JSON)  │
│ metadata (JSON)     │     └─────────────────────┘
└─────────────────────┘
                            ┌─────────────────────┐
                            │ run_summaries       │
                            │─────────────────────│
                            │ run_id (PK/FK)      │
                            │ computed_at         │
                            │ total_training_time │
                            │ total_steps         │
                            │ steps_per_sec       │
                            │ avg_gpu_utilization │
                            │ peak_memory         │
                            │ anomaly_count       │
                            │ final_metrics (JSON)│
                            │ best_metrics (JSON) │
                            │ efficiency_score    │
                            │ efficiency_details  │
                            └─────────────────────┘
```

---

### Table Details

#### `runs`

One row per training run. Small table, read frequently.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | UUID primary key |
| `name` | `VARCHAR` | Human-readable name |
| `status` | `VARCHAR` | `running`, `completed`, `failed`, `crashed` (no heartbeat), `killed` |
| `last_heartbeat` | `DOUBLE` | Unix timestamp of the last SDK heartbeat or `running` status PATCH (null only for runs created before heartbeats that have not been migrated; the open-time migration fills it or marks them `crashed`) |
| `created_at` | `DOUBLE` | Unix timestamp |
| `finished_at` | `DOUBLE` | Unix timestamp (null if running) |
| `hyperparams` | `JSON` | Arbitrary hyperparameter dict |
| `git_hash` | `VARCHAR` | Git commit hash |
| `git_dirty` | `BOOLEAN` | Uncommitted changes flag |
| `gpu_model` | `VARCHAR` | GPU model name |
| `gpu_memory_total` | `BIGINT` | Total VRAM in bytes (GPU 0; kept for compatibility) |
| `gpus` | `JSON` | Every GPU visible to the run: index, name, uuid, memory_total, driver_version, mig_mode, is_mig, mig_devices (per-instance memory; utilization is always null) |
| `gpu_count` | `INTEGER` | Number of visible GPUs |
| `driver_version` | `VARCHAR` | NVIDIA driver version |
| `cuda_version` | `VARCHAR` | CUDA version string |
| `pytorch_version` | `VARCHAR` | PyTorch version string |
| `python_version` | `VARCHAR` | Python version string |
| `hostname` | `VARCHAR` | Machine hostname |
| `tags` | `JSON` | Array of tag strings |
| `notes` | `VARCHAR` | Free-text notes |
| `project` | `VARCHAR` | Owning project |
| `updated_at` | `DOUBLE` | Last activity: bumped on every successful ingest (not duplicate batches) and every status/metadata change. Backfilled at migration from the newest data of each run. Default run sort |
| `run_group` | `VARCHAR` | `init(group=...)` (exposed as `group`) |
| `archived_at` | `DOUBLE` | Archive stamp (null = active). Archived runs, and runs of archived projects, are hidden from lists by default |

Related organisation tables: `projects` (+ `archived_at`, `retention` JSON policy), `pinned_runs(username, run_id, pinned_at)` for per-user pins,
`schema_meta(key, done_at)` for one-time repairs (legacy duplicate rows), and `alert_rules.updated_at`. A hard delete clears `runs` and every table
that has a `run_id` column (`RUN_CHILD_TABLES` in `db.py`) plus `<data dir>/media/<run_id>/`. `seen_batches` rows live as long as their run.

---

#### `metrics`

Append-only time series. Bulk of experiment tracking data.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `step` | `INTEGER` | Training step number |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `name` | `VARCHAR` | Metric name (e.g., `"train/loss"`) |
| `value` | `DOUBLE` | Metric value |

**Query pattern**: `SELECT step, value FROM metrics WHERE run_id = ? AND name = ? ORDER BY step`

---

#### `profile_events`

GPU profiling data from `torch.profiler`. Can grow large quickly.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `step` | `INTEGER` | Training step |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `name` | `VARCHAR` | Operator name (e.g., `"aten::mm"`) |
| `category` | `VARCHAR` | `cuda_kernel` or `cpu_op` |
| `duration_us` | `DOUBLE` | Total time in microseconds |
| `cuda_time_us` | `DOUBLE` | CUDA time |
| `cpu_time_us` | `DOUBLE` | CPU time |
| `input_shapes` | `JSON` | Input tensor shapes |
| `memory_bytes` | `BIGINT` | Net memory delta |
| `device_id` | `INTEGER` | GPU device index |

---

#### `system_snapshots`

Periodic GPU/system state from pynvml polling.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `gpu_utilization` | `DOUBLE` | GPU utilization (%) |
| `memory_used` | `BIGINT` | Memory used (bytes) |
| `memory_total` | `BIGINT` | Total memory (bytes) |
| `temperature` | `DOUBLE` | Temperature (°C) |
| `power_draw` | `DOUBLE` | Power draw (W) |
| `fan_speed` | `DOUBLE` | Fan speed (%) |
| `sm_clock_mhz` | `INTEGER` | SM clock (MHz) |
| `memory_clock_mhz` | `INTEGER` | Memory clock (MHz) |
| `process_memory_used` | `BIGINT` | Process-attributed memory |
| `process_exclusive` | `BOOLEAN` | Exclusive context flag |
| `attributed_utilization` | `DOUBLE` | Process-attributed GPU util |
| `attributed_power` | `DOUBLE` | Process-attributed power |
| `gpu_index` | `INTEGER` | CUDA ordinal of the GPU within the run's visible devices (NULL on old rows = GPU 0); one row per GPU per poll |

---

#### `param_stats`

Gradient and weight statistics from `run.watch()`.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `step` | `INTEGER` | Training step |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `param_name` | `VARCHAR` | Parameter name |
| `grad_norm` | `DOUBLE` | Gradient L2 norm |
| `grad_min` | `DOUBLE` | Gradient minimum |
| `grad_max` | `DOUBLE` | Gradient maximum |
| `grad_mean` | `DOUBLE` | Gradient mean |
| `weight_norm` | `DOUBLE` | Weight L2 norm |
| `weight_min` | `DOUBLE` | Weight minimum |
| `weight_max` | `DOUBLE` | Weight maximum |
| `weight_mean` | `DOUBLE` | Weight mean |
| `grad_histogram` | `JSON` | 32-bin gradient histogram |
| `weight_histogram` | `JSON` | 32-bin weight histogram |

---

#### `alert_rules`

Admin-managed webhook alert rules: `id`, `name`, `url` (secret), `format`, `project` (NULL = all), `enabled`,
`created_at`, `created_by`, `triggers` (JSON list of `{kind, min_severity, metric, op, threshold}`; the flat
`kind`/`min_severity`/`metric`/`op`/`threshold` columns are the pre-multi-trigger layout, migrated into `triggers`
and unused). Companion tables `alert_sent(rule_id, dkey, sent_at)` (dedupe window + per-minute rate limit) and
`alert_violations(rule_id, trigger_key, run_id, violating)` (threshold-crossing state) persist alert state across
restarts. Run summaries are computed on a managed single-worker executor drained on shutdown. Delivery logic lives in
`pyrometer/server/alerts.py` (one worker thread, bounded queue; hooks in the ingest routes and
`db_mark_stale_runs`). See [server-api.md](server-api.md#alerts).

#### `events`

Structured events (anomalies, checkpoints, custom).

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `step` | `INTEGER` | Associated step |
| `event_type` | `VARCHAR` | Event category |
| `severity` | `VARCHAR` | `info`, `warning`, `error` |
| `message` | `VARCHAR` | Human-readable message |
| `metadata` | `JSON` | Additional context |

---

#### `logs`

Captured stdout/stderr lines.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `stream` | `VARCHAR` | `stdout` or `stderr` |
| `line` | `VARCHAR` | Log line content |
| `step` | `INTEGER` | Associated step |

---

#### `artifacts`

Tracked file artifacts.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `step` | `INTEGER` | Associated step |
| `artifact_type` | `VARCHAR` | `checkpoint`, `config`, etc. |
| `path` | `VARCHAR` | File path |
| `size_bytes` | `BIGINT` | File size |
| `metadata` | `JSON` | Additional context |

---

#### `media`

Images, histograms, figures.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `step` | `INTEGER` | Training step |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `name` | `VARCHAR` | Media name |
| `media_type` | `VARCHAR` | `image`, `histogram`, `figure` |
| `data` | `VARCHAR` | JSON string (histograms) or an inline base64 PNG that could not be decoded; `NULL` for file-backed images/figures |
| `path` | `VARCHAR` | Image/figure file, relative to `<data dir>/media` (`<run_id>/<sha256(name)[:16]>-<step>.png`); `NULL` if the payload is inline |

---

#### `samples`

Generation samples from `run.log_samples`, one row per sample dict.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Foreign key to `runs` |
| `step` | `INTEGER` | Training step |
| `timestamp` | `DOUBLE` | Unix timestamp |
| `name` | `VARCHAR` | Sample stream name |
| `idx` | `INTEGER` | Order within (name, step) |
| `reward` | `DOUBLE` | Numeric `reward` key if present, else NULL |
| `data` | `VARCHAR` | The sample dict as JSON text |

---

#### `run_summaries`

Derived statistics computed when a run finishes.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `VARCHAR` | Primary key, FK to `runs` |
| `computed_at` | `DOUBLE` | Unix timestamp |
| `total_training_time_s` | `DOUBLE` | Wall-clock duration |
| `total_steps` | `INTEGER` | Total training steps |
| `steps_per_sec` | `DOUBLE` | Throughput |
| `per_gpu` | `JSON` | Per-GPU breakdown (avg utilization, peak process / device memory per `gpu_index`) |
| `avg_gpu_utilization` | `DOUBLE` | Average GPU utilization: per-process values over the GPUs this run uses when available, else device-wide across all monitored GPUs |
| `peak_process_memory_bytes` | `BIGINT` | Peak VRAM usage (process-attributed if available, else device), summed over GPUs per poll |
| `anomaly_count` | `INTEGER` | Number of anomaly events |
| `final_metrics` | `JSON` | Final metric values |
| `best_metrics` | `JSON` | Best metric values with direction |
| `efficiency_score` | `VARCHAR` | Letter grade (A-F) |
| `efficiency_details` | `JSON` | Component scores |

---

## Design Decisions

### Why DuckDB?

**Column-oriented storage**: Analytical queries like "give me the loss curve for these 5 runs" or "aggregate kernel times by operator type" are extremely fast without index tuning.

**Embeddable**: Single file, no daemon. Users don't need to install or configure a database server.

**Compression**: Handles millions of profiler events gracefully with built-in compression.

**Parquet export**: Native Parquet support for exporting data to pandas or other tools.

**Trade-offs**:
- Not designed for concurrent multi-process writes (fine for single-user v0.1)
- Single connection + `threading.Lock` serializes all writes

---

### Why Hash Routing?

The FastAPI server serves both the REST API (`GET /runs/{id}`) and the React SPA (`GET /` for all pages).

**Problem**: With browser routing (`/runs/{id}`), a page refresh would hit the FastAPI API endpoint instead of serving the React app.

**Solution**: Hash routing (`/#/runs/{id}`) means the server only ever sees `GET /` for page navigation. The hash fragment is never sent to the server.

**Trade-offs**:
- URLs look less clean (`/#/runs/{id}` vs `/runs/{id}`)
- No server-side rendering or SEO benefits (not needed for a local dashboard)

---

### Why Polling + WebSocket?

**WebSocket**: Primary mechanism for live updates. Pushes small event objects (`{"type": "metrics", "run_id": "..."}`) to connected clients.

**Polling fallback**: 5-second polling ensures the dashboard stays live even if WebSocket fails (proxy misconfiguration, firewall, etc.).

**Data fetching**: Actual data is still fetched over REST on demand. WebSocket only triggers refetches.

**Trade-offs**:
- More network traffic than WebSocket-only (but simpler error handling)
- Polling interval matches SDK flush interval (5s) for efficiency

---

### Why key_averages()?

`torch.profiler.key_averages()` aggregates events by operator name instead of returning raw per-event data.

**Benefits**:
- Dramatically reduces storage and network overhead (thousands → tens of events per step)
- Right granularity for operator breakdown charts
- Training loop overhead is minimal

**Trade-offs**:
- Loses wall-clock ordering — can't build true Gantt timelines
- Phase 6's Kernel Timeline uses a proportional layout as an honest approximation

For true timeline visualization, a future version could capture raw `prof.events()` separately.

---

## Component Details

### SDK Internals

#### Threading Model

```
Main Thread (training loop)
    │
    ├── run.log() → buffer metrics (thread-safe with lock)
    ├── run.watch() hooks → buffer param stats
    └── run.profile() → torch.profiler (synchronous) + background HTTP POST
    │
    ├── Flush Thread (daemon, 5s interval)
    │   ├── Flush metrics buffer
    │   ├── Flush param stats buffer
    │   └── Flush log buffer
    │
    ├── Sender Thread (daemon) — the only thread that talks HTTP
    │   └── Ordered queue → POST with batch id, retry w/ backoff, spill to ~/.pyrometer/spool
    │
    └── System Monitor Thread (daemon, 2s interval)
        └── Poll pynvml → enqueue system snapshot
```

All payloads are enqueued on a `Transport` (`pyrometer/_transport.py`); callers never perform network I/O.

#### Buffer Management

| Buffer | Max Size | Flush Trigger |
|--------|----------|---------------|
| Metrics | 100 entries | 5s OR 100 entries |
| Param stats | Unbounded | 5s |
| Logs | 50 lines | 50 lines OR 5s |

#### Console Capture

`_TeeWriter` wraps `sys.stdout` and `sys.stderr`:

```python
class _TeeWriter:
    def write(self, text):
        self._orig.write(text)  # Display normally
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._on_line(line, "stdout")  # Buffer for server
```

---

### Server Internals

#### Request Flow

```
HTTP POST /runs/{id}/metrics
    │
    ▼
FastAPI route handler
    │
    ▼
Pydantic validation (MetricEntry[])
    │
    ▼
db_insert_metrics() (with lock)
    │
    ▼
_ws.broadcast({"type": "metrics", "run_id": "..."})
    │
    ▼
JSON response
```

#### WebSocket Manager

```python
class _WsManager:
    _connections: list[WebSocket]
    _last_broadcast: dict[tuple[str, str], float]  # throttle state

    async def broadcast(payload, throttle_key=None, throttle_ms=500, project=None, readable=None):
        # Throttle to avoid flooding during rapid updates (the throttle map is bounded)
        # Per connection: re-authenticate (revoked key/session -> close 1008), then filter by project visibility
        # Fire-and-forget send (dead connections silently removed)
```

`broadcast_move` sends `run_moved` / `run_deleted` / `run_created` depending on which of the old and new projects a client can
read, so no private project name leaks. Idle sockets re-validate their credential every `PYROMETER_WS_RECHECK` seconds.

#### Static File Serving

Custom `_SPAMount` class extends Starlette's `Mount` to return `Match.NONE` for non-HTTP connections. This prevents the catch-all `/` mount from matching WebSocket upgrade requests.

---

### Frontend Architecture

#### Component Hierarchy

```
App.tsx
├── RunSidebar
│   ├── Run row (checkbox + NavLink + StatusBadge)
│   └── Footer (Compare button)
│
└── Routes
    ├── EmptyState
    ├── RunDetailPage
    │   ├── Header (StatusBadge, summary stats)
    │   ├── HyperparamTable
    │   ├── MetricChart (×N)
    │   ├── KernelTimeline
    │   ├── OperatorBreakdown
    │   ├── MemoryDeltaChart
    │   ├── GradientHeatmap
    │   ├── LogViewer
    │   └── MediaGallery
    │
    └── CompareView
        ├── CompareChart (×N)
        └── HyperparamDiff
```

#### State Management

- **No global state library** (Redux, Zustand, etc.)
- **Local state**: `useState` in each component
- **Shared selection**: Lifted to `App.tsx` for run comparison
- **Stable refs**: `useRef` for WebSocket handlers to avoid stale closures

#### Data Fetching

```typescript
// RunDetailPage.tsx
useEffect(() => {
  async function initialLoad() {
    const [run, metrics, snapshots, ...] = await Promise.all([
      api.getRun(runId),
      api.getMetrics(runId),
      api.getSystemSnapshots(runId),
      // ...
    ])
    // Update state
  }
  initialLoad()
  
  // Poll every 5s if run is still active
  if (run.status === 'running') {
    intervalRef.current = setInterval(initialLoad, 5000)
  }
}, [runId])
```

#### WebSocket Hook

```typescript
// useWebSocket.ts
let _socket: WebSocket | null = null
let _handlers = new Set<Handler>()

export function useWebSocket(handler: Handler) {
  const handlerRef = useRef(handler)
  handlerRef.current = handler
  
  useEffect(() => {
    const fn = (msg) => handlerRef.current(msg)
    _handlers.add(fn)
    _ensureConnected()
    return () => _handlers.delete(fn)
  }, [])
}
```

---

## Threading Model

### SDK Threads

| Thread | Purpose | Interval |
|--------|---------|----------|
| Main | Training loop, `run.log()`, `run.profile()` | User-defined |
| Flush | Ship buffered metrics/logs | 5s |
| System Monitor | Poll pynvml, POST snapshots | 2s |
| Profile shipper | POST profile events (spawned per profile call) | On-demand |
| Anomaly detector | POST anomaly events (spawned per detection) | On-demand |

### Server Threads

| Thread | Purpose |
|--------|---------|
| Uvicorn workers | Handle HTTP requests (default: 1 worker) |
| DuckDB lock | Serialize all DB writes (single connection) |
| WebSocket broadcast | Fire-and-forget sends in request handlers |

---

## Error Handling

### SDK

- Network I/O happens only on the sender thread; failures are retried with exponential backoff (cap 60 s) and never block training
- Every payload is journaled to `~/.pyrometer/spool/<run_id>/queue.jsonl` (`PYROMETER_SPOOL_DIR`) before sending and acked after;
  the 500-item memory queue is just a cache of it. The sender replays the backlog automatically after an outage; leftovers after
  exit/SIGKILL go up with `pyrometer sync` (which skips runs whose process holds `owner.lock`). Metrics, stdout lines and gradient stats
  buffered in memory are mirrored in `metrics.wal` / `logs.wal` / `param_stats.wal`. Media live in `blobs/`, poison payloads in `dead.jsonl`. `init()` with the server down
  degrades: the run is created by the sender once the server answers
- Every payload has a batch id; the server's `seen_batches` table (run_id, batch_id, seen_at) discards repeats
- `run.finish()` makes a 10 s final drain, leaves the rest in the journal, prints one warning, and doesn't raise

### Server

- Pydantic validation catches malformed requests
- HTTPException with consistent error envelope
- DuckDB errors caught at DB layer, re-raised as 500
- WebSocket send failures silently remove dead connections

### Frontend

- Error boundaries not implemented in v0.1 (known gap)
- API errors shown as inline messages
- WebSocket disconnect triggers reconnect with backoff
