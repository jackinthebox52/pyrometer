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

A 5-second polling fallback on the run page ensures the dashboard stays live even if WebSocket is unavailable. Live refreshes fetch only what is new and merge it into the charts (see [Large runs](#large-runs-downsampling-and-zoom)); they never reload the whole history.

---

## Run Sidebar

The fixed left panel lists runs, most recently active first.

### Run Row

Each row displays:

- **Checkbox**: Select for comparison or bulk actions (color-coded). Shift-click selects the range from the last clicked run
- **Heat chip**: a small bar coloured by status. Running runs glow on a blackbody scale by their GPU load over the last 60 s
  (`gpu_heat` on `GET /runs`: dull red when idle, amber to white when busy) and pulse slowly; completed runs cool to steel grey;
  failed and crashed (no heartbeat from the SDK) runs are red; killed runs are muted. Finished runs' names are dimmer than live ones
- **Run name**: Click to navigate to run detail; a star marks pinned runs, "archived" marks archived ones
- **Time ago**: Since the last update (or creation when sorted by created)
- **Tags**: Hashtag pills

### Sorting, search & filters

- **Sort**: Last updated (default), Created, or Name. The choice is remembered in the browser
- **Search bar**: Case-insensitive substring match on name, group or tag
- **Filters** (button under the search bar): project (ordered by last activity), status, group, created date range, tag chips (a run must carry all chosen tags) and **Show archived**. Archived runs and runs of archived projects are hidden by default
- Every filter is applied by the server (`GET /runs?q=&tags=&created_after=&created_before=&status=&project=&group=`), so the sidebar pages through the matches instead of loading everything; the search box is debounced (300 ms). Filter options (groups, tags, status counts) come from `GET /runs/facets`, i.e. from all visible runs, not just the loaded pages

### Pinned runs and groups

- **Pinned** runs (pin from the run page) sit in their own section at the top. Pins are per user
- Runs that share a `group` (`pyrometer.init(group=...)`) nest under a collapsible group header showing the run count and a status
  summary (for example "3 running · 2 completed"). The counts come from the server for the whole group; when only part of a group is loaded
  the header shows `loaded/total`. The header checkbox selects every loaded run of the group. Collapsed state is remembered

### Paging and live updates

The sidebar loads 50 runs at a time (scroll to the bottom or press **Load more**) and de-duplicates by run id. Live events
reorder, insert and remove rows without a reload: a created/updated/moved run is re-read and re-placed, a deleted run disappears
(and is deselected), and project archive/delete refreshes the list. There is no polling: when a run ingests data the server pushes a
throttled `run_activity` event (at most one per run every 2 s, only to users who can see the run) and the sidebar re-reads its first page,
itself at most every 2 s, so reordering by "last updated" and status changes stay live. Facets are refreshed at most every 3 s.

### Selection for Comparison

1. Check the boxes next to 2+ runs you want to compare
2. The footer shows a "Compare N runs →" button
3. Click the button to navigate to the comparison view

Selected runs are color-coded from a palette of 6 distinct colors. The same color is used consistently across:
- Sidebar checkboxes
- Chart lines in comparison view
- Hyperparameter diff table columns

### Bulk actions (admins)

With one or more runs selected, admins get a **Bulk** button in the sidebar footer: add/remove tags, archive, unarchive, move to a
project (tick *force* for runs that are still running) and delete (typed `delete` confirmation). The request goes to
`POST /runs/bulk`; any runs that failed are listed with the reason instead of being skipped silently. Other users only see Compare:
dashboard sessions may not write run metadata unless they are admins (see [auth.md](auth.md#organising-runs-and-projects)).

### Live Updates

- **New run**: Appears immediately when `pyrometer.init()` is called
- **Status change**: Updates when `run.finish()` marks the run complete
- **Pulse animation**: Running runs have a slowly pulsing heat chip (off with reduced motion)

---

## Run Detail View

The main view for inspecting a single training run. The page is split into tabs (the active tab is in the URL as `?tab=`; tabs
with no data are hidden):

| Tab | Contents |
|---|---|
| **Overview** | GPU load curve, key numbers (loss trend, steps, steps/sec, avg GPU, peak VRAM, efficiency grade, anomalies), pinned charts, recent events |
| **Metrics** | Every metric chart, grouped by prefix (`auto/`, `val/`, `episode/`…), with a filter box, the chart defaults toolbar and the events toggle |
| **System** | GPU charts (utilisation, memory, temperature, power; process vs device), per-GPU summary, hardware and environment |
| **Model health** | Gradient heatmap and anomaly detail (from `run.watch()` and the anomaly detectors) |
| **Profiler** | Kernel timeline, operator breakdown, memory deltas and hints for each profiled step |
| **Logs** | Captured stdout/stderr |
| **Media & samples** | Images, figures, histograms and `log_samples` tables |
| **Artifacts** | Files recorded with `log_artifact` |
| **Notes & config** | Notes and the hyperparameter table |

### GPU load curve

The Overview opens on the run's GPU utilisation over its whole life, coloured by load on a blackbody scale (idle is dark red,
busy is amber, flat-out is white), averaged across GPUs (process-attributed utilisation when available). Triangle marks above it
are events; red ones are anomalies. Hover for elapsed time, step, GPU % and the nearest event. Drag across a period (or click a
point) to open the Metrics tab zoomed to that time window; **Reset zoom** returns to the full run.

### Customize view (per project)

**Customize view** (right of the tabs) chooses what each project shows: which tabs are visible, which metrics are pinned to the
Overview (defaults: loss, val loss, learning rate, throughput or episode return, up to four) and which metrics are hidden. On the
Metrics tab, hover a chart for **Pin** and **Hide** buttons; a group heading can hide the whole prefix (`auto/*`). Hidden metrics
are listed under **Hidden (n)** with Show buttons. The settings apply to every run of that project and are stored in this browser
(`localStorage`, key `pyrometer.view.<project>`), so each user keeps their own view.

### Chart controls

Each chart's options (x-axis, log scale, normalisation, smoothing, reset) are behind the **⋯** button in its header, shown on hover
or keyboard focus (always on touch screens). Options that differ from the defaults show as small chips in the header (`log`,
`ema 0.6`, `x: time`) so the state stays visible.

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
| **Summary stats** | Efficiency grade, steps, throughput, GPU utilization, anomalies; on multi-GPU runs one chip per GPU (avg util, peak VRAM) |
| **Hardware info** | Every visible GPU (model, memory, MIG), driver, CUDA, PyTorch, Python versions, hostname, git commit |

#### Organising a run

Admins can edit run metadata from the header; everyone else sees it read-only:

- **rename** (inline; Enter saves, Esc cancels), **group** edit, **tags** (x removes, the "+ tag" box adds, comma separated) and **notes** (edit block)
- **manage** menu: Archive/Unarchive, Move to project (confirm dialog; a running run needs the explicit override because its key may lose access) and
  Delete, which needs the run name typed to confirm. Deleting from another tab or user shows "This run was deleted" and returns to the start page
- **pin** (any signed-in user, not API keys) and **export** (a zip of Parquet files with every table of the run) are available to everyone who can see the run

Dialogs are keyboard-friendly: Esc cancels, Tab stays inside, focus returns to the trigger.

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
  GPU 0: NVIDIA A100-SXM4-40GB (40.0 GB)
  Driver: 535.54.03
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

- **log**: logarithmic y-axis with decade ticks (1e-3, 0.01, 0.1, 1, ...; faint minor lines at 2..9 up to six
  decades; 1/2/5 ticks on short ranges). Values <= 0 cannot be shown and are hidden; the chart notes how many.
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

**Chart defaults.** The "Chart defaults" bar (metrics header on the run page, and the compare header) sets log, normalization,
x-axis and smoothing once for every chart, shared by the run page and the compare view. A chart you change individually
keeps its own override (remembered per metric name, separately for run detail and compare view, in the browser's
localStorage); its **reset** button drops the overrides so it follows the defaults again, and setting a chart back to the
current default value also drops the override. Per-chart storage from older versions is migrated automatically (values
equal to the built-in defaults are treated as "follow the default"). Step-less events (no `step`) appear on the wall-time
and elapsed axes, placed by timestamp.

#### Large runs: downsampling and zoom

Charts do not load every point. Run page and compare view ask the server for about **two points per pixel of chart width**
(`max_points`, clamped to 200-4000; measured with a `ResizeObserver` on the charts themselves: a page-wide load uses the widest chart on screen, a zoom refetch its own chart's width, and widening the charts a lot (window, layout) triggers one reload at the new resolution), and the server keeps
the minimum, maximum, first and last point of every bucket (GPU stats: first/last and the peak/low utilization, peak memory and
temperature; gradient stats: first/last and the peaks), so a one-step spike survives. Runs that fit within the budget are returned whole.

- **Zoom**: drag over a chart to select a range. The window is re-fetched at full resolution (again capped at about two points per
  pixel, so a still-long window is thinned again) and the chart shows a **reset zoom** button. Step axis: `step_min/step_max`; wall-time and
  elapsed axes: `t_min/t_max` (elapsed adds the run's start); a metric-as-x axis: the selection is mapped back to the steps whose axis
  value falls inside it (plus one neighbouring point each side) and both the series and the axis metric are fetched for that step window.
  Changing the x-axis drops the zoom. Zoom composes with log scale, normalization, smoothing, event markers and (compare view) grouping bands.
  In the GPU section all charts share one zoom (they share the time axis). In the gradient detail charts the window is fetched per parameter.
- **Linked zoom (compare view)**: a **link zoom** toggle (on by default) makes a drag on one chart zoom every compare chart to the same x window
  (each fetches its own runs' data for it; resetting one resets all; a chart whose x-axis differs from the selection ignores it). Turn it off for independent zoom.
- **X-axis on GPU and gradient charts**: GPU charts offer **elapsed** (default) and **wall time**; the gradient/weight detail charts offer step, wall time and
  elapsed. A GPU zoom converts to a timestamp window whichever axis you dragged on.
- **Heatmap zoom**: drag across columns of the gradient heatmap to zoom to that step range (every parameter is refetched at full resolution, one column per 4 px);
  **reset zoom** in its header goes back. A plain click still selects a parameter.
- **Metric-as-x on downsampled data**: the axis metric and the plotted metric are thinned independently, so they rarely share exact steps;
  points are placed by linear interpolation of the axis metric between its kept points (not outside its range). Zoom in for exact joins.
- **Smoothing (EMA) is an approximation on thinned data.** It runs in the browser over the points the server returned (per-bucket
  min/max/first/last), so it follows the kept extremes more than a smoothing of every raw point would, and it is not time-weighted.
  It is exact whenever the series was not thinned (short runs, or a zoomed window that fits within the budget). Smoothing was kept
  client-side because normalization, log clipping and the x-axis are too, and a server-side EMA would have to be re-implemented for every
  combination.
- **Live runs**: new points are fetched as a tail (everything newer than the newest timestamp already held, minus 5 s) and merged by
  (series, step), so a long run never refetches its history. When the merged tail makes the held data more than about twice the
  budget it is resampled once. A zoomed chart refetches its window when new data arrives. Points logged late with an older timestamp
  (for example a `pyrometer sync` replay) are not in that tail, so the server sends a throttled `run_backfill` WebSocket event (`kind`: `metrics`,
  `system` or `param_stats`, with a trailing event after a burst) when an ingested batch is older than the run's newest data, and the page reloads the affected
  series in full.
- The gradient heatmap puts every parameter on one shared column grid (at most one column per 4 px, peak norm per cell; hovering a binned
  column shows its step range), because parameters are thinned separately.
- **Images** in the media gallery are loaded through their file URL (`inline=false`, `loading="lazy"`), so a run with thousands of images does not
  load any base64 until you open one.

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
use the filter box to search, and click "more" to expand long text. Sorting (numeric-aware: 9 < 10, rows without
the key last), filtering and pinning all run on the server over the whole step, not just the loaded page. In the run view, "add pane"
opens another step side by side; in compare view each run gets a pane. The diamond button pins a prompt so every
pane shows only rows with that exact prompt (whitespace differences are ignored when matching).

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
Grouping is client-side. `examples/lr_sweep.py` and `examples/optimizer_compare.py` run three seeds per arm for this. With downsampled
runs (see below) each run is thinned separately, so the band is computed from the kept points (interpolated onto the union grid): spikes
are preserved, but the std is a close approximation, not the exact per-step statistic. Zoom in to get full resolution.

### Shareable compare URLs

The whole compare view lives in the URL, so it can be bookmarked, pasted to a colleague and navigated with the browser's back and forward
buttons: `#/compare?ids=a,b,c&group=hyperparams&ignore=seed&band=minmax&x=step&log=1&norm=first&smooth=0.9`.

| Parameter | Meaning |
|---|---|
| `ids` | Compared run ids. Opening or navigating to the URL also selects them in the sidebar |
| `group` | `none`, `hyperparams` or `tag`; always written |
| `ignore` | Hyperparam keys ignored when grouping (default `seed`, omitted when default) |
| `band` | `std` (default, omitted) or `minmax` |
| `x`, `log`, `norm`, `smooth` | The chart defaults (x-axis, log scale, normalization, smoothing). Per-chart overrides stay in the browser |

A URL that carries any grouping field is authoritative (missing ones mean the default), so a shared link reproduces the view. A URL with
only `ids` (an old bookmark) falls back to your last-used grouping in localStorage, and the URL is then completed. Opening a link with
chart parameters applies them as your chart defaults (they replace your stored defaults, the same as using the "Chart defaults" bar). Typing in the "ignore" box
replaces the history entry instead of adding one per keystroke.

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

## Projects and retention (Settings)

Projects are listed by last activity. Everyone can export a project (Parquet zip). Admins can also:

- **Archive / Unarchive** a project (hides it and its runs; data and ingestion are untouched; "Show archived projects" lists them). Project pickers elsewhere (sidebar filter, key and alert forms) hide archived projects
- **Delete** it behind a typed-name confirmation. The dialog explains that API keys bound to the project are revoked, its members removed and its alert rules deleted, and offers *also delete its runs* (the server refuses while runs exist otherwise). `default` can't be archived or deleted
- Set a **retention policy**: per table (system snapshots, logs, profile events, param stats, events, artifacts, media, samples, metrics) delete rows older than N days. Empty means keep forever, which is the default. **Run now** applies it immediately
- API keys show when they were last used (or "never used"), alert rules show when they were last updated, and users are listed alphabetically

## Alerts (Settings)

Admins see an **Alerts** section in Settings. Add a rule with a name, a webhook URL (generic JSON, ntfy,
Discord or Slack), one or more triggers (run crashed, run failed, event at or above a severity, anomaly/NaN, or a
metric threshold such as `loss > 5`; pick one and press **Add trigger**, repeat for more; the rule fires when any
matches) and an optional project. **Send test** posts a test message; rules can be
disabled or deleted. After saving, the URL is shown masked (host only). See [server-api.md](server-api.md#alerts).
