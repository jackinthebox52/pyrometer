"""Pyrometer SDK — drop-in experiment tracking and GPU profiling for PyTorch."""

import os
import re
import sys
import time
import uuid

import functools
import inspect

import httpx

from pyrometer._autolog import AutologConfig, NullRun, name_loss, rank_info
from pyrometer._hardware import fingerprint_fast
from pyrometer._presets import PRESETS, Preset, get_preset
from pyrometer._run import Run, _run_stack
from pyrometer._transport import spool_root

# Connecting to a dead server must not stall the start of training; a slow one still gets a few seconds.
_INIT_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def init(
    name: str | None = None,
    hyperparams: dict | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
    server: str | None = None,
    api_key: str | None = None,
    project: str | None = None,
    offline: bool | None = None,
    anomaly_detection: bool | dict = True,
    resume: str | None = None,
    group: str | None = None,
    autolog: bool | dict | None = None,
    rank_zero_only: bool = True,
    preset: str | None = None,
    monitor_interval: float | None = None,
) -> Run:
    """Initialize a new training run and register it with the Pyrometer server.

    Captures hardware info (GPU model, VRAM, CUDA/PyTorch versions) and git
    state (commit hash, dirty flag) automatically. ``init`` does not wait for the slow part
    (importing torch / initialising NVML to read CUDA and GPU details): that is filled in a moment later by a background
    update of the run, unless your program has imported torch already. Starts a background system
    monitor thread that polls pynvml every 2 seconds.

    Args:
        name: Human-readable run name. Auto-generated from run_id if omitted.
        hyperparams: Arbitrary key-value dict of hyperparameters to store.
        tags: List of string tags for filtering runs in the dashboard.
        notes: Free-text notes attached to the run.
        server: Base URL of the Pyrometer API server. Defaults to the
            ``PYROMETER_SERVER`` environment variable, then
            ``http://localhost:8321``.
        api_key: API key (create one in the dashboard's Settings page, or on the
            server with ``pyrometer keys create``). Defaults to the
            ``PYROMETER_API_KEY`` environment variable. Prefer the environment
            variable so the key never lands in source control. Not needed when
            the server runs in local ``--no-auth`` mode.
        project: Project to file the run under. A global key registers an unknown
            project as public on first use; a project key defaults to its own
            project; with neither, runs go to the public ``default`` project.
            Use :func:`create_project` for a private project.
        offline: Record everything to a local spool (``~/.pyrometer/spool/<run_id>/``,
            override with ``PYROMETER_SPOOL_DIR``) without touching the network, including
            run creation. Upload later with ``pyrometer sync``. Defaults to true when
            ``PYROMETER_MODE=offline``.
        anomaly_detection: ``True`` (default, conservative thresholds), ``False`` (off), or a
            dict overriding thresholds, e.g. ``{"warmup": 100, "metric_z": 15,
            "cooldown_steps": 1000}``. See ``docs/sdk-reference.md`` for all keys. NaN/Inf in a
            metric or gradient is always reported while detection is enabled.

        resume: Continue an existing run instead of starting a new one. Pass the run id
            (``run.run_id`` of the earlier run; it must exist, in a project this key can write),
            or ``"auto"`` to use ``$PYROMETER_RUN_ID``: resume it if it exists, create it with
            that id otherwise (handy for restartable jobs: export one id, always call
            ``init(resume="auto")``). If ``resume`` is omitted and ``PYROMETER_RUN_ID`` is set,
            ``"auto"`` is assumed. The run goes back to ``running`` (also when it was
            ``completed``, ``failed``, ``crashed`` or ``killed``) and a ``run_resumed`` event
            marks the point on the charts. The run keeps its original name, hyperparams, tags
            and notes; if you pass different ``hyperparams`` or ``tags`` a warning is printed
            and the difference is stored in the event's metadata. Check ``run.resumed`` and
            ``run.last_step`` to continue your step counter. Metric anomaly detectors are seeded
            from the run's last ~100 stored values per metric (best effort, in the background)
            so their warmup is not repeated. Re-logged steps replace the earlier values (last
            write wins per metric name and step). Offline mode: the run id is trusted, ``run.last_step`` is
            ``None``, and the resume happens when you ``pyrometer sync``. Resuming into a
            different ``project`` than the run's raises ``ValueError``.

        group: Name of the experiment or sweep this run belongs to (e.g. ``"lr-sweep-2026-11"``).
            The dashboard lists a project's groups and can filter runs by group. Defaults to the
            ``PYROMETER_GROUP`` environment variable. Up to 128 characters; stored on the run and
            not changed by ``resume``.

        autolog: Automatic collection while the run is open (default on; ``PYROMETER_AUTOLOG=0``
            turns the default off). ``False`` disables it. A dict sets :class:`AutologConfig` options
            up front, e.g. ``{"log_every": 50, "grad_norm": True}``; change them live with
            ``run.config``. Autolog counts optimizer steps, logs learning rates and steps/sec,
            and captures the loss of every ``backward()``; see the "Autolog" section of
            ``docs/sdk-reference.md`` for every ``auto/`` metric.
        rank_zero_only: In a distributed job (``RANK`` / ``LOCAL_RANK`` / ``WORLD_SIZE``, as set
            by ``torchrun``) only rank 0 creates a run; other ranks get an inert stand-in whose
            methods all do nothing (and ``run.run_id`` is ``None``). Pass ``False`` for one run per rank.

        preset: Named defaults for a kind of job: ``"llm-pretrain"``, ``"finetune"``, ``"rlhf"``, ``"rl"`` or
            ``"benchmark"`` (``pyrometer presets`` lists what each one sets). A preset only chooses
            :class:`AutologConfig` options and the GPU sampling interval, never anything about your model.
            Anything you pass explicitly wins: ``autolog={...}`` options beat the preset's, ``monitor_interval=``
            beats its interval, and ``run.config.<option> = ...`` changes the live value later. Defaults to
            ``$PYROMETER_PRESET``. Unknown names raise ``ValueError``.
        monitor_interval: Seconds between GPU snapshots (default 2.0, or the preset's value; minimum 0.1).

    Under ``pyrometer run script.py`` the launcher has already created the run: the script's first
    ``init()`` call returns that same run instead of creating another (see the "Launcher" section of
    ``docs/sdk-reference.md``).

    ``Run`` is a context manager: ``with pyrometer.init(...) as run:`` finishes the run when the block
    ends (``completed``; ``killed`` on Ctrl-C; ``failed`` on any other exception or a non-zero
    ``SystemExit``) and never swallows the exception.

    Delivery is durable. Every payload is appended to an on-disk journal
    (``~/.pyrometer/spool/<run_id>/``) before it is sent and acknowledged after, so a hard kill
    loses at most the last few milliseconds. If the server is unreachable (also right here at
    ``init``: connection refused, timeout or a 5xx after at most ~3-8 s) this does not raise: one
    warning is printed, the run is created locally, and a background sender retries with
    exponential backoff, creates the run once the server answers and then drains the journal in
    order (never blocking your training loop). ``pyrometer sync`` uploads whatever is left after
    the process exited. Authentication and validation errors still raise immediately.

    Raises:
        PermissionError: If the server rejects the API key (missing, invalid,
            or revoked).
        LookupError: If ``resume`` names a run that does not exist (or that the key cannot see).
        ValueError: If ``resume`` is malformed, or the run belongs to another project.

    Returns:
        A :class:`Run` instance (or an inert stand-in on non-zero ranks). Use it as a context
        manager, or call :meth:`Run.finish` when training completes.

    Example::

        with pyrometer.init(
            name="my-experiment",
            hyperparams={"lr": 3e-4, "batch_size": 64},
            tags=["baseline"],
        ) as run:
            train(model, optimizer)   # loss, lr, step, steps/sec are logged automatically
    """
    claimed = _launcher_run
    if claimed is not None and not claimed._launcher_claimed and not claimed._finished:
        return _attach_launcher_run(
            claimed, name=name, hyperparams=hyperparams, tags=tags, notes=notes, server=server, project=project,
            group=group, autolog=autolog, resume=resume, offline=offline, preset=preset,
        )
    preset_obj = get_preset(preset or os.environ.get("PYROMETER_PRESET"))
    if monitor_interval is not None and not (isinstance(monitor_interval, (int, float)) and monitor_interval >= 0.1):
        raise ValueError("monitor_interval must be a number of seconds >= 0.1")
    if autolog is None:
        autolog = os.environ.get("PYROMETER_AUTOLOG", "1").strip().lower() not in ("0", "false", "off", "no")
    if preset_obj is not None and (autolog is True or isinstance(autolog, dict)):
        autolog = {**preset_obj.autolog, **(autolog if isinstance(autolog, dict) else {})}
    if rank_zero_only:
        rank, _world = rank_info()
        if rank not in (None, 0):
            return NullRun(AutologConfig(**(autolog if isinstance(autolog, dict) else {})))  # type: ignore[return-value]
    if isinstance(autolog, dict):
        AutologConfig(**autolog)  # validate early
    if offline is None:
        offline = os.environ.get("PYROMETER_MODE", "").strip().lower() == "offline"

    from pyrometer._anomaly import AnomalyConfig

    AnomalyConfig.from_option(anomaly_detection)  # validate early, before registering the run

    resume_mode: str | None = None
    run_id = str(uuid.uuid4())
    env_id = (os.environ.get("PYROMETER_RUN_ID") or "").strip()
    if resume is None and env_id:
        resume = "auto"
    if resume is not None:
        if resume == "auto":
            if not env_id:
                raise ValueError('resume="auto" needs the PYROMETER_RUN_ID environment variable')
            run_id, resume_mode = env_id, "auto"
        else:
            run_id, resume_mode = str(resume), "must"
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", run_id):
            raise ValueError(f"Invalid run id {run_id!r}: 8-64 characters of letters, digits, '_' or '-'")

    hw = fingerprint_fast()  # cheap part; the torch-dependent fields follow via a background PATCH
    payload = {
        "run_id": run_id,
        "created_at": time.time(),
        "name": name,
        "hyperparams": hyperparams or {},
        "tags": tags or [],
        "notes": notes,
        "project": project,
        "group": (group if group is not None else os.environ.get("PYROMETER_GROUP", "")).strip()[:128] or None,
        **hw,
    }
    if resume_mode:
        payload["resume"] = resume_mode

    server = (server or os.environ.get("PYROMETER_SERVER") or "http://localhost:8321").rstrip("/")
    api_key = api_key or os.environ.get("PYROMETER_API_KEY") or None

    if offline:
        from pyrometer._transport import Spool

        Spool(run_id).write_meta(server=server, created=False, run_id=run_id, create=payload)
        run = Run(run_id=run_id, server_url=server, api_key=None, offline=True,
                  anomaly_detection=anomaly_detection, autolog=autolog,
                  preset=preset_obj, monitor_interval=monitor_interval)
        if resume_mode:
            run._mark_resumed(None, None, {"offline": True})
        _probe_hardware(run, hw)
        return run

    degraded: str | None = None
    resp = None
    try:
        resp = httpx.post(
            f"{server}/runs",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=_INIT_TIMEOUT,
        )
        if resp.status_code >= 500 or resp.status_code in (408, 425, 429):
            degraded = f"HTTP {resp.status_code}"
    except httpx.TransportError as e:  # unreachable, DNS failure, timeout: never fail training over it
        degraded = type(e).__name__

    if degraded is not None:
        print(
            f"pyrometer: server at {server} is not available ({degraded}); the run continues and its data is "
            f"journaled in {spool_root() / run_id}. It will be uploaded automatically once the server answers "
            "(or with: pyrometer sync).",
            file=sys.stderr,
        )
        run = Run(run_id=run_id, server_url=server, api_key=api_key, anomaly_detection=anomaly_detection,
                  create=payload, autolog=autolog, preset=preset_obj, monitor_interval=monitor_interval)
        if resume_mode:
            run._mark_resumed(None, None, {"deferred": True})
        _probe_hardware(run, hw)
        return run

    if resume_mode == "must" and resp.status_code == 404:
        raise LookupError(f"Cannot resume run {run_id!r}: not found on {server}, or this key cannot use it.")
    if resp.status_code == 409:
        raise ValueError(resp.json().get("detail", {}).get("error") or "Run belongs to another project")
    if resp.status_code == 404 and project is not None:
        raise LookupError(
            f"Project {project!r} not found on {server}, or this key cannot use it. "
            "Use a global key (registers it) or the project's own key."
        )
    if resp.status_code in (401, 403):
        raise PermissionError(
            f"Pyrometer server at {server} rejected the request ({resp.status_code}). "
            "Set PYROMETER_API_KEY (or pass api_key=) to a valid API key; create one in the "
            "dashboard's Settings page or with `pyrometer keys create --name <name>`."
        )
    resp.raise_for_status()
    run_data = resp.json()["data"]

    run = Run(
        run_id=run_data["run_id"], server_url=server, api_key=api_key,
        anomaly_detection=anomaly_detection, autolog=autolog, preset=preset_obj,
        monitor_interval=monitor_interval,
    )
    if run_data.get("resumed"):
        changes = _diff_config(run_data, hyperparams, tags)
        if changes:
            print(
                f"pyrometer: resumed run {run.run_id}; the stored hyperparams/tags were kept, "
                f"the ones passed to init() differ ({', '.join(sorted(changes))}).",
                file=sys.stderr,
            )
        run._mark_resumed(run_data.get("last_step"), run_data.get("previous_status"), changes)
    _probe_hardware(run, hw)
    return run


