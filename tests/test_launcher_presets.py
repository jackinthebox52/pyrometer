"""Phase 3 W7: ``pyrometer run`` launcher, presets, log_episode, loss fallback, autograd.grad capture, compiled step."""

import json
import os
import subprocess
import sys
import textwrap

import pytest

torch = pytest.importorskip("torch")

import pyrometer  # noqa: E402
from pyrometer import _autolog, _gpu, _launcher  # noqa: E402
from pyrometer._presets import PRESETS, get_preset  # noqa: E402
from pyrometer._run import Run  # noqa: E402
from pyrometer._transport import QUEUE_FILE  # noqa: E402

ORIG_GRAD = torch.autograd.grad
ORIG_BACKWARD = torch.Tensor.backward


@pytest.fixture(autouse=True)
def no_hardware_probe(monkeypatch):
    monkeypatch.setattr(Run, "_start_hardware_probe", lambda self, known: None)
    monkeypatch.delenv("PYROMETER_PRESET", raising=False)
    monkeypatch.delenv("PYROMETER_RUN_ID", raising=False)


@pytest.fixture
def calls(monkeypatch):
    got: list[tuple[int, dict]] = []
    orig = Run._log

    def rec(self, metrics, step, ts):
        got.append((step, dict(metrics)))
        return orig(self, metrics, step, ts)

    monkeypatch.setattr(Run, "_log", rec)
    return got


def values(calls, name):
    return {s: m[name] for s, m in calls if name in m}


def start(**kw):
    kw.setdefault("offline", True)
    return pyrometer.init(**kw)


# --- launcher: parsing ---------------------------------------------------------------------------


def test_split_args():
    opts, kind, target, rest = _launcher.split_args(
        ["--name", "n", "--tag=a", "--tag", "b", "--preset", "rl", "--no-autolog", "train.py", "--lr", "1e-3", "-x"]
    )
    assert (kind, target, rest) == ("script", "train.py", ["--lr", "1e-3", "-x"])
    assert opts["name"] == "n" and opts["tags"] == ["a", "b"] and opts["preset"] == "rl" and opts["autolog"] is False
    opts, kind, target, rest = _launcher.split_args(["--project", "p", "-m", "pkg.mod", "--flag"])
    assert (kind, target, rest) == ("module", "pkg.mod", ["--flag"]) and opts["project"] == "p"
    with pytest.raises(ValueError):
        _launcher.split_args(["--name"])
    with pytest.raises(ValueError):
        _launcher.split_args(["--bogus", "x.py"])
    with pytest.raises(ValueError):
        _launcher.split_args(["--name", "n"])


def test_parse_argv():
    hp = _launcher.parse_argv(
        ["--lr", "1e-3", "--epochs=5", "--bf16", "--no-cache", "--name", "run1", "--seed", "-3", "--tag", "a",
         "--tag", "b", "-b", "32", "--wd", "0.0", "--on", "true", "--off", "none", "--id", "007", "data.txt"]
    )
    assert hp["lr"] == 1e-3 and hp["epochs"] == 5 and hp["bf16"] is True and hp["no_cache"] is True
    assert hp["name"] == "run1" and hp["seed"] == -3 and hp["tag"] == ["a", "b"] and hp["b"] == 32
    assert hp["wd"] == 0.0 and hp["on"] is True and hp["off"] is None and hp["id"] == "007"
    assert hp["positional"] == ["data.txt"] and hp["argv"][0] == "--lr"
    # a flag directly followed by a bare word takes it as its value (inherent ambiguity: use --flag=true)
    assert _launcher.parse_argv(["--bf16", "extra"])["bf16"] == "extra"
    assert _launcher.parse_argv([]) == {"argv": []}
    assert _launcher.parse_argv(["--x", "inf"])["x"] == "inf"  # non-finite stays a string (not JSON-able)


# --- launcher: real processes --------------------------------------------------------------------


