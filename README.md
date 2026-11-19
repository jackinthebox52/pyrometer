# Pyrometer

Self-hosted PyTorch experiment dashboard with integrated GPU profiling.
Track metrics, compare runs, and profile GPU kernels — all local, no cloud, no accounts.

**Status: Phase 1 complete** — SDK + API server + DuckDB storage. Frontend coming in Phase 2.

---

## Quickstart

```bash
# Install
git clone <repo>
cd pyrometer
uv sync

# Start the server (default: http://localhost:8321)
uv run pyrometer serve
```

Add three lines to any training script:

```python
import pyrometer

run = pyrometer.init(
    name="my-experiment",
    # Pass any config you care about — optimizer, scheduler, architecture, anything.
    # hyperparams is a free-form dict stored as JSON; nested dicts work fine.
    hyperparams={
        "optimizer": "AdamW",
        "lr": 3e-4,
        "weight_decay": 0.01,
        "betas": [0.9, 0.95],
        "scheduler": "cosine",
        "warmup_steps": 1000,
        "batch_size": 64,
    },
    tags=["baseline"],
)

for step, batch in enumerate(dataloader):
    loss = train_step(model, batch)
    # Log any scalar. Use run.log for values that change per step (e.g. LR schedule).
    run.log({
        "train/loss": loss.item(),
        "lr": scheduler.get_last_lr()[0],
    }, step=step)

run.finish()
```

---

## SDK Reference

### `pyrometer.init(...) → Run`

Registers a new run with the server. Automatically captures:
- Hardware: GPU model, VRAM, CUDA version, PyTorch version, hostname
- Git state: commit hash, dirty flag

| Argument | Type | Default | Description |
|---|---|---|---|
| `name` | `str \| None` | auto-generated | Human-readable run name |
| `hyperparams` | `dict` | `{}` | Arbitrary hyperparameter key-value pairs |
| `tags` | `list[str]` | `[]` | Tags for filtering in the dashboard |
| `notes` | `str \| None` | `None` | Free-text notes |
| `server` | `str` | `http://localhost:8321` | API server base URL |

### `run.log(metrics, step)`

Log a dict of scalar metrics at a training step. Batches in memory and flushes every 5 seconds or 100 entries.

```python
run.log({"train/loss": 0.42, "lr": 3e-4}, step=global_step)
```

### `run.profile(step)` *(context manager)*

Profile a single training step. *(Phase 1: stub — full `torch.profiler` integration in Phase 4.)*

```python
with run.profile(step=global_step):
    val_loss = val_step(model, batch)
```

### `run.finish()`

Flush remaining metrics, mark the run as `completed`, close the HTTP session. Always call this.

---

## REST API

All responses: `{"data": <payload>, "error": null}` or `{"data": null, "error": "<message>"}`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/runs` | Create a run |
| `GET` | `/runs` | List all runs |
| `GET` | `/runs/{run_id}` | Get run detail |
| `PATCH` | `/runs/{run_id}` | Update status or notes |
| `POST` | `/runs/{run_id}/metrics` | Ingest a batch of metrics |
| `GET` | `/runs/{run_id}/metrics` | Query metrics (optional `?name=train/loss`) |
| `GET` | `/runs/{run_id}/metric-names` | List distinct metric names for a run |
| `POST` | `/runs/{run_id}/system` | Ingest a GPU system snapshot |
| `GET` | `/runs/{run_id}/system` | Query system snapshots |
| `GET` | `/health` | Health check |

### Example — list runs

```bash
curl http://localhost:8321/runs | python3 -m json.tool
```

### Example — query a metric

```bash
curl "http://localhost:8321/runs/<run_id>/metrics?name=train/loss" | python3 -m json.tool
```

---

## Run the example

```bash
# Terminal 1 — server
uv run pyrometer serve

# Terminal 2 — synthetic training script (uses GPU 1 via CUDA_VISIBLE_DEVICES=1)
uv run python examples/synthetic_train.py
```

The script logs `train/loss`, `train/accuracy`, `val/loss`, and `val/accuracy` across 5 epochs.
While it runs, check the server logs and then query the API to see your data.

---

## Storage

Data is stored in `~/.pyrometer/data.duckdb` (a single file — no daemon, no setup).
Override with `PYROMETER_DB_PATH=/path/to/data.duckdb uv run pyrometer serve`.

Query the database directly:

```python
import duckdb
conn = duckdb.connect("~/.pyrometer/data.duckdb")
conn.execute("SELECT name, status, created_at FROM runs ORDER BY created_at DESC").df()
```

---

## Server options

```bash
uv run pyrometer serve --host 0.0.0.0 --port 8321
```

---

## Architecture

```
Training Script
    └── pyrometer SDK
        ├── run.log()           → buffers metrics, flushes every 5s/100 entries
        ├── system monitor      → pynvml polling every 2s (background thread)
        └── HTTP POST → FastAPI server
                            └── DuckDB (.duckdb file)
                                ├── runs
                                ├── metrics
                                ├── profile_events  (Phase 4)
                                └── system_snapshots
```

## Tech stack

- **SDK**: Python 3.12, httpx, nvidia-ml-py
- **Server**: FastAPI, uvicorn, DuckDB
- **Frontend**: React 18 + TypeScript + Vite + Recharts + d3 *(Phase 2+)*