# The run ``pyrometer run`` created for the script it launches (set by ``pyrometer._launcher``).
_launcher_run: Run | None = None


def _set_launcher_run(run: "Run | None") -> None:
    global _launcher_run
    _launcher_run = run


def _attach_launcher_run(
    run: Run, *, name, hyperparams, tags, notes, server, project, group, autolog, resume, offline, preset
) -> Run:
    """The script's first ``init()`` under ``pyrometer run``: adopt the launcher's run instead of creating a second.

    Applied to the existing run: ``name`` (unless ``--name`` was given), ``hyperparams`` (merged over the ones parsed
    from argv, the script's win), ``tags`` (union), ``notes``, ``group`` (unless ``--group`` was given),
    ``autolog={...}`` options and ``preset`` (unless ``--preset`` was given), ``autolog=False`` (stops autolog).
    Options that decide where or how the run exists (``server``, ``api_key``, ``project``, ``offline``, ``resume``,
    ``anomaly_detection``, ``rank_zero_only``) cannot change any more: a differing value is reported once and ignored.
    Later ``init()`` calls create independent runs as usual.
    """
    run._launcher_claimed = True
    body: dict = {}
    explicit = run._launcher_explicit  # type: ignore[attr-defined]
    if name and "name" not in explicit:
        body["name"] = name
    if hyperparams:
        merged = {**run._launcher_hp, **hyperparams}  # type: ignore[attr-defined]
        run._launcher_hp = merged  # type: ignore[attr-defined]
        body["hyperparams"] = merged
    if tags:
        union = list(dict.fromkeys([*run._launcher_tags, *tags]))  # type: ignore[attr-defined]
        run._launcher_tags = union  # type: ignore[attr-defined]
        body["tags"] = union
    if notes is not None:
        body["notes"] = notes
    if group and "group" not in explicit:
        body["group"] = group[:128]
    if body:
        run._transport.submit("PATCH", f"/runs/{run.run_id}", body)
    ignored = []
    if project is not None and project != run._launcher_project:  # type: ignore[attr-defined]
        ignored.append("project")
    if resume is not None:
        ignored.append("resume")
    if offline:
        ignored.append("offline")
    if server and server.rstrip("/") != run.server:
        ignored.append("server")
    if ignored:
        print(
            f"pyrometer: init() attached to the run started by `pyrometer run`; ignoring {', '.join(ignored)} "
            "(set them on the launcher command line or environment).",
            file=sys.stderr,
        )
    pobj = get_preset(preset)
    if pobj is not None and "preset" not in explicit:
        for k, v in pobj.autolog.items():
            setattr(run.config, k, v)
        run.preset = pobj.name
        run._reminders = pobj.reminders
        run._monitor_interval = pobj.monitor_interval
        run._capture_throttle = pobj.capture_throttle
    if isinstance(autolog, dict):
        for k, v in autolog.items():
            setattr(run.config, k, v)
    elif autolog is False:
        run._autolog.stop()
    return run


