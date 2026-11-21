"""Autolog engine: step counter, loss capture, hooks lifecycle, context manager, decorator, ranks, overhead."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

torch = pytest.importorskip("torch")

import pyrometer  # noqa: E402
from torch.optim import optimizer as _optmod  # noqa: E402

from pyrometer import _autolog  # noqa: E402
from pyrometer._run import Run  # noqa: E402
from pyrometer._transport import Spool  # noqa: E402

ORIG_TENSOR_BACKWARD = torch.Tensor.backward
ORIG_AUTOGRAD_BACKWARD = torch.autograd.backward


@pytest.fixture(autouse=True)
def no_hardware_probe(monkeypatch):
    """The background torch/NVML probe is irrelevant here and makes every init/finish seconds slower."""
    monkeypatch.setattr(Run, "_start_hardware_probe", lambda self, known: None)


@pytest.fixture
def calls(monkeypatch):
    """Every metric dict that reaches ``Run._log`` as ``(step, metrics)``."""
    got: list[tuple[int, dict]] = []
    orig = Run._log

    def rec(self, metrics, step, ts):
        got.append((step, dict(metrics)))
        return orig(self, metrics, step, ts)

    monkeypatch.setattr(Run, "_log", rec)
    return got


@pytest.fixture
def statuses(monkeypatch):
    got: list[str] = []
    orig = Run._finish

    def rec(self, status):
        if not self._finished:
            got.append(status)
        return orig(self, status)

    monkeypatch.setattr(Run, "_finish", rec)
    return got


def start(**kw):
    kw.setdefault("offline", True)
    return pyrometer.init(**kw)


def values(calls, name):
    return {s: m[name] for s, m in calls if name in m}


def tiny():
    model = torch.nn.Linear(4, 1)
    return model, torch.optim.SGD(model.parameters(), lr=0.1)


def train(model, opt, steps, run=None, accum=1):
    for _ in range(steps):
        for _ in range(accum):
            model(torch.randn(8, 4)).pow(2).mean().backward()
        opt.step()
        opt.zero_grad()


def hooks_clean():
    return (
        torch.Tensor.backward is ORIG_TENSOR_BACKWARD
        and torch.autograd.backward is ORIG_AUTOGRAD_BACKWARD
        and not _optmod._global_optimizer_post_hooks
        and not torch.nn.modules.module._global_forward_hooks
    )


# --- metrics -------------------------------------------------------------------------------


def test_step_lr_loss_throughput(calls):
    model, opt = tiny()
    with start(autolog={"log_every": 5}) as run:
        train(model, opt, 12)
        assert run.step == 12
    assert sorted(values(calls, "auto/lr/0")) == [1, 5, 10, 12]
    assert set(values(calls, "auto/loss")) == {1, 5, 10}
    assert set(values(calls, "auto/steps_per_sec")) == {5, 10}  # no rate for step 1, none for the final flush
    assert all(v == pytest.approx(0.1) for v in values(calls, "auto/lr/0").values())


def test_loss_value_is_exact_and_lazy_for_cuda_like_path(calls):
    w = torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.SGD([w], lr=0.0)
    with start(autolog={"log_every": 1}):
        (w * 3).sum().backward()
        opt.step()
    assert values(calls, "auto/loss")[1] == pytest.approx(3.0)


def test_grad_accumulation_logs_mean_per_optimizer_step(calls):
    w = torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.SGD([w], lr=0.0)
    with start(autolog={"log_every": 1}) as run:
        for _ in range(3):
            for c in (1.0, 2.0, 3.0, 6.0):
                (w * c).sum().backward()
            opt.step()
            opt.zero_grad()
        assert run.step == 3  # steps are optimizer steps, not backwards
    assert values(calls, "auto/loss") == {1: pytest.approx(3.0), 2: pytest.approx(3.0), 3: pytest.approx(3.0)}


def test_two_loss_streams_are_indexed_and_nameable(calls):
    a, b = torch.nn.Parameter(torch.ones(1)), torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.SGD([a, b], lr=0.0)
    with start(autolog={"log_every": 1}) as run:
        for _ in range(2):
            (a * 1.0).sum().backward()
            (b * 5.0).sum().backward()
            opt.step()
            opt.zero_grad()
    assert values(calls, "auto/loss/0")[1] == pytest.approx(1.0)
    assert values(calls, "auto/loss/1")[2] == pytest.approx(5.0)
    assert "auto/loss" not in {k for _, m in calls for k in m}

    calls.clear()
    with start(autolog={"log_every": 1}) as run:
        run.name_loss("gen", (a * 2.0).sum()).backward()
        run.name_loss("disc", (b * 4.0).sum()).backward()
        opt.step()
    assert values(calls, "auto/loss/gen")[1] == pytest.approx(2.0)
    assert values(calls, "auto/loss/disc")[1] == pytest.approx(4.0)


def test_autograd_backward_is_captured_once(calls):
    w = torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.SGD([w], lr=0.0)
    with start(autolog={"log_every": 1}):
        torch.autograd.backward((w * 7).sum())
        (w * 3).sum().backward()  # Tensor.backward calls autograd.backward inside: must not count twice
        opt.step()
    assert values(calls, "auto/loss/0")[1] == pytest.approx(7.0)
    assert values(calls, "auto/loss/1")[1] == pytest.approx(3.0)  # one entry, not two


def test_non_scalar_backward_is_ignored(calls):
    w = torch.nn.Parameter(torch.ones(3))
    opt = torch.optim.SGD([w], lr=0.0)
    with start(autolog={"log_every": 1}):
        (w * 2).backward(torch.ones(3))
        opt.step()
    assert not values(calls, "auto/loss")


def test_two_optimizers_first_drives_or_configured(calls):
    a, b = torch.nn.Parameter(torch.ones(1)), torch.nn.Parameter(torch.ones(1))
    oa, ob = torch.optim.SGD([a], lr=0.1), torch.optim.Adam([b], lr=0.01)
    with start(autolog={"log_every": 1}) as run:
        for _ in range(3):
            (a + b).sum().backward()
            ob.step()  # first to step: drives
            oa.step()
        assert run.step == 3
    assert values(calls, "auto/lr/0")[3] == pytest.approx(0.01)
    assert values(calls, "auto/lr/opt1/0")[3] == pytest.approx(0.1)

    calls.clear()
    with start(autolog={"log_every": 1}) as run:
        run.config.step_optimizer = oa
        for _ in range(4):
            (a + b).sum().backward()
            ob.step()
            oa.step()
        assert run.step == 4
    assert values(calls, "auto/lr/0")[4] == pytest.approx(0.1)


def test_explicit_step_wins_and_default_step(calls):
    model, opt = tiny()
    with start(autolog={"log_every": 1000}) as run:
        run.log({"a": 1.0})  # no optimizer yet: counts step-less log calls
        run.log({"a": 2.0})
        train(model, opt, 3)
        run.log({"b": 1.0})  # automatic step: optimizer steps completed
        run.log({"b": 2.0}, step=99)  # explicit wins
        run.set_step(10)
        train(model, opt, 1)
        run.log({"c": 1.0})
    assert values(calls, "a") == {0: 1.0, 1: 2.0}
    assert sorted(values(calls, "b").items()) == [(3, 1.0), (99, 2.0)]
    assert values(calls, "c") == {11: 1.0}


def test_step_required_when_autolog_off():
    with start(autolog=False) as run:
        with pytest.raises(ValueError, match="step is required"):
            run.log({"x": 1.0})
        run.log({"x": 1.0}, step=1)


def test_log_samples_and_profile_optional_step():
    with start() as run:
        run.log_samples([{"prompt": "p", "completion": "c"}])  # no step
        run.log_samples(5, [{"prompt": "p"}])  # old form still works
        with run.profile():
            pass


# --- lifecycle -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected",
    [
        (None, "completed"),
        (KeyboardInterrupt(), "killed"),
        (SystemExit(0), "completed"),
        (SystemExit(), "completed"),
        (SystemExit(1), "failed"),
        (SystemExit("boom"), "failed"),
        (ValueError("x"), "failed"),
    ],
)
def test_exit_status(statuses, exc, expected):
    if exc is None:
        with start():
            pass
    else:
        with pytest.raises(type(exc)):
            with start():
                raise exc
    assert statuses == [expected]
    assert hooks_clean()


def test_hooks_removed_after_exit_even_on_error():
    assert hooks_clean()
    with pytest.raises(RuntimeError):
        with start():
            assert not hooks_clean()
            raise RuntimeError("x")
    assert hooks_clean()


def test_plain_init_finish_still_works(calls):
    model, opt = tiny()
    run = start(autolog={"log_every": 2})
    train(model, opt, 4)
    run.finish()
    assert hooks_clean()
    assert set(values(calls, "auto/lr/0")) >= {1, 2, 4}


def test_nested_runs_and_restore():
    model, opt = tiny()
    with start() as outer:
        with start() as inner:
            train(model, opt, 2)
            assert inner.step == outer.step == 2
        assert not hooks_clean()  # the outer run is still wired up
        train(model, opt, 1)
        assert outer.step == 3 and inner.step == 2
    assert hooks_clean()


def test_restore_when_third_party_wrapped_on_top():
    seen = []
    with start():
        inner = torch.Tensor.backward

        def outer(self, *a, **k):
            seen.append(1)
            return inner(self, *a, **k)

        torch.Tensor.backward = outer
    try:
        w = torch.nn.Parameter(torch.ones(1))
        (w * 2).sum().backward()  # our wrapper is inert, theirs still works
        assert seen == [1]
    finally:
        torch.Tensor.backward = ORIG_TENSOR_BACKWARD
        torch.autograd.backward = ORIG_AUTOGRAD_BACKWARD


def test_autolog_false_installs_nothing(calls):
    model, opt = tiny()
    with start(autolog=False) as run:
        assert hooks_clean()
        train(model, opt, 3)
        assert run.step == 0
    assert not any(k.startswith("auto/") for _, m in calls for k in m)


def test_env_disables_autolog(monkeypatch):
    monkeypatch.setenv("PYROMETER_AUTOLOG", "0")
    with start() as run:
        assert hooks_clean()
    monkeypatch.setenv("PYROMETER_AUTOLOG", "1")
    with start():
        assert not hooks_clean()


def test_threads_record_losses_safely(calls):
    import threading

    w = torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.SGD([w], lr=0.0)

    def work():
        for _ in range(50):
            (w * 1.0).sum().backward()

    with start(autolog={"log_every": 1}):
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        opt.step()
    assert values(calls, "auto/loss")[1] == pytest.approx(1.0)


def test_config_validation_and_live_change(calls):
    with start() as run:
        with pytest.raises(AttributeError):
            run.config.nope = 1
        with pytest.raises(ValueError):
            run.config.log_every = 0
        run.config.log_every = 2
        assert run.config.log_every == 2


# --- track / dataloader / gradient options -------------------------------------------------


def test_track_tokens_samples_and_data_wait(calls):
    model, opt = tiny()
    data = [(torch.randn(8, 4),) for _ in range(6)]

    def slow(items):
        for x in items:
            time.sleep(0.01)
            yield x

    class Loader:
        def __iter__(self):
            return slow(data)

        def __len__(self):
            return len(data)

    with start(autolog={"log_every": 3}) as run:
        loader = run.track(dataloader=Loader(), tokens_per_batch=lambda b: 8 * 16)
        assert len(loader) == 6
        for (x,) in loader:
            model(x).pow(2).mean().backward()
            opt.step()
            opt.zero_grad()
    assert values(calls, "tokens_seen")[6] == 6 * 128
    assert values(calls, "samples_seen")[6] == 6 * 8  # inferred from the batch
    assert values(calls, "auto/data_wait_ms")[6] >= 5.0
    assert 0 < values(calls, "auto/data_wait_frac")[6] <= 1.0
    assert values(calls, "auto/tokens_per_sec")[6] > 0


def test_track_hints_with_grad_accumulation(calls):
    model, opt = tiny()
    with start(autolog={"log_every": 1}) as run:
        run.track(tokens_per_batch=100, samples_per_batch=4)
        train(model, opt, 2, accum=3)
    assert values(calls, "tokens_seen") == {1: 300.0, 2: 600.0}
    assert values(calls, "samples_seen")[2] == 24.0


def test_grad_norm_and_nan_detection(calls):
    w = torch.nn.Parameter(torch.ones(3))
    opt = torch.optim.SGD([w], lr=0.0)
    events = []
    with start(autolog={"log_every": 1, "grad_norm": True}) as run:
        run._transport.submit = lambda method, path, body=None, **kw: events.append((path, body))
        (w * 2).sum().backward()
        opt.step()
        opt.zero_grad()
        (w * float("nan")).sum().backward()
        opt.step()
    assert values(calls, "auto/grad_norm")[1] == pytest.approx(2 * 3 ** 0.5, rel=1e-4)
    assert any(b and b.get("event_type") == "anomaly_detected" for p, b in events if p.endswith("/events"))


def test_grad_health_global_discovery_and_cleanup():
    model = torch.nn.Sequential(torch.nn.Linear(4, 4), torch.nn.Linear(4, 1))
    opt = torch.optim.SGD(model.parameters(), lr=0.0)
    with start(autolog={"log_every": 1000}) as run:
        assert not torch.nn.modules.module._global_forward_hooks
        run.config.grad_log_freq = 1
        run.config.grad_health = True  # live switch-on
        for _ in range(2):
            model(torch.randn(2, 4)).sum().backward()
            opt.step()
        stats = [s["param_name"] for s in run._param_stats_buffer]
        assert len(stats) >= 4 and any("Linear" in n for n in stats)
        run.config.grad_health = False
        assert not torch.nn.modules.module._global_forward_hooks and not run._watch_hooks
    assert hooks_clean()


def test_grad_health_uses_tracked_model():
    model = torch.nn.Linear(4, 1)
    opt = torch.optim.SGD(model.parameters(), lr=0.0)
    with start(autolog={"log_every": 1000, "grad_log_freq": 1}) as run:
        run.track(model=model)
        run.config.grad_health = True
        model(torch.randn(2, 4)).sum().backward()
        assert {s["param_name"] for s in run._param_stats_buffer} == {"weight", "bias"}


def test_profile_every_ties_to_step_counter(monkeypatch):
    shipped = []
    monkeypatch.setattr(Run, "_ship_profile_async", lambda self, prof, step, ts: shipped.append(step))
    model, opt = tiny()
    with start(autolog={"log_every": 1000, "profile_every": 4}) as run:
        train(model, opt, 10)
    assert shipped == [5, 9]  # whole iterations after steps 4 and 8 (the one after 8 completes at 9)


@pytest.mark.skipif(not hasattr(torch, "compile"), reason="no torch.compile")
def test_torch_compile(calls):
    model = torch.nn.Linear(4, 1)
    try:
        compiled = torch.compile(model, backend="eager")
        compiled(torch.randn(2, 4))
    except Exception as e:  # pragma: no cover - platform dependent
        pytest.skip(f"torch.compile unsupported here: {e}")
    opt = torch.optim.SGD(model.parameters(), lr=0.1)
    with start(autolog={"log_every": 2}) as run:
        for _ in range(4):
            compiled(torch.randn(8, 4)).pow(2).mean().backward()
            opt.step()
            opt.zero_grad()
        assert run.step == 4
    assert set(values(calls, "auto/loss")) == {1, 2, 4}
    assert hooks_clean()


# --- decorator -----------------------------------------------------------------------------


def hyperparams_of(run_id):
    return Spool(run_id).read_meta()["create"]["hyperparams"]


def test_decorator_hyperparams_and_status(statuses):
    seen = {}

    @pyrometer.run(offline=True, tags=["t"])
    def train_fn(lr, model, data, epochs=3, path="/tmp/x", nested=None, run=None, **extra):
        seen["run"] = run
        seen["current"] = pyrometer.current_run()
        return 42

    big = "x" * 1000
    out = train_fn(1e-3, torch.nn.Linear(2, 2), torch.zeros(4), nested={"a": [1, 2, {"b": big}]}, flag=True)
    assert out == 42
    hp = hyperparams_of(seen["run"].run_id)
    assert hp["lr"] == 1e-3 and hp["epochs"] == 3 and hp["path"] == "/tmp/x" and hp["flag"] is True
    assert hp["model"] == "<Linear>" and hp["data"] == "<Tensor>"
    assert "run" not in hp
    assert hp["nested"]["a"][2]["b"].endswith("...") and len(hp["nested"]["a"][2]["b"]) < 300
    assert seen["current"] is seen["run"]
    assert Spool(seen["run"].run_id).read_meta()["create"]["name"] == "train_fn"
    assert statuses == ["completed"] and pyrometer.current_run() is None

    @pyrometer.run
    def broken():
        raise ValueError("bad")

    os.environ["PYROMETER_MODE"] = "offline"
    try:
        with pytest.raises(ValueError):
            broken()
    finally:
        del os.environ["PYROMETER_MODE"]
    assert statuses[-1] == "failed"


# --- ranks ---------------------------------------------------------------------------------


def test_non_zero_rank_gets_inert_run(monkeypatch):
    monkeypatch.setenv("RANK", "1")
    monkeypatch.setenv("WORLD_SIZE", "2")
    with pyrometer.init(offline=True) as run:
        assert isinstance(run, _autolog.NullRun) and run.run_id is None
        run.log({"x": 1.0}, step=1)
        run.log({"x": 1.0})
        run.watch(object())
        with run.profile():
            pass
        sentinel = object()
        assert run.track(dataloader=sentinel) is sentinel
        t = torch.ones(1)
        assert run.name_loss("n", t) is t
    assert hooks_clean()
    with pyrometer.init(offline=True, rank_zero_only=False) as run:
        assert run.run_id


def test_local_rank_used_without_rank(monkeypatch):
    monkeypatch.delenv("RANK", raising=False)
    monkeypatch.setenv("WORLD_SIZE", "4")
    monkeypatch.setenv("LOCAL_RANK", "2")
    assert _autolog.rank_info() == (2, 4)
    monkeypatch.setenv("WORLD_SIZE", "1")
    assert _autolog.rank_info() == (None, 1)


DDP_SCRIPT = textwrap.dedent(
    """
    import os, sys, torch, torch.distributed as dist
    import pyrometer
    dist.init_process_group("gloo")
    model = torch.nn.parallel.DistributedDataParallel(torch.nn.Linear(4, 1))
    opt = torch.optim.SGD(model.parameters(), lr=0.1)
    with pyrometer.init(offline=True, autolog={"log_every": 1}) as run:
        for _ in range(3):
            model(torch.randn(8, 4)).pow(2).mean().backward()
            opt.step(); opt.zero_grad()
        print("RANK", dist.get_rank(), "RUN", run.run_id, "STEP", run.step, flush=True)
    dist.destroy_process_group()
    """
)


@pytest.mark.slow
def test_ddp_only_rank_zero_logs(tmp_path):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = tmp_path / "ddp.py"
    script.write_text(DDP_SCRIPT)
    spool = tmp_path / "spool"
    procs = []
    for rank in (0, 1):
        env = dict(
            os.environ, RANK=str(rank), WORLD_SIZE="2", MASTER_ADDR="127.0.0.1", MASTER_PORT=str(port),
            PYROMETER_SPOOL_DIR=str(spool), GLOO_SOCKET_IFNAME="lo",
        )
        procs.append(subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True))
    outs = [p.communicate(timeout=180) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    line0 = [ln for ln in outs[0][0].splitlines() if ln.startswith("RANK")][0].split()
    line1 = [ln for ln in outs[1][0].splitlines() if ln.startswith("RANK")][0].split()
    assert line0[1] == "0" and line0[3] != "None" and line0[5] == "3"
    assert line1[1] == "1" and line1[3] == "None" and line1[5] == "0"
    assert [d.name for d in spool.iterdir()] == [line0[3]]


# --- overhead ------------------------------------------------------------------------------


def _loop_time(steps, **kw):
    torch.manual_seed(0)
    model = torch.nn.Sequential(torch.nn.Linear(64, 128), torch.nn.ReLU(), torch.nn.Linear(128, 1))
    opt = torch.optim.SGD(model.parameters(), lr=0.01)
    x = torch.randn(32, 64)
    run = start(**kw)
    try:
        t0 = time.perf_counter()
        for _ in range(steps):
            model(x).pow(2).mean().backward()
            opt.step()
            opt.zero_grad()
        return (time.perf_counter() - t0) / steps
    finally:
        run.finish()


def test_overhead_is_small(capsys):
    steps = 1500
    base = min(_loop_time(steps, autolog=False) for _ in range(3))
    auto = min(_loop_time(steps) for _ in range(3))
    extra_us = (auto - base) * 1e6
    with capsys.disabled():
        print(f"\nautolog overhead: base {base * 1e6:.0f} us/step, autolog {auto * 1e6:.0f} us/step, "
              f"+{extra_us:.1f} us/step ({(auto / base - 1) * 100:.1f}%)")
    assert extra_us < 60 or auto < base * 1.25


# --- resume: seeding the gradient detector --------------------------------------------------


def test_recent_param_stats_endpoint_and_grad_detector_seed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pyrometer.server import auth
    from pyrometer.server.app import app
    from pyrometer._anomaly import AnomalyConfig, GradDetector

    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    auth._cache = None
    auth._setup_token = None
    client = TestClient(app, headers={"Authorization": f"Bearer {auth.create_key('sdk')[1]}"})
    rid = "abcdef1234567890"
    client.post("/runs", json={"run_id": rid})
    stat = {"timestamp": 1.0, "grad_min": 0, "grad_max": 0, "grad_mean": 0, "weight_norm": 1.0,
            "weight_min": 0, "weight_max": 0, "weight_mean": 0, "grad_histogram": [], "weight_histogram": []}
    rows = [dict(stat, step=s, param_name=p, grad_norm=float(s)) for s in range(1, 31) for p in ("w", "b")]
    assert client.post(f"/runs/{rid}/param-stats", json=rows).status_code == 201
    got = client.get(f"/runs/{rid}/param-stats/recent", params={"limit": 5}).json()
    assert got["error"] is None and got["data"]["w"] == [26.0, 27.0, 28.0, 29.0, 30.0]
    assert client.get("/runs/nope-nope-nope/param-stats/recent").status_code in (200, 404)

    cfg = AnomalyConfig()
    cold, warm = GradDetector(cfg), GradDetector(cfg)
    warm.seed("w", [1.0] * 40)
    assert cold.observe("w", 5000.0, 100, False) is None  # no history yet: blind
    assert warm.observe("w", 5000.0, 100, False) is not None  # seeded: a 5000x spike is caught at once


def test_lazy_device_values_are_read_only_when_their_copy_finished():
    """CUDA scalars are staged to pinned memory with an event; an unfinished event leaves the entry queued
    (no blocking read), ``force`` (finish) drains everything."""
    import threading
    from types import SimpleNamespace

    from pyrometer._autolog import _AutologEngine

    logged: list = []
    eng = _AutologEngine.__new__(_AutologEngine)
    eng._lock = threading.RLock()
    eng.run = SimpleNamespace(_log=lambda vals, k, ts: logged.append((k, vals)))
    state = {"done": False}
    ev = SimpleNamespace(query=lambda: state["done"])
    eng._lazy = [(1, 0.0, {"auto/loss": torch.tensor(2.5)}, [ev])]
    eng._resolve_lazy()
    assert logged == [] and len(eng._lazy) == 1
    state["done"] = True
    eng._resolve_lazy()
    assert logged == [(1, {"auto/loss": 2.5})] and eng._lazy == []
    eng._lazy = [(2, 0.0, {"auto/loss": torch.tensor(1.0)}, [SimpleNamespace(query=lambda: False)])]
    eng._resolve_lazy(force=True)
    assert logged[-1] == (2, {"auto/loss": 1.0})


def test_stage_falls_back_to_the_tensor_without_cuda():
    from pyrometer._autolog import _AutologEngine

    staged, events = _AutologEngine._stage({"x": torch.tensor(3.0)})
    assert float(staged["x"]) == 3.0 and events == []


def test_cuda_rates_come_from_completed_work_events(calls, monkeypatch):
    """With CUDA the CPU runs ahead, so steps/sec is measured between completion events (deferred until both
    have completed) rather than CPU wall time; the first cadence is only the baseline."""
    state = {"done": False, "t": 0.0}

    class FakeEvent:
        def __init__(self) -> None:
            self.t = state["t"]
            state["t"] += 1000.0  # each cadence window = 1000 ms of GPU time

        def query(self) -> bool:
            return state["done"]

        def synchronize(self) -> None:
            pass

        def elapsed_time(self, other) -> float:
            return other.t - self.t

    monkeypatch.setattr(_autolog._AutologEngine, "_make_event", staticmethod(lambda opt: FakeEvent()))
    run = start(name="gpu-rates", autolog={"log_every": 5})
    model = torch.nn.Linear(2, 1)
    opt = torch.optim.SGD(model.parameters(), lr=0.1)
    for _ in range(10):
        opt.zero_grad()
        model(torch.randn(3, 2)).sum().backward()
        opt.step()
    rate_logs = [m for _, m in calls if "auto/steps_per_sec" in m]
    assert rate_logs == []  # nothing completed yet, wall-clock rate not used
    state["done"] = True
    for _ in range(5):
        opt.zero_grad()
        model(torch.randn(3, 2)).sum().backward()
        opt.step()
    rates = [(k, m["auto/steps_per_sec"]) for k, m in calls if "auto/steps_per_sec" in m]
    # k=1 was the baseline, so the first window is 4 steps; then 5 steps per 1000 ms of GPU time
    assert rates[0] == (5, pytest.approx(4.0)) and all(v == pytest.approx(5.0) for _, v in rates[1:])
    assert len(rates) == 3
    run.finish()


def test_off_cadence_backwards_are_counted_but_not_retained(calls):
    """Hot-path trim: a backward that cannot reach a cadence step only bumps the micro-batch count; the loss of
    the backward right before a cadence step is kept and logged as usual."""
    run = start(name="hot-path", autolog={"log_every": 4})
    model = torch.nn.Linear(2, 1)
    opt = torch.optim.SGD(model.parameters(), lr=0.1)
    eng = run._autolog
    kept = []
    for _ in range(7):
        model(torch.randn(3, 2)).sum().backward()
        s = next(iter(eng._streams.values()))
        kept.append(len(s.pending))
        opt.step()
        opt.zero_grad()
    # step 1 is a cadence step; steps 2-3 are not (their backwards keep nothing), step 4 is the next cadence
    assert kept[0] == 1 and kept[1] == 0 and kept[2] == 0 and kept[3] == 1
    run.finish()
    steps = sorted(k for k, m in calls if "auto/loss" in m)
    assert steps[:2] == [1, 4]