def launch(tmp_path, body, *args, opts=(), env=None, module=None, timeout=180):
    """Run ``body`` as a script under ``pyrometer run`` with an offline spool; returns (process, spool dir)."""
    spool = tmp_path / "pspool"
    target = []
    if module is None:
        path = tmp_path / "train_script.py"
        path.write_text(textwrap.dedent(body))
        target = [str(path)]
    else:
        target = ["-m", module]
    full_env = {**os.environ, "PYROMETER_SPOOL_DIR": str(spool), "PYROMETER_MODE": "offline", **(env or {})}
    full_env.pop("PYROMETER_RUN_ID", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pyrometer", "run", *opts, *target, *args],
        env=full_env, capture_output=True, text=True, timeout=timeout, cwd=str(tmp_path),
    )
    return proc, spool


def run_dirs(spool):
    return [p for p in spool.iterdir() if p.is_dir()] if spool.exists() else []


def create_payload(spool_dir):
    return json.loads((spool_dir / "meta.json").read_text())["create"]


def queue_items(spool_dir):
    return [json.loads(line) for line in (spool_dir / QUEUE_FILE).read_text().splitlines() if line.strip()]


def metric_rows(spool_dir):
    return [r for it in queue_items(spool_dir) if it["path"].endswith("/metrics") for r in it["json"]]


def statuses(spool_dir):
    return [it["json"]["status"] for it in queue_items(spool_dir)
            if it["method"] == "PATCH" and "status" in (it["json"] or {})]


TRAIN = """
    import sys, torch
    model = torch.nn.Linear(4, 1)
    opt = torch.optim.SGD(model.parameters(), lr=0.1)
    for _ in range(5):
        model(torch.randn(8, 4)).pow(2).mean().backward()
        opt.step(); opt.zero_grad()
"""


@pytest.mark.slow
def test_launcher_runs_script_with_autolog_and_argv_hyperparams(tmp_path):
    proc, spool = launch(tmp_path, TRAIN, "extra", "--lr", "3e-4", "--epochs=2", "--bf16",
                         opts=["--tag", "t1", "--tag", "t2", "--group", "g1"])
    assert proc.returncode == 0, proc.stderr
    (d,) = run_dirs(spool)
    create = create_payload(d)
    assert create["name"] == "train_script" and create["tags"] == ["t1", "t2"] and create["group"] == "g1"
    hp = create["hyperparams"]
    assert hp["lr"] == 3e-4 and hp["epochs"] == 2 and hp["bf16"] is True and hp["positional"] == ["extra"]
    assert hp["argv"] == ["extra", "--lr", "3e-4", "--epochs=2", "--bf16"]
    assert hp["script"].endswith("train_script.py")
    rows = metric_rows(d)
    assert {r["name"] for r in rows} >= {"auto/loss", "auto/lr/0"}
    assert {r["step"] for r in rows if r["name"] == "auto/lr/0"} == {1, 5}  # log_every=10: step 1 + final flush
    assert statuses(d)[-1] == "completed"


@pytest.mark.slow
@pytest.mark.parametrize("code,expected_rc,expected_status", [
    ("sys.exit(3)", 3, "failed"),
    ("sys.exit(0)", 0, "completed"),
    ("raise SystemExit(2)", 2, "failed"),
    ("raise ValueError('boom')", 1, "failed"),
    ("raise KeyboardInterrupt", None, "killed"),
])
def test_launcher_exit_semantics(tmp_path, code, expected_rc, expected_status):
    proc, spool = launch(tmp_path, f"import sys\n{code}\n", opts=["--no-autolog"])
    (d,) = run_dirs(spool)
    assert statuses(d)[-1] == expected_status, proc.stderr
    if expected_rc is None:
        assert proc.returncode != 0
    else:
        assert proc.returncode == expected_rc, proc.stderr
    if code.startswith("raise ValueError"):
        assert "ValueError: boom" in proc.stderr  # the traceback is not swallowed
        events = [it["json"] for it in queue_items(d) if it["path"].endswith("/events")]
        assert any(e["event_type"] == "exception" for e in events)


