# Pyrometer

Self-hosted PyTorch experiment dashboard with integrated GPU profiling.
Track metrics, compare runs, and profile GPU kernels — all local, no cloud, no accounts.

**Features**

- **Autolog**: wrap training in `with pyrometer.init(...) as run:` and the step counter, loss, learning rate, steps/sec,
  tokens/samples throughput and (opt-in) gradient norm and health are recorded with no logging calls in your loop.
  `pyrometer run train.py` does the same to an unmodified script; **presets** (`llm-pretrain`, `finetune`, `rlhf`,
  `rl`, `benchmark`) pick sensible defaults for a kind of job.
- **SDK**: `run.log`, `watch` (gradient/weight stats), `profile` (torch.profiler), `event`, `log_artifact`,
  `log_image` / `log_histogram` / `log_figure`, `log_samples` (prompt/completion/reward tables) and
  `log_episode` (rolling RL episode statistics).
- **Durable delivery**: payloads are journaled to disk before sending (a SIGKILL loses ~nothing), retried with backoff and replayed automatically after outages, `init()` works with the server down; `init(offline=True)` records
  without a network and `pyrometer sync` uploads later. Retries never double-insert.
- **Run health**: heartbeat plus statuses `running` / `completed` / `failed` / `crashed` / `killed`; a run that goes
  silent is marked `crashed` and flips back if it resumes.
- **Resume**: `init(resume=run_id | "auto")` (or `PYROMETER_RUN_ID`) continues an existing run; re-logged steps replace the earlier values (no duplicates) and anomaly detectors keep their history.
- **System monitoring**: every visible GPU, plus per-process GPU memory/utilization, CPU and RAM.
- **Anomaly events**: NaN/Inf and robust loss/gradient spike detection, tunable via `init(anomaly_detection=...)`.
- **Dashboard charts**: log-scale, EMA smoothing and normalization per chart; selectable x-axis (step, wall time,
  relative time, or any logged metric such as `tokens_seen`); compare view with grouping by hyperparams/tag
  (e.g. ignore `seed`) showing mean +/- std bands; Samples tab to browse and compare generations across steps/runs.
- **Alerts**: admin-configured webhooks (generic JSON, ntfy, Discord, Slack) for crashes, failures, event severity,
  anomalies and metric thresholds.
- **Organisation**: runs sort by last activity; rename, retag and annotate runs, archive or hard-delete runs and projects (admins), move runs between projects, bulk actions, per-user pins, `init(group=...)` sweeps ([docs/server-api.md](docs/server-api.md)).
- **Data lifecycle**: server-side downsampling and zoom on reads (spikes preserved), images stored as files, Parquet export per run/project, optional per-project retention policy.
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

### 1. Wrap your training in `with`

```python
import pyrometer

with pyrometer.init(name="my-experiment", hyperparams={"lr": 3e-4, "batch_size": 64}) as run:
    train(model, optimizer, dataloader)     # your existing loop, untouched
```

