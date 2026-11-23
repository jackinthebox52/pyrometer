"""``gpu_heat`` on the runs list: mean utilization over the last 60s of each run's own snapshot timeline."""

import pytest

from pyrometer.server import db


def _run(name: str) -> str:
    return db.db_create_run(name, {}, [], None, None, False, None, None, None, None, None, None)["run_id"]


def _snap(run_id: str, t: float, util: float | None, attributed: float | None = None) -> None:
    db.db_insert_system_snapshot(
        run_id, {"timestamp": t, "gpu_utilization": util, "attributed_utilization": attributed}
    )


def test_gpu_heat_window_and_fallback():
    hot = _run("hot")
    _snap(hot, 1000.0, 0.0)  # older than 60s before the newest snapshot: excluded
    _snap(hot, 1100.0, 100.0, attributed=80.0)  # attributed wins over raw utilization
    _snap(hot, 1140.0, 40.0)  # no attributed value: falls back to gpu_utilization
    cold = _run("cold")
    _snap(cold, 5.0, 10.0)  # its own timeline, not wall clock
    none = _run("none")

    runs = {r["name"]: r for r in db.db_list_runs()}
    assert runs["hot"]["gpu_heat"] == pytest.approx(0.6)
    assert runs["cold"]["gpu_heat"] == pytest.approx(0.1)
    assert runs["none"]["gpu_heat"] is None
    assert none  # created without snapshots


def test_gpu_heat_empty_list():
    assert db.db_list_runs() == []
