"""Pyrometer SDK — drop-in experiment tracking and GPU profiling for PyTorch."""

import os

from pyrometer._hardware import fingerprint
from pyrometer._run import Run


def init(
    name: str | None = None,
    hyperparams: dict | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
    server: str | None = None,
    api_key: str | None = None,
) -> Run:
    """Initialize a new training run and register it with the Pyrometer server.

    Captures hardware info (GPU model, VRAM, CUDA/PyTorch versions) and git
    state (commit hash, dirty flag) automatically. Starts a background system
    monitor thread that polls pynvml every 2 seconds.

    Args:
        name: Human-readable run name. Auto-generated from run_id if omitted.
        hyperparams: Arbitrary key-value dict of hyperparameters to store.
        tags: List of string tags for filtering runs in the dashboard.
        notes: Free-text notes attached to the run.
        server: Base URL of the Pyrometer API server. Defaults to the
            ``PYROMETER_SERVER`` environment variable, then
            ``http://localhost:8321``.
        api_key: API key with the ``ingest`` scope (create one on the server
            with ``pyrometer keys create``). Defaults to the
            ``PYROMETER_API_KEY`` environment variable. Prefer the environment
            variable so the key never lands in source control. Not needed when
            the server runs in local ``--no-auth`` mode.

    Raises:
        PermissionError: If the server rejects the API key (missing, invalid,
            revoked, or lacking the ``ingest`` scope).

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
    import httpx

    hw = fingerprint()
    payload = {
        "name": name,
        "hyperparams": hyperparams or {},
        "tags": tags or [],
        "notes": notes,
        **hw,
    }

    server = (server or os.environ.get("PYROMETER_SERVER") or "http://localhost:8321").rstrip("/")
    api_key = api_key or os.environ.get("PYROMETER_API_KEY") or None

    resp = httpx.post(
        f"{server}/runs",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        timeout=10,
    )
    if resp.status_code in (401, 403):
        raise PermissionError(
            f"Pyrometer server at {server} rejected the request ({resp.status_code}). "
            "Set PYROMETER_API_KEY (or pass api_key=) to a key with the 'ingest' scope; "
            "create one with `pyrometer keys create --name <name>`."
        )
    resp.raise_for_status()
    run_data = resp.json()["data"]

    return Run(run_id=run_data["run_id"], server_url=server, api_key=api_key)


__all__ = ["init", "Run"]
