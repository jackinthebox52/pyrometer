"""``db_leaderboard``: eval metrics per run, last value plus extremes."""

from pyrometer.server import db


def _run(name: str, group: str | None = None) -> str:
    return db.db_create_run(name, {}, [], None, None, False, None, None, None, None, group, None)["run_id"]


def _log(run_id: str, step: int, **metrics: float) -> None:
    db.db_insert_metrics(run_id, [{"step": step, "timestamp": float(step), "name": k, "value": v} for k, v in metrics.items()])


def test_leaderboard_scores_and_filters():
    a, b, c = _run("a", "g1"), _run("b", "g1"), _run("c", "g2")
    _log(a, 1, **{"eval/acc": 0.5, "train/loss": 3.0})
    _log(a, 2, **{"eval/acc": 0.4})
    _log(b, 1, **{"eval/acc": 0.7, "eval/f1": 0.6})
    _log(c, 1, **{"train/loss": 1.0})  # no eval metrics: left out

    lb = db.db_leaderboard(None, None, None)
    assert lb["metrics"] == ["eval/acc", "eval/f1"]
    rows = {r["name"]: r for r in lb["rows"]}
    assert set(rows) == {"a", "b"}
    assert rows["a"]["scores"]["eval/acc"] == {"last": 0.4, "step": 2, "min": 0.4, "max": 0.5}
    assert [r["name"] for r in db.db_leaderboard(None, None, "g2")["rows"]] == []
    assert db.db_leaderboard([], None, None) == {"metrics": [], "rows": []}
