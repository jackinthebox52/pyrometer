"""Presets: named, documented defaults for autolog options and a few ``init`` options.

A preset never inspects the model. It only picks sensible settings for a kind of job, and everything it sets
can be overridden: an explicit ``init(autolog={...})`` option wins over the preset, ``run.config.<option> = ...``
wins over both (it is live), and an explicit ``monitor_interval=`` wins over the preset's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Preset:
    """One preset. ``autolog`` holds :class:`~pyrometer.AutologConfig` options; the rest are ``init`` options."""

    name: str
    summary: str
    autolog: dict[str, Any] = field(default_factory=dict)
    monitor_interval: float = 2.0  # seconds between GPU snapshots
    capture_throttle: bool = False  # log GPU clock-throttle reasons (events + hw/gpu<i>/throttle_reasons)
    reminders: tuple[str, ...] = ()  # "tokens" | "samples": hints printed once at finish if never used
    conventions: str = ""  # metric naming conventions the dashboard and the docs assume


PRESETS: dict[str, Preset] = {
    "llm-pretrain": Preset(
        name="llm-pretrain",
        summary="Language-model pretraining: tokens/sec, gradient norm, sparse profiling.",
        autolog={"log_every": 10, "grad_norm": True, "profile_every": 500, "throughput": True},
        reminders=("tokens",),
        conventions="Call run.track(tokens_per_batch=...) so tokens_seen / auto/tokens_per_sec exist; log "
        "eval metrics as eval/loss, eval/ppl.",
    ),
    "finetune": Preset(
        name="finetune",
        summary="Supervised fine-tuning: gradient norm, tokens and samples counters, generation samples.",
        autolog={"log_every": 10, "grad_norm": True, "throughput": True},
        reminders=("tokens", "samples"),
        conventions="Metrics train/loss, eval/loss, eval/<task metric>; log generations with run.log_samples "
        "(keys prompt, completion, reference, reward).",
    ),
    "rlhf": Preset(
        name="rlhf",
        summary="RLHF / preference optimisation: gradient norm, reward and KL naming, generation samples.",
        autolog={"log_every": 10, "grad_norm": True, "throughput": True},
        reminders=("samples",),
        conventions="Name reward/mean, reward/std, kl (or kl/mean), policy/entropy, policy/clip_frac; log "
        "prompt/completion/reward rows with run.log_samples each eval.",
    ),
    "rl": Preset(
        name="rl",
        summary="Reinforcement learning: env_steps axis and rolling episode statistics.",
        autolog={"log_every": 10, "grad_norm": True, "episode_window": 100, "episode_log_every": 10},
        conventions="Call run.log_episode(return_, length) when an episode ends: it logs episode/return_mean|min|max, "
        "episode/length_mean and the env_steps axis metric (select it as the chart x-axis).",
    ),
    "benchmark": Preset(
        name="benchmark",
        summary="Throughput / hardware benchmarking: fast GPU sampling, clock and throttle capture, no loss.",
        autolog={"loss": False, "lr": False, "throughput": True, "log_every": 10},
        monitor_interval=0.5,
        capture_throttle=True,
        conventions="Compare auto/steps_per_sec, auto/tokens_per_sec and the system charts across runs; "
        "clock throttling shows as thermal_throttle events.",
    ),
}


def get_preset(name: str | None) -> Preset | None:
    """Look a preset up by name (``None`` -> ``None``). Raises ``ValueError`` listing the valid names."""
    if name is None or name == "":
        return None
    try:
        return PRESETS[str(name).strip().lower()]
    except KeyError:
        raise ValueError(f"unknown preset {name!r}; valid presets: {', '.join(sorted(PRESETS))}") from None
