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
- Auth is on by default. Dashboard **accounts** (admin / user; first admin via first-run setup token, or trusted
  reverse-proxy identity header e.g. authentik) and **API keys** (SDK): global keys read/write every project and
  can register projects; project keys are bound to one project. Only admins manage users, keys and membership.
  Runs belong to **projects** (public or private); visibility is enforced per route and on WebSocket events.
  `--no-auth` local mode is loopback-only. See `docs/auth.md`.
- SDK is model-agnostic and optimizer-agnostic — never inspect or assume model architecture, 
  just log what the user passes. torch.profiler handles operator-level capture regardless of model type.


## Development Rules
- After implementing any feature, update `DEVNOTES.md` (local-only, gitignored, never tracked) with what was built, design decisions made, and any known limitations
- After implementing any feature or changing one, update the relevant documentation files in the docs/ directory
- After any significant milestone, update `README.md` usage examples and feature list
- Write docstrings on all public SDK methods — these are user-facing API
- Type hints everywhere (Python and TypeScript)
- No premature abstraction — keep it simple until a pattern repeats 3x
- DuckDB queries go in a dedicated `db.py` module, not scattered through route handlers
- API endpoints return consistent JSON shape: `{"data": ..., "error": null}` or `{"data": null, "error": "message"}`
- Frontend components go in `/frontend/src/components/`, pages in `/frontend/src/pages/`, API calls in `/frontend/src/api/`
- **Any new SDK feature that produces dashboard-visible data must be demo'd in all three main example scripts: `examples/synthetic_train.py`, `examples/lr_sweep.py`, and `examples/optimizer_compare.py`**

## Repo Hygiene (public repo — enforce on every commit)
- `AGENTS.md` is a symlink to this file. Edit `CLAUDE.md`, never replace the symlink with a copy.
- Agent notes, scratch files, plans and TODO lists (e.g. anything under `notes/`, `scratch/`, `local/`) are never
  committed and never tracked. They are gitignored; keep them there. This includes `DEVNOTES.md`,
  which stays on disk for agents to maintain but is never tracked.
- Never commit secrets (API keys, tokens, `.env`, `auth.json`) or anything specific to the maintainer's
  environment: IP addresses, hostnames, domains, network layout, homelab details. Use placeholders
  (`pyrometer.example.com`, `192.0.2.x`) in docs and examples.
- Never commit agent-specific or personal config (`.claude/`, personal skills, `CLAUDE.local.md`).
  Personal skills are kept untracked in `local/` and installed to `~/.claude/skills/`, never in this repo.
- Before committing, check `git status` / `git diff --cached` for any of the above.

## First-Time Deployment Setup ("set it up")
Maintainer-authorized development-cycle convenience. When the maintainer says **"set it up"** for a new
deployment, do the whole first-run yourself instead of walking them through it:
1. Deploy or start the server. Target, paths and procedure are in `CLAUDE.local.md` (untracked).
2. Create the admin from the host: `pyrometer admin set-password --user admin` (generates a password; no setup token needed).
3. Create a global API key: `pyrometer keys create --name <name>`.
4. Paste the admin password and the API key to the maintainer in chat, once, and say they are shown once.
   Never write them to any file, commit, log or doc. Verify with `/health` and one authenticated request,
   without echoing the credentials again.
Only do this when asked. It is a dev-cycle shortcut, not a production practice; the maintainer handles
keeping the chat log free of live credentials.

## MCP Tools

- Use the context7 tool if you ever need up-to-date information about a library. (e.g. pytorch, scikit-learn, numpy, uvicorn)

## SDK Public API (canonical reference)
- `pyrometer.init(name, hyperparams, tags, server, api_key, project, group, offline, anomaly_detection)` → Run  (`server` ← `PYROMETER_SERVER`, `api_key` ← `PYROMETER_API_KEY` if omitted; `group` ← `PYROMETER_GROUP` (sweep/experiment name; filter + list per project); `offline` ← `PYROMETER_MODE=offline`: spool only, upload with `pyrometer sync`; `anomaly_detection` True|False|dict of threshold overrides; `resume=<run_id>|"auto"` (← `PYROMETER_RUN_ID`) reattaches to an existing run, see `run.resumed` / `run.last_step`)
- `pyrometer.create_project(name, private, server, api_key)` → dict  (needs a global key; private projects return their own key)
- `run.log(metrics_dict, step)` → None
- `run.profile(step)` → context manager wrapping torch.profiler
- `run.watch(model, log_freq=100)` → None  (registers gradient/weight monitoring hooks)
- `run.event(message, event_type, severity, metadata)` → None  (structured event / milestone marker)
- `run.log_artifact(path, artifact_type, metadata)` → None  (track saved files without uploading)
- `run.log_image(name, image, step)` → None  (PIL Image, numpy array, or torch.Tensor CHW/HW/NCHW)
- `run.log_histogram(name, data, step, num_bins=32)` → None  (tensor/array → bin edges + counts)
- `run.log_figure(name, figure, step)` → None  (matplotlib Figure → PNG)
- `run.log_samples(step, samples, name="samples", mode="replace")` → None  (list of dicts: prompt/completion/reward/...; Samples tab; `mode="replace"` overwrites the (name, step), `"append"` adds)
- `run.finish()` → None
- System monitor thread starts automatically on init, polls pynvml every 2s
- stdout/stderr capture installed automatically on init, restored on finish()

## Anti-Patterns to Avoid
- Don't add caching/Redis/message queues — DuckDB is fast enough for single-user
- Don't profile every training step — SDK defaults to profiling every 100 steps
- Don't use ORM (SQLAlchemy etc) — raw DuckDB SQL is clearer for analytical queries
- Don't split the frontend into micro-components prematurely
