# Pyrometer Documentation

**Self-hosted PyTorch experiment dashboard with integrated GPU profiling**

Track metrics, compare runs, and profile GPU kernels — all local, no cloud, no accounts.

---

## Table of Contents

### Getting Started
- [Quickstart](#quickstart)
- [Installation](#installation)
- [Basic Usage](#basic-usage)

### SDK Reference
- [`pyrometer.init()`](sdk-reference.md#pyrometerinit)
- [`run.log()`](sdk-reference.md#runlog)
- [`run.watch()`](sdk-reference.md#runwatch)
- [`run.profile()`](sdk-reference.md#runprofile)
- [`run.finish()`](sdk-reference.md#runfinish)
- [Media Logging](sdk-reference.md#media-logging)
- [Events & Artifacts](sdk-reference.md#events--artifacts)

### Web Application
- [Dashboard Overview](webapp-guide.md#dashboard-overview)
- [Run Sidebar](webapp-guide.md#run-sidebar)
- [Run Detail View](webapp-guide.md#run-detail-view)
- [Run Comparison](webapp-guide.md#run-comparison)
- [Profiler View](webapp-guide.md#profiler-view)
- [Model Health](webapp-guide.md#model-health)

### Authentication & Deployment
- [Authentication](auth.md)
- [Deployment (Docker, nginx, authentik)](deployment.md)

### Server API
- [REST API Reference](server-api.md#rest-api-reference)
- [WebSocket Events](server-api.md#websocket-events)

### Architecture
- [System Architecture](architecture.md#system-architecture)
- [Data Model](architecture.md#data-model)
- [Design Decisions](architecture.md#design-decisions)

### Development
- [Developer Guide](development.md#developer-guide)
- [Project Structure](development.md#project-structure)
- [Extending Pyrometer](development.md#extending-pyrometer)

---

## Quickstart

### 1. Start the server

```bash
# Clone and install
git clone <repo>
cd pyrometer
uv sync

# Start the server (default: http://localhost:8321)
uv run pyrometer serve --no-auth        # local mode (loopback only)

# ...or with auth (the default): create a key, then export it for your scripts
uv run pyrometer serve
uv run pyrometer keys create --name my-project --scopes ingest
export PYROMETER_API_KEY=pyro_...
```

### 2. Add Pyrometer to your training script

```python
import pyrometer

run = pyrometer.init(
    name="my-experiment",
    hyperparams={
        "optimizer": "AdamW",
        "lr": 3e-4,
        "batch_size": 64,
    },
    tags=["baseline"],
)

for step, batch in enumerate(dataloader):
    loss = train_step(model, batch)
    run.log({
        "train/loss": loss.item(),
        "lr": scheduler.get_last_lr()[0],
    }, step=step)

run.finish()
```

### 3. Open the dashboard

Navigate to **http://localhost:8321** to see your runs, metrics, and GPU profiling data.

---

## Installation

### Prerequisites

- Python 3.12+
- (Optional) NVIDIA GPU with CUDA for GPU profiling
- (Optional) PyTorch for deep learning integration

### Install with `uv`

```bash
git clone <repo>
cd pyrometer
uv sync
```

### Install as a pip package

```bash
pip install pyrometer
```

### Verify installation

```bash
uv run pyrometer serve --host 0.0.0.0 --port 8321
```

---

## Basic Usage

### Minimal example

```python
import pyrometer

# Initialize a run
run = pyrometer.init(
    name="my-first-run",
    hyperparams={"lr": 3e-4, "batch_size": 64},
)

# Training loop
for epoch in range(10):
    for step, batch in enumerate(dataloader):
        loss = model.train_step(batch)
        run.log({"train/loss": loss.item()}, step=step)

# Always call finish() at the end
run.finish()
```

### With GPU profiling

```python
import pyrometer

run = pyrometer.init(name="profiled-run")

for epoch in range(100):
    for step, batch in enumerate(dataloader):
        # Profile every 10th step
        if step % 10 == 0:
            with run.profile(step=step):
                loss = train_step(model, batch)
        else:
            loss = train_step(model, batch)
        
        run.log({"train/loss": loss.item()}, step=step)
    
    run.log({"val/loss": validate(model)}, step=step)

run.finish()
```

### With gradient monitoring

```python
import pyrometer
import torch.nn as nn

model = MyModel()
run = pyrometer.init(name="monitored-run")

# Monitor gradient statistics every 50 backward passes
run.watch(model, log_freq=50)

for step, batch in enumerate(dataloader):
    loss = train_step(model, batch)
    loss.backward()
    optimizer.step()
    run.log({"train/loss": loss.item()}, step=step)

run.finish()
```

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Zero-config SDK** | Drop into any PyTorch script with 3 lines of code |
| **Secure by default** | API keys (hashed at rest) and authentik/proxy support; see [auth.md](auth.md) |
| **Local storage** | All data stored in a single DuckDB file (`~/.pyrometer/data.duckdb`) |
| **GPU profiling** | Kernel-level timing, memory allocation tracking via `torch.profiler` |
| **System monitoring** | Automatic GPU utilization, temperature, power draw polling via pynvml |
| **Gradient monitoring** | Per-parameter gradient norms, histograms, anomaly detection |
| **Run comparison** | Multi-run metric overlay with hyperparameter diff tables |
| **Live updates** | WebSocket-powered real-time dashboard updates during training |
| **Console capture** | Automatic stdout/stderr capture associated with training steps |
| **Media logging** | Log images, histograms, and matplotlib figures |
| **LLM-ready export** | One-click copy of run summaries formatted for LLM analysis |

---

## Architecture Overview

```
Training Script (any machine)
    │
    ├── pyrometer SDK
    │   ├── run.log() → buffers metrics, flushes every 5s/100 entries
    │   ├── run.watch() → gradient/weight monitoring hooks
    │   ├── run.profile() → torch.profiler wrapper
    │   ├── System monitor thread → pynvml polling every 2s
    │   └── Console capture → tee stdout/stderr
    │
    ▼
FastAPI Server (localhost:8321)
    │
    ├── REST API endpoints
    ├── WebSocket push endpoint
    └── DuckDB storage
        ├── runs
        ├── metrics
        ├── profile_events
        ├── system_snapshots
        ├── param_stats
        ├── events
        ├── logs
        ├── artifacts
        └── media
    │
    ▼
React Dashboard (single-page app)
    ├── Run sidebar with selection
    ├── Run detail with metrics, profiler, model health
    └── Run comparison view
```

---

## Storage

Data is stored in `~/.pyrometer/data.duckdb` — a single file, no daemon required.

### Override storage location

```bash
PYROMETER_DB_PATH=/path/to/data.duckdb uv run pyrometer serve
```

### Query the database directly

```python
import duckdb

conn = duckdb.connect("~/.pyrometer/data.duckdb")

# List all runs
conn.execute("SELECT name, status, created_at FROM runs ORDER BY created_at DESC").df()

# Get metrics for a specific run
conn.execute("""
    SELECT step, name, value 
    FROM metrics 
    WHERE run_id = 'your-run-id' 
    AND name = 'train/loss'
    ORDER BY step
""").df()
```

---

## Example Scripts

The `examples/` directory contains ready-to-run examples:

| Script | Description |
|--------|-------------|
| `synthetic_train.py` | Minimal working example with synthetic data |
| `lr_sweep.py` | Learning rate sweep with multiple runs |
| `optimizer_compare.py` | Compare different optimizers |
| `ablation_study.py` | Ablation study with hyperparameter variations |

Run an example:

```bash
# Terminal 1: Start the server
uv run pyrometer serve

# Terminal 2: Run the example
uv run python examples/synthetic_train.py
```

---

## Server Options

```bash
# Default (127.0.0.1:8321, auth required)
uv run pyrometer serve

# Local mode, no auth (loopback only)
uv run pyrometer serve --no-auth

# API keys
uv run pyrometer keys create --name my-project --scopes ingest
uv run pyrometer keys list
uv run pyrometer keys revoke <id>

# Custom host and port
uv run pyrometer serve --host 0.0.0.0 --port 8080

# Environment variables
PYROMETER_DB_PATH=/custom/path/db.duckdb      # database file
PYROMETER_KEYS_FILE=/custom/path/keys.json    # API key store
PYROMETER_TRUSTED_PROXIES=127.0.0.1           # peers whose identity header is trusted
PYROMETER_AUTH=off                            # same as --no-auth
```

---

## Support

- **GitHub Issues**: Report bugs and feature requests
- **Documentation**: Browse the full docs in the `docs/` directory
