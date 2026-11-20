# Pyrometer SDK Reference

Complete API reference for the Pyrometer Python SDK.

---

## Table of Contents

- [Initialization](#initialization)
  - [`pyrometer.init()`](#pyrometerinit)
- [Core Methods](#core-methods)
  - [`run.log()`](#runlog)
  - [`run.watch()`](#runwatch)
  - [`run.profile()`](#runprofile)
  - [`run.finish()`](#runfinish)
- [Media Logging](#media-logging)
  - [`run.log_image()`](#runlog_image)
  - [`run.log_histogram()`](#runlog_histogram)
  - [`run.log_figure()`](#runlog_figure)
- [Events & Tracking](#events--tracking)
  - [`run.event()`](#runevent)
  - [`run.log_artifact()`](#runlog_artifact)
- [Internal Behavior](#internal-behavior)

---

## Initialization

### `pyrometer.init()`

Registers a new training run with the Pyrometer server and returns a `Run` handle.

```python
import pyrometer

run = pyrometer.init(
    name="my-experiment",
    hyperparams={"lr": 3e-4, "batch_size": 64},
    tags=["baseline", "resnet"],
    notes="Initial baseline with default hyperparameters",
    server="http://localhost:8321",
    api_key="pyro_...",   # or set PYROMETER_API_KEY (preferred)
    project="team-a",     # optional; see auth.md
)
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str \| None` | Auto-generated | Human-readable run name. If omitted, generates `run-{run_id[:8]}`. |
| `hyperparams` | `dict` | `{}` | Arbitrary hyperparameter dictionary. Nested dicts and arrays are preserved as JSON. |
| `tags` | `list[str]` | `[]` | List of string tags for filtering runs in the dashboard. |
| `notes` | `str \| None` | `None` | Free-text notes attached to the run. |
| `server` | `str \| None` | `$PYROMETER_SERVER`, else `"http://localhost:8321"` | Base URL of the Pyrometer API server. |
| `api_key` | `str \| None` | `$PYROMETER_API_KEY` | API key (create one in the dashboard under **API keys**, or `pyrometer keys create`). Not needed against a `--no-auth` local server. Prefer the environment variable so the key stays out of source control. |

| `project` | `str \| None` | `None` | Project for the run. A global key registers an unknown project as public on first use; a project key defaults to its own project; otherwise runs go to the public `default` project. |
| `offline` | `bool \| None` | `$PYROMETER_MODE == "offline"` | Record to the local spool without touching the network (including run creation); upload later with `pyrometer sync`. See [Durable delivery](#durable-delivery-offline-mode-and-pyrometer-sync). |
| `resume` | `str \| None` | `$PYROMETER_RUN_ID` implies `"auto"` | Continue an existing run: a run id, or `"auto"` (use `$PYROMETER_RUN_ID`; resume if it exists, create it with that id otherwise). See [Resuming a run](#resuming-a-run). |
| `anomaly_detection` | `bool \| dict` | `True` | `False` disables automatic anomaly events; a dict overrides thresholds. See [Anomaly Detection](#anomaly-detection). |

Raises `PermissionError` if the server rejects the key, `LookupError` if the project doesn't exist or the key can't use it (or `resume` names an unknown run), `ValueError` if `resume` is malformed or the run belongs to another project. See [auth.md](auth.md).

#### Resuming a run

`init(resume=run_id)` reattaches to an existing run instead of creating one (build on the client-chosen run id:
`run.run_id` of the first run). `init(resume="auto")` reads `$PYROMETER_RUN_ID` and resumes the run if it exists,
creating it under that id otherwise; just setting `PYROMETER_RUN_ID` implies `"auto"`. Handy for restartable jobs.

```python
run = pyrometer.init(name="llm", hyperparams=cfg, resume="auto")   # export PYROMETER_RUN_ID=my-llm-run-1
start = run.last_step + 1 if run.resumed and run.last_step is not None else 0
for step in range(start, total_steps):
    ...
    run.log({"loss": loss}, step=step)
```

- **Status**: the run returns to `running`, also from `completed`, `failed`, `crashed` or `killed` (resuming a
  finished run is allowed; its summary is recomputed at the next `finish()`). A `run_resumed` event
  (metadata: `previous_status`, `last_step`) marks the point on the charts.
- **Config**: the original name, hyperparams, tags and notes are kept. Hyperparams/tags you pass that differ are not
  applied: a warning is printed and the differences are stored in the event's `config_changes` metadata.
- `run.server` is the server base URL the run reports to (handy for printing a link: `f"{run.server}/#/runs/{run.run_id}"`).
- **Steps**: `run.resumed` is `True`; `run.last_step` is the highest step stored on the server (`None` if none).
  Continue from `last_step + 1`, or restore from an older checkpoint and re-log: the server keeps the **last value
  written per `(run, metric name, step)`** (and per `(param, step)` for gradient stats, `(name, step)` for images and
  histograms, and by default per `(name, step)` for `log_samples`), so re-logged steps replace the earlier values
  instead of duplicating them. Rows duplicated by older versions are not repaired.
- **Anomaly detectors** are seeded from the run's last ~100 stored values per metric (one small request in a
  background thread, best effort: if it fails or the run is offline they start fresh) so the warmup is not repeated.
  Gradient-norm history is not carried over.
- **Offline**: no server is consulted, so the id is trusted and `run.last_step` is `None`. The resume (status back to
  `running`, or creation if unknown) is applied when you `pyrometer sync`; with an explicit id the server returns 404
  at sync if the run does not exist there.
- **Access**: the key must be able to write the run's project (a project key bound to another project is refused,
  answered as "not found"). Passing a `project` that differs from the run's raises `ValueError`.
- Spooling, retries and heartbeats work as for any run.

### `pyrometer.create_project()`

```python
info = pyrometer.create_project("team-a", private=True)
info["key"]   # the project's own API key (private projects only), shown once
```

Registers a project. Needs a **global** API key. Parameters: `name`, `private=False`, `server=None`, `api_key=None`.
Raises `PermissionError` (not a global key), `ValueError` (bad or taken name).

#### Returns

A [`Run`](#run-object) instance for logging metrics and managing the training run.

#### Automatic Capture

On initialization, Pyrometer automatically captures:

- **Hardware info**: GPU model, VRAM, CUDA version, PyTorch version, hostname
- **Git state**: Commit hash and dirty flag (uncommitted changes)

#### Example

```python
run = pyrometer.init(
    name="lstm-forget-bias-fix",
    hyperparams={
        "optimizer": "AdamW",
        "lr": 3e-4,
        "weight_decay": 0.01,
        "betas": [0.9, 0.95],
        "scheduler": "cosine",
        "warmup_steps": 1000,
        "batch_size": 64,
        "hidden_dim": 256,
    },
    tags=["lstm", "sequential-mnist", "bugfix"],
    notes="Testing forget gate bias initialization from paper",
)
```

---

## Core Methods

### `run.log()`

Log a dictionary of scalar metrics at a given training step.

```python
run.log({
    "train/loss": loss.item(),
    "train/accuracy": accuracy,
    "lr": scheduler.get_last_lr()[0],
}, step=global_step)
```

#### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `metrics` | `dict[str, float]` | Dictionary mapping metric names to scalar values. Names can use slash notation (e.g., `"train/loss"`, `"val/accuracy"`). |
| `step` | `int` | Global training step. Used as the x-axis in dashboard charts. |

#### Behavior

- **Buffering**: Metrics are batched in memory and flushed to the server every **5 seconds** or when **100 entries** accumulate (whichever comes first).
- **Non-blocking**: The training loop is never blocked by network I/O.
- **Thread-safe**: Safe to call from multiple threads.

#### Example

```python
for epoch in range(100):
    for step, batch in enumerate(dataloader):
        loss = train_step(model, batch)
        acc = compute_accuracy(model, batch)
        
        run.log({
            "train/loss": loss.item(),
            "train/accuracy": acc,
            "lr": optimizer.param_groups[0]["lr"],
        }, step=step)
    
    # Validation
    val_loss = validate(model, val_loader)
    run.log({"val/loss": val_loss}, step=step)
```

#### Custom x-axes: log your axis as a metric

Charts default to `step`, but the dashboard can plot against wall time, elapsed time, or **any logged metric**.
To get e.g. an env-steps or tokens axis, just log it as an ordinary metric at the same step as the values you want to
plot against it (the dashboard joins on `step`):

```python
run.log({"train/loss": loss, "tokens_seen": tokens_seen}, step=step)
```

Log it on every step (or at least wherever you log the metrics you want on that axis, including validation).
Points whose step has no axis value are hidden, and the axis metric should be non-decreasing in step.

---

### `run.watch()`

Monitor gradient and weight statistics for all trainable parameters in a PyTorch model.

```python
run.watch(model, log_freq=100)
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model` | `torch.nn.Module` | — | PyTorch model whose parameters will be monitored. |
| `log_freq` | `int` | `100` | How often (in backward passes per parameter) to compute and ship statistics. |

#### Behavior

- Registers a backward hook (`Tensor.register_hook`) on every `requires_grad` parameter.
- Every `log_freq` backward passes, computes per-parameter statistics:
  - Gradient: L2 norm, min, max, mean, 32-bin histogram
  - Weight: L2 norm, min, max, mean, 32-bin histogram
- Ships statistics to the server in a background thread.
- **Anomaly detection** automatically fires alerts for:
  - Gradient norm spikes (>1000× rolling median, after a 20-sample warmup, rate-limited)
  - Dead layers (<1e-8 norm for 200 consecutive samples)
  - NaN/Inf in gradients or weights (once per parameter until it recovers)

#### Example

```python
import pyrometer
import torch.nn as nn

model = nn.Sequential(
    nn.Linear(784, 256),
    nn.ReLU(),
    nn.Linear(256, 10),
)

run = pyrometer.init(name="monitored-run")

# Monitor every 50 backward passes
run.watch(model, log_freq=50)

for step, batch in enumerate(dataloader):
    loss = train_step(model, batch)
    loss.backward()
    optimizer.step()
    run.log({"train/loss": loss.item()}, step=step)

run.finish()
```

#### Dashboard Visualization

The **Model Health** section in the run detail view displays:

- **Gradient Heatmap**: Rows = parameters, columns = steps, color = log10(grad_norm)
- **Anomaly Events**: Timeline markers for detected anomalies
- **Per-parameter charts**: Click a row to see gradient/weight norm evolution

---

### `run.profile()`

Profile a single training step using `torch.profiler`.

```python
with run.profile(step=global_step):
    loss = train_step(model, batch)
```

#### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `step` | `int` | The training step being profiled. Stored alongside all events. |

#### Behavior

- Activates `torch.profiler.profile` with:
  - CPU + CUDA activity capture
  - Input shape recording
  - Memory profiling
  - `with_stack=False` (avoids 10-50x data bloat)
- After the block exits, aggregates events via `prof.key_averages()` and ships to the server in a background thread.
- **Sub-microsecond events** (<1μs) are filtered out as bookkeeping noise.

#### Usage Pattern

Profile **periodically** (e.g., every 100 steps), not every step — each profiling call adds measurable overhead and produces thousands of events.

```python
for step, batch in enumerate(dataloader):
    # Profile one step per epoch
    if step % steps_per_epoch == 0:
        with run.profile(step=step):
            loss = train_step(model, batch)
    else:
        loss = train_step(model, batch)
    
    run.log({"train/loss": loss.item()}, step=step)
```

#### Dashboard Visualization

The **Profiler View** displays:

- **Kernel Timeline**: d3 swim-lane chart showing CPU/CUDA time allocation
- **Operator Breakdown**: Ranked bar chart of top operators by CUDA time
- **Memory Delta**: Net memory allocation/deallocation per operator
- **Optimization Hints**: Rule-based suggestions (e.g., "use pin_memory")

---

### `run.finish()`

Mark the run as completed, flush remaining metrics, and close the HTTP session.

```python
run.finish()
```

#### Behavior

- Flushes all buffered metrics, logs, and parameter statistics to the sender and makes a short (10 s) final delivery attempt.
- Anything the server still did not accept stays in the run's spool directory and one warning names it (see below).
- Restores original stdout/stderr (if console capture was enabled).
- Removes gradient monitoring hooks.

#### Heartbeat and exit status

- The SDK sends a heartbeat (`POST /runs/{id}/heartbeat`) every 30 s, starting at init. Heartbeats go through the
  transport but are *ephemeral*: never retried, spooled or saved offline, since a stale heartbeat is meaningless.
- If `finish()` is never called the run is closed at interpreter exit: `completed` on a normal exit, `failed`
  after an uncaught exception or `sys.exit(<non-zero>)` (also `sys.exit("message")`), `killed` on Ctrl-C or SIGTERM.
  `sys.exit(0)` / `sys.exit()` end `completed`. (The exit code is read by wrapping `sys.exit`; a hand-written
  `raise SystemExit(1)` bypasses it and ends `completed`. A `sys.exit()` you catch is forgotten once the program logs
  again.)
- **SIGTERM** is chained, not replaced: our handler marks every live run `killed` and finishes it, then calls the
  handler your application had installed (or, if there was none, lets the default action terminate the process). If
  the application ignores SIGTERM (`SIG_IGN`) nothing is installed, and the run is not closed. Installing needs the main
  thread; an `init()` from another thread skips it and a later `init()` from the main thread retries. A handler you
  install *after* `init()` replaces ours; call `run.finish()` from it.
- A hard kill (SIGKILL, power loss, lost machine) cannot report anything; the server marks the run `crashed`
  after 5 minutes without a heartbeat (`PYROMETER_HEARTBEAT_TIMEOUT`, seconds, on the server).
- Patches the run status to `"completed"` in the database.
- Closes the HTTP client connection.

#### Durable delivery, offline mode and `pyrometer sync`

Nothing the SDK sends is dropped when the server is unreachable. All payloads (metrics, logs, events, artifacts,
media, profile data, system snapshots, parameter stats, the final status) go through one background sender that
never blocks your training loop:

- **Write-ahead journal.** Every payload is appended to `~/.pyrometer/spool/<run_id>/queue.jsonl` (override the root
  with `PYROMETER_SPOOL_DIR`) *before* it is sent, and acknowledged (the journal is truncated once the sender has
  caught up) after the server accepted it. The API key is never written to the spool.
- **Guarantee.** The append is a plain `write()` on the calling thread, so it survives the process being killed
  (SIGKILL, OOM): anything `log()`, `event()`, `log_image()` etc. already returned from is replayed by the next
  `pyrometer sync` (or the next `init` of the same run id). `log()` metrics that are still in the SDK's batching buffer
  are covered too, by small write-ahead files (`metrics.wal`, `logs.wal` for captured stdout/stderr lines,
  `param_stats.wal` for `watch()` gradient statistics), recovered with the same batch ids. The journal is `fsync`'d by
  the sender thread at most once per second and at `finish()`, so a power loss or kernel crash can lose up to about a
  second; an application crash cannot. Not covered: data produced while the disk refuses writes (kept in memory only,
  with a warning).
- **Ownership lock.** The process that owns a run holds a lock on `owner.lock` in the run's spool directory (an
  `flock`, with the pid inside; Windows falls back to a pid file). `pyrometer sync` skips a run whose owner is alive
  ("skipped (run is still active ...)", not an error) and takes over a stale lock left by a dead process.
- A failed delivery is retried with exponential backoff (1 s doubling to a 60 s cap), in order. The in-memory queue is
  only a cache of the journal (500 payloads); beyond that the sender reads the backlog from disk. When the connection
  returns the sender replays the backlog by itself, oldest first, run creation before any data and the final status
  last; no `pyrometer sync` is needed while the process is alive.
- **Server down at `init()`**: connection errors, timeouts (3 s connect, 8 s overall) and 5xx/429 do not raise. One
  warning is printed, the run is created locally and the sender creates it on the server (idempotent, `resume` honored)
  as soon as it answers, then drains. `run.last_step` is `None` for a resume deferred this way. 401/403 and
  validation errors (4xx) still raise immediately.
- **Media** (images, figures, histograms over 32 KB) are written as separate files in `blobs/` and referenced from the
  JSONL; they are base64-encoded only on the wire.
- **Poison payloads.** If one payload keeps failing (6 attempts, 4xx immediately) while `GET /health` answers, it is
  moved to `dead.jsonl` in the spool, one warning is printed, and newer data keeps flowing. If the server is
  down (health fails) nothing is dead-lettered. `pyrometer sync` retries dead letters too.
- Each payload carries a client batch id (`X-Pyrometer-Batch`) that stays the same in the journal, dead-letter file and
  every replay. The server ignores ids it has already seen for a run, so a re-send never double-inserts.
- `pyrometer.init(..., offline=True)` (or `PYROMETER_MODE=offline`) does no networking at all: run creation and every
  payload stay in the journal.

Upload spooled data with the CLI (needs `PYROMETER_API_KEY`; the server comes from `--server`, `$PYROMETER_SERVER`, or
the URL recorded in the spool). It creates the run if needed, replays the payloads, and deletes them on success:

```bash
pyrometer sync                       # every run in the spool
pyrometer sync <run_id>              # one run
pyrometer sync /path/to/spool-dir    # a spool root or one run directory (e.g. copied from another machine)
```

Safe to repeat: already-delivered payloads are skipped by batch id. It refuses to touch a run whose process is still
alive (see the ownership lock above), so `pyrometer sync` with no argument is safe to run while jobs are training.

#### Always Call This

Always call `run.finish()` at the end of training to ensure no data is lost and the run is marked complete in the dashboard.

```python
try:
    for epoch in range(100):
        train(...)
except KeyboardInterrupt:
    run.finish()  # Still flush data on interrupt
    raise
```

---

## Media Logging

### `run.log_image()`

Log an image at a given training step.

```python
run.log_image("predictions", output_image_tensor, step=global_step)
```

#### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Display name for this image stream (e.g., `"predictions"`, `"attention_maps"`). |
| `image` | `PIL.Image \| np.ndarray \| torch.Tensor` | Image to log. See supported formats below. |
| `step` | `int \| None` | Training step. Defaults to the last step passed to `run.log()`. |

#### Supported Image Formats

| Type | Requirements |
|------|--------------|
| `PIL.Image.Image` | Any mode (RGB, L, etc.) |
| `np.ndarray` | Shape `(H, W)` or `(H, W, C)` |
| `torch.Tensor` | Shape `(C, H, W)`, `(H, W)`, or `(N, C, H, W)` (first image in batch used) |

Float tensors are clamped to `[0, 1]` and scaled to uint8 automatically.

#### Example

```python
# Log model predictions as images
with torch.no_grad():
    sample_x = torch.randn(8, 3, 224, 224)
    output = model(sample_x)  # (8, 3, 224, 224)
    pred_img = output[0].cpu()  # First image: (3, 224, 224)

run.log_image("predictions", pred_img, step=global_step)
```

#### Dashboard Visualization

The **Media Gallery** shows a grid of logged images with a step slider to scrub through training progression.

---

### `run.log_histogram()`

Log a histogram of a tensor or array at a given training step.

```python
run.log_histogram("activation_distribution", model_output.detach(), step=global_step)
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | — | Display name (e.g., `"activation_distribution"`, `"logits"`). |
| `data` | `torch.Tensor \| np.ndarray \| list` | — | Data to histogram. Flattened before binning. |
| `step` | `int \| None` | — | Training step. Defaults to last logged step. |
| `num_bins` | `int` | `32` | Number of histogram bins. |

#### Example

```python
# Monitor logit distribution
logits = model(batch)
run.log_histogram("logits", logits.detach().cpu().numpy(), step=global_step)

# Monitor activation distribution in a specific layer
activations = model.encoder.output
run.log_histogram("encoder_activations", activations.flatten(), step=global_step)
```

---

### `run.log_figure()`

Log a matplotlib figure at a given training step.

```python
fig, ax = plt.subplots()
ax.imshow(confusion_matrix)
run.log_figure("confusion_matrix", fig, step=global_step)
plt.close(fig)
```

#### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Display name (e.g., `"confusion_matrix"`, `"pr_curve"`). |
| `figure` | `matplotlib.figure.Figure` | Matplotlib figure to log. |
| `step` | `int \| None` | Training step. Defaults to last logged step. |

#### Example

```python
import matplotlib.pyplot as plt

# Confusion matrix
fig, ax = plt.subplots(figsize=(8, 6))
im = ax.imshow(conf_matrix, cmap="Blues")
ax.set_xlabel("Predicted")
ax.set_ylabel("True")
plt.colorbar(im, ax=ax)

run.log_figure("confusion_matrix", fig, step=global_step)
plt.close(fig)

# Precision-Recall curve
fig, ax = plt.subplots()
ax.plot(recall, precision)
ax.set_xlabel("Recall")
ax.set_ylabel("Precision")
ax.set_title("PR Curve")

run.log_figure("pr_curve", fig, step=global_step)
plt.close(fig)
```

---

### `run.log_samples()`

Log generation samples (prompt / completion / reward ...) for a step; shown in the dashboard's Samples section.

```python
run.log_samples(step, [
    {"prompt": p, "completion": c, "reward": r, "reference": ref},
], name="eval_generations")
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `step` | `int` | Training step the samples belong to. |
| `samples` | `list[dict]` | Arbitrary JSON-able dicts (non-JSON values are stringified). Columns are derived from the keys; a numeric `reward` is indexed for sorting and the LLM summary. |
| `name` | `str` | Sample stream name (default `"samples"`). |
| `mode` | `"replace" \| "append"` | `"replace"` (default): overwrite what is stored for `(name, step)`. `"append"`: add to it. A list larger than 200 is split into several requests; only the first replaces. |

Limits: 64 KB per sample (the SDK truncates the longest string values to fit, with a warning; the server
rejects bigger ones with HTTP 413), 200 samples per request (the SDK splits larger lists; the server
accepts up to 500 / 4 MB per call). Log a few dozen samples every N steps, not every step. Calling it again for the
same `(name, step)` **replaces** the earlier samples (default `mode="replace"`), so a resumed run that re-logs a step does
not duplicate; pass `mode="append"` to add to what is stored (several calls for one step). Delivered through the
durable transport, so it survives outages and `pyrometer sync`.

---

## Events & Tracking

### `run.event()`

Log a structured event on the training timeline.

```python
run.event("switched to phase 2 training", metadata={"phase": 2})
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | — | Human-readable description of the event. |
| `event_type` | `str` | `"custom"` | Category string. Built-in values: `"checkpoint_saved"`, `"lr_scheduled"`, `"anomaly_detected"`, `"cuda_error"`, `"oom_warning"`, `"thermal_throttle"`, `"custom"`. |
| `severity` | `str` | `"info"` | One of `"info"`, `"warning"`, `"error"`. |
| `metadata` | `dict \| None` | `{}` | Optional JSON-serializable dict of extra context. |

#### Example

```python
# Custom milestone
run.event("Reached 50% of training", event_type="custom", metadata={"progress": 0.5})

# Learning rate schedule change
scheduler.step()
run.event(
    "Learning rate annealed",
    event_type="lr_scheduled",
    metadata={"new_lr": optimizer.param_groups[0]["lr"]},
)

# Checkpoint saved
torch.save(model.state_dict(), ckpt_path)
run.event(
    f"Best model saved (val_loss={val_loss:.4f})",
    event_type="checkpoint_saved",
    metadata={"path": ckpt_path, "val_loss": val_loss},
)
```

#### Dashboard Visualization

Events appear as **vertical marker lines** on all metric charts. Hover a marker to see the event message and metadata.

---

### `run.log_artifact()`

Track a saved file artifact (model checkpoint, config, etc.).

```python
torch.save(model.state_dict(), ckpt_path)
run.log_artifact(ckpt_path, artifact_type="checkpoint", metadata={"val_loss": val_loss})
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | `str` | — | Absolute or relative path to the saved file. |
| `artifact_type` | `str` | `"checkpoint"` | Category string (e.g., `"checkpoint"`, `"config"`, `"dataset"`, `"export"`). |
| `metadata` | `dict \| None` | `{}` | Optional JSON-serializable dict (e.g., `{"step": 5000, "val_loss": 0.23}`). |

#### Behavior

- Does **not** upload the file — only records path, on-disk size, timestamp, and metadata.
- File size is computed from the filesystem at log time.

#### Example

```python
# Save and track checkpoint
ckpt_path = f"/checkpoints/model_epoch{epoch}.pt"
torch.save({
    "model": model.state_dict(),
    "optimizer": optimizer.state_dict(),
    "epoch": epoch,
}, ckpt_path)

run.log_artifact(
    ckpt_path,
    artifact_type="checkpoint",
    metadata={"epoch": epoch, "val_loss": val_loss},
)

# Track config file
run.log_artifact(
    "config.yaml",
    artifact_type="config",
    metadata={"format": "yaml"},
)
```

#### Dashboard Visualization

Artifacts are listed in a table in the run detail view, showing path, size, timestamp, and metadata.

---

## Internal Behavior

### Buffering and Flushing

The SDK maintains several internal buffers:

| Buffer | Flush Trigger |
|--------|---------------|
| Metrics | Every 5 seconds OR 100 entries |
| Parameter stats | Every 5 seconds |
| Console logs | Every 50 lines |

All buffers are flushed immediately on `run.finish()`.

### Background Threads

On `pyrometer.init()`, two daemon threads are started:

1. **Flush thread**: Wakes every 5 seconds to flush buffered metrics.
2. **System monitor thread**: Polls pynvml every 2 seconds for **every GPU visible to the process** (honours `CUDA_VISIBLE_DEVICES`, indices or UUIDs; `gpu_index` is the CUDA ordinal) and reports utilization, temperature, power and memory per GPU. It also attributes VRAM and SM utilization to this process (plus its child processes when `psutil` is installed); the `process_*`/`attributed_*` fields are null on GPUs where the process has no context or the driver does not support it.

### Console Capture

Pyrometer automatically captures stdout/stderr by installing a `TeeWriter` that:

- Forwards output to the original stream (so you still see it in the terminal)
- Buffers complete lines and ships them to the server every 50 lines or 5 seconds

This behavior is always on; there's no way to disable it in v0.1.

### Anomaly Detection

Detection is deliberately conservative: ordinary noisy training should produce zero events.

- **Metrics** (`run.log`): NaN/Inf always fires (once per metric until it recovers). Spikes use a
  rolling median/MAD over the last 100 values, need a 50-sample warmup per metric, a robust z-score
  of at least 12, and a deviation larger than 1x the median. Cooldown is 500 steps per metric, max 5
  spike events per metric per run. **Sparse metrics** (per-epoch or per-eval values that would never reach 50
  samples) are checked from `sparse_min_samples` (8) samples on, with the z threshold and the relative-change floor
  `sparse_strictness` (2.5) times stricter at that point, relaxing linearly to the normal values at 50 samples
  (so at 8 samples a spike needs z >= 30 and a jump of more than 2.5x the median). Set `sparse_min_samples` to
  `warmup` or more to turn this off.
- **Gradients** (`run.watch`): NaN/Inf, norm > 1000x rolling median (20-sample warmup), and dead
  layers (200 consecutive samples under 1e-8). Max 20 gradient events per run.

Configure with `pyrometer.init(anomaly_detection=...)`: `True` (default), `False` (off), or a dict
overriding any of: `window`, `warmup`, `metric_z`, `metric_rel_change`, `sparse_min_samples`, `sparse_strictness`, `cooldown_steps`,
`max_events_per_key`, `grad_warmup`, `grad_spike_factor`, `grad_z`, `dead_threshold`, `dead_streak`,
`max_grad_events`, `nan_inf`. Unknown keys raise `ValueError`.

```python
run = pyrometer.init(name="x", anomaly_detection={"warmup": 100, "metric_z": 15})
```

Events are sent as `anomaly_detected` via the normal durable transport. The server's
`anomaly_count` (and the stability score) simply counts those events, so tuning applies automatically.

### Error Handling

All network operations are wrapped in try/except blocks. If the server is unavailable:

- Metrics continue to buffer in memory (up to 100 entries, then oldest are dropped)
- Training continues uninterrupted
- Errors are logged to the Python logger at DEBUG level

---

## Complete Example

```python
import pyrometer
import torch
import torch.nn as nn

# Initialize
run = pyrometer.init(
    name="resnet-baseline",
    hyperparams={
        "model": "resnet18",
        "lr": 3e-4,
        "batch_size": 64,
        "epochs": 100,
        "optimizer": "AdamW",
        "scheduler": "cosine",
    },
    tags=["resnet", "imagenet", "baseline"],
    notes="Standard ResNet-18 baseline with AdamW",
)

# Setup
model = resnet18(pretrained=False)
optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=100)

# Monitor gradients every 50 steps
run.watch(model, log_freq=50)

best_val_acc = 0
global_step = 0

for epoch in range(100):
    model.train()
    for step, batch in enumerate(train_loader):
        # Profile every 10th step
        if step % 10 == 0:
            with run.profile(step=global_step):
                optimizer.zero_grad()
                loss = train_step(model, batch)
                loss.backward()
                optimizer.step()
        else:
            optimizer.zero_grad()
            loss = train_step(model, batch)
            loss.backward()
            optimizer.step()
        
        # Log metrics
        run.log({
            "train/loss": loss.item(),
            "lr": scheduler.get_last_lr()[0],
        }, step=global_step)
        
        global_step += 1
    
    # Validation
    model.eval()
    val_loss, val_acc = validate(model, val_loader)
    run.log({"val/loss": val_loss, "val/accuracy": val_acc}, step=global_step)
    
    # Log confusion matrix at epoch end
    fig, ax = plot_confusion_matrix(model, val_loader)
    run.log_figure("confusion_matrix", fig, step=global_step)
    plt.close(fig)
    
    # Save best checkpoint
    if val_acc > best_val_acc:
        best_val_acc = val_acc
        ckpt_path = f"/checkpoints/best_epoch{epoch}.pt"
        torch.save(model.state_dict(), ckpt_path)
        run.log_artifact(ckpt_path, metadata={"epoch": epoch, "val_acc": val_acc})
        run.event(f"New best: val_acc={val_acc:.4f}", event_type="checkpoint_saved")
    
    scheduler.step()
    print(f"Epoch {epoch}: val_acc={val_acc:.4f}")

run.finish()
```
