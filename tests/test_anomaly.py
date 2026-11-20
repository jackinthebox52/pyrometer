import math
import random

import pytest

from pyrometer._anomaly import AnomalyConfig, GradDetector, MetricDetector


def _healthy_loss(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    # decaying loss with heavy-ish noise and occasional moderate bumps
    return [2.0 * math.exp(-i / 400) + 0.3 + rng.gauss(0, 0.05) + (0.2 if rng.random() < 0.02 else 0)
            for i in range(n)]


@pytest.mark.parametrize("seed", range(5))
def test_healthy_noisy_curve_fires_nothing(seed):
    det = MetricDetector(AnomalyConfig())
    events = [det.observe("loss", v, i) for i, v in enumerate(_healthy_loss(3000, seed))]
    assert not any(events)


def test_healthy_grad_norms_fire_nothing():
    det = GradDetector(AnomalyConfig())
    rng = random.Random(0)
    for i in range(3000):
        g = abs(rng.lognormvariate(-2, 0.7))
        assert det.observe("w", g, i, False) is None


def test_real_spike_fires_once_then_cooldown():
    det = MetricDetector(AnomalyConfig())
    for i, v in enumerate(_healthy_loss(200, 1)):
        assert det.observe("loss", v, i) is None
    first = det.observe("loss", 50.0, 200)
    assert first and first[0] == "warning"
    assert det.observe("loss", 60.0, 201) is None  # cooldown


def test_no_spike_during_warmup():
    det = MetricDetector(AnomalyConfig())
    for i in range(5):  # fewer than sparse_min_samples
        det.observe("loss", 1.0 + 0.01 * i, i)
    assert det.observe("loss", 500.0, 5) is None


def test_nan_always_fires_even_in_warmup_once_per_streak():
    det = MetricDetector(AnomalyConfig())
    assert det.observe("loss", float("nan"), 0)[0] == "error"
    assert det.observe("loss", float("inf"), 1) is None  # no flood
    det.observe("loss", 1.0, 2)
    assert det.observe("loss", float("nan"), 3) is not None  # recovered then diverged again


def test_disabled_and_overrides():
    assert MetricDetector(AnomalyConfig.from_option(False)).observe("l", float("nan"), 0) is None
    cfg = AnomalyConfig.from_option({"warmup": 5, "cooldown_steps": 0})
    assert cfg.warmup == 5 and cfg.enabled
    with pytest.raises(ValueError):
        AnomalyConfig.from_option({"bogus": 1})


def test_grad_spike_nan_and_dead_layer():
    det = GradDetector(AnomalyConfig())
    for i in range(30):
        assert det.observe("w", 0.1 + 0.001 * (i % 3), i, False) is None
    assert "spike" in det.observe("w", 500.0, 30, False)
    assert "NaN" in det.observe("w", 1.0, 31, True)
    dead = GradDetector(AnomalyConfig())
    msgs = [dead.observe("d", 0.0, i, False) for i in range(300)]
    assert sum(m is not None for m in msgs) == 1 and msgs[199] is not None


# --- sparse metrics (adaptive warmup) -----------------------------------------------------------

def _sparse_series(kind: str, n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    if kind == "val_loss":  # noisy, decreasing
        return [1.5 * math.exp(-i / 15) + 0.4 + rng.gauss(0, 0.04) for i in range(n)]
    if kind == "accuracy":  # saturating, bounded
        return [min(0.99, 0.5 + 0.45 * (1 - math.exp(-i / 10)) + rng.gauss(0, 0.01)) for i in range(n)]
    if kind == "plateau_noisy":
        return [1.0 + rng.gauss(0, 0.1) for _ in range(n)]
    return [rng.lognormvariate(0, 0.3) for _ in range(n)]  # heavy-tailed positive


@pytest.mark.parametrize("kind", ["val_loss", "accuracy", "plateau_noisy", "heavy"])
def test_sparse_healthy_metrics_fire_nothing(kind):
    fired = 0
    for seed in range(300):
        det = MetricDetector(AnomalyConfig())
        for i, v in enumerate(_sparse_series(kind, 49, seed)):  # never reaches the 50-sample warmup
            fired += det.observe("val", v, i * 100) is not None
    assert fired == 0


def test_sparse_metric_spike_is_detected_before_full_warmup():
    det = MetricDetector(AnomalyConfig())
    for i, v in enumerate(_sparse_series("val_loss", 12, 3)):
        assert det.observe("val_loss", v, i * 100) is None
    found = det.observe("val_loss", 40.0, 1200)
    assert found and found[0] == "warning" and "val_loss" in found[1]


def test_sparse_needs_min_samples_and_is_stricter_when_small():
    det = MetricDetector(AnomalyConfig())
    for i in range(7):
        det.observe("m", 1.0 + 0.01 * (i % 3), i)
    assert det.observe("m", 500.0, 7) is None  # 7 samples: still warming up
    # a clear but moderate outlier (2.5x the median): ignored with 8 samples, flagged once fully warmed up
    def series(n):
        d = MetricDetector(AnomalyConfig(cooldown_steps=0))
        for i in range(n):
            d.observe("m", 1.0 + 0.01 * (i % 3), i)
        return d

    assert series(8).observe("m", 2.5, 100) is None
    assert series(60).observe("m", 2.5, 100) is not None


def test_sparse_can_be_disabled_with_min_samples():
    det = MetricDetector(AnomalyConfig(sparse_min_samples=50))
    for i in range(20):
        det.observe("m", 1.0 + 0.01 * (i % 3), i)
    assert det.observe("m", 500.0, 20) is None


def test_seed_prefills_history_without_events_and_skips_warmup():
    det = MetricDetector(AnomalyConfig())
    det.seed("loss", [1.0 + 0.01 * (i % 5) for i in range(100)] + [float("nan")])
    found = det.observe("loss", 80.0, 5000)  # first value of the resumed process
    assert found and found[0] == "warning"
    cold = MetricDetector(AnomalyConfig())
    assert cold.observe("loss", 80.0, 5000) is None
    det.seed("loss", [1.0] * 500)  # capped to the window, oldest dropped
    assert len(det._hist["loss"]) == det.cfg.window
