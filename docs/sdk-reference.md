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
| `api_key` | `str \| None` | `$PYROMETER_API_KEY` | API key with the `ingest` scope (`pyrometer keys create`). Not needed against a `--no-auth` local server. Prefer the environment variable so the key stays out of source control. |

Raises `PermissionError` if the server rejects the key. See [auth.md](auth.md).

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
  - Gradient norm spikes (>100× rolling median)
  - Dead layers (<1e-8 norm for 50 consecutive samples)
  - NaN/Inf in gradients or weights

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

- Flushes all buffered metrics, logs, and parameter statistics to the server.
- Restores original stdout/stderr (if console capture was enabled).
- Removes gradient monitoring hooks.
- Patches the run status to `"completed"` in the database.
- Closes the HTTP client connection.

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
2. **System monitor thread**: Polls pynvml every 2 seconds for GPU stats (utilization, temperature, power, memory).

### Console Capture

Pyrometer automatically captures stdout/stderr by installing a `TeeWriter` that:

- Forwards output to the original stream (so you still see it in the terminal)
- Buffers complete lines and ships them to the server every 50 lines or 5 seconds

This behavior is always on; there's no way to disable it in v0.1.

### Anomaly Detection

The SDK performs automatic anomaly detection on:

- **Gradient norms**: Rolling z-score for spikes, streak detection for dead layers
- **Metric values**: NaN/Inf detection, rolling z-score for loss spikes

When an anomaly is detected, a warning event is logged to the server in a daemon thread (non-blocking).

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
