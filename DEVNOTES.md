# Pyrometer — Developer Notes

## Phase 1: Foundation (complete)

### What was built

- **SDK** (`pyrometer/`): `pyrometer.init()`, `run.log()`, `run.finish()`, `run.profile()` (stub).
  Background flush thread (every 5s or 100 entries) and pynvml system monitor thread (every 2s).
- **FastAPI server** (`pyrometer/server/app.py`): REST API for runs, metrics, and system snapshots.
  All DB access goes through `pyrometer/server/db.py` — route handlers contain no SQL.
- **DuckDB schema** (`pyrometer/server/db.py`): Four tables — `runs`, `metrics`, `profile_events`, `system_snapshots`.
  All four tables are created at startup even though `profile_events` isn't populated until Phase 4;
  better to define the schema once than deal with migrations later.
- **CLI** (`pyrometer/cli.py`): `uv run pyrometer serve [--host HOST] [--port PORT]`.
- **Example** (`examples/synthetic_train.py`): Works with real PyTorch (random tensors, no torchvision)
  or falls back to a pure-Python simulation.

### Design decisions

- **Single DuckDB connection + threading.Lock**: Avoids multi-writer conflicts inside a single process.
  Fine for single-user v0.1. Revisit if concurrent multi-process writes ever become necessary.
- **Metric buffering**: SDK batches in memory, flushes every 5s or 100 entries — training loop overhead is negligible.
- **`CUDA_VISIBLE_DEVICES=1`**: Set via `os.environ.setdefault` at the top of `examples/synthetic_train.py`
  before torch is imported. pynvml is not affected by `CUDA_VISIBLE_DEVICES`; the monitor thread uses
  physical device index 0 unconditionally (which maps to GPU 1 in this environment).
- **torch as direct dependency**: Added to pyproject.toml by the user. SDK guards all torch imports with
  try/except so it degrades gracefully on CPU-only machines.
- **API response envelope**: All endpoints return `{"data": <payload>, "error": null}` on success.
  Errors raise HTTPException with `detail={"data": null, "error": "<message>"}`.

### Known gaps (addressed in later phases)

- `run.profile()` is a no-op stub — real `torch.profiler` integration is Phase 4.
- No WebSocket live updates (Phase 7).
- Metrics/snapshot ingestion doesn't validate that the `run_id` exists — skipped intentionally; DuckDB won't error and the overhead isn't worth it for v0.1.

---

## Phase 2: Basic Dashboard (complete)

### What was built

- **React frontend** (`frontend/`): Vite + React 18 + TypeScript + Tailwind CSS + Recharts.
- **Run sidebar** (`RunSidebar.tsx`): Fixed left panel listing all runs, sorted newest-first.
  Filters by name or tag. Polls `/runs` every 5 s. Shows status dot (pulse-animated for running),
  name, time-ago, and tags.
- **Run detail page** (`RunDetailPage.tsx`): Full metadata panel (GPU, CUDA, PyTorch, Python,
  hostname, git hash/dirty), tags, notes, hyperparameter table, one `MetricChart` per logged metric
  name, and GPU stats charts (utilization, temperature, power draw, VRAM). Polls the API every 5 s
  while mounted so the loss curve grows live during training.
- **MetricChart** (`MetricChart.tsx`): Recharts `LineChart` with JetBrains Mono axis labels,
  amber line, no dots, custom tooltip. `isAnimationActive={false}` prevents jank on rapid polls.
- **HyperparamTable** (`HyperparamTable.tsx`): Alternating-row key/value table.
- **StatusBadge** (`StatusBadge.tsx`): Colored dot + label; dot pulses via CSS animation for running.
- **API client** (`api/client.ts`): Typed `apiFetch` wrapper that unwraps the `{"data","error"}`
  envelope and throws on errors.
- **FastAPI static serving**: `app.py` mounts `frontend/dist` at `"/"` after all API routes
  using `StaticFiles(html=True)`. Only present when the dist directory exists (dev mode unaffected).
