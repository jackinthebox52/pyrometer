---
name: pyrometer
description: Instrument PyTorch training or inference code with Pyrometer, a self-hosted experiment dashboard with GPU profiling. Use when adding experiment tracking, metric logging, profiling, or run comparison to an ML project, or when the user asks how a training run is behaving.
---

# Pyrometer

Pyrometer is a self-hosted PyTorch experiment dashboard. A Python SDK logs
metrics, gradients, profiler traces, media and system stats to a Pyrometer
server (FastAPI + DuckDB), which serves a web dashboard.

## When to use it

Use Pyrometer when a project trains or evaluates models and the user would
benefit from seeing loss curves, comparing runs, or finding GPU bottlenecks.
Don't add it to projects with no ML workload, and don't add it unprompted to
a project that already uses another tracker (W&B, MLflow, TensorBoard) —
ask first.

## Setup

1. `pip install pyrometer` (torch is a peer dependency, not installed for you).
2. Make sure a server is reachable. Locally: `pyrometer serve`
   (default `http://localhost:8321`). Pass a different server with
   `pyrometer.init(..., server="http://host:8321")`.
3. Check reachability with `curl <server>/health` before instrumenting.

## Minimal instrumentation

```python
import pyrometer

run = pyrometer.init(
    name="baseline",
    hyperparams={"lr": 3e-4, "batch_size": 64},
    tags=["baseline"],
)
run.watch(model, log_freq=100)          # gradient/weight monitoring

for step, batch in enumerate(loader):
    loss = train_step(batch)
    run.log({"train/loss": loss.item()}, step=step)
    if step % 100 == 0:
        with run.profile(step):          # profile sparingly
            train_step(batch)

run.finish()
```

Always call `run.finish()` (use `try/finally`) so stdout capture is restored
and the run is marked complete.

## Guidelines

- **Log what the user passes; don't inspect the model.** Pyrometer is model-
  and optimizer-agnostic. Pick metric names from the existing code.
- **Hyperparameters:** pass the full config dict to `init` so runs are
  comparable. Nested dicts are fine.
- **Profile sparingly.** Default to every ~100 steps; profiling every step
  slows training substantially.
- **Name and tag runs meaningfully** (`tags=["lr-sweep"]`) — the dashboard's
  compare view groups by these.
- Use `run.event()` for milestones (LR drops, checkpoints, divergence) and
  `run.log_artifact()` to record saved files (nothing is uploaded).
- Media: `run.log_image`, `run.log_histogram`, `run.log_figure`.
- Log from one process only in distributed training (rank 0).

## Reading results

REST API, all responses shaped `{"data": ..., "error": null}`:

- `GET /runs` — list runs
- `GET /runs/{id}` — run detail
- `GET /health` — liveness

Runs are stored in a DuckDB file (`~/.pyrometer/data.duckdb` by default,
override with `PYROMETER_DB_PATH`) and can be queried directly.

See `docs/sdk-reference.md` in the Pyrometer repo for the full API.