That is the whole integration. While the block is open, Pyrometer hooks PyTorch globally (and removes the hooks on
exit) and records, under the `auto/` prefix: the **step** (optimizer steps), the **loss** of every `backward()`, the
**learning rate**, **steps/sec**, and optionally tokens/samples throughput, gradient norm and per-parameter gradient
health. The run finishes by itself: `completed`, `killed` on Ctrl-C, `failed` on an exception (never swallowed).
Add whatever autolog cannot know by hand, `run.log({"val/acc": acc})` (no `step=` needed), and tell it about your data
with `run.track(tokens_per_batch=..., dataloader=...)`. See [docs/sdk-reference.md](docs/sdk-reference.md#autolog).

### 2. Or change no code at all: `pyrometer run`

```bash
pyrometer run --name baseline --project vision --tag sweep train.py --lr 3e-4 --epochs 20
pyrometer run -m my_pkg.train --config configs/base.yaml
torchrun --nproc_per_node 4 -m pyrometer run train.py     # rank 0 logs
```

The script runs in the same process under a run that autolog is already attached to; `--lr 3e-4 --epochs 20` become the
run's hyperparams (best effort, plus the raw `argv`), and the exit status becomes the run status. If the script calls
`pyrometer.init()` itself, its first call attaches to the launcher's run instead of creating a second one.

### 3. Presets

```python
with pyrometer.init(name="gpt-small", preset="llm-pretrain") as run:    # or: pyrometer run --preset llm-pretrain
    ...
```

`llm-pretrain`, `finetune`, `rlhf`, `rl` and `benchmark` set autolog options (gradient norm, profiling cadence, episode
statistics, fast GPU sampling with clock-throttle capture, ...). `pyrometer presets` lists exactly what each one does.
Anything you pass explicitly (`autolog={...}`, `run.config.<option> = ...`) overrides a preset.

### 4. Or log everything explicitly

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
    # autolog=False,                 # no automatic collection: pass step= to every log call
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

Registers a new run with the server (a context manager: use it with `with`). Under `pyrometer run` the script's first
call returns the launcher's run. Automatically captures:
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
| `autolog` | `bool \| dict \| None` | `True` | Automatic step/loss/lr/throughput collection; a dict sets options |
| `preset` | `str \| None` | `None` (or `PYROMETER_PRESET`) | `llm-pretrain`, `finetune`, `rlhf`, `rl` or `benchmark` |
| `group` | `str \| None` | `None` | Experiment / sweep name |

Delivery is durable: if the server is unreachable, data is retried in the background and spilled to
`~/.pyrometer/spool/<run_id>/` (`PYROMETER_SPOOL_DIR`) instead of being lost. Run `pyrometer sync` to upload it;
retries never double-insert.

### `run.log(metrics, step=None)`

Log a dict of scalar metrics at a training step. Batches in memory and flushes every 5 seconds or 100 entries. Without
`step` the automatic step (`run.step`, completed optimizer steps) is used.

```python
run.log({"train/loss": 0.42, "lr": 3e-4}, step=global_step)
run.log({"val/acc": acc})                     # autolog on: lands on the current optimizer step
```

### `run.log_episode(return_, length=None, step=None, **info)`

Reinforcement learning: call it when an episode ends. Every 10 episodes it logs `episode/return_mean|min|max`,
`episode/length_mean`, `episode/count` and the cumulative `env_steps` axis over a rolling window of 100 episodes
(`run.config.episode_log_every` / `episode_window`).

### `run.profile(step=None)` *(context manager)*

Profile a single training step with `torch.profiler` (operators, CPU/CUDA time, shapes, memory).

```python
with run.profile():
    val_loss = val_step(model, batch)
```

### `run.log_samples(step, samples, name="samples", mode="replace")`

Log a list of JSON-able dicts (e.g. `prompt`, `completion`, `reward`, `reference`) for the dashboard Samples tab. `mode="replace"` (default) overwrites what is stored for that `(name, step)` (so resumed runs do not duplicate); `mode="append"` adds to it.

### `run.finish()`

Flush remaining metrics, mark the run as `completed`, close the HTTP session. Called for you at the end of a `with`
block (which also sets `failed` / `killed` as appropriate); call it yourself when you do not use `with`.

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

The script uses the `with` style and the `llm-pretrain` preset: autolog records `auto/loss`, `auto/lr/0`,
`auto/grad_norm`, `auto/tokens_per_sec` and the step counter, and the script adds `train/accuracy`, `val/loss`,
`val/accuracy`, an image, samples and a checkpoint artifact across 5 epochs (with one deliberate loss spike at step 300).
`lr_sweep.py` and `optimizer_compare.py` run 9 runs each (3 settings x 3 seeds) for the compare view. The same script
works through the launcher: `uv run pyrometer run examples/synthetic_train.py`.

To see every dashboard feature without a GPU or torch, `python examples/demo_live.py` feeds about ten fake jobs
(multi-GPU pretraining with an anomaly, an LR sweep that diverges, RLHF with samples, a vision run with media, a crashed
benchmark and a killed run) into a running server over 10 minutes (`--minutes`, `--speed` to shorten).

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
        ├── autolog             → global optimizer-step / backward hooks (with-block or `pyrometer run`)
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

- **SDK**: Python 3.11+, httpx, nvidia-ml-py
- **Server**: FastAPI, uvicorn, DuckDB
- **Frontend**: React 18 + TypeScript + Vite + Recharts + d3 *(Phase 2+)*