def _probe_hardware(run: Run, hw: dict) -> None:
    """Complete the fingerprint (torch/CUDA/GPU details) in the background, then start autolog."""
    run._start_hardware_probe(hw)
    run._autolog.start(run._last_step if run._step_known else None, run.resumed)


def _diff_config(stored: dict, hyperparams: dict | None, tags: list[str] | None) -> dict:
    """Differences between a resumed run's stored config and what ``init`` was given.

    Only keys the caller actually passed are compared (omitted hyperparams/tags mean "no opinion").
    """
    changes: dict = {}
    if hyperparams:
        old = stored.get("hyperparams") or {}
        for k, v in hyperparams.items():
            if k not in old:
                changes[f"hyperparams.{k}"] = {"stored": None, "passed": v}
            elif old[k] != v:
                changes[f"hyperparams.{k}"] = {"stored": old[k], "passed": v}
    if tags and set(tags) != set(stored.get("tags") or []):
        changes["tags"] = {"stored": stored.get("tags") or [], "passed": list(tags)}
    return changes


def create_project(
    name: str,
    private: bool = False,
    server: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Register a project on the server.

    Needs a global API key (not a project key). Public projects are visible to
    every signed-in dashboard user. A private project is visible only to its
    members and admins, and gets its own API key, returned once.

    Args:
        name: 1-64 characters: letters, digits, ``.``, ``_`` or ``-``.
        private: Make the project private and mint its project key.
        server: Base URL; defaults to ``PYROMETER_SERVER``, then ``http://localhost:8321``.
        api_key: A global API key; defaults to ``PYROMETER_API_KEY``.

    Returns:
        ``{"project": {...}, "key": str | None}``. ``key`` is the project's API
        key for a private project (store it now; it is never shown again).

    Raises:
        PermissionError: If the key is missing, invalid, or not a global key.
        ValueError: If the name is invalid or already taken.

    Example::

        info = pyrometer.create_project("team-a", private=True)
        # export PYROMETER_API_KEY=<info["key"]> for scripts that log to team-a
    """
    import httpx

    server = (server or os.environ.get("PYROMETER_SERVER") or "http://localhost:8321").rstrip("/")
    api_key = api_key or os.environ.get("PYROMETER_API_KEY") or None
    resp = httpx.post(
        f"{server}/projects",
        json={"name": name, "visibility": "private" if private else "public"},
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        timeout=10,
    )
    if resp.status_code in (401, 403):
        raise PermissionError(
            f"Pyrometer server at {server} refused to register a project ({resp.status_code}). "
            "This needs a global API key."
        )
    if resp.status_code in (400, 409):
        raise ValueError(resp.json()["detail"]["error"])
    resp.raise_for_status()
    return resp.json()["data"]


def current_run() -> Run | None:
    """The most recently opened run that is still open in this process, or ``None``."""
    return _run_stack[-1] if _run_stack else None


_MAX_HP_ITEMS = 50
_MAX_HP_STR = 200


def _json_safe(value, depth: int = 0):
    """A JSON-safe, bounded rendering of a function argument; tensors, modules and other objects become
    a ``"<ClassName>"`` placeholder."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if isinstance(value, str):
        return value if len(value) <= _MAX_HP_STR else value[:_MAX_HP_STR] + "..."
    if hasattr(value, "__fspath__"):
        return _json_safe(os.fspath(value), depth)
    if depth < 3:
        if isinstance(value, dict):
            return {str(k)[:_MAX_HP_STR]: _json_safe(v, depth + 1) for k, v in list(value.items())[:_MAX_HP_ITEMS]}
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)[:_MAX_HP_ITEMS]
            return [_json_safe(v, depth + 1) for v in items]
    import enum

    if isinstance(value, enum.Enum):
        return str(value)
    return f"<{type(value).__name__}>"


