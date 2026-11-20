"""Pyrometer SDK — drop-in experiment tracking and GPU profiling for PyTorch."""

import os
import re
import sys
import time
import uuid

import httpx

from pyrometer._hardware import fingerprint_fast
from pyrometer._run import Run
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
        A :class:`Run` instance. Call :meth:`Run.log` to record metrics and
        :meth:`Run.finish` when training completes.

    Example::

        run = pyrometer.init(
            name="my-experiment",
            hyperparams={"lr": 3e-4, "batch_size": 64},
            tags=["baseline"],
        )
    """
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
                  anomaly_detection=anomaly_detection)
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
                  create=payload)
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
        anomaly_detection=anomaly_detection,
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


def _probe_hardware(run: Run, hw: dict) -> None:
    """Complete the fingerprint (torch/CUDA/GPU details) in the background."""
    run._start_hardware_probe(hw)


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


__all__ = ["init", "create_project", "Run"]