@pytest.mark.slow
def test_script_calling_init_attaches_to_launcher_run(tmp_path):
    body = """
        import pyrometer
        with pyrometer.init(name="from-script", hyperparams={"lr": 0.5, "extra": 1}, tags=["s"], notes="n") as run:
            print("RUNID", run.run_id)
            run.log({"x": 1.0}, step=1)
        with pyrometer.init(name="second") as run2:   # only the first init attaches
            print("RUN2", run2.run_id)
    """
    proc, spool = launch(tmp_path, body, "--lr", "0.1", "--seed", "4", opts=["--tag", "cli", "--no-autolog"])
    assert proc.returncode == 0, proc.stderr
    dirs = run_dirs(spool)
    assert len(dirs) == 2
    rid = [ln.split()[1] for ln in proc.stdout.splitlines() if ln.startswith("RUNID")][0]
    rid2 = [ln.split()[1] for ln in proc.stdout.splitlines() if ln.startswith("RUN2")][0]
    assert {d.name for d in dirs} == {rid, rid2} and rid != rid2
    d = spool / rid
    patches = [it["json"] for it in queue_items(d) if it["method"] == "PATCH"]
    adopted = next(p for p in patches if "hyperparams" in p)
    assert adopted["name"] == "from-script"  # no --name given: the script's name applies
    assert adopted["hyperparams"]["lr"] == 0.5 and adopted["hyperparams"]["seed"] == 4  # script wins, argv kept
    assert adopted["hyperparams"]["extra"] == 1 and adopted["hyperparams"]["argv"] == ["--lr", "0.1", "--seed", "4"]
    assert adopted["tags"] == ["cli", "s"] and adopted["notes"] == "n"
    assert statuses(d) == ["completed"]


@pytest.mark.slow
def test_cli_name_wins_over_script_init_and_conflicts_warn(tmp_path):
    body = """
        import pyrometer
        run = pyrometer.init(name="script-name", project="other", resume="abcdefgh12345678")
        run.finish()
    """
    proc, spool = launch(tmp_path, body, opts=["--name", "cli-name", "--project", "mine", "--no-autolog"])
    assert proc.returncode == 0, proc.stderr
    (d,) = run_dirs(spool)
    assert not any(it["json"] and it["json"].get("name") == "script-name" for it in queue_items(d) if it["method"] == "PATCH")
    assert create_payload(d)["name"] == "cli-name"
    assert "ignoring project, resume" in proc.stderr


@pytest.mark.slow
def test_launcher_module_and_no_autolog(tmp_path):
    (tmp_path / "mymod.py").write_text(textwrap.dedent(TRAIN) + "\nprint('MOD', __name__, sys.argv[1:])\n")
    proc, spool = launch(tmp_path, "", "--k", "v", module="mymod", opts=["--no-autolog"])
    assert proc.returncode == 0, proc.stderr
    assert "MOD __main__ ['--k', 'v']" in proc.stdout
    (d,) = run_dirs(spool)
    assert create_payload(d)["name"] == "mymod"
    assert not [r for r in metric_rows(d) if r["name"].startswith("auto/")]


@pytest.mark.slow
def test_launcher_preset_and_non_zero_rank(tmp_path):
    body = """
        import pyrometer
        run = pyrometer.current_run()
        print("PRESET", run.preset if run else None)
    """
    proc, spool = launch(tmp_path, body, opts=["--preset", "benchmark", "--no-autolog"])
    assert proc.returncode == 0 and "PRESET benchmark" in proc.stdout, proc.stderr
    # another rank: the script still runs, nothing is recorded
    (tmp_path / "r1").mkdir()
    proc, spool = launch(tmp_path / "r1", body, env={"RANK": "1", "WORLD_SIZE": "2"}, opts=["--preset", "rl"])
    assert proc.returncode == 0 and "PRESET None" in proc.stdout, proc.stderr
    assert not run_dirs(spool)


def test_launcher_usage_errors(tmp_path):
    for argv in (["--preset", "nope", "x.py"], ["missing_script.py"], []):
        proc = subprocess.run([sys.executable, "-m", "pyrometer", "run", *argv], capture_output=True, text=True,
                              cwd=str(tmp_path), timeout=60,
                              env={**os.environ, "PYROMETER_SPOOL_DIR": str(tmp_path / "s")})
        assert proc.returncode != 0 and "pyrometer run" in proc.stderr


# --- presets ---------------------------------------------------------------------------------------


