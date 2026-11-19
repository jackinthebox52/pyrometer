# Pyrometer - Claude Development Notes

## Project
Self-hosted PyTorch experiment dashboard with integrated GPU profiling.
Monorepo: Python SDK (`/sdk`), FastAPI server (`/server`), React frontend (`/frontend`).

## Architecture Decisions
- DuckDB for storage (single file, columnar, analytical queries)
- FastAPI + uvicorn for API
- React 18 + TypeScript + Vite + Recharts + d3 for frontend
- SDK ships as pip-installable package, torch is a peer dependency
- WebSocket for live dashboard updates during active training runs
- SDK is model-agnostic and optimizer-agnostic — never inspect or assume model architecture, 
  just log what the user passes. torch.profiler handles operator-level capture regardless of model type.


## Development Rules
- After implementing any feature, update `DEVNOTES.md` with what was built, design decisions made, and any known limitations
- After implementing any feature or changing one, update the relevant documentation files in the docs/ directory
- After any significant milestone, update `README.md` usage examples and feature list
- Write docstrings on all public SDK methods — these are user-facing API
- Type hints everywhere (Python and TypeScript)
- No premature abstraction — keep it simple until a pattern repeats 3x
- DuckDB queries go in a dedicated `db.py` module, not scattered through route handlers
- API endpoints return consistent JSON shape: `{"data": ..., "error": null}` or `{"data": null, "error": "message"}`
- Frontend components go in `/frontend/src/components/`, pages in `/frontend/src/pages/`, API calls in `/frontend/src/api/`
- **Any new SDK feature that produces dashboard-visible data must be demo'd in all three main example scripts: `examples/synthetic_train.py`, `examples/lr_sweep.py`, and `examples/optimizer_compare.py`**

## MCP Tools

- Use the context7 tool if you ever need up-to-date information about a library. (e.g. pytorch, scikit-learn, numpy, uvicorn)

## SDK Public API (canonical reference)
- `pyrometer.init(name, hyperparams, tags, server)` → Run
- `run.log(metrics_dict, step)` → None
- `run.profile(step)` → context manager wrapping torch.profiler
- `run.watch(model, log_freq=100)` → None  (registers gradient/weight monitoring hooks)
- `run.event(message, event_type, severity, metadata)` → None  (structured event / milestone marker)
- `run.log_artifact(path, artifact_type, metadata)` → None  (track saved files without uploading)
- `run.log_image(name, image, step)` → None  (PIL Image, numpy array, or torch.Tensor CHW/HW/NCHW)
- `run.log_histogram(name, data, step, num_bins=32)` → None  (tensor/array → bin edges + counts)
- `run.log_figure(name, figure, step)` → None  (matplotlib Figure → PNG)
- `run.finish()` → None
- System monitor thread starts automatically on init, polls pynvml every 2s
- stdout/stderr capture installed automatically on init, restored on finish()

## Anti-Patterns to Avoid
- Don't add caching/Redis/message queues — DuckDB is fast enough for single-user
- Don't add user auth for v0.1
- Don't profile every training step — SDK defaults to profiling every 100 steps
- Don't use ORM (SQLAlchemy etc) — raw DuckDB SQL is clearer for analytical queries
- Don't split the frontend into micro-components prematurely
