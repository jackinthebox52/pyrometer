"""Pyrometer SDK — drop-in experiment tracking and GPU profiling for PyTorch."""

from pyrometer._hardware import fingerprint
from pyrometer._run import Run


def init(
    name: str | None = None,
    hyperparams: dict | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
    server: str = "http://localhost:8321",
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
        server: Base URL of the Pyrometer API server.

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

    resp = httpx.post(f"{server}/runs", json=payload, timeout=10)
    resp.raise_for_status()
    run_data = resp.json()["data"]

    return Run(run_id=run_data["run_id"], server_url=server)


__all__ = ["init", "Run"]