def test_every_preset_is_valid_and_documented():
    assert set(PRESETS) == {"llm-pretrain", "finetune", "rlhf", "rl", "benchmark"}
    for p in PRESETS.values():
        _autolog.AutologConfig(**p.autolog)  # every key valid
        assert p.summary and p.conventions
    with pytest.raises(ValueError, match="valid presets"):
        get_preset("nope")


def test_llm_pretrain_defaults_and_overrides():
    with start(preset="llm-pretrain") as run:
        assert run.preset == "llm-pretrain"
        c = run.config
        assert (c.log_every, c.grad_norm, c.profile_every, c.throughput) == (10, True, 500, True)
    with start(preset="llm-pretrain", autolog={"profile_every": 50, "grad_norm": False}) as run:
        assert run.config.profile_every == 50 and run.config.grad_norm is False and run.config.log_every == 10
        run.config.log_every = 7  # live change wins over both
        assert run.config.log_every == 7
    with start(preset="llm-pretrain", autolog=False) as run:  # autolog off: nothing for the preset to configure
        assert not run._autolog.enabled


def test_preset_from_environment_and_unknown(monkeypatch):
    monkeypatch.setenv("PYROMETER_PRESET", "rl")
    with start() as run:
        assert run.preset == "rl" and run.config.episode_log_every == 10
    with pytest.raises(ValueError):
        start(preset="bogus")
    monkeypatch.delenv("PYROMETER_PRESET")
    with start() as run:
        assert run.preset is None and run.config.grad_norm is False and run._monitor_interval == 2.0


def test_benchmark_preset_monitor_and_no_loss(calls):
    model = torch.nn.Linear(4, 1)
    opt = torch.optim.SGD(model.parameters(), lr=0.1)
    with start(preset="benchmark", autolog={"log_every": 1}) as run:
        assert run._monitor_interval == 0.5 and run._capture_throttle is True
        assert run.config.loss is False and run.config.lr is False
        for _ in range(3):
            model(torch.randn(4, 4)).sum().backward()
            opt.step()
            opt.zero_grad()
    names = {k for _, m in calls for k in m}
    assert "auto/steps_per_sec" in names and "auto/loss" not in names and not any(n.startswith("auto/lr") for n in names)
    with start(preset="benchmark", monitor_interval=1.0) as run:
        assert run._monitor_interval == 1.0
    with pytest.raises(ValueError):
        start(monitor_interval=0.01)


def test_reminders_printed_once_at_finish(capfd):
    with start(preset="finetune") as run:
        pass
    err = capfd.readouterr().err
    assert "preset finetune: no tokens_per_batch hint" in err and "no run.log_samples" in err
    with start(preset="finetune") as run:
        run.track(tokens_per_batch=8)
        model = torch.nn.Linear(2, 1)
        opt = torch.optim.SGD(model.parameters(), lr=0.1)
        model(torch.randn(2, 2)).sum().backward()
        opt.step()
        run.log_samples([{"prompt": "a", "completion": "b"}])
    assert "preset" not in capfd.readouterr().err


def test_throttle_decoding_and_event(monkeypatch):
    assert _gpu.decode_throttle(0x4 | 0x40) == ["sw_power_cap", "hw_thermal_slowdown"]
    assert _gpu.decode_throttle(0x1) == ["gpu_idle"]

    class FakeNvml:
        mask = 0

        def nvmlDeviceGetCurrentClocksEventReasons(self, handle):
            return FakeNvml.mask

    events, logged = [], []
    with start(preset="benchmark") as run:
        monkeypatch.setattr(run, "event", lambda msg, **kw: events.append((msg, kw)))
        monkeypatch.setattr(run, "_log", lambda m, step, ts: logged.append(m))
        state: dict = {}
        nv, handles = FakeNvml(), [(0, object())]
        run._sample_throttle(nv, handles, state)  # idle at start: nothing
        FakeNvml.mask = 0x1
        run._sample_throttle(nv, handles, state)  # gpu_idle is not a slowdown: metric, no event
        FakeNvml.mask = 0x40
        run._sample_throttle(nv, handles, state)
        run._sample_throttle(nv, handles, state)  # unchanged: nothing new
        FakeNvml.mask = 0
        run._sample_throttle(nv, handles, state)
    assert [list(m.values())[0] for m in logged] == [1.0, 64.0, 0.0]
    assert len(events) == 1 and events[0][1]["event_type"] == "thermal_throttle"
    assert "hw_thermal_slowdown" in events[0][0]


