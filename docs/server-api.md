# Pyrometer Server API Reference

Complete REST API and WebSocket reference for the Pyrometer server.

---

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [Response Format](#response-format)
- [Health](#health)
- [Runs](#runs)
  - [Create Run](#post-runs)
  - [List Runs](#get-runs)
  - [Get Run](#get-runsrun_id)
  - [Update Run](#patch-runsrun_id)
- [Metrics](#metrics)
  - [Ingest Metrics](#post-runsrun_idmetrics)
  - [Get Metrics](#get-runsrun_idmetrics)
  - [Get Metric Names](#get-runsrun_idmetric-names)
- [Profile Events](#profile-events)
  - [Ingest Profile](#post-runsrun_idprofile)
  - [Get Profile](#get-runsrun_idprofile)
  - [Get Profile Steps](#get-runsrun_idprofilesteps)
- [System Snapshots](#system-snapshots)
  - [Ingest Snapshot](#post-runsrun_idsystem)
  - [Get Snapshots](#get-runsrun_idsystem)
- [Parameter Stats](#parameter-stats)
  - [Ingest Stats](#post-runsrun_idparam-stats)
  - [Get Stats](#get-runsrun_idparam-stats)
  - [Get Param Names](#get-runsrun_idparam-statsnames)
- [Events](#events)
  - [Ingest Event](#post-runsrun_idevents)
  - [Get Events](#get-runsrun_idevents)
- [Logs](#logs)
  - [Ingest Logs](#post-runsrun_idlogs)
  - [Get Logs](#get-runsrun_idlogs)
- [Artifacts](#artifacts)
  - [Ingest Artifact](#post-runsrun_idartifacts)
  - [Get Artifacts](#get-runsrun_idartifacts)
- [Media](#media)
  - [Ingest Media](#post-runsrun_idmedia)
  - [Get Media](#get-runsrun_idmedia)
  - [Get Media Names](#get-runsrun_idmedianames)
- [Run Summaries](#run-summaries)
  - [Get Summary](#get-runsrun_idsummary)
  - [Get LLM Summary](#get-runsrun_idllm-summary)
- [WebSocket](#websocket)
  - [Connect](#get-ws)
  - [Event Types](#event-types)

---

## Overview

The Pyrometer server is a FastAPI application that:

- Receives metrics, profiles, and system data from the SDK
- Serves the React dashboard as static files
- Pushes real-time updates via WebSocket

**Base URL**: `http://localhost:8321`

**Default port**: 8321 (configurable via `--port`)

## Authentication

Every endpoint except `GET /health` and the `/auth/*` endpoints below requires a
credential (see [auth.md](auth.md)). Reads (`GET`, WebSocket `/ws`) need the `read`
scope; writes (`POST`/`PATCH`) need `ingest`.

```bash
curl -H "Authorization: Bearer $PYROMETER_API_KEY" http://localhost:8321/runs
```

| Status | Meaning |
|--------|---------|
| `401` | Missing or invalid credentials (`WWW-Authenticate: Bearer`) |
| `403` | Valid key, but it lacks the required scope |

| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/session` | `{"auth": "required"\|"off", "authenticated": bool, "user": str\|null}` |
| `POST` | `/auth/login` | Body `{"key": "..."}`. Exchanges a `read` key for an HttpOnly session cookie |
| `POST` | `/auth/logout` | Clears the session cookie |

In `--no-auth` local mode all endpoints are open. A WebSocket without valid credentials is rejected at the handshake.

---

## Response Format

All endpoints return a consistent JSON envelope:

### Success

```json
{
  "data": <payload>,
  "error": null
}
```

### Error

HTTP status code indicates the error type, with details in the response:

```json
{
  "data": null,
  "error": "Run not found"
}
```

---

## Health

### `GET /health`

Health check endpoint.

**Response**:

```json
{
  "data": {
    "status": "ok"
  },
  "error": null
}
```

---

## Runs

### `POST /runs`

Create a new training run.

**Request Body**:

```json
{
  "name": "my-experiment",
  "hyperparams": {
    "lr": 0.0003,
    "batch_size": 64
  },
  "tags": ["baseline"],
  "notes": "Initial baseline run",
  "git_hash": "abc123...",
  "git_dirty": false,
  "gpu_model": "NVIDIA A100",
  "gpu_memory_total": 42949672960,
  "cuda_version": "12.1",
  "pytorch_version": "2.1.0",
  "python_version": "3.12.0",
  "hostname": "gpu-server-1"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` | No | Human-readable run name. Auto-generated if omitted. |
| `hyperparams` | `object` | No | Arbitrary hyperparameter dict. |
| `tags` | `string[]` | No | List of tags for filtering. |
| `notes` | `string` | No | Free-text notes. |
| `git_hash` | `string` | No | Git commit hash. |
| `git_dirty` | `boolean` | No | True if uncommitted changes exist. |
| `gpu_model` | `string` | No | GPU model name. |
| `gpu_memory_total` | `integer` | No | Total VRAM in bytes. |
| `cuda_version` | `string` | No | CUDA version string. |
| `pytorch_version` | `string` | No | PyTorch version string. |
| `python_version` | `string` | No | Python version string. |
| `hostname` | `string` | No | Machine hostname. |

**Response** (201 Created):

```json
{
  "data": {
    "run_id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "my-experiment",
    "status": "running",
    "created_at": "2024-01-15T10:30:00Z"
  },
  "error": null
}
```

**WebSocket Event**: `{"type": "run_created", "run_id": "..."}`

---

### `GET /runs`

List all runs, sorted newest-first.

**Response**:

```json
{
  "data": [
    {
      "run_id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "my-experiment",
      "status": "running",
      "created_at": "2024-01-15T10:30:00Z",
      "finished_at": null,
      "hyperparams": {"lr": 0.0003},
      "gpu_model": "NVIDIA A100",
      "tags": ["baseline"]
    }
  ],
  "error": null
}
```

---

### `GET /runs/{run_id}`

Get details for a specific run.

**Response**:

```json
{
  "data": {
    "run_id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "my-experiment",
    "status": "running",
    "created_at": "2024-01-15T10:30:00Z",
    "finished_at": null,
    "hyperparams": {"lr": 0.0003, "batch_size": 64},
    "git_hash": "abc123...",
    "git_dirty": false,
    "gpu_model": "NVIDIA A100",
    "gpu_memory_total": 42949672960,
    "cuda_version": "12.1",
    "pytorch_version": "2.1.0",
    "python_version": "3.12.0",
    "hostname": "gpu-server-1",
    "tags": ["baseline"],
    "notes": "Initial baseline run"
  },
  "error": null
}
```

**404 Response**:

```json
{
  "data": null,
  "error": "Run not found"
}
```

---

### `PATCH /runs/{run_id}`

Update run status or notes.

**Request Body**:

```json
{
  "status": "completed",
  "notes": "Updated notes"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | `string` | No | One of: `running`, `completed`, `crashed`, `killed` |
| `notes` | `string` | No | Updated notes text |

**Response**:

```json
{
  "data": {
    "updated": true
  },
  "error": null
}
```

**WebSocket Event**: `{"type": "run_updated", "run_id": "..."}`

---

## Metrics

### `POST /runs/{run_id}/metrics`

Ingest a batch of metric entries.

**Request Body**:

```json
[
  {
    "step": 100,
    "timestamp": 1705315800.123,
    "name": "train/loss",
    "value": 0.42
  },
  {
    "step": 100,
    "timestamp": 1705315800.123,
    "name": "lr",
    "value": 0.0003
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `step` | `integer` | Training step number |
| `timestamp` | `float` | Unix timestamp (seconds) |
| `name` | `string` | Metric name (e.g., `"train/loss"`) |
| `value` | `float` | Metric value |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": 2
  },
  "error": null
}
```

**WebSocket Event**: `{"type": "metrics", "run_id": "..."}`

---

### `GET /runs/{run_id}/metrics`

Get all metrics for a run, optionally filtered by name.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `string` | Optional metric name filter |

**Request Examples**:

```bash
# All metrics
curl http://localhost:8321/runs/{run_id}/metrics

# Filter by name
curl "http://localhost:8321/runs/{run_id}/metrics?name=train/loss"
```

**Response**:

```json
{
  "data": [
    {
      "step": 0,
      "timestamp": 1705315800.0,
      "name": "train/loss",
      "value": 2.302
    },
    {
      "step": 1,
      "timestamp": 1705315800.1,
      "name": "train/loss",
      "value": 2.105
    }
  ],
  "error": null
}
```

---

### `GET /runs/{run_id}/metric-names`

Get distinct metric names for a run.

**Response**:

```json
{
  "data": ["lr", "train/accuracy", "train/loss", "val/loss"],
  "error": null
}
```

---

## Profile Events

### `POST /runs/{run_id}/profile`

Ingest profile events from a profiled step.

**Request Body**:

```json
[
  {
    "step": 100,
    "timestamp": 1705315800.123,
    "name": "aten::mm",
    "category": "cuda_kernel",
    "duration_us": 450.5,
    "cuda_time_us": 400.0,
    "cpu_time_us": 50.5,
    "input_shapes": [[64, 512], [512, 256]],
    "memory_bytes": 1048576,
    "device_id": 0
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `step` | `integer` | Training step number |
| `timestamp` | `float` | Unix timestamp |
| `name` | `string` | Operator name (e.g., `"aten::mm"`) |
| `category` | `string` | `cuda_kernel` or `cpu_op` |
| `duration_us` | `float` | Total time in microseconds |
| `cuda_time_us` | `float` | CUDA time in microseconds |
| `cpu_time_us` | `float` | CPU time in microseconds |
| `input_shapes` | `array` | List of input tensor shapes |
| `memory_bytes` | `integer` | Net memory delta (positive=alloc, negative=free) |
| `device_id` | `integer` | GPU device index |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": 1
  },
  "error": null
}
```

---

### `GET /runs/{run_id}/profile`

Get profile events for a run, optionally filtered by step.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `step` | `integer` | Optional step filter |

**Response**:

```json
{
  "data": [
    {
      "step": 100,
      "timestamp": 1705315800.123,
      "name": "aten::mm",
      "category": "cuda_kernel",
      "duration_us": 450.5,
      "cuda_time_us": 400.0,
      "cpu_time_us": 50.5,
      "input_shapes": [[64, 512], [512, 256]],
      "memory_bytes": 1048576,
      "device_id": 0
    }
  ],
  "error": null
}
```

Results are sorted by `duration_us DESC` (most expensive operators first).

---

### `GET /runs/{run_id}/profile/steps`

Get the sorted list of steps that have profile data.

**Response**:

```json
{
  "data": [0, 100, 200, 400, 800],
  "error": null
}
```

---

## System Snapshots

### `POST /runs/{run_id}/system`

Ingest a GPU system snapshot.

**Request Body**:

```json
{
  "timestamp": 1705315800.123,
  "gpu_utilization": 87.5,
  "memory_used": 32212254720,
  "memory_total": 42949672960,
  "temperature": 72.0,
  "power_draw": 250.5,
  "fan_speed": 65.0,
  "sm_clock_mhz": 1410,
  "memory_clock_mhz": 1593,
  "process_memory_used": 30064771072,
  "process_exclusive": false,
  "attributed_utilization": 85.2,
  "attributed_power": 245.0
}
```

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `float` | Unix timestamp |
| `gpu_utilization` | `float` | Device-wide GPU utilization (%) |
| `memory_used` | `integer` | Device-wide memory used (bytes) |
| `memory_total` | `integer` | Total device memory (bytes) |
| `temperature` | `float` | GPU temperature (°C) |
| `power_draw` | `float` | Power draw (W) |
| `fan_speed` | `float` | Fan speed (%) |
| `sm_clock_mhz` | `integer` | SM clock frequency (MHz) |
| `memory_clock_mhz` | `integer` | Memory clock frequency (MHz) |
| `process_memory_used` | `integer` | Process-attributed memory (bytes) |
| `process_exclusive` | `boolean` | True if process has exclusive context |
| `attributed_utilization` | `float` | Process-attributed GPU utilization (%) |
| `attributed_power` | `float` | Process-attributed power draw (W) |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": true
  },
  "error": null
}
```

**WebSocket Event**: `{"type": "system", "run_id": "..."}`

---

### `GET /runs/{run_id}/system`

Get all system snapshots for a run.

**Response**:

```json
{
  "data": [
    {
      "timestamp": 1705315800.0,
      "gpu_utilization": 85.0,
      "memory_used": 30064771072,
      "memory_total": 42949672960,
      "temperature": 70.0,
      "power_draw": 240.0,
      "fan_speed": 60.0,
      "sm_clock_mhz": 1410,
      "memory_clock_mhz": 1593,
      "process_memory_used": 28991209472,
      "process_exclusive": false,
      "attributed_utilization": 82.5,
      "attributed_power": 235.0
    }
  ],
  "error": null
}
```

---

## Parameter Stats

### `POST /runs/{run_id}/param-stats`

Ingest gradient and weight statistics.

**Request Body**:

```json
[
  {
    "step": 100,
    "timestamp": 1705315800.123,
    "param_name": "encoder.layer.0.attn.query.weight",
    "grad_norm": 0.0234,
    "grad_min": -0.15,
    "grad_max": 0.18,
    "grad_mean": 0.001,
    "weight_norm": 1.234,
    "weight_min": -0.5,
    "weight_max": 0.6,
    "weight_mean": 0.01,
    "grad_histogram": [0.0, 0.1, 0.2, ...],
    "weight_histogram": [0.0, 0.05, 0.15, ...]
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `step` | `integer` | Training step |
| `timestamp` | `float` | Unix timestamp |
| `param_name` | `string` | Full parameter name from `named_parameters()` |
| `grad_norm` | `float` | Gradient L2 norm |
| `grad_min` | `float` | Gradient minimum |
| `grad_max` | `float` | Gradient maximum |
| `grad_mean` | `float` | Gradient mean |
| `weight_norm` | `float` | Weight L2 norm |
| `weight_min` | `float` | Weight minimum |
| `weight_max` | `float` | Weight maximum |
| `weight_mean` | `float` | Weight mean |
| `grad_histogram` | `float[]` | 32-bin gradient histogram |
| `weight_histogram` | `float[]` | 32-bin weight histogram |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": 1
  },
  "error": null
}
```

**WebSocket Event**: `{"type": "param_stats", "run_id": "..."}`

---

### `GET /runs/{run_id}/param-stats`

Get parameter statistics, optionally filtered by parameter name.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `string` | Optional parameter name filter |

**Response**:

```json
{
  "data": [
    {
      "step": 100,
      "timestamp": 1705315800.123,
      "param_name": "encoder.layer.0.attn.query.weight",
      "grad_norm": 0.0234,
      "grad_min": -0.15,
      "grad_max": 0.18,
      "grad_mean": 0.001,
      "weight_norm": 1.234,
      "weight_min": -0.5,
      "weight_max": 0.6,
      "weight_mean": 0.01,
      "grad_histogram": [0.0, 0.1, ...],
      "weight_histogram": [0.0, 0.05, ...]
    }
  ],
  "error": null
}
```

---

### `GET /runs/{run_id}/param-stats/names`

Get parameter names in insertion order (preserves `named_parameters()` order).

**Response**:

```json
{
  "data": [
    "encoder.layer.0.attn.query.weight",
    "encoder.layer.0.attn.query.bias",
    "encoder.layer.0.attn.key.weight",
    ...
  ],
  "error": null
}
```

---

## Events

### `POST /runs/{run_id}/events`

Ingest a structured event.

**Request Body**:

```json
{
  "timestamp": 1705315800.123,
  "step": 100,
  "event_type": "checkpoint_saved",
  "severity": "info",
  "message": "Best model saved (val_loss=0.23)",
  "metadata": {"path": "/ckpt/model.pt", "val_loss": 0.23}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `float` | Unix timestamp |
| `step` | `integer` | Optional training step |
| `event_type` | `string` | Event category (see below) |
| `severity` | `string` | `info`, `warning`, or `error` |
| `message` | `string` | Human-readable message |
| `metadata` | `object` | Optional JSON metadata |

**Event Types**:

| Type | Description |
|------|-------------|
| `custom` | User-defined event |
| `checkpoint_saved` | Model checkpoint saved |
| `lr_scheduled` | Learning rate changed |
| `anomaly_detected` | Automatic anomaly (gradient spike, dead layer, NaN) |
| `cuda_error` | CUDA runtime error |
| `oom_warning` | Out-of-memory warning |
| `thermal_throttle` | GPU thermal throttling |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": true
  },
  "error": null
}
```

---

### `GET /runs/{run_id}/events`

Get all events for a run.

**Response**:

```json
{
  "data": [
    {
      "timestamp": 1705315800.0,
      "step": 100,
      "event_type": "checkpoint_saved",
      "severity": "info",
      "message": "Best model saved",
      "metadata": {"val_loss": 0.23}
    }
  ],
  "error": null
}
```

---

## Logs

### `POST /runs/{run_id}/logs`

Ingest console log lines.

**Request Body**:

```json
[
  {
    "timestamp": 1705315800.123,
    "stream": "stdout",
    "line": "Epoch 1/100 — train_loss=2.34  val_loss=2.10",
    "step": 100
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `float` | Unix timestamp |
| `stream` | `string` | `stdout` or `stderr` |
| `line` | `string` | Log line content |
| `step` | `integer` | Optional associated step |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": 1
  },
  "error": null
}
```

**WebSocket Event**: `{"type": "logs", "run_id": "..."}`

---

### `GET /runs/{run_id}/logs`

Get console logs, optionally filtered by stream.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `stream` | `string` | Optional filter: `stdout` or `stderr` |

**Response**:

```json
{
  "data": [
    {
      "timestamp": 1705315800.0,
      "stream": "stdout",
      "line": "Epoch 1/100 starting...",
      "step": 0
    }
  ],
  "error": null
}
```

---

## Artifacts

### `POST /runs/{run_id}/artifacts`

Track a file artifact.

**Request Body**:

```json
{
  "timestamp": 1705315800.123,
  "step": 100,
  "artifact_type": "checkpoint",
  "path": "/checkpoints/model_epoch50.pt",
  "size_bytes": 268435456,
  "metadata": {"epoch": 50, "val_loss": 0.23}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `float` | Unix timestamp |
| `step` | `integer` | Optional associated step |
| `artifact_type` | `string` | `checkpoint`, `config`, `dataset`, `export` |
| `path` | `string` | File path |
| `size_bytes` | `integer` | File size in bytes |
| `metadata` | `object` | Optional JSON metadata |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": true
  },
  "error": null
}
```

---

### `GET /runs/{run_id}/artifacts`

Get all artifacts for a run.

**Response**:

```json
{
  "data": [
    {
      "timestamp": 1705315800.0,
      "step": 5000,
      "artifact_type": "checkpoint",
      "path": "/checkpoints/model_epoch50.pt",
      "size_bytes": 268435456,
      "metadata": {"epoch": 50, "val_loss": 0.23}
    }
  ],
  "error": null
}
```

---

## Media

### `POST /runs/{run_id}/media`

Ingest media (images, histograms, figures).

**Request Body**:

```json
{
  "step": 100,
  "timestamp": 1705315800.123,
  "name": "predictions",
  "media_type": "image",
  "data": "iVBORw0KGgoAAAANSUhEUgAA..."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `step` | `integer` | Training step |
| `timestamp` | `float` | Unix timestamp |
| `name` | `string` | Media name (e.g., `"predictions"`) |
| `media_type` | `string` | `image`, `histogram`, or `figure` |
| `data` | `string` | Base64 PNG for images/figures, JSON string for histograms |

**Response** (201 Created):

```json
{
  "data": {
    "inserted": true
  },
  "error": null
}
```

---

### `GET /runs/{run_id}/media`

Get media, optionally filtered by name.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `string` | Optional media name filter |

**Response**:

```json
{
  "data": [
    {
      "step": 100,
      "timestamp": 1705315800.0,
      "name": "predictions",
      "media_type": "image",
      "data": "iVBORw0KGgoAAAANSUhEUgAA..."
    }
  ],
  "error": null
}
```

---

### `GET /runs/{run_id}/media/names`

Get media names in insertion order.

**Response**:

```json
{
  "data": ["predictions", "confusion_matrix", "attention_maps"],
  "error": null
}
```

---

## Run Summaries

### `GET /runs/{run_id}/summary`

Get derived statistics for a run.

**Response**:

```json
{
  "data": {
    "run_id": "550e8400-e29b-41d4-a716-446655440000",
    "computed_at": 1705315800.0,
    "total_training_time_s": 2712.5,
    "total_steps": 10000,
    "steps_per_sec": 3.69,
    "avg_gpu_utilization": 87.3,
    "peak_process_memory_bytes": 32212254720,
    "anomaly_count": 2,
    "final_metrics": {
      "train/loss": 0.0234,
      "val/accuracy": 0.9456
    },
    "best_metrics": {
      "val/loss": {
        "value": 0.23,
        "step": 8500,
        "direction": "min"
      }
    },
    "efficiency_score": "A",
    "efficiency_details": {
      "gpu_utilization": 0.873,
      "memory_efficiency": 0.75,
      "compute_efficiency": 0.92
    }
  },
  "error": null
}
```

If no summary has been computed (e.g., run still in progress), one is computed on-the-fly and cached.

---

### `GET /runs/{run_id}/llm-summary`

Get a plain-text summary formatted for LLM pasting.

**Response** (text/plain):

```
=== my-experiment ===
Status: completed  |  Duration: 45m 12s  |  Efficiency: A
Started: 2024-01-15T10:30:00Z

HYPERPARAMETERS
  optimizer: AdamW
  lr: 0.0003
  batch_size: 64

HARDWARE
  GPU: NVIDIA A100-SXM4-40GB (40.0 GB)
  Python 3.12.0  |  PyTorch 2.1.0  |  CUDA 12.1

GIT
  Commit: abc12345 (clean)

METRICS — Final values
  train/loss                   0.0234
  val/accuracy                 0.9456

SYSTEM
  Steps: 10000 (45.2 steps/sec)
  Avg GPU utilization: 87.3%
  Peak VRAM: 32.4 GB / 40.0 GB

EVENTS (3 total, 1 anomalies)
  [step 5000] checkpoint_saved (info): Best model saved
```

---

## WebSocket

### `GET /ws`

WebSocket endpoint for real-time push notifications.

**Connection**:

```javascript
const ws = new WebSocket("ws://localhost:8321/ws");
```

**Keep-alive**: The server ignores client messages; the connection is kept alive by the client.

**Reconnection**: Clients should implement exponential backoff on disconnect.

---

### Event Types

The server broadcasts JSON events to all connected clients:

```json
{"type": "run_created", "run_id": "550e8400-e29b-41d4-a716-446655440000"}
```

| Type | Trigger | Description |
|------|---------|-------------|
| `run_created` | `POST /runs` | New run registered |
| `run_updated` | `PATCH /runs/{id}` | Run status/notes changed |
| `metrics` | `POST /runs/{id}/metrics` | New metrics ingested |
| `system` | `POST /runs/{id}/system` | New system snapshot |
| `param_stats` | `POST /runs/{id}/param-stats` | New gradient stats |
| `logs` | `POST /runs/{id}/logs` | New console logs |

**Throttling**: Events are throttled per-run to 500ms to avoid flooding clients during rapid updates.

**Client behavior**: On receiving an event, clients should refetch the relevant data via REST.

---

## Error Codes

| HTTP Status | Description |
|-------------|-------------|
| 200 | Success |
| 201 | Created (successful ingestion) |
| 400 | Bad request (invalid JSON, missing fields) |
| 404 | Not found (run doesn't exist) |
| 500 | Internal server error |

---

## Rate Limiting

The server does not implement rate limiting in v0.1. All endpoints are designed for single-user, local deployment.

---

## CORS

CORS is enabled for all origins (`*`) to allow development from different ports. This is intentional for the self-hosted, single-user use case.
