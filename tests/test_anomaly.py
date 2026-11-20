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
    for i in range(10):
        det.observe("loss", 1.0 + 0.01 * i, i)
    assert det.observe("loss", 500.0, 10) is None


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