def test_attach_applies_script_preset_to_launcher_run(tmp_path):
    run = start(name="launcher", autolog=True)
    run._launcher_explicit, run._launcher_hp, run._launcher_tags, run._launcher_project = set(), {}, [], None
    pyrometer._set_launcher_run(run)
    try:
        same = pyrometer.init(preset="llm-pretrain", autolog={"log_every": 3}, hyperparams={"a": 1})
        assert same is run and run.config.grad_norm is True and run.config.log_every == 3
        assert run.preset == "llm-pretrain"
        other = pyrometer.init(offline=True)  # second init: an independent run
        assert other is not run
        other.finish()
    finally:
        pyrometer._set_launcher_run(None)
        run.finish()


# --- log_episode -----------------------------------------------------------------------------------


def test_log_episode_rolling_stats(calls):
    with start(autolog={"episode_log_every": 4, "episode_window": 6}) as run:
        for i in range(10):
            run.log_episode(float(i), 10 + i, step=i, success=float(i % 2 == 0))
    ep = [(s, m) for s, m in calls if "episode/return_mean" in m]
    assert [s for s, _ in ep] == [3, 7, 9]  # every 4 episodes, plus the flush of the last two at finish
    m3, m7, m9 = (m for _, m in ep)
    assert m3["episode/return_mean"] == 1.5 and m3["episode/return_min"] == 0 and m3["episode/return_max"] == 3
    assert m3["episode/length_mean"] == 11.5 and m3["episode/count"] == 4
    assert m3["env_steps"] == 10 + 11 + 12 + 13
    assert m7["episode/return_mean"] == pytest.approx((2 + 3 + 4 + 5 + 6 + 7) / 6)  # window of 6
    assert m7["episode/success_mean"] == pytest.approx(3 / 6)
    assert m9["env_steps"] == sum(range(10, 20)) and m9["episode/count"] == 10


def test_log_episode_uses_auto_step_and_optional_length(calls):
    model = torch.nn.Linear(2, 1)
    opt = torch.optim.SGD(model.parameters(), lr=0.1)
    with start(autolog={"episode_log_every": 1}) as run:
        for _ in range(2):
            model(torch.randn(2, 2)).sum().backward()
            opt.step()
            opt.zero_grad()
            run.log_episode(5.0)
    assert values(calls, "episode/return_mean") == {1: 5.0, 2: 5.0}
    assert not any("env_steps" in m for _, m in calls)


def test_log_episode_with_rl_preset_and_inert_on_null_run(calls, monkeypatch):
    with start(preset="rl") as run:
        for _ in range(10):
            run.log_episode(1.0, 5)
        assert run.config.episode_log_every == 10
    assert len(values(calls, "env_steps")) == 1
    monkeypatch.setenv("RANK", "1")
    monkeypatch.setenv("WORLD_SIZE", "2")
    with start() as nr:
        nr.log_episode(1.0, 2, success=1.0)  # NullRun accepts it


# --- routed items: loss fallback, autograd.grad, compiled step -------------------------------------


def manual_updates(w, n):
    for _ in range(n):
        loss = (w * 2.0).sum()
        loss.backward()
        with torch.no_grad():
            w.sub_(0.0 * w.grad)
            w.grad.zero_()


def test_loss_fallback_without_optimizer_step(calls):
    w = torch.nn.Parameter(torch.ones(1))
    with start(autolog={"log_every": 100}) as run:
        manual_updates(w, 1100)
        assert run.step == 0
    got = values(calls, "auto/loss")
    # the grace is 1024 backwards: the first multiple of log_every after it is 1100, and the pending window is logged
    # retroactively in chunks of log_every, at the stream's own backward count
    assert min(got) == 100 and max(got) == 1100 and sorted(got) == list(range(100, 1101, 100))
    assert all(v == pytest.approx(2.0) for v in got.values())