def run(
    fn=None,
    *,
    name: str | None = None,
    hyperparams: dict | None = None,
    **init_kwargs,
):
    """Decorator: run a training function inside ``with pyrometer.init(...) as run:``.

    The function's call arguments (with defaults applied) become the run's hyperparams, JSON-safe:
    numbers, strings, bools, ``None`` and short lists/dicts as they are; tensors, modules, datasets and
    other objects as a ``"<ClassName>"`` placeholder; long strings and containers are truncated.
    ``hyperparams=`` entries override same-named arguments. ``name`` defaults to the function name. All
    other keyword arguments go to :func:`init` (``project``, ``tags``, ``autolog``, ...). If the function
    has a parameter called ``run`` that the caller did not pass, the :class:`Run` is injected there.

    Works bare (``@pyrometer.run``) or with arguments (``@pyrometer.run(name="x", tags=["a"])``).
    The exception, if any, propagates after the run is closed (``failed`` / ``killed``).

    Example::

        @pyrometer.run(name="baseline", project="mnist")
        def train(lr: float = 3e-4, epochs: int = 10):
            ...   # autolog captures step, loss, lr, steps/sec
    """

    def deco(f):
        sig = inspect.signature(f)

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                call = {k: v for k, v in bound.arguments.items() if k != "run"}
                # **kwargs parameters arrive as one dict: flatten them
                for pname, param in sig.parameters.items():
                    if param.kind is inspect.Parameter.VAR_KEYWORD and pname in call:
                        call.update(call.pop(pname))
                    elif param.kind is inspect.Parameter.VAR_POSITIONAL and pname in call:
                        call[pname] = list(call[pname])
            except TypeError:
                call = {}
            hp = {k: _json_safe(v) for k, v in call.items()}
            hp.update({str(k): _json_safe(v) for k, v in (hyperparams or {}).items()})
            with init(name=name or f.__name__, hyperparams=hp, **init_kwargs) as r:
                if "run" in sig.parameters and "run" not in kwargs and not (
                    args and list(sig.parameters).index("run") < len(args)
                ):
                    kwargs["run"] = r
                return f(*args, **kwargs)

        return wrapper

    return deco(fn) if callable(fn) else deco


__all__ = ["init", "create_project", "run", "current_run", "name_loss", "AutologConfig", "Run", "PRESETS", "Preset"]
