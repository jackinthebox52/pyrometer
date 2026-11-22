# Pyrometer Developer Guide

Guide for extending and contributing to Pyrometer.

---

## Table of Contents

- [Project Structure](#project-structure)
- [Development Setup](#development-setup)
- [Building the Frontend](#building-the-frontend)
- [Code Style](#code-style)
- [Adding Features](#adding-features)
  - [New SDK Method](#new-sdk-method)
  - [New API Endpoint](#new-api-endpoint)
  - [New Dashboard Component](#new-dashboard-component)
- [Testing](#testing)
- [Release Process](#release-process)
- [Known Gaps](#known-gaps)

---

## Project Structure

```
pyrometer/
├── pyrometer/              # Python SDK + Server
│   ├── __init__.py         # SDK entry point (pyrometer.init)
│   ├── _hardware.py        # Hardware fingerprinting
│   ├── _run.py             # Run class (log, watch, profile, finish)
│   ├── cli.py              # CLI entry point (pyrometer serve)
│   └── server/
│       ├── __init__.py
│       ├── app.py          # FastAPI application (route handlers)
│       └── db.py           # DuckDB access layer (all queries)
│
├── frontend/               # React dashboard
│   ├── src/
│   │   ├── App.tsx         # Root component + router
│   │   ├── main.tsx        # Entry point
│   │   ├── index.css       # Global styles
│   │   ├── api/
│   │   │   ├── client.ts   # API client (apiFetch wrapper)
│   │   │   └── types.ts    # TypeScript interfaces
│   │   ├── components/     # Reusable UI components
│   │   │   ├── CompareChart.tsx
│   │   │   ├── GradientHeatmap.tsx
│   │   │   ├── KernelTimeline.tsx
│   │   │   ├── LogViewer.tsx
│   │   │   ├── MediaGallery.tsx
│   │   │   ├── MemoryDeltaChart.tsx
│   │   │   ├── MetricChart.tsx
│   │   │   ├── OperatorBreakdown.tsx
│   │   │   ├── RunSidebar.tsx
│   │   │   └── ...
│   │   ├── hooks/
│   │   │   └── useWebSocket.ts  # WebSocket hook
│   │   ├── pages/
│   │   │   ├── CompareView.tsx
│   │   │   ├── EmptyState.tsx
│   │   │   └── RunDetailPage.tsx
│   │   └── utils/
│   │       └── format.ts   # Formatting utilities
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   └── tsconfig.json
│
├── examples/               # Example training scripts
│   ├── synthetic_train.py
│   ├── lr_sweep.py
│   ├── optimizer_compare.py
│   ├── ablation_study.py
│   └── run_all.py
│
├── docs/                   # Documentation
│   ├── index.md
│   ├── sdk-reference.md
│   ├── webapp-guide.md
│   ├── server-api.md
│   ├── architecture.md
│   └── development.md
│
├── scripts/                # Utility scripts
├── main.py                 # Entry point (uv run pyrometer)
├── pyproject.toml          # Python dependencies
├── uv.lock                 # Lock file
├── README.md
└── CLAUDE.md
```

---

## Development Setup

### Prerequisites

- Python 3.11+
- `uv` package manager
- Node.js 18+ (for frontend development)

### Python Environment

```bash
# Install dependencies
uv sync

# Run the server
uv run pyrometer serve

# Run an example
uv run python examples/synthetic_train.py
```

### Frontend Development

```bash
cd frontend

# Install dependencies
npm install

# Start dev server (with Vite HMR)
npm run dev

# The dev server proxies API requests to localhost:8321
# Open http://localhost:5173 in your browser
```

### Running Both

For live development, run both the server and frontend dev server:

```bash
# Terminal 1: Python server
uv run pyrometer serve

# Terminal 2: Frontend dev server
cd frontend && npm run dev
```

---

## Building the Frontend

### Production Build

```bash
cd frontend
npm run build
```

This outputs to `frontend/dist/`, which the FastAPI server serves as static files.

### Serve Production Build

```bash
# The server automatically serves frontend/dist if it exists
uv run pyrometer serve
# Open http://localhost:8321
```

### Dev Mode

When `frontend/dist` doesn't exist, the server runs in API-only mode. Use the Vite dev server for frontend development.

---

## Code Style

### Python

- **Type hints**: Use type annotations for all function signatures
- **Docstrings**: Google-style docstrings for public APIs
- **Naming**: snake_case for functions/variables, PascalCase for classes
- **Imports**: Standard library → third-party → local, with blank lines between
- **Line length**: 100 characters max

Example:

```python
def db_get_metrics(run_id: str, name: str | None = None) -> list[dict]:
    """Get all metrics for a run, optionally filtered by name.
    
    Args:
        run_id: The run UUID.
        name: Optional metric name filter.
    
    Returns:
        List of metric dicts with step, timestamp, name, value.
    """
    with _lock:
        conn = get_conn()
        # ...
```

### TypeScript/React

- **TypeScript**: Strict mode, no `any` types
- **Components**: Functional components with hooks
- **Props**: TypeScript interfaces for prop types
- **Naming**: PascalCase for components, camelCase for functions/variables
- **Tailwind**: Use Tailwind classes for styling, inline styles for dynamic values

Example:

```typescript
interface MetricChartProps {
  name: string
  data: Metric[]
  events: RunEvent[]
}

export default function MetricChart({ name, data, events }: MetricChartProps) {
  const smoothed = useSmooth(data, alpha)
  
  return (
    <div className="rounded-lg p-4" style={{ backgroundColor: '#111318' }}>
      <RechartsLineChart data={smoothed}>
        {/* ... */}
      </RechartsLineChart>
    </div>
  )
}
```

---

## Adding Features

### New SDK Method

1. **Add method to `pyrometer/_run.py`**:

```python
def log_scalar(self, name: str, value: float, step: int) -> None:
    """Log a single scalar metric.
    
    Convenience wrapper around log() for single metrics.
    
    Args:
        name: Metric name (e.g., "train/loss").
        value: Scalar value.
        step: Training step.
    """
    if self._finished:
        return
    self.log({name: value}, step=step)
```

2. **Update docstrings in `pyrometer/__init__.py`** if the method is part of the public API.

3. **Add server endpoint** (if needed) in `pyrometer/server/app.py`:

```python
@app.post("/runs/{run_id}/scalars", status_code=201)
async def ingest_scalar(run_id: str, entry: ScalarEntry):
    # ...
```

4. **Add DB function** in `pyrometer/server/db.py`:

```python
def db_insert_scalar(run_id: str, entry: dict) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("INSERT INTO scalars VALUES (...)", [...])
```

5. **Update frontend types** in `frontend/src/api/types.ts`:

```typescript
export interface Scalar {
  step: number
  timestamp: number
  name: string
  value: number
}
```

6. **Add API client method** in `frontend/src/api/client.ts`:

```typescript
getScalars: (id: string) => apiFetch<Scalar[]>(`/runs/${id}/scalars`),
```

---

### New API Endpoint

1. **Define Pydantic model** in `pyrometer/server/app.py`:

```python
class NewEntry(BaseModel):
    field1: str
    field2: int | None = None
```

2. **Add route handler**:

```python
@app.post("/runs/{run_id}/new-endpoint", status_code=201)
async def ingest_new(run_id: str, entries: list[NewEntry]):
    inserted = db_insert_new(run_id, [e.model_dump() for e in entries])
    await _ws.broadcast({"type": "new", "run_id": run_id})
    return ok({"inserted": inserted})
```

3. **Add DB function** in `pyrometer/server/db.py`:

```python
def db_insert_new(run_id: str, entries: list[dict]) -> int:
    with _lock:
        conn = get_conn()
        conn.executemany("INSERT INTO new_table VALUES (...)", [...])
    return len(entries)
```

4. **Update API documentation** in `docs/server-api.md`.

---

### New Dashboard Component

1. **Create component file** in `frontend/src/components/`:

```typescript
// frontend/src/components/NewComponent.tsx
import React from 'react'

interface NewComponentProps {
  data: SomeData[]
}

export default function NewComponent({ data }: NewComponentProps) {
  return (
    <div className="rounded-lg p-4" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
      <h2 className="text-xs font-mono uppercase mb-3" style={{ color: '#c8cad6', letterSpacing: '0.12em' }}>
        New Component
      </h2>
      {/* Component content */}
    </div>
  )
}
```

2. **Import and use in parent component**:

```typescript
// frontend/src/pages/RunDetailPage.tsx
import NewComponent from '../components/NewComponent.tsx'

// In the component render:
<NewComponent data={someData} />
```

3. **Follow existing patterns**:
   - Use the same color palette (`#111318` for cards, `#252830` for borders)
   - Use JetBrains Mono for numeric data
   - Use `SectionLabel` for section headers
   - Support dark mode (all colors are hard-coded for the dark theme)

---

## Testing

### Manual Testing

The project currently relies on manual testing via the example scripts:

```bash
# Terminal 1: Start server
uv run pyrometer serve

# Terminal 2: Run example
uv run python examples/synthetic_train.py

# Open http://localhost:8321 and verify:
# - Run appears in sidebar
# - Metrics chart updates live
# - Profiler view shows data
# - Comparison view works
```

### Future Testing Infrastructure

Planned additions:

1. **Unit tests** for SDK (`tests/test_sdk.py`):
   - Test `run.log()` buffering
   - Test `run.watch()` hook registration
   - Test `run.profile()` context manager

2. **Integration tests** for server (`tests/test_server.py`):
   - Test API endpoints
   - Test WebSocket events
   - Test DB queries

3. **Frontend tests** (`frontend/src/**/*.test.tsx`):
   - Component rendering tests
   - API client tests
   - WebSocket hook tests

---

## Release Process

### Version Bump

1. Update version in `pyproject.toml`:

```toml
[project]
name = "pyrometer"
version = "0.2.0"  # Bump this
```

2. Update version in `pyrometer/server/app.py`:

```python
app = FastAPI(title="Pyrometer API", version="0.2.0")
```

3. Update `CHANGELOG.md` (create if needed).

### Publish to PyPI

```bash
# Build package
uv build

# Publish
uv publish
```

### Tag Release

```bash
git tag -a v0.2.0 -m "Release v0.2.0"
git push origin v0.2.0
```

---

## Known Gaps

These are documented gaps that should be addressed in future releases:

### SDK

- [ ] No way to disable console capture
- [ ] `run.profile()` always captures CPU+CUDA; no CPU-only mode
- [ ] Single-GPU assumption in `_ship_profile` (`device_id = 0`)
- [ ] No explicit support for multi-node training

### Server

- [ ] No authentication (intentional for v0.1, but worth revisiting)
- [ ] No rate limiting (fine for single-user, local deployment)
- [ ] No pagination for large metric queries
- [ ] No API versioning

### Frontend

- [ ] No error boundary (unhandled errors white-screen)
- [ ] No chart zoom/pan in comparison charts
- [ ] No PNG export for charts
- [ ] No keyboard shortcuts
- [ ] No search in console logs
- [ ] Fonts loaded from Google Fonts CDN (fails offline)
- [ ] `KernelTimeline` doesn't redraw on container resize after mount (no `ResizeObserver` in v0.1, added in Phase 7)

### Documentation

- [ ] No video tutorials
- [ ] No troubleshooting guide
- [ ] No performance tuning guide

---

## Contributing

### Pull Request Process

1. Create a feature branch from `main`
2. Make changes following the code style guidelines
3. Test manually with the example scripts
4. Update documentation if adding/changing functionality
5. Submit PR with a clear description of changes

### Commit Messages

Follow conventional commits:

```
feat: add gradient histogram visualization
fix: correct memory delta calculation in profiler
docs: update SDK reference with log_figure example
refactor: extract WebSocket manager class
```

### Code Review

- All PRs require at least one review
- Reviewers check for:
  - Correctness
  - Code style adherence
  - Documentation updates
  - No breaking changes (or properly versioned)

---

## Debugging

### SDK Debugging

Enable debug logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

import pyrometer
run = pyrometer.init(...)
```

### Server Debugging

The server logs at INFO level by default. For debug logs:

```bash
uv run pyrometer serve  # Check server output
```

### Frontend Debugging

Open browser DevTools:

- **Console**: Check for errors
- **Network**: Inspect API requests
- **React DevTools**: Inspect component state

### Database Debugging

Query the DuckDB directly:

```python
import duckdb

conn = duckdb.connect("~/.pyrometer/data.duckdb")

# List runs
conn.execute("SELECT * FROM runs").df()

# Get metrics for a run
conn.execute("""
    SELECT name, COUNT(*), MIN(step), MAX(step)
    FROM metrics
    WHERE run_id = 'your-run-id'
    GROUP BY name
""").df()
```

---

## Performance Considerations

### SDK

- **Buffering**: Metrics are batched to reduce HTTP overhead
- **Background threads**: Network I/O never blocks the training loop
- **Profiler**: Use `key_averages()` to reduce event count

### Server

- **Single DB connection + lock**: Serializes writes, fine for single-user
- **WebSocket throttling**: 500ms throttle per event type per run
- **No caching**: DuckDB is fast enough for analytical queries

### Frontend

- **Polling interval**: 5s matches SDK flush interval
- **Targeted refetches**: WebSocket events trigger specific data refetches, not full reloads
- **No animation**: `isAnimationActive={false}` prevents jank on rapid polls

---

## Future Roadmap

### Phase 9+ (Planned)

- [ ] Multi-GPU support
- [ ] Distributed training support
- [ ] Artifact upload (optional S3/local storage)
- [ ] Run annotations and comments
- [ ] Dashboard theming (light mode)
- [ ] Offline mode with sync

### Phase 10+ (Future)

- [ ] LLM chatbot for run analysis
- [ ] Automated optimization suggestions (ML-driven)
- [ ] Run grouping and clustering
- [ ] Alerting (email/Slack on anomaly)
- [ ] Multi-user support with authentication
