# Pyrometer Web Application Guide

User guide for the Pyrometer dashboard — experiment tracking, run comparison, and GPU profiling.

---

## Table of Contents

- [Dashboard Overview](#dashboard-overview)
- [Run Sidebar](#run-sidebar)
- [Run Detail View](#run-detail-view)
  - [Header & Metadata](#header--metadata)
  - [Hyperparameters](#hyperparameters)
  - [Metrics](#metrics)
  - [GPU Stats](#gpu-stats)
  - [Profiler View](#profiler-view)
  - [Model Health](#model-health)
  - [Console Logs](#console-logs)
  - [Artifacts & Media](#artifacts--media)
- [Run Comparison](#run-comparison)
  - [Selecting Runs](#selecting-runs)
  - [Metric Overlay](#metric-overlay)
  - [Hyperparameter Diff](#hyperparameter-diff)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Tips & Best Practices](#tips--best-practices)

---

## Dashboard Overview

The Pyrometer dashboard is a single-page React application served by the FastAPI server at `http://localhost:8321`.

### Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  Run Sidebar                    │  Main Content Area            │
│  ┌─────────────────┐            │                               │
│  │ ○ run-1         │            │  ┌─────────────────────────┐  │
│  │ ● run-2         │            │  │  Run Detail View        │  │
│  │ ○ run-3         │            │  │                         │  │
│  │ ○ run-4         │            │  │  - Header               │  │
│  │                 │            │  │  - Metrics              │  │
│  │ [Compare 2 →]   │            │  │  - Profiler             │  │
│  └─────────────────┘            │  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Navigation

Pyrometer uses **hash-based routing** (`#/runs/{id}`, `#/compare`) so the server only ever serves `GET /` for page loads. This avoids conflicts with API routes.

| Route | Description |
|-------|-------------|
| `/` | Empty state (select a run from the sidebar) |
| `/runs/{runId}` | Run detail view |
| `/compare?ids=id1,id2,...` | Run comparison view |

### Live Updates

The dashboard updates in real-time via **WebSocket** push from the server:

- New runs appear in the sidebar immediately after `pyrometer.init()`
- Metric charts grow as `run.log()` flushes data
- GPU stats update every 2 seconds from the system monitor thread

A 5-second polling fallback ensures the dashboard stays live even if WebSocket is unavailable.

---

## Run Sidebar

The fixed left panel lists all runs, sorted newest-first.

### Run Row

Each row displays:

- **Checkbox**: Select for comparison (color-coded)
- **Status dot**: Green (running), blue (completed), orange (failed), red (crashed: no heartbeat from the SDK), yellow (killed)
- **Run name**: Click to navigate to run detail
- **Time ago**: When the run was created (e.g., "2 hours ago")
- **Tags**: Hashtag pills for filtering

### Search & Filter

- **Search bar**: Filter runs by name or tag (case-insensitive substring match)
- **Tag filter**: Click a tag pill to filter runs by that tag

### Selection for Comparison

1. Check the boxes next to 2+ runs you want to compare
2. The footer shows a "Compare N runs →" button
3. Click the button to navigate to the comparison view

Selected runs are color-coded from a palette of 6 distinct colors. The same color is used consistently across:
- Sidebar checkboxes
- Chart lines in comparison view
- Hyperparameter diff table columns

### Live Updates

- **New run**: Appears immediately when `pyrometer.init()` is called
- **Status change**: Updates when `run.finish()` marks the run complete
- **Pulse animation**: Running runs have an animated status dot

---

## Run Detail View

The main view for inspecting a single training run.

### Header & Metadata

```
┌──────────────────────────────────────────────────────────────┐
│  my-experiment                              [copy for llm]   │
│  ● running  2 hours ago                     run-abc123...    │
│  #baseline  #resnet                                            │
│                                                                │
│  [A grade] [steps: 10,000] [steps/sec: 45.2] [avg GPU: 87%]  │
│                                                                │
│  GPU: NVIDIA A100 40GB  │  CUDA 12.1  │  PyTorch 2.1.0       │
│  Python 3.12.0          │  Host: gpu-server-1  │  Git: abc123 │
└──────────────────────────────────────────────────────────────┘
```

#### Elements

| Element | Description |
|---------|-------------|
| **Run name** | User-provided or auto-generated name |
| **Status badge** | Current run status with colored dot |
| **Time ago** | Relative time since run started |
| **Run ID** | UUID (truncated, hover for full ID) |
| **Tags** | Clickable hashtag pills |
| **Summary stats** | Efficiency grade, steps, throughput, GPU utilization, anomalies |
| **Hardware info** | GPU model, CUDA, PyTorch, Python versions, hostname, git commit |

#### "Copy for LLM" Button

Clicking this copies a plain-text summary of the run formatted for pasting into an LLM context window. The summary includes:

- Run metadata (name, status, duration, hyperparameters)
- Hardware info
- Git state
- Final and best metric values
- System statistics (GPU utilization, peak memory)
- Anomaly events

Example output:

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

### Hyperparameters

A key-value table showing all hyperparameters passed to `pyrometer.init()`.

```
┌─────────────────────────────────────┐
│ Hyperparameters                     │
├──────────────────┬──────────────────┤
│ optimizer        │ AdamW            │
│ lr               │ 0.0003           │
│ batch_size       │ 64               │
│ weight_decay     │ 0.01             │
│ betas            │ [0.9, 0.95]      │
│ scheduler        │ cosine           │
└──────────────────┴──────────────────┘
```

- Nested dicts and arrays are serialized as JSON
- Alternating row colors for readability

---

### Metrics

Line charts for all logged metrics (e.g., `train/loss`, `val/accuracy`, `lr`).

```
┌─────────────────────────────────────────────────────────────┐
│ Metrics                               [events on]           │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ train/loss                                          │    │
│  │  2.5 ─┬─────────────────────────────────────────    │    │
│  │       │ ╲                                           │    │
│  │  1.5 ─┤  ╲───────●──────────────────────────────    │    │
│  │       │          ╲                                  │    │
│  │  0.5 ─┤           ╲─────────────────────────────    │    │
│  │       └──────────────────────────────────────────    │    │
│  │       0        2500       5000       7500      10k   │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ val/accuracy                                        │    │
│  │  ...                                                │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### Features

| Feature | Description |
|---------|-------------|
| **X-axis** | Training step (integer) |
| **Y-axis** | Metric value (auto-scaled) |
| **Line color** | Amber (#f59e0b) |
| **Font** | JetBrains Mono for all numeric labels |
| **No dots** | Lines only, no per-point markers |
| **No animation** | `isAnimationActive={false}` prevents jank on rapid polls |
| **Tooltip** | Hover for exact step and value |
| **Event markers** | Vertical lines for logged events (toggle on/off) |
| **Chart options** | Per-chart log y-scale, EMA smoothing slider and normalization (see below) |

#### Chart Options

Every metric chart (run detail, compare view, and the expanded modal) has a control row:

- **log**: logarithmic y-axis. Values <= 0 cannot be shown and are hidden; the chart notes how many.
- **raw / ÷ first / % of best**: normalization. `÷ first` divides by the first value (1.0 = start), which makes
  runs with different scales comparable. `% of best` expresses values as a percentage of the best value seen
  (minimum for metrics whose name contains loss/err/perplexity/latency/time/grad_norm, otherwise maximum).
- **smooth**: EMA smoothing, 0 to 0.99 (Shift+scroll over a chart also adjusts it). The raw curve is drawn faded
  behind the smoothed one. Normalization is applied before smoothing, log clipping after.

- **x: step / wall time / elapsed / any metric**: x-axis selector (run detail and compare view). Wall time shows clock
  times, elapsed is time since the run was created, and any logged metric (e.g. `tokens_seen`, `env_steps`) can be the
  axis; see "log your axis as a metric" in the SDK reference. Metric axes are joined on step. If the axis is not
  monotonic in step the points are sorted by x and the chart says so; points with no axis value at their step are
  hidden (counted in a note). EMA smoothing runs in x order. Event markers are moved to the matching x (event
  timestamp for time axes, the axis value at the latest logged step <= the event's step for a metric axis); markers
  that cannot be placed are hidden with a note. In the compare view a metric axis is resolved per run (each run's
  own values); runs that never logged the metric are excluded from that chart with a note listing them.

Choices are remembered per metric name (separately for run detail and compare view) in the browser's localStorage.
In the compare view the global EMA slider still sets smoothing for all charts; a per-chart change afterwards wins.

#### Event Markers

Toggle event markers with the **"events on/off"** button in the metrics header. When enabled:

- Vertical lines mark steps where events were logged
- Color indicates severity: blue (info), amber (warning), red (error)
- Hover shows event type and message

---

### GPU Stats

Time-series charts for GPU system metrics captured by the pynvml polling thread.

Every GPU visible to the training process is monitored. With more than one GPU, each chart has one line per GPU
(`GPU 0`, `GPU 1`, ...). Where per-process numbers are available, utilization, power and VRAM show two lines per
GPU: **process** (what this run uses) and **device** (the whole GPU, including other processes), in a muted shade of
the same colour. Runs recorded before multi-GPU support appear as a single GPU 0.

```
┌─────────────────────────────────────────────────────────────┐
│ GPU Stats                                                   │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ GPU Util (%)                                        │    │
│  │  100 ─┬─────────────────────────────────────────    │    │
│  │       │    ╱╲  ╱╲  ╱╲                               │    │
│  │   50 ─┤───╱──╲╱──╲╱─────────────────────────────    │    │
│  │       └──────────────────────────────────────────    │    │
│  │       0s       300s      600s       900s      1200s  │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Temperature (°C)                                    │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Power (W)                                           │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ VRAM (GB)                                           │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### Metrics

| Metric | Description |
|--------|-------------|
| **GPU Util (%)** | Percentage of GPU cores active |
| **Temperature (°C)** | GPU die temperature |
| **Power (W)** | Real-time power draw |
| **VRAM (GB)** | GPU memory used (process-attributed if available) |

#### X-Axis

Elapsed time in seconds from the first snapshot (not wall-clock time or step number).

---

### Profiler View

GPU kernel profiling data from `torch.profiler` via `run.profile()`.

```
┌─────────────────────────────────────────────────────────────┐
│ Profiler                                                    │
│                                                             │
│  Step: [0] [100] [200] [400] [800]                         │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Kernel Timeline                                     │    │
│  │  CPU  ████████████████████░░░░░░░░░░░░░░░░░░░░░░    │    │
│  │  CUDA ██████████████████████████████████░░░░░░░░    │    │
│  │       0μs    100μs    200μs    300μs    400μs   500μs│    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Operator Breakdown                                  │    │
│  │  aten::mm        ████████████████████████  45%      │    │
│  │  aten::bmm       ██████████████  25%                │    │
│  │  aten::relu      ██████  10%                        │    │
│  │  ...                                              │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Memory Delta                                        │    │
│  │  aten::empty     ████████████████████  +256 MB      │    │
│  │  aten::mm        ██████████  +128 MB                │    │
│  │  aten::delete    ██████████  -128 MB                │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Optimization Hints                                  │    │
│  │ ▸ 12% of step time in data transfers (aten::to).   │    │
│  │   Consider pin_memory=True in DataLoader.          │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### Step Selector

Pill buttons for each profiled step. Click to switch between profiled steps. Only steps where `run.profile()` was called appear here.

#### Kernel Timeline

A d3 swim-lane chart showing CPU and CUDA time allocation:

- **Top lane**: CPU operators
- **Bottom lane**: CUDA kernels
- **Bar width**: Proportional to accumulated time
- **Color**: Operator type (matmul=blue, conv=green, memory=red, elementwise=gray)
- **Labels**: Operator names (truncated) shown inside bars when space permits
- **Tooltip**: Hover for exact timing and input shapes

**Note**: This is a **proportional layout**, not a true timeline. Events are aggregated by `key_averages()`, which loses wall-clock ordering. The visualization correctly answers "where did the time go?" but doesn't show concurrency.

#### Operator Breakdown

Horizontal stacked bar chart of top 20 operators by `duration_us`:

- **Ambar segment**: CUDA time
- **Indigo segment**: CPU time
- **Tooltip**: Per-segment time and percentage of operator total

#### Memory Delta

Horizontal bar chart of net memory delta per operator:

- **Green bars**: Net allocation
- **Red bars**: Net deallocation
- **Top 15**: By absolute `memory_bytes`

#### Optimization Hints

Rule-based suggestions displayed when conditions are met:

| Rule | Hint |
|------|------|
| `aten::to` / `copy_` > 5% of step time | "Consider pin_memory=True in DataLoader" |
| CPU time > 2× CUDA time | "GPU may be underutilized — consider torch.compile() or more DataLoader workers" |
| >10 operators with <1MB allocations | "Pre-allocating buffers or using torch.no_grad() can reduce memory overhead" |

---

### Model Health

Gradient and weight statistics from `run.watch()`.

```
┌─────────────────────────────────────────────────────────────┐
│ Model Health ▼                                              │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Gradient Heatmap                                    │    │
│  │  encoder.layer.0.attn.q.weight  ████░░░░░░░░░░░░    │    │
│  │  encoder.layer.0.attn.k.weight  ████░░░░░░░░░░░░    │    │
│  │  encoder.layer.0.attn.v.weight  ████░░░░░░░░░░░░    │    │
│  │  encoder.layer.0.mlp.fc1.weight ████████░░░░░░░░    │    │
│  │  ...                                                │    │
│  │       step 0    50    100   150   200   250   300   │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ▾ Events (12)                                              │
│  ▾ Anomaly Events (2)                                       │
│    [step 150] anomaly_detected (warning): Gradient norm    │
│    spike in encoder.layer.3.attn.q.weight                  │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### Gradient Heatmap

- **Rows**: Parameter names (truncated to 40 chars)
- **Columns**: Steps where `run.watch()` logged stats
- **Color**: log10(grad_norm) on dark→amber→red scale
- **Tooltip**: Hover for exact gradient norm
- **Click row**: Expands detail panel with gradient/weight norm line charts

#### Resize Behavior

The heatmap uses a `ResizeObserver` to adapt to container width changes.

#### Anomaly Events

List of automatically detected anomalies:

- **Gradient spikes**: >100× rolling median
- **Dead layers**: <1e-8 norm for 50 consecutive samples
- **NaN/Inf**: In gradients or weights

---

### Console Logs

Captured stdout/stderr from the training script.

```
┌─────────────────────────────────────────────────────────────┐
│ Console Logs ▼                                              │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ [stdout] Epoch 1/100 starting...                    │    │
│  │ [stdout] Epoch 1/100 — train_loss=2.34  val_loss=2.1│    │
│  │ [stdout] Epoch 2/100 starting...                    │    │
│  │ [stderr] /usr/lib/python3.12/site-packages/torch... │    │
│  │ [stdout] Epoch 2/100 — train_loss=1.89  val_loss=1.7│    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### Features

- **Stream indicator**: `[stdout]` or `[stderr]` prefix
- **Step association**: Logs are associated with the nearest known step
- **Scrollable**: Long output is scrollable within the panel
- **Search**: Filter by keyword (future enhancement)

---

### Artifacts & Media

#### Artifacts Table

Lists files tracked via `run.log_artifact()`:

| Type | Path | Size | Step | Metadata |
|------|------|------|------|----------|
| checkpoint | `/ckpt/model_epoch50.pt` | 256 MB | 5000 | `{"epoch": 50, "val_loss": 0.23}` |
| config | `config.yaml` | 2 KB | 0 | `{"format": "yaml"}` |

#### Samples

Shown on the run detail page (and in the compare view) when `run.log_samples()` was used. Pick a stream and step;
the table is paginated server-side (50 per page), columns come from the sample keys, click a header to sort,
use the filter box to search the loaded page, and click "more" to expand long text. In the run view, "add pane"
opens another step side by side; in compare view each run gets a pane. The diamond button pins a prompt so every
pane shows only rows with that exact prompt.

#### Media Gallery

Grid of logged images with a step slider:

```
┌─────────────────────────────────────────────────────────────┐
│ Media Gallery                                               │
│                                                             │
│  Step: ◄  0    100    200    300    400    500  ►          │
│                                                             │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐        │
│  │         │  │         │  │         │  │         │        │
│  │  img 1  │  │  img 2  │  │  img 3  │  │  img 4  │        │
│  │         │  │         │  │         │  │         │        │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘        │
│                                                             │
│  predictions (4 images at step 500)                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Run Comparison

Compare multiple runs side-by-side with metric overlays and hyperparameter diffs.

### Selecting Runs

1. In the run sidebar, check the boxes next to runs you want to compare
2. Each selected run gets a unique color from the palette
3. Click the "Compare N runs →" button in the sidebar footer
4. Navigate to `#/compare?ids=id1,id2,id3`

### Grouping runs (seeds)

The **Group** control in the compare header aggregates runs into groups, drawn as one mean line with a band:

- **hyperparams**: runs whose hyperparams are equal once the ignored keys are dropped (default `seed`; the "ignore"
  box takes a comma-separated list). Group labels show the hyperparams that differ between groups (e.g. `lr=0.01`).
- **tags**: runs with the same tags after dropping tags that every compared run shares.
- Band: mean +/- sample std, or mean with min/max.

Each run is first resolved onto the chart's x-axis, normalized, smoothed (EMA) and log-clipped as usual; then each
group is interpolated linearly onto the union of its runs' x values and aggregated. A point is only drawn where at
least 2 runs cover that x (no extrapolation), and the tooltip shows `n=` for the runs behind each point. Under log
scale a band edge that would be <= 0 is clamped just above zero. Groups with one run are plain lines (no band).
The run pills and per-chart legend toggle whole groups while grouping is on; the per-run raw faded curve is not drawn.
Grouping is client-side and not remembered across reloads. `examples/lr_sweep.py` and `examples/optimizer_compare.py`
run three seeds per arm for this.

### Metric Overlay

Each metric gets its own chart with all selected runs overlaid:

```
┌─────────────────────────────────────────────────────────────┐
│ Compare: run-1  run-2  run-3          EMA: ○━━━━━━━━━━○ 0.5│
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ train/loss                                          │    │
│  │  2.5 ─┬─────────────────────────────────────────    │    │
│  │       │ ╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲  │    │
│  │  1.5 ─┤                                          ╲╱╲╱╲  │    │
│  │       │                                            ╲╱╲╱╲│    │
│  │  0.5 ─┤                                             ╲╱╲╱│    │
│  │       └──────────────────────────────────────────    │    │
│  │       0        2500       5000       7500      10k   │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ val/accuracy                                        │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### Features

| Feature | Description |
|---------|-------------|
| **Multi-line** | One line per run, color-coded |
| **EMA smoothing** | Slider (0.00–0.95) for exponential moving average |
| **Tooltip** | Shows all runs' values at the hovered step |
| **Legend** | Run name + last smoothed value per line |
| **connectNulls** | Handles runs with different step counts |

#### EMA Smoothing

- `alpha=0`: Raw data (no smoothing)
- `alpha=0.5`: Moderate smoothing
- `alpha=0.95`: Heavy smoothing (trend line)

The smoothed value is what's shown in tooltips and legends.

---

### Hyperparameter Diff

Tabular comparison of hyperparameters across selected runs:

```
┌─────────────────────────────────────────────────────────────┐
│ Hyperparameter Diff                                         │
│                                                             │
│  ┌─────────────┬─────────────┬─────────────┬─────────────┐  │
│  │             │   run-1     │   run-2     │   run-3     │  │
│  ├─────────────┼─────────────┼─────────────┼─────────────┤  │
│  │ optimizer   │ AdamW       │ AdamW       │◆SGD         │  │
│  │ lr          │ 0.0003      │◆0.001       │ 0.01        │  │
│  │ batch_size  │ 64          │ 64          │ 64          │  │
│  │ weight_decay│ 0.01        │ 0.01        │◆0           │  │
│  └─────────────┴─────────────┴─────────────┴─────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

#### Features

- **Column headers**: Color-coded to match chart lines
- **Differing rows**: Highlighted with amber background and `◆` prefix on the key
- **Serialization**: Nested dicts and arrays shown as JSON strings
- **Sorted keys**: Alphabetical order for easy scanning

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl/Cmd + K` | Focus search bar (future) |
| `Esc` | Clear selection / close modal (future) |

*Note: Keyboard shortcuts are minimal in v0.1. More may be added in future releases.*

---

## Tips & Best Practices

### Naming Conventions

Use descriptive run names that encode key hyperparameters:

```python
# Good
run = pyrometer.init(name="resnet-lr3e-4-wd0.01-bs64")

# Better (use hyperparams dict instead)
run = pyrometer.init(
    name="resnet-baseline",
    hyperparams={"lr": 3e-4, "weight_decay": 0.01, "batch_size": 64},
)
```

### Tagging Strategy

Use tags for categorical metadata you'll want to filter by:

```python
run = pyrometer.init(
    name="experiment-1",
    tags=["resnet", "imagenet", "baseline", "phase1"],
)
```

### Profiling Frequency

Profile sparingly to avoid overhead:

```python
# Good: Profile every 10th step
for step, batch in enumerate(dataloader):
    if step % 10 == 0:
        with run.profile(step=step):
            train_step(...)
    else:
        train_step(...)
```

### Gradient Monitoring

Set `log_freq` based on your batch size and training duration:

```python
# Small models, short runs: log every 20 steps
run.watch(model, log_freq=20)

# Large models, long runs: log every 100-500 steps
run.watch(model, log_freq=500)
```

### Comparison Workflow

1. Run a hyperparameter sweep (e.g., learning rates: 1e-4, 3e-4, 1e-3)
2. Select the best 3-5 runs in the sidebar
3. Click "Compare" to see metric overlays and hyperparameter diffs
4. Identify which hyperparameters correlate with better performance

### Exporting for Analysis

Use the **"Copy for LLM"** button to export a run summary for:

- Pasting into an LLM for analysis
- Sharing with collaborators
- Including in lab notebooks or reports

### Debugging Poor GPU Utilization

1. Check the **GPU Stats** chart for low utilization (%)
2. Open the **Profiler View** and look at the Operator Breakdown
3. Check **Optimization Hints** for suggestions
4. Review the **Kernel Timeline** for CPU bottlenecks

---

## Alerts (Settings)

Admins see an **Alerts** section in Settings. Add a rule with a name, a webhook URL (generic JSON, ntfy,
Discord or Slack), a trigger (run crashed, run failed, event at or above a severity, anomaly/NaN, or a metric
threshold such as `loss > 5`) and an optional project. **Send test** posts a test message; rules can be
disabled or deleted. After saving, the URL is shown masked (host only). See [server-api.md](server-api.md#alerts).
