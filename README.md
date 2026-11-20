# Pyrometer

Self-hosted PyTorch experiment dashboard with integrated GPU profiling.
Track metrics, compare runs, and profile GPU kernels — all local, no cloud, no accounts.

**Features**

- **SDK**: `run.log`, `watch` (gradient/weight stats), `profile` (torch.profiler), `event`, `log_artifact`,
  `log_image` / `log_histogram` / `log_figure`, and `log_samples` (prompt/completion/reward tables).
- **Durable delivery**: failed uploads retry with backoff and spill to a local spool; `init(offline=True)` records
  without a network and `pyrometer sync` uploads later. Retries never double-insert.
- **Run health**: heartbeat plus statuses `running` / `completed` / `failed` / `crashed` / `killed`; a run that goes
  silent is marked `crashed` and flips back if it resumes.
- **Resume**: `init(resume=run_id | "auto")` (or `PYROMETER_RUN_ID`) continues an existing run.
- **System monitoring**: every visible GPU, plus per-process GPU memory/utilization, CPU and RAM.
- **Anomaly events**: NaN/Inf and robust loss/gradient spike detection, tunable via `init(anomaly_detection=...)`.
- **Dashboard charts**: log-scale, EMA smoothing and normalization per chart; selectable x-axis (step, wall time,
  relative time, or any logged metric such as `tokens_seen`); compare view with grouping by hyperparams/tag
  (e.g. ignore `seed`) showing mean +/- std bands; Samples tab to browse and compare generations across steps/runs.
- **Alerts**: admin-configured webhooks (generic JSON, ntfy, Discord, Slack) for crashes, failures, event severity,
  anomalies and metric thresholds.
- **Auth and projects**: accounts, API keys, public/private projects ([docs/auth.md](docs/auth.md)).

---

## Quickstart

```bash
# Install
git clone <repo>
cd pyrometer
uv sync

# Start the server in local mode (http://localhost:8321, no auth, loopback only)
uv run pyrometer serve --no-auth
```

Auth is on by default. Start the server, open the dashboard, and enter the one-time setup token
from the server log to create the first admin. Then create an API key under **Settings** and
export it for your scripts. Runs can be filed under public or private **projects** with their own
keys and per-user access (see [docs/auth.md](docs/auth.md)):

```bash
uv run pyrometer serve
export PYROMETER_API_KEY=pyro_...
export PYROMETER_SERVER=http://localhost:8321                      # optional
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
    # resume="auto",                 # continue the run named by $PYROMETER_RUN_ID
    # offline=True,                  # no network; upload later with `pyrometer sync`
    # anomaly_detection={...},       # or False to disable
)

for step, batch in enumerate(dataloader):
    loss = train_step(model, batch)
    # Log any scalar. Use run.log for values that change per step (e.g. LR schedule).
    run.log({
        "train/loss": loss.item(),
        "lr": scheduler.get_last_lr()[0],
        "tokens_seen": tokens_seen,   # log an axis as a metric to chart against it
    }, step=step)

# Generation samples for the Samples tab
run.log_samples(step, [{"prompt": "2+2=", "completion": "4", "reward": 1.0}])

run.finish()
```

Upload data saved offline or left undelivered:

```bash
uv run pyrometer sync                                   # every run in ~/.pyrometer/spool
uv run pyrometer sync --server https://pyrometer.example.com
```

Webhook alerts (crash, failure, event severity, anomaly, metric thresholds) are configured by admins under
**Settings**; see [docs/server-api.md](docs/server-api.md#alerts).

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
| `offline` | `bool` | `False` (or `PYROMETER_MODE=offline`) | Write everything to a local spool, no network; upload later with `pyrometer sync` |
| `resume` | `str \| None` | `None` (or `PYROMETER_RUN_ID`) | Run id to continue, or `"auto"` |
| `anomaly_detection` | `bool \| dict` | `True` | Disable or tune automatic anomaly events |

Delivery is durable: if the server is unreachable, data is retried in the background and spilled to
`~/.pyrometer/spool/<run_id>/` (`PYROMETER_SPOOL_DIR`) instead of being lost. Run `pyrometer sync` to upload it;
retries never double-insert.

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

### `run.log_samples(step, samples, name="samples")`

Log a list of JSON-able dicts (e.g. `prompt`, `completion`, `reward`, `reference`) for the dashboard Samples tab.

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
uv run pyrometer serve --host 127.0.0.1 --port 8321   # defaults
uv run pyrometer serve --no-auth                      # local mode, loopback only
```

Authentication, API keys and reverse-proxy (authentik) support: [docs/auth.md](docs/auth.md).
Docker and nginx deployment: [docs/deployment.md](docs/deployment.md).

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