- **`aiofiles`** added to `pyproject.toml` (required by Starlette's `StaticFiles`).

### Design decisions

- **HashRouter over BrowserRouter**: FastAPI serves `GET /runs/{id}` as an API endpoint. Using
  hash-based routing (`/#/runs/{id}`) means the server only ever sees `GET /` for page navigation —
  no conflicts with API routes and no SPA fallback complexity.
- **Aesthetic**: Near-black (#0a0b0e) + amber (#f59e0b) accent + JetBrains Mono for all numeric
  data. Dot-grid body background via CSS `radial-gradient`. Designed to feel like a scientific
  instrument, not a generic SaaS dashboard.
- **5 s polling instead of WebSocket**: WebSocket live updates are Phase 7. Polling every 5 s
  (matching the SDK flush interval) gives acceptable live-updating behavior without adding
  complexity. The interval is cleared on component unmount.
- **All metrics fetched at once**: `GET /runs/{id}/metrics` returns all metric names/steps in one
  call. Grouped and sorted client-side. Acceptable for v0.1 run sizes; add pagination in Phase 5+
  if needed.
- **System charts reuse MetricChart**: Snapshot index used as the `step` axis (not wall-clock time).
  This is a known limitation — Phase 5 should use proper time-axis charts for system data.

### Known gaps

- No torch.profiler / kernel timeline (Phase 4-6).
- System chart X-axis is snapshot index, not wall-clock time.
- Fonts loaded from Google Fonts CDN; will fail offline. Bundle fonts in Phase 8.
- No error boundary — an unhandled component error white-screens.

---

## Phase 3: Run Comparison (complete)

### What was built

- **Run selection in sidebar** (`RunSidebar.tsx`): Checkbox column added to each run row.
  Checkboxes appear with a color-coded check (each selected run gets a unique color from the
  `RUN_COLORS` palette). Footer becomes a "Compare N runs →" button when 2+ runs are selected,
  with an ✕ to clear. Selection state is lifted to `App.tsx`.
- **`/compare` route** (`App.tsx`): Added `CompareView` at `#/compare?ids=id1,id2,...`.
  Selection state (`selectedIds: string[]`) lives in `App` so it survives navigation between
  run detail and comparison pages.
- **`CompareChart`** (`CompareChart.tsx`): Multi-line Recharts `LineChart`. One `Line` per run,
  color-coded from `RUN_COLORS`. EMA smoothing applied before merging — slider 0.00–0.95.
  Data merged by step into `{step, runId1: value, runId2: value}` rows; `connectNulls` handles
  runs with different step counts. Tooltip shows each run's name + smoothed value.
- **`HyperparamDiff`** (`HyperparamDiff.tsx`): Tabular diff. Rows = all unique hyperparam keys
  across selected runs (sorted). Columns = one per run, color-coded. Rows where the value
  differs across runs are highlighted with a subtle amber background and a ◆ prefix on the key.
  Values serialized with `JSON.stringify` to handle nested dicts, arrays, booleans consistently.
- **`CompareView`** (`CompareView.tsx`): Full-width comparison page. Reads run IDs from
  `?ids=` query params. Fetches all runs + all metrics in parallel. Header shows run pills
  (name, status dot, time) + smoothing slider. Grid of `CompareChart` for each metric.
  Full-width `HyperparamDiff` below.
- **`StatusBadge`** (`StatusBadge.tsx`): Added `compact` prop — dot only, no label text.
  Used in CompareView run pills.

### Design decisions

- **URL-based comparison state**: Run IDs passed as `?ids=id1,id2,...` in the query string.
  Comparison is bookmarkable and shareable. Selection state in `App` is the source of truth
  for the sidebar checkboxes; navigating to `/compare` is just a read of that state.
- **RUN_COLORS palette exported from CompareView**: Six distinct colors (amber, cyan, rose,
  violet, lime, orange) used consistently across sidebar checkboxes, chart lines, and table
  column headers. Imported into `RunSidebar` to show which color each selected run will get.
- **EMA smoothing**: `alpha=0` is raw, `alpha=0.95` is very smooth. Applied before merging
  step maps so the smoothed values are what get plotted and shown in tooltips.
- **Checkbox UX**: Checkbox is a `<button>` left of each `NavLink` — clicking the checkbox
  toggles selection without navigating. NavLink click still navigates to run detail. The two
  interactions are independent.
- **No polling in CompareView**: Comparison is a snapshot; it fetches once on mount. If a run
  is still active, the user can refresh. Adding polling is straightforward but adds noise to
  the comparison (curves changing while you're reading them).

### Known gaps

- No chart zoom/pan in comparison charts (Phase 5+).
- No PNG export for comparison charts.
- EMA smoothing changes the legend's "last value" display — it shows the smoothed last value,
  not the raw last metric value. This is intentional but could be confusing.
- Max 6 runs in comparison before colors repeat.

---

## Phase 4: GPU Profiler SDK + Ingestion (complete)

### What was built

- **`run.profile(step)`** (`pyrometer/_run.py`): Implemented as a real `torch.profiler` context manager.
  Activates `torch.profiler.profile(activities=[CPU, CUDA], record_shapes=True, profile_memory=True, with_stack=False)`
  for the wrapped block. After the block exits, calls `prof.key_averages()` and extracts per-operator
  aggregates in a daemon thread (`_ship_profile`) so the training loop is never blocked.
- **`_ship_profile()`**: Filters sub-microsecond noise events, coerces `torch.Size` objects in
  `input_shapes` to plain int lists, categorizes events as `cuda_kernel` or `cpu_op`, ships to
  `POST /runs/{run_id}/profile`.
- **Server endpoints** (`pyrometer/server/app.py`):
  - `POST /runs/{run_id}/profile` — ingests a list of `ProfileEvent` objects.
  - `GET /runs/{run_id}/profile?step=N` — returns events, optionally filtered to one profiled step,
    sorted by `duration_us DESC` (most expensive operators first).
  - `GET /runs/{run_id}/profile/steps` — returns the sorted list of steps that have profile data,
    used by the frontend to populate a step selector.
- **DB functions** (`pyrometer/server/db.py`):
  `db_insert_profile_events`, `db_get_profile_events`, `db_get_profile_steps`.
  `input_shapes` stored as JSON string, parsed back on read.
- **`examples/synthetic_train.py`**: Added `run.profile(step=global_step)` call on the first step of
  each epoch (5 profiling events per run) to populate the `profile_events` table for testing.

### Design decisions

- **`key_averages()` over raw events**: Raw `prof.events()` can produce tens of thousands of entries
  per profiled step. `key_averages()` aggregates by operator name, which is the right granularity for
  the operator breakdown chart (Phase 5) and dramatically reduces storage and network overhead.
  Phase 6 (kernel timeline) can switch to raw events per-step if fine-grained timing is needed.
- **Background thread for shipping**: `torch.profiler` finishes synchronously, but the HTTP POST
  shouldn't add latency to the training loop. Spinning a daemon thread means the overhead is bounded
  by network RTT to localhost (sub-millisecond).
- **`with_stack=False`**: Stack traces make profiler output 10-50x larger and aren't needed for
  operator-breakdown or timeline views. Excluded unconditionally.
- **Sub-microsecond filter**: `total_us < 1.0` drops internal autograd bookkeeping events
  (e.g. `ProfilerStep*`, `record_function`) that are noise at this granularity.
- **`device_id = 0`**: Hardcoded for single-GPU setups. Multi-GPU support would require mapping
  `evt.device_index` from the profiler output — deferred to a future phase.

### Known gaps

- `key_averages()` loses wall-clock ordering — events can't be placed on a timeline. Phase 6 will
  need a separate raw-event capture pass or a different profiler schedule.
- Memory delta (`memory_bytes`) can be negative (deallocation). The frontend handles sign (green/red).
- Single-GPU assumption in `_ship_profile` (`device_id: 0`).
- `run.profile()` is always a full CPU+CUDA capture. No way to disable CUDA activity for CPU-only runs.

---

## Phase 5: Profiler Dashboard (complete)

### What was built

- **`ProfileEvent` type** (`frontend/src/api/types.ts`): TypeScript interface matching the server's
  `ProfileEvent` Pydantic model.
- **API client additions** (`frontend/src/api/client.ts`): `api.getProfileSteps(runId)` and
  `api.getProfileEvents(runId, step?)` wired to the Phase 4 endpoints.
- **`OperatorBreakdown`** (`frontend/src/components/OperatorBreakdown.tsx`): Horizontal stacked bar chart
  (Recharts `BarChart` with `layout="vertical"`). Top 20 operators by `duration_us`, stacked into
  CUDA (amber) and CPU (indigo) segments. Y-axis shows operator names (truncated to 34 chars).
  Tooltip shows per-segment time and its share of that operator's total. Footer shows total step time.
- **`MemoryDeltaChart`** (`frontend/src/components/MemoryDeltaChart.tsx`): Horizontal bar chart of
  net memory delta per operator. Green bars = allocation, red bars = deallocation. Top 15 by absolute
  `memory_bytes`. Represents the best visualization possible with `key_averages()` data (no
  intra-step timeline; see known gaps).
- **`OptimizationHints`** (inline in `RunDetailPage.tsx`): Hardcoded rule-based hints rendered below
  the charts. Three rules: (1) aten::to / copy_ transfers >5% of step → suggest pin_memory;
  (2) CPU time >2× CUDA time → suggest torch.compile or more DataLoader workers;
  (3) >10 operators with <1 MB allocations → suggest pre-allocation or torch.no_grad().
- **Profiler section in `RunDetailPage`**: Step selector (pill buttons for each profiled step, amber
  highlight on active), profile event fetch on step change, grid of OperatorBreakdown +
  MemoryDeltaChart + OptimizationHints. Only rendered when profiled steps exist.
- **System chart X-axis fix**: System snapshot charts now use elapsed seconds (`timestamp - t0`)
  as the step value instead of snapshot index, giving a real time axis.
- **Format utilities** (`frontend/src/utils/format.ts`): Added `formatMicroseconds` and
  `formatBytesSigned` helpers used by the new components.

### Design decisions

- **`key_averages()` data model constraints**: True memory waterfall (allocation curve over time
  within a step) requires raw per-event timing, which `key_averages()` doesn't provide. Phase 6's
  kernel timeline work will need a different data capture strategy (raw `prof.events()` or a
  custom schedule). The "memory delta by operator" chart is the richest visualization possible
  with the current data model.
- **Step selector as pill buttons**: Profiled steps are sparse (e.g. steps 0, 100, 200, 400).
  Pill buttons are faster to scan than a dropdown and show all options at once.
- **Inline `OptimizationHints` function component**: No separate file since it's only used in
  `RunDetailPage` and is self-contained enough not to warrant its own module yet.

### Known gaps

- Memory chart is per-operator delta, not a time-series waterfall within a step.
- Optimization hints are hardcoded rules; no ML-driven suggestions.

---

## Phase 6: Kernel Timeline (complete)

### What was built

- **`KernelTimeline.tsx`**: d3 v7 swim-lane chart showing CPU and CUDA time allocation per
  operator across a profiled step. Two horizontal lanes — CPU on top, CUDA below — each lane
  rendered as a ribbon of color-coded operator bars whose widths are proportional to their
  accumulated time. Both lanes share a common x-axis (μs → ms → s) so relative durations are
  comparable across lanes.
- Installed `d3@7.9.0` and `@types/d3` as a direct frontend dependency.
- Placed as the first (centerpiece) chart in the Profiler section, above Operator Breakdown.

### Design decisions

- **Proportional layout, not true timeline**: `torch.profiler.key_averages()` produces
  aggregated `FunctionEventAvg` objects with total CPU/CUDA time but no wall-clock start/end
  times within the step. A true Gantt timeline (with simultaneous CPU/GPU lanes showing
  concurrency) would require switching to `prof.events()` for raw `FunctionEvent` records.
  The proportional layout is an honest, readable approximation that correctly answers "where
  did the time go?" without misleading the viewer about event ordering.
- **Shared x-axis (maxUs scale)**: Both lanes are drawn against the same linear scale
  `[0, max(cpuTotal, cudaTotal)]`. The shorter lane naturally ends before the right edge,
  indicating it consumed less total time. Using per-lane scales would misrepresent relative
  device utilization.
- **0.2% threshold + top-50 cap**: Operators below 0.2% of their lane's total are grouped into
  a single dark "other" bar at the right edge of the lane. Keeps the visualization readable
  regardless of how many operators the profiler captures (typically 200–2000 per step).
- **d3 ref pattern**: SVG is entirely d3-managed via `useRef` + `useEffect`. The tooltip is a
  plain positioned `<div>` whose `innerHTML` and `style.left/top` are set by d3 mouse handlers —
  no React state updates on hover (avoids per-pixel re-renders).
- **Inline labels inside bars**: Rendered only when bar width > 48px. Character budget computed
  as `floor((bw - 10) / 6.2)` matching JetBrains Mono at 9px. Labels use `rgba(0,0,0,0.72)`
  to work across the full palette of bar colors.

### Known gaps

- No zoom/pan (Phase 7 polish). The proportional layout is already readable without zoom for
  typical training models; a future `d3.zoom` pass can add it.
- No CPU↔CUDA arrows showing launch-to-execution relationships — requires raw event data with
  correlation IDs that `key_averages()` discards.
- If the container resizes after mount the SVG is not redrawn (no `ResizeObserver`). Hard refresh
  fixes it; Phase 7 can wire up a resize observer.

---

## Phase 7: Live Updates + Polish (complete)

### What was built

- **WebSocket push endpoint** (`pyrometer/server/app.py`): `GET /ws` FastAPI WebSocket endpoint.
  A `_WsManager` class holds the list of active connections and broadcasts JSON event objects
  to all of them. Broadcasts are fire-and-forget — a failed send silently removes the dead connection.
  The four ingestion/mutation routes (`POST /runs`, `PATCH /runs/{id}`, `POST /runs/{id}/metrics`,
  `POST /runs/{id}/system`) were made `async` and now `await _ws.broadcast(...)` after each write.
  Event shape: `{"type": "run_created"|"run_updated"|"metrics"|"system", "run_id": "<uuid>"}`.
- **Vite proxy for WS** (`frontend/vite.config.ts`): Added `/ws` proxy with `ws: true` so the dev
  server correctly upgrades WebSocket connections to `localhost:8321`.
- **`useWebSocket` hook** (`frontend/src/hooks/useWebSocket.ts`): Singleton WebSocket shared across
  all mounted components. Connects once on first subscriber, auto-reconnects with 3 s back-off on
  close. Uses a `useRef`-based stable handler pattern so components can close over reactive state
  without stale-closure issues.
- **`RunDetailPage` live updates**: Added `loadRef` to hold the current `load` function. `useWebSocket`
  triggers an immediate refetch whenever the server pushes a `metrics`, `system`, or `run_updated`
  event for the current `runId`. 5 s polling is kept as a fallback for environments where WebSocket
  is unavailable.
- **`RunSidebar` live updates**: Same pattern — `useWebSocket` triggers an immediate sidebar refresh
  on `run_created` or `run_updated`. The sidebar is the first component to show a new run after
  `pyrometer.init()` is called.
- **Skeleton loading state** (`RunDetailPage.tsx`): Replaced the plain "Loading…" spinner with a
  structural skeleton that mirrors the page layout (header row, metadata grid, two chart placeholders).
  Uses Tailwind's `animate-pulse` with `#1a1d24` fill to match the dark theme.
- **`KernelTimeline` ResizeObserver**: Added a `ResizeObserver` that tracks the container's
  `contentRect.width` in a `containerWidth` state variable. The d3 drawing `useEffect` now depends
  on `[events, containerWidth]`, so the SVG redraws correctly when the card is widened (e.g. expand
  modal, window resize, sidebar collapse). Fixes the Phase 6 known gap.

### Design decisions

- **WS as push trigger, not data carrier**: The WebSocket only carries small event objects
  (`{"type": ..., "run_id": ...}`). Actual data is still fetched over REST on demand. This keeps
  the WS code trivial and avoids duplicating serialization logic; the REST endpoints are already
  the source of truth for data shape and pagination.
- **5 s polling kept as fallback**: WebSocket can fail (proxy misconfiguration, firewall, load
  balancer timeouts). Polling every 5 s ensures the dashboard stays live even if WS is unavailable,
  matching the SDK's metric flush interval.
- **Singleton WS in module scope**: A single `WebSocket` object is shared across all hook
  invocations via module-level variables. This is simpler than a React context and appropriate for
  a single-page app where components mount/unmount but the WS should stay open.
- **Structural skeleton over spinner**: A skeleton that matches the page layout prevents layout shift
  when data loads and gives the user a preview of where information will appear, reducing perceived
  load time.

### Known gaps

- WebSocket has no authentication — consistent with the "no auth for v0.1" policy.
- CompareView does not live-update (by design — comparison is a snapshot; changing data while
  comparing is disorienting).
- `useWebSocket` handler identity is stabilized via `useRef`, not `useCallback`, to avoid
  subscribing/unsubscribing on every render. If a component passes a new handler every render
  and that handler has reactive deps, use `useCallback` at the call site.

---

## Phase 8: Gradient & Weight Monitoring (complete)

### What was built

- **`run.watch(model, log_freq=100)`** (`pyrometer/_run.py`): Registers a backward hook
  (`Tensor.register_hook`) on every `requires_grad` parameter in a `torch.nn.Module`. Every
  `log_freq` backward passes, computes per-parameter stats (grad L2 norm, min, max, mean;
  weight L2 norm, min, max, mean; 32-bin histograms for both) and buffers them. The buffer
  is flushed every 5 seconds by the existing flush loop. Hooks are removed on `finish()` and
  whenever `watch()` is called again (idempotent re-registration).
- **Anomaly detection** (`_check_grad_anomaly`): Per-parameter rolling history of the last 200
  grad norms. Fires a warning event if: (1) norm exceeds 100× running median (spike), (2) norm
  stays below 1e-8 for 50 consecutive logged samples (dead layer), or (3) NaN/Inf in grad/weight.
  Each anomaly POSTs to `POST /runs/{id}/events` in a daemon thread so it never blocks backward.
- **`_last_step` tracking** (`Run.log`): `self._last_step = step` set at the start of every
  `log()` call so parameter hooks can associate stats with the nearest training step.
- **New DuckDB table `param_stats`**: `run_id`, `step`, `timestamp`, `param_name`, `grad_norm`,
  `grad_min`, `grad_max`, `grad_mean`, `weight_norm`, `weight_min`, `weight_max`, `weight_mean`,
  `grad_histogram` (JSON array of 32 floats), `weight_histogram` (JSON array of 32 floats).
- **New DuckDB table `events`**: `run_id`, `timestamp`, `step`, `event_type`, `severity`,
  `message`, `metadata` (JSON). Used for anomaly events here; Phase 9 adds user-facing
  `run.event()` and more event types.
- **Server endpoints** (`pyrometer/server/app.py`):
  - `POST /runs/{id}/param-stats` — batch ingest, broadcasts `{"type": "param_stats"}` via WS.
  - `GET /runs/{id}/param-stats` — all stats for a run (optional `?name=` filter).
  - `GET /runs/{id}/param-stats/names` — ordered list of param names (preserves `named_parameters()` order).
  - `POST /runs/{id}/events` — ingest a single event (used internally by SDK).
  - `GET /runs/{id}/events` — all events for a run.
- **`GradientHeatmap`** (`frontend/src/components/GradientHeatmap.tsx`): d3 v7 heatmap where
  rows = parameter names, columns = profiled steps, cell color = log10(grad_norm) on a
  dark→amber→red scale. ResizeObserver keeps the SVG in sync with container width. Hover
  tooltip shows param name, step, and exact norm. Clicking a row expands a detail panel with
  `grad_norm` and `weight_norm` line charts (reuses `MetricChart`).
- **"Model Health" section** in `RunDetailPage.tsx`: Shown when `param_stats` data exists.
  Contains the `GradientHeatmap` followed by an anomaly events list (colored by severity).
  Section also responds to `param_stats` WS push events via the existing `useWebSocket` hook.
- **`examples/synthetic_train.py`**: Added `run.watch(model, log_freq=20)` after model creation.

### Design decisions

- **`Tensor.register_hook` over model-level hooks**: Leaf-parameter hooks fire exactly once
  per backward pass per parameter, which is the cleanest rate-limiting primitive. The
  `log_freq` countdown is per-parameter (not per-model), so all parameters log at the same
  backward-pass cadence.
- **`_last_step` as step proxy**: The hook fires during `.backward()`, before the optimizer
  updates weights. There's no step number naturally available in the hook context. Reading
  `self._last_step` (set by the most recent `run.log()` call) is a good proxy — in a
  standard training loop, `log()` is called at the same step, so the association is accurate.
- **CPU copy before stats**: `grad.detach().float().cpu()` before all stat computations.
  Ensures correct behavior regardless of whether the model is on CUDA or CPU, and avoids
  GPU→CPU sync inside torch.histc on large tensors where CUDA histc can stall.
- **32-bin histograms stored but not visualized yet**: The schema and data pipeline for
  weight/gradient distribution histograms is fully in place. The Phase 8 dashboard shows
  norm line charts on param click (clean, immediately useful). Ridge/small-multiples histogram
  evolution is a natural Phase 10 addition using the stored `grad_histogram` columns.
- **`events` table introduced in Phase 8**: Phase 9 formally adds the user-facing `run.event()`
  API, but the table and server endpoints are already defined here since anomaly detection
  needs somewhere to write. This avoids schema migration between phases.

### Known gaps

- Histogram visualization (ridge plot / small-multiples) deferred to Phase 10.
- `_last_step` can be slightly behind the actual backward step if the user calls `backward()`
  multiple times between `log()` calls (e.g., gradient accumulation). Acceptable for v0.1.
- Anomaly median threshold (100×) is fixed; could be configurable via `watch()` kwargs later.
- No per-layer gradient flow arrows or aggregated layer-group views for very deep models.

---

## Phase 9: Unstructured Log Capture & System Events (complete)

### What was built

- **`_TeeWriter`** (`pyrometer/_run.py`): A file-like wrapper that forwards `write()` calls to the
  original stream (stdout/stderr still appear in the terminal normally) while buffering complete
  lines via a `\n`-split loop. Thread-safe via a per-instance `threading.Lock`. Installed
  automatically at `Run.__init__` via `_install_stdout_capture()`. Restored to original streams
  in `finish()` via `_restore_stdout_capture()`.
- **Console log capture**: All `print()` statements and any writes to `sys.stdout`/`sys.stderr`
  during a run are captured without any user code changes. Lines are timestamped and tagged with
  the current `_last_step`. The buffer auto-flushes to `POST /runs/{id}/logs` when it reaches 50
  lines, and on the 5 s flush loop cycle. The complete buffer drains on `finish()`.
- **`run.event(message, event_type, severity, metadata)`**: User-facing structured event API.
  POSTs immediately to `POST /runs/{id}/events`. Events appear as vertical dashed reference lines
  on all MetricChart charts in the dashboard — amber for warning, blue for info, red for error.
- **`run.log_artifact(path, artifact_type, metadata)`**: Tracks saved files without uploading them.
  Reads the file size via `os.path.getsize` (gracefully handles missing files), then POSTs to
  `POST /runs/{id}/artifacts`. Renders as a table in the run detail view.
- **New DuckDB table `logs`**: `run_id`, `timestamp`, `stream` (stdout/stderr), `line`, `step`.
- **New DuckDB table `artifacts`**: `run_id`, `timestamp`, `step`, `artifact_type`, `path`,
  `size_bytes`, `metadata` (JSON).
- **Server endpoints** (`pyrometer/server/app.py`):
  - `POST /runs/{id}/logs` — batch ingest log lines; broadcasts `{"type": "logs"}` via WS.
  - `GET /runs/{id}/logs` — all log lines (optional `?stream=stdout|stderr` filter).
  - `POST /runs/{id}/artifacts` — ingest a single artifact record.
  - `GET /runs/{id}/artifacts` — all artifacts for a run, ordered by timestamp.
- **`LogViewer`** (`frontend/src/components/LogViewer.tsx`): Scrollable terminal-style component.
  Stream filter tabs (all/stdout/stderr — stderr tab only shown if stderr lines exist). Text
  search input filters lines client-side. Step numbers shown in a dim left gutter. stderr lines
  rendered in orange (#f97316), stdout in muted grey (#8b8fa8). Auto-scrolls to bottom on new
  lines unless search is active. Line count shown as `filtered/total`.
- **Event markers on MetricChart** (`frontend/src/components/MetricChart.tsx`): Added optional
  `events?: RunEvent[]` prop. Events with non-null `step` are rendered as `<ReferenceLine>` from
  Recharts with a dashed vertical stroke (color by severity) and a small dot label at the top.
  Deduplication ensures only one line per step. Passed from `RunDetailPage` to every MetricChart
  in the training metrics grid.
- **"Console" section** in `RunDetailPage.tsx`: Rendered when `logs.length > 0`. Shows `LogViewer`.
  WS `"logs"` events trigger a refresh.
- **"Artifacts" section** in `RunDetailPage.tsx`: Rendered when `artifacts.length > 0`. Shows a
  compact dark-themed table: type (amber), path (muted, word-break-all), size (formatted bytes),
  step number.

### Design decisions

- **Tee over redirect**: Replacing `sys.stdout` with a pure redirect would break any code that
  checks `sys.stdout.fileno()` (e.g., subprocess, ipython detection). The `_TeeWriter` forwards
  `fileno()`, `isatty()`, and all other attrs via `__getattr__` so it's transparent to callers.
- **Line-oriented buffering**: The `_TeeWriter` accumulates partial writes until a `\n` is seen,
  then emits the complete line. This correctly handles `print()` (which appends `\n`) and `write()`
  calls that don't include newlines (e.g., progress bars that use `\r`). Partial lines without a
  newline are flushed on `flush()`.
- **Step association via `_last_step`**: Captured log lines are tagged with `_last_step` at the
  time of the write. This is the same proxy used by `watch()` hooks — accurate for the standard
  training loop pattern where the user calls `log()` and then `print()` at each step.
- **No upload for artifacts**: Uploading model checkpoints would require significant storage
  management (deduplication, garbage collection, disk quotas). For v0.1 the path + metadata is
  sufficient to correlate checkpoints with training state. "Restore this checkpoint" functionality
  can be added in a future phase using the stored path.
- **Single-run stdout capture**: If the user calls `pyrometer.init()` twice in the same process,
  the second `Run` will wrap the first run's `_TeeWriter`. Restoring on `finish()` correctly
  unwinds this since each `_TeeWriter` holds a reference to the stream it wrapped. This is an
  edge case — the normal use is one run per process.
- **Event markers: deduplication per step**: Multiple events at the same step (e.g., two anomaly
  events fired at step 100) would produce overlapping lines. We keep only the first per step in
  the `Chart` component's `useMemo`, so the chart stays readable.

### Known gaps

- `_TeeWriter` does not capture output from C extensions or subprocesses (which write to the
  underlying file descriptor, not the Python `sys.stdout` object). This is a fundamental Python
  limitation and acceptable for v0.1.
- `run.event()` makes a synchronous HTTP call inside the training loop. For most use cases
  (events fired once per epoch or less) this is negligible. If high-frequency events are needed
  in the future, add an event buffer like the metrics buffer.
- Artifact table in the dashboard shows only path — no "open in file manager" or copy button yet.