def test_loss_fallback_short_program_flushes_at_end(calls):
    w = torch.nn.Parameter(torch.ones(1))
    with start(autolog={"log_every": 10}):
        manual_updates(w, 25)
    assert sorted(values(calls, "auto/loss")) == [10, 20, 25]


def test_no_fallback_when_an_optimizer_steps(calls):
    w = torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.SGD([w], lr=0.0)
    with start(autolog={"log_every": 1}):
        for _ in range(1000):
            (w * 1.0).sum().backward()  # one huge accumulation window
        opt.step()
    assert set(values(calls, "auto/loss")) == {1}


def test_fallback_and_hooks_restored(calls):
    w = torch.nn.Parameter(torch.ones(1))
    with start(autolog={"capture_grad": True, "log_every": 1}):
        pass
    assert torch.autograd.grad is ORIG_GRAD and torch.Tensor.backward is ORIG_BACKWARD


def test_autograd_grad_capture_is_opt_in(calls):
    w = torch.nn.Parameter(torch.ones(1))

    def update(n):
        for _ in range(n):
            loss = (w * 3.0).sum()
            (g,) = torch.autograd.grad(loss, w)
            with torch.no_grad():
                w.sub_(0.0 * g)

    with start(autolog={"log_every": 1}):
        assert torch.autograd.grad is ORIG_GRAD
        update(3)
    assert "auto/loss" not in {k for _, m in calls for k in m}

    with start(autolog={"log_every": 5, "capture_grad": True}) as run:
        assert torch.autograd.grad is not ORIG_GRAD
        update(10)
    assert sorted(values(calls, "auto/loss")) == [5, 10]
    assert all(v == pytest.approx(3.0) for v in values(calls, "auto/loss").values())
    assert torch.autograd.grad is ORIG_GRAD

    calls.clear()
    with start(autolog={"log_every": 1}) as run:  # live switch
        run.config.capture_grad = True
        update(2)
        run.config.capture_grad = False
        assert torch.autograd.grad is ORIG_GRAD
        update(2)
    assert sorted(values(calls, "auto/loss")) == [1, 2]


def _compiled_step_run(calls, backend):
    model = torch.nn.Linear(4, 1)
    opt = torch.optim.Adam(model.parameters(), lr=0.1)

    @torch.compile(backend=backend)
    def step():
        opt.step()

    with start(autolog={"log_every": 2}) as run:
        for _ in range(6):
            model(torch.randn(8, 4)).pow(2).mean().backward()
            step()
            opt.zero_grad()
        assert run.step == 6
    assert set(values(calls, "auto/loss")) == {1, 2, 4, 6}
    assert sorted(values(calls, "auto/lr/0")) == [1, 2, 4, 6]


def test_compiled_optimizer_step_eager_backend(calls):
    _compiled_step_run(calls, "eager")


@pytest.mark.slow
def test_compiled_optimizer_step_inductor(calls):
    try:
        _compiled_step_run(calls, "inductor")
    except (AssertionError, KeyError):
        raise
    except Exception as e:  # pragma: no cover - needs a C++ toolchain
        pytest.skip(f"inductor unavailable here: {e}")


# --- server: PATCH hyperparams ----------------------------------------------------------------------


def test_patch_run_hyperparams(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pyrometer.server import auth
    from pyrometer.server.app import app

    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    auth._cache = None
    auth._setup_token = None
    client = TestClient(app, headers={"Authorization": f"Bearer {auth.create_key('sdk')[1]}"})
    rid = "abcdef1234567890"
    assert client.post("/runs", json={"run_id": rid, "hyperparams": {"a": 1}, "tags": ["x"]}).status_code < 300
    assert client.patch(f"/runs/{rid}", json={"hyperparams": {"a": 2, "argv": ["--a", "2"]}, "tags": ["x", "y"]}).status_code == 200
    run = client.get(f"/runs/{rid}").json()["data"]
    assert run["hyperparams"] == {"a": 2, "argv": ["--a", "2"]} and run["tags"] == ["x", "y"]
