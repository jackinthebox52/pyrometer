# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Two audiences with equal weight: a solo ML researcher or engineer, and a small team or lab sharing one
self-hosted server. Both keep the dashboard open next to a terminal while long PyTorch jobs run
(pretraining, fine-tuning, RLHF/RL, benchmarks). They check in now and then during a run and come back
afterwards to compare runs and work out why one was slower or worse.

## Product Purpose
Pyrometer is a self-hosted experiment tracker that also profiles GPU performance. Wrapping training in
`with pyrometer.init(...) as run:` records metrics, system and GPU usage, gradient health, logs, media,
samples and torch.profiler traces with no logging calls. The dashboard serves three jobs with equal weight:
- **live run health:** is the run alive, is loss falling, is the GPU busy, has anything broken
- **post-hoc comparison:** which config won
- **performance diagnosis:** why a run is slow, from kernels, operators and memory

Success means each user sees what matters for their job and project, with everything else kept out of the
way.

## Positioning
Tracking and profiling live in one self-hosted tool, with per-process GPU attribution and autolog that never
syncs the stream. Working line: "the tracker that tells you why your run is slow."

## Operating Context
- Usually open in a browser tab beside a terminal or editor, sometimes for hours. It is also checked from a
  phone during long runs.
- Data arrives live over a WebSocket while runs train.
- Runs belong to projects (public or private) and can be grouped into sweeps.
- Accounts are admin or user. Only admins archive, delete or move runs; users can pin runs.

## Capabilities and Constraints
- **Stack:** React 18, TypeScript, Vite, Tailwind, Recharts and d3, served by FastAPI from one container.
- **Routes:** run list sidebar, run detail, compare view, settings (accounts, keys, projects, alerts,
  retention).
- **Run data:** metrics with log-scale, smoothing, normalization and a choice of x-axis; GPU and system
  charts; model health (gradient heatmap and anomalies); console logs; artifacts; media; samples; profiler
  (kernel timeline, operator breakdown, memory deltas, hints).
- **Presets:** llm-pretrain, finetune, rlhf, rl and benchmark set different SDK defaults.
- **Requirement:** what is shown must be easy to configure per job or project, so users can hide what they
  don't need. Ease of use is paramount.

## Brand Commitments
- The name is "Pyrometer". No logo asset exists yet.
- The user said nothing in the existing visual design is sacred.

## Evidence on Hand
- The example scripts in `examples/` generate realistic synthetic runs for demos.
- There are no testimonials, customers or benchmark claims. Never invent any.

## Product Principles
1. **Ease of use first.** The common check-in ("is my run OK?") is answered at a glance, with no hunting.
2. **Show only what matters.** Panels and sections can be configured per project or job, and unused data
   stays out of the way.
3. **Tell people why, not just what.** Surface performance diagnosis next to the metrics, not hidden
   behind it.
4. **Trust the numbers.** Exact values, units and honest empty or missing-data states. Never imply data
   exists when it does not.
