"""Deterministic, conservative anomaly detection for metrics and gradient norms.

Pure Python (no I/O, no clocks, no threads) so it is easy to test. ``Run`` feeds values in and
submits whatever :meth:`MetricDetector.observe` / :meth:`GradDetector.observe` return.

Design: NaN/Inf always fires (when detection is enabled). Spikes use a rolling median/MAD
(robust to heavy-tailed noise), need a warmup of samples, must clear both a robust z-score and a
relative-change floor, are rate-limited by a per-key cooldown measured in training steps, and are
capped per key and per run. Metrics that are logged rarely (per epoch, per eval) would never leave the
warmup, so detection starts earlier for them (``sparse_min_samples``) with thresholds that are
``sparse_strictness`` times stricter and relax linearly to the normal ones as samples accumulate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields


@dataclass
class AnomalyConfig:
    """Tunable anomaly-detection thresholds. Every field can be overridden via
    ``pyrometer.init(anomaly_detection={...})``."""

    enabled: bool = True
    nan_inf: bool = True  # always-on first-class check (still off if enabled=False)
    window: int = 100  # rolling window of recent values per metric / parameter
    warmup: int = 50  # samples a metric needs before spike detection starts
    metric_z: float = 12.0  # robust z (0.6745*|x-median|/MAD) needed to flag a metric spike
    metric_rel_change: float = 1.0  # also |x-median| must exceed this * |median|
    sparse_min_samples: int = 8  # sparse metrics (logged < ``warmup`` times, e.g. per epoch) are checked from here on
    sparse_strictness: float = 2.5  # ...with z and relative-change thresholds this many times stricter at that
    # point, relaxing linearly to the normal thresholds at ``warmup`` samples. Set ``sparse_min_samples`` >=
    # ``warmup`` to restore "no spike detection before ``warmup`` samples".
    cooldown_steps: int = 500  # min steps between spike events for the same metric/param
    max_events_per_key: int = 5  # spike events per metric / parameter per run
    grad_warmup: int = 20  # gradient samples per parameter before spike detection
    grad_spike_factor: float = 1000.0  # grad norm must exceed this * rolling median
    grad_z: float = 12.0
    dead_threshold: float = 1e-8
    dead_streak: int = 200  # consecutive samples below dead_threshold -> "dead layer"
    max_grad_events: int = 20  # grad spike/dead events per run across all parameters

    @classmethod
    def from_option(cls, option: bool | dict | None) -> "AnomalyConfig":
        """Build a config from the ``init(anomaly_detection=...)`` value."""
        if option is None or option is True:
            return cls()
        if option is False:
            return cls(enabled=False)
        if isinstance(option, dict):
            known = {f.name for f in fields(cls)}
            unknown = set(option) - known
            if unknown:
                raise ValueError(
                    f"Unknown anomaly_detection keys: {sorted(unknown)}. Valid: {sorted(known)}"
                )
            return cls(**option)
        raise TypeError("anomaly_detection must be True, False or a dict of overrides")


def _median(sorted_vals: list[float]) -> float:
    n = len(sorted_vals)
    mid = n // 2
    return sorted_vals[mid] if n % 2 else 0.5 * (sorted_vals[mid - 1] + sorted_vals[mid])


def _robust_stats(window: list[float]) -> tuple[float, float]:
    """Return (median, scale) where scale is the MAD (falling back to mean abs deviation)."""
    s = sorted(window)
    med = _median(s)
    mad = _median(sorted(abs(x - med) for x in window))
    if mad <= 0:
        mad = sum(abs(x - med) for x in window) / len(window) / 1.253314  # MeanAD -> sigma-ish
        mad *= 0.6745  # keep the same convention as MAD for the z formula
    return med, mad


class MetricDetector:
    """Per-metric detector. ``observe`` returns ``(severity, message)`` or ``None``."""

    def __init__(self, cfg: AnomalyConfig) -> None:
        self.cfg = cfg
        self._hist: dict[str, list[float]] = {}
        self._last_fire: dict[str, int] = {}
        self._fired: dict[str, int] = {}
        self._nan_reported: set[str] = set()

    def observe(self, name: str, value: float, step: int) -> tuple[str, str] | None:
        cfg = self.cfg
        if not cfg.enabled:
            return None
        if not math.isfinite(value):
            if cfg.nan_inf and name not in self._nan_reported:
                # Once per metric: a diverged run logs NaN every step and would otherwise flood.
                self._nan_reported.add(name)
                return "error", f"NaN or Inf in metric '{name}'"
            return None
        self._nan_reported.discard(name)  # recovered; a later NaN is news again

        hist = self._hist.setdefault(name, [])
        window = hist[-cfg.window:]
        result = None
        if len(window) >= min(cfg.warmup, cfg.sparse_min_samples):
            result = self._check_spike(name, value, step, window)
        hist.append(value)
        if len(hist) > cfg.window:
            del hist[0]
        return result

    def seed(self, name: str, values: list[float]) -> None:
        """Pre-load history (oldest first) for ``name``, e.g. from a resumed run's stored metrics.

        Seeded values come *before* anything observed so far; they never fire events and do not
        touch cooldowns or NaN state.
        """
        if not self.cfg.enabled:
            return
        good = [float(v) for v in values if math.isfinite(v)]
        hist = self._hist.setdefault(name, [])
        hist[:0] = good
        if len(hist) > self.cfg.window:
            del hist[: len(hist) - self.cfg.window]

    def _strictness(self, n: int) -> float:
        """Threshold multiplier for a metric with ``n`` samples: 1 once warmed up, more before."""
        cfg = self.cfg
        if n >= cfg.warmup or cfg.warmup <= cfg.sparse_min_samples:
            return 1.0
        frac = (cfg.warmup - n) / (cfg.warmup - cfg.sparse_min_samples)
        return 1.0 + (cfg.sparse_strictness - 1.0) * min(1.0, frac)

    def _check_spike(
        self, name: str, value: float, step: int, window: list[float]
    ) -> tuple[str, str] | None:
        cfg = self.cfg
        if self._fired.get(name, 0) >= cfg.max_events_per_key:
            return None
        last = self._last_fire.get(name)
        if last is not None and step - last < cfg.cooldown_steps:
            return None
        med, scale = _robust_stats(window)
        dev = abs(value - med)
        if scale <= 1e-12:
            return None
        z = 0.6745 * dev / scale
        k = self._strictness(len(window))
        if z < cfg.metric_z * k or dev <= cfg.metric_rel_change * k * abs(med):
            return None
        self._last_fire[name] = step
        self._fired[name] = self._fired.get(name, 0) + 1
        return "warning", f"Spike in '{name}': {value:.4g} (robust z={z:.1f}, median={med:.4g})"


class GradDetector:
    """Per-parameter gradient-norm detector. ``observe`` returns messages (no severity: warning)."""

    def __init__(self, cfg: AnomalyConfig) -> None:
        self.cfg = cfg
        self._hist: dict[str, list[float]] = {}
        self._streak: dict[str, int] = {}
        self._last_fire: dict[str, int] = {}
        self._fired: dict[str, int] = {}
        self._total = 0
        self._nan_reported: set[str] = set()

    def observe(self, param: str, grad_norm: float, step: int, has_nan_inf: bool) -> str | None:
        cfg = self.cfg
        if not cfg.enabled:
            return None
        if has_nan_inf or not math.isfinite(grad_norm):
            if cfg.nan_inf and param not in self._nan_reported:
                self._nan_reported.add(param)
                return "NaN or Inf detected in gradients or weights"
            return None
        self._nan_reported.discard(param)

        hist = self._hist.setdefault(param, [])
        window = hist[-cfg.window:]
        msg: str | None = None
        if len(window) >= cfg.grad_warmup and self._may_fire(param, step):
            med, scale = _robust_stats(window)
            if med > 0 and grad_norm > cfg.grad_spike_factor * med:
                z = 0.6745 * (grad_norm - med) / scale if scale > 1e-12 else float("inf")
                if z >= cfg.grad_z:
                    msg = f"Gradient norm spike: {grad_norm:.4g} vs median {med:.4g}"
                    self._record(param, step)
        hist.append(grad_norm)
        if len(hist) > cfg.window:
            del hist[0]

        if grad_norm < cfg.dead_threshold:
            streak = self._streak.get(param, 0) + 1
            self._streak[param] = streak
            if streak == cfg.dead_streak and msg is None and self._total < cfg.max_grad_events:
                self._total += 1
                msg = (
                    f"Gradient norm below {cfg.dead_threshold:g} for {streak} consecutive "
                    "samples — possible dead layer"
                )
        else:
            self._streak[param] = 0
        return msg

    def _may_fire(self, param: str, step: int) -> bool:
        cfg = self.cfg
        if self._total >= cfg.max_grad_events or self._fired.get(param, 0) >= cfg.max_events_per_key:
            return False
        last = self._last_fire.get(param)
        return last is None or step - last >= cfg.cooldown_steps

    def _record(self, param: str, step: int) -> None:
        self._last_fire[param] = step
        self._fired[param] = self._fired.get(param, 0) + 1
        self._total += 1


__all__ = ["AnomalyConfig", "MetricDetector", "GradDetector"]
