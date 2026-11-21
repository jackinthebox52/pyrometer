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
2. Make sure a server is reachable. Locally: `pyrometer serve --no-auth`
   (loopback only, `http://localhost:8321`). Shared/remote servers require
   auth: open the dashboard, finish first-run setup, and create a key under
   Settings (or `pyrometer keys create --name <name>` on the server). Prefer a
   project-bound key (`--project`) for anything shared or automated.
3. Point the SDK at it through the environment, not source code:
   `PYROMETER_SERVER=http://host:8321` and `PYROMETER_API_KEY=pyro_...`
   (or `pyrometer.init(..., server=, api_key=)`).
4. Check reachability with `curl <server>/health` before instrumenting.
   `init()` raises `PermissionError` if the key is missing, invalid or revoked.

**Never commit API keys or the server's address into a project.** Read them
from environment variables or a secret store.

## Minimal instrumentation

Prefer the `with` style: autolog then records the step counter, loss (every `backward()`), learning rate and steps/sec
with no logging calls, and the run finishes itself (`completed`, `killed` on Ctrl-C, `failed` on an exception).

```python
import pyrometer

with pyrometer.init(
    name="baseline",
    hyperparams={"lr": 3e-4, "batch_size": 64},
    tags=["baseline"],
    preset="llm-pretrain",           # optional: llm-pretrain | finetune | rlhf | rl | benchmark
) as run:
    loader = run.track(dataloader=loader, tokens_per_batch=B * T, model=model)   # optional hints
    for batch in loader:
        loss = train_step(batch)     # loss.backward(); optimizer.step() inside: autolog sees both
    run.log({"val/acc": evaluate()})  # anything autolog cannot know; no step= needed
```

Zero code changes: `pyrometer run --name baseline --preset finetune train.py --lr 3e-4` (script args become
hyperparams; works as `torchrun ... -m pyrometer run train.py`; a script's own `init()` attaches to that run).
`pyrometer presets` shows what each preset sets; explicit `init(autolog={...})` options and `run.config.<opt> = ...` override
them. Without `with`: `run = pyrometer.init(...)` ... `run.finish()` (use `try/finally`); with `autolog=False` every
`run.log` needs `step=`.

## Guidelines

- **Log what the user passes; don't inspect the model.** Pyrometer is model-
  and optimizer-agnostic. Pick metric names from the existing code.
- **Hyperparameters:** pass the full config dict to `init` so runs are
  comparable. Nested dicts are fine.
- **Profile sparingly.** Default to every ~100 steps (`run.profile()` around one iteration, or
  `autolog={"profile_every": 100}`); profiling every step slows training substantially.
- **Autolog metrics live under `auto/`** (`auto/loss`, `auto/lr/0`, `auto/steps_per_sec`, `auto/grad_norm` with
  `grad_norm=True`); don't re-log the loss by hand. For axes use `tokens_seen` / `samples_seen` (from `run.track`) or
  `env_steps` (from `run.log_episode(return_, length)` in RL).
- **File runs under a project** (`project="my-model"`) on shared servers. A global key registers it on
  first use; use `pyrometer.create_project(name, private=True)` for a private one (it returns that
  project's own key, shown once). A project key alone implies its project.
- **Name and tag runs meaningfully** (`tags=["lr-sweep"]`) — the dashboard's
  compare view groups by these.
- Use `run.event()` for milestones (LR drops, checkpoints, divergence) and
  `run.log_artifact()` to record saved files (nothing is uploaded).
- Samples: `run.log_samples([{"prompt":..., "completion":..., "reward":...}], name=...)` for LLM/RL generations.
- Media: `run.log_image`, `run.log_histogram`, `run.log_figure`.
- Distributed: `init()` already logs from rank 0 only (other ranks get an inert run), so one script works unchanged.

## Reading results

REST API (send `Authorization: Bearer $PYROMETER_API_KEY`), all responses shaped `{"data": ..., "error": null}`:

- `GET /runs` — list runs
- `GET /runs/{id}` — run detail
- `GET /health` — liveness

Runs are stored in a DuckDB file (`~/.pyrometer/data.duckdb` by default,
override with `PYROMETER_DB_PATH`) and can be queried directly.

See `docs/sdk-reference.md` in the Pyrometer repo for the full API.
