"""DuckDB access layer — all queries live here, never in route handlers."""

import datetime
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import duckdb

_DB_PATH = os.environ.get(
    "PYROMETER_DB_PATH",
    str(Path.home() / ".pyrometer" / "data.duckdb"),
)

_BATCH_TTL_S = 30 * 86400
_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None


def get_conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = duckdb.connect(_DB_PATH)
        _init_schema(_conn)
    return _conn


def get_lock() -> threading.Lock:
    return _lock


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id           VARCHAR PRIMARY KEY,
            name             VARCHAR,
            status           VARCHAR DEFAULT 'running',
            created_at       DOUBLE,
            finished_at      DOUBLE,
            hyperparams      JSON,
            git_hash         VARCHAR,
            git_dirty        BOOLEAN,
            gpu_model        VARCHAR,
            gpu_memory_total BIGINT,
            cuda_version     VARCHAR,
            pytorch_version  VARCHAR,
            python_version   VARCHAR,
            hostname         VARCHAR,
            tags             JSON,
            notes            VARCHAR,
            project          VARCHAR
        )
    """)

    # Projects. Databases from before projects get the column appended (same position as in the
    # CREATE above) and their runs land in the public "default" project.
    conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS project VARCHAR")
    # Liveness: stamped by SDK heartbeats; NULL for runs from before heartbeats (never auto-crashed).
    conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS last_heartbeat DOUBLE")
    conn.execute("UPDATE runs SET project = 'default' WHERE project IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            name       VARCHAR PRIMARY KEY,
            visibility VARCHAR NOT NULL,
            created_at DOUBLE,
            created_by VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_members (
            project  VARCHAR,
            username VARCHAR,
            PRIMARY KEY (project, username)
        )
    """)
    conn.execute(
        "INSERT INTO projects VALUES ('default', 'public', ?, 'system') ON CONFLICT DO NOTHING",
        [time.time()],
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            run_id    VARCHAR,
            step      INTEGER,
            timestamp DOUBLE,
            name      VARCHAR,
            value     DOUBLE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS profile_events (
            run_id       VARCHAR,
            step         INTEGER,
            timestamp    DOUBLE,
            name         VARCHAR,
            category     VARCHAR,
            duration_us  DOUBLE,
            cuda_time_us DOUBLE,
            cpu_time_us  DOUBLE,
            input_shapes JSON,
            memory_bytes BIGINT,
            device_id    INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_snapshots (
            run_id                 VARCHAR,
            timestamp              DOUBLE,
            gpu_utilization        DOUBLE,
            memory_used            BIGINT,
            memory_total           BIGINT,
            temperature            DOUBLE,
            power_draw             DOUBLE,
            fan_speed              DOUBLE,
            sm_clock_mhz           INTEGER,
            memory_clock_mhz       INTEGER,
            process_memory_used    BIGINT,
            process_exclusive      BOOLEAN,
            attributed_utilization DOUBLE,
            attributed_power       DOUBLE,
            gpu_index              INTEGER
        )
    """)
    # Migration: add new columns to databases created before this schema version.
    for _col, _type in [
        ("process_memory_used",    "BIGINT"),
        ("process_exclusive",      "BOOLEAN"),
        ("attributed_utilization", "DOUBLE"),
        ("attributed_power",       "DOUBLE"),
        ("gpu_index",              "INTEGER"),  # NULL on old rows = GPU 0
    ]:
        try:
            conn.execute(
                f"ALTER TABLE system_snapshots ADD COLUMN IF NOT EXISTS {_col} {_type}"
            )
        except Exception:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS param_stats (
            run_id           VARCHAR,
            step             INTEGER,
            timestamp        DOUBLE,
            param_name       VARCHAR,
            grad_norm        DOUBLE,
            grad_min         DOUBLE,
            grad_max         DOUBLE,
            grad_mean        DOUBLE,
            weight_norm      DOUBLE,
            weight_min       DOUBLE,
            weight_max       DOUBLE,
            weight_mean      DOUBLE,
            grad_histogram   JSON,
            weight_histogram JSON
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            run_id     VARCHAR,
            timestamp  DOUBLE,
            step       INTEGER,
            event_type VARCHAR,
            severity   VARCHAR,
            message    VARCHAR,
            metadata   JSON
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            run_id    VARCHAR,
            timestamp DOUBLE,
            stream    VARCHAR,
            line      VARCHAR,
            step      INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            run_id        VARCHAR,
            timestamp     DOUBLE,
            step          INTEGER,
            artifact_type VARCHAR,
            path          VARCHAR,
            size_bytes    BIGINT,
            metadata      JSON
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS media (
            run_id     VARCHAR,
            step       INTEGER,
            timestamp  DOUBLE,
            name       VARCHAR,
            media_type VARCHAR,
            data       VARCHAR
        )
    """)

    # LLM/RL generation samples: one row per sample dict (JSON text), grouped by (name, step).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS samples (
            run_id    VARCHAR,
            step      INTEGER,
            timestamp DOUBLE,
            name      VARCHAR,
            idx       INTEGER,
            reward    DOUBLE,
            data      VARCHAR
        )
    """)

    # Client batch ids already ingested, so a retried batch is ignored (see db_claim_batch).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_batches (
            run_id   VARCHAR,
            batch_id VARCHAR,
            seen_at  DOUBLE,
            PRIMARY KEY (run_id, batch_id)
        )
    """)
    conn.execute("DELETE FROM seen_batches WHERE seen_at < ?", [time.time() - _BATCH_TTL_S])

    # Webhook alert rules (admin-managed). ``url`` is a secret: never returned by list/get helpers' callers unmasked.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_rules (
            id           VARCHAR PRIMARY KEY,
            name         VARCHAR,
            kind         VARCHAR,
            url          VARCHAR,
            format       VARCHAR,
            project      VARCHAR,
            min_severity VARCHAR,
            metric       VARCHAR,
            op           VARCHAR,
            threshold    DOUBLE,
            enabled      BOOLEAN,
            created_at   DOUBLE,
            created_by   VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_summaries (
            run_id                    VARCHAR PRIMARY KEY,
            computed_at               DOUBLE,
            total_training_time_s     DOUBLE,
            total_steps               INTEGER,
            steps_per_sec             DOUBLE,
            avg_gpu_utilization       DOUBLE,
            peak_process_memory_bytes BIGINT,
            anomaly_count             INTEGER,
            final_metrics             JSON,
            best_metrics              JSON,
            efficiency_score          VARCHAR,
            efficiency_details        JSON
        )
    """)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_to_iso(ts: float | None) -> str | None:
    """Convert a Unix timestamp (seconds) to an ISO 8601 string, or None."""
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def _parse_json(val):
    if val is None:
        return None
    if isinstance(val, str):
        return json.loads(val)
    return val


def _row_to_run_summary(row: tuple) -> dict:
    return {
        "project": row[8],
        "last_heartbeat": _ts_to_iso(row[9]),
        "run_id": row[0],
        "name": row[1],
        "status": row[2],
        "created_at": _ts_to_iso(row[3]),
        "finished_at": _ts_to_iso(row[4]),
        "hyperparams": _parse_json(row[5]) or {},
        "gpu_model": row[6],
        "tags": _parse_json(row[7]) or [],
    }


def _row_to_run_detail(row: tuple) -> dict:
    cols = [
        "run_id", "name", "status", "created_at", "finished_at",
        "hyperparams", "git_hash", "git_dirty", "gpu_model", "gpu_memory_total",
        "cuda_version", "pytorch_version", "python_version", "hostname", "tags", "notes",
        "project", "last_heartbeat",
    ]
    d = dict(zip(cols, row))
    d["hyperparams"] = _parse_json(d["hyperparams"]) or {}
    d["tags"] = _parse_json(d["tags"]) or []
    d["created_at"] = _ts_to_iso(d["created_at"])
    d["finished_at"] = _ts_to_iso(d["finished_at"])
    d["last_heartbeat"] = _ts_to_iso(d["last_heartbeat"])
    return d


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def db_create_run(
    name: str | None,
    hyperparams: dict,
    tags: list[str],
    notes: str | None,
    git_hash: str | None,
    git_dirty: bool,
    gpu_model: str | None,
    gpu_memory_total: int | None,
    cuda_version: str | None,
    pytorch_version: str | None,
    python_version: str | None,
    hostname: str | None,
    project: str = "default",
    run_id: str | None = None,
    created_at: float | None = None,
) -> dict:
    """Insert a run. ``run_id``/``created_at`` come from the SDK (offline runs keep their identity)."""
    run_id = run_id or str(uuid.uuid4())
    resolved_name = name or f"run-{run_id[:8]}"
    now = created_at or time.time()

    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO runs (
                run_id, name, status, created_at, finished_at, hyperparams, git_hash, git_dirty,
                gpu_model, gpu_memory_total, cuda_version, pytorch_version, python_version,
                hostname, tags, notes, project, last_heartbeat
            ) VALUES (
                ?, ?, 'running', ?, NULL,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                run_id, resolved_name, now,
                json.dumps(hyperparams),
                git_hash, git_dirty,
                gpu_model, gpu_memory_total,
                cuda_version, pytorch_version, python_version,
                hostname,
                json.dumps(tags),
                notes,
                project,
                time.time(),  # server clock: a run synced from a spool starts "alive" now
            ],
        )

    return {
        "run_id": run_id, "name": resolved_name, "status": "running",
        "created_at": _ts_to_iso(now), "project": project,
    }


def db_list_runs(projects: list[str] | None = None) -> list[dict]:
    """All runs, or only those in ``projects`` (``None`` = no restriction)."""
    db_mark_stale_runs()
    where, params = "", []
    if projects is not None:
        if not projects:
            return []
        where = f"WHERE project IN ({', '.join('?' * len(projects))})"
        params = list(projects)
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            f"""
            SELECT run_id, name, status, created_at, finished_at,
                   hyperparams, gpu_model, tags, project, last_heartbeat
            FROM runs
            {where}
            ORDER BY created_at DESC
            """,
            params,
        ).fetchall()
    return [_row_to_run_summary(r) for r in rows]


def db_get_run_project(run_id: str) -> str | None:
    """The project a run belongs to, or ``None`` if the run does not exist."""
    with _lock:
        row = get_conn().execute("SELECT project FROM runs WHERE run_id = ?", [run_id]).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _row_to_project(row: tuple) -> dict:
    return {
        "name": row[0],
        "visibility": row[1],
        "created_at": _ts_to_iso(row[2]),
        "created_by": row[3],
        "runs": row[4] if len(row) > 4 else None,
    }


def db_get_project(name: str) -> dict | None:
    with _lock:
        row = get_conn().execute(
            "SELECT name, visibility, created_at, created_by FROM projects WHERE name = ?", [name]
        ).fetchone()
    return _row_to_project(row) if row else None


def db_list_projects(names: list[str] | None = None) -> list[dict]:
    """Projects with run counts; ``names`` restricts the result (``None`` = all)."""
    where, params = "", []
    if names is not None:
        if not names:
            return []
        where = f"WHERE p.name IN ({', '.join('?' * len(names))})"
        params = list(names)
    with _lock:
        rows = get_conn().execute(
            f"""
            SELECT p.name, p.visibility, p.created_at, p.created_by, COUNT(r.run_id)
            FROM projects p LEFT JOIN runs r ON r.project = p.name
            {where}
            GROUP BY p.name, p.visibility, p.created_at, p.created_by
            ORDER BY p.name
            """,
            params,
        ).fetchall()
    return [_row_to_project(r) for r in rows]


def db_create_project(name: str, visibility: str, created_by: str) -> dict | None:
    """Create a project; returns ``None`` if the name is taken."""
    with _lock:
        conn = get_conn()
        inserted = conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING name",
            [name, visibility, time.time(), created_by],
        ).fetchone()
    return db_get_project(name) if inserted else None


def db_set_project_visibility(name: str, visibility: str) -> bool:
    with _lock:
        row = get_conn().execute(
            "UPDATE projects SET visibility = ? WHERE name = ? RETURNING name", [visibility, name]
        ).fetchone()
    return row is not None


def db_get_members(project: str) -> list[str]:
    with _lock:
        rows = get_conn().execute(
            "SELECT username FROM project_members WHERE project = ? ORDER BY username", [project]
        ).fetchall()
    return [r[0] for r in rows]


def db_set_members(project: str, usernames: list[str]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM project_members WHERE project = ?", [project])
        for u in sorted(set(usernames)):
            conn.execute("INSERT INTO project_members VALUES (?, ?)", [project, u])


def db_is_member(project: str, username: str) -> bool:
    with _lock:
        row = get_conn().execute(
            "SELECT 1 FROM project_members WHERE project = ? AND username = ?", [project, username]
        ).fetchone()
    return row is not None


def db_user_project_names(username: str) -> list[str]:
    """Projects a regular user may read: every public one plus those they are a member of."""
    with _lock:
        rows = get_conn().execute(
            """
            SELECT name FROM projects WHERE visibility = 'public'
            UNION
            SELECT project FROM project_members WHERE username = ?
            """,
            [username],
        ).fetchall()
    return [r[0] for r in rows]


def db_remove_user_memberships(username: str) -> None:
    with _lock:
        get_conn().execute("DELETE FROM project_members WHERE username = ?", [username])


def db_get_run(run_id: str) -> dict | None:
    db_mark_stale_runs()
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", [run_id]).fetchone()
    if row is None:
        return None
    return _row_to_run_detail(row)


TERMINAL_STATUSES = ("completed", "failed", "crashed", "killed")


def heartbeat_timeout() -> float:
    """Seconds without a heartbeat after which a running run counts as crashed (``PYROMETER_HEARTBEAT_TIMEOUT``, default 300)."""
    try:
        return max(1.0, float(os.environ.get("PYROMETER_HEARTBEAT_TIMEOUT", "300")))
    except ValueError:
        return 300.0


def db_mark_stale_runs(now: float | None = None) -> list[str]:
    """Flip ``running`` runs whose last heartbeat is older than the timeout to ``crashed``.

    Runs without a heartbeat timestamp (created before heartbeats existed) are left alone.
    Returns the ids that changed. ``finished_at`` is set to the last heartbeat seen.
    """
    now = time.time() if now is None else now
    with _lock:
        rows = get_conn().execute(
            """
            UPDATE runs SET status = 'crashed', finished_at = last_heartbeat
            WHERE status = 'running' AND last_heartbeat IS NOT NULL AND last_heartbeat < ?
            RETURNING run_id
            """,
            [now - heartbeat_timeout()],
        ).fetchall()
    ids = [r[0] for r in rows]
    # This UPDATE is the one place a run turns crashed, so alerting here fires once per crash
    # whichever caller (sweeper, list, get) got there first.
    if _status_hook is not None:
        for run_id in ids:
            _status_hook(run_id, "crashed")
    return ids


_status_hook: Callable[[str, str], None] | None = None


def set_status_hook(hook: Callable[[str, str], None] | None) -> None:
    """Register a callback ``(run_id, new_status)`` invoked when the server marks a run ``crashed``."""
    global _status_hook
    _status_hook = hook


def db_get_run_status(run_id: str) -> str | None:
    with _lock:
        row = get_conn().execute("SELECT status FROM runs WHERE run_id = ?", [run_id]).fetchone()
    return row[0] if row else None


def db_heartbeat(run_id: str) -> None:
    """Record liveness. A ``crashed`` run that is heard from again goes back to ``running``.

    Terminal statuses set explicitly (completed/failed/killed) are never touched by a heartbeat.
    """
    with _lock:
        get_conn().execute(
            """
            UPDATE runs SET last_heartbeat = ?,
                status = CASE WHEN status = 'crashed' THEN 'running' ELSE status END,
                finished_at = CASE WHEN status = 'crashed' THEN NULL ELSE finished_at END
            WHERE run_id = ?
            """,
            [time.time(), run_id],
        )


def db_resume_run(run_id: str) -> dict:
    """Reattach to an existing run: status back to ``running``, heartbeat refreshed.

    Original name, hyperparams and tags are kept. Returns what the SDK needs to continue:
    ``last_step`` (highest logged step, ``None`` if nothing was logged), ``previous_status``.
    """
    with _lock:
        conn = get_conn()
        prev = conn.execute("SELECT status FROM runs WHERE run_id = ?", [run_id]).fetchone()[0]
        conn.execute(
            "UPDATE runs SET status = 'running', finished_at = NULL, last_heartbeat = ? WHERE run_id = ?",
            [time.time(), run_id],
        )
        last_step = conn.execute("SELECT MAX(step) FROM metrics WHERE run_id = ?", [run_id]).fetchone()[0]
    run = db_get_run(run_id)
    return {
        **{k: run[k] for k in ("run_id", "name", "status", "created_at", "project", "hyperparams", "tags")},
        "resumed": True,
        "previous_status": prev,
        "last_step": last_step,
    }


def db_update_run(run_id: str, status: str | None, notes: str | None) -> None:
    with _lock:
        conn = get_conn()
        if status is not None:
            finished_at = (
                time.time()
                if status in TERMINAL_STATUSES
                else None
            )
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                [status, finished_at, run_id],
            )
        if notes is not None:
            conn.execute(
                "UPDATE runs SET notes = ? WHERE run_id = ?",
                [notes, run_id],
            )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def db_claim_batch(run_id: str, batch_id: str) -> bool:
    """Record a client batch id; False if this run already ingested it (a retry)."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "INSERT INTO seen_batches VALUES (?, ?, ?) ON CONFLICT DO NOTHING RETURNING batch_id",
            [run_id, batch_id, time.time()],
        ).fetchone()
    return row is not None


def db_release_batch(run_id: str, batch_id: str) -> None:
    """Forget a claimed batch id after its insert failed, so the client's retry is processed."""
    with _lock:
        get_conn().execute("DELETE FROM seen_batches WHERE run_id = ? AND batch_id = ?", [run_id, batch_id])


def db_insert_metrics(run_id: str, metrics: list[dict]) -> int:
    rows = [(run_id, m["step"], m["timestamp"], m["name"], m["value"]) for m in metrics]
    with _lock:
        conn = get_conn()
        conn.executemany("INSERT INTO metrics VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def db_get_metrics(run_id: str, name: str | None = None) -> list[dict]:
    with _lock:
        conn = get_conn()
        if name:
            rows = conn.execute(
                "SELECT step, timestamp, name, value FROM metrics "
                "WHERE run_id = ? AND name = ? ORDER BY step",
                [run_id, name],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT step, timestamp, name, value FROM metrics "
                "WHERE run_id = ? ORDER BY step",
                [run_id],
            ).fetchall()
    return [{"step": r[0], "timestamp": r[1], "name": r[2], "value": r[3]} for r in rows]


def db_get_metric_names(run_id: str) -> list[str]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT DISTINCT name FROM metrics WHERE run_id = ? ORDER BY name",
            [run_id],
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Profile events
# ---------------------------------------------------------------------------

def db_insert_profile_events(run_id: str, events: list[dict]) -> int:
    rows = [
        (
            run_id,
            e["step"],
            e["timestamp"],
            e["name"],
            e["category"],
            e["duration_us"],
            e.get("cuda_time_us"),
            e.get("cpu_time_us"),
            json.dumps(e.get("input_shapes") or []),
            e.get("memory_bytes"),
            e.get("device_id"),
        )
        for e in events
    ]
    with _lock:
        conn = get_conn()
        conn.executemany("INSERT INTO profile_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def db_get_profile_events(run_id: str, step: int | None = None) -> list[dict]:
    cols = [
        "step", "timestamp", "name", "category", "duration_us",
        "cuda_time_us", "cpu_time_us", "input_shapes", "memory_bytes", "device_id",
    ]
    with _lock:
        conn = get_conn()
        if step is not None:
            rows = conn.execute(
                "SELECT step, timestamp, name, category, duration_us, cuda_time_us, "
                "cpu_time_us, input_shapes, memory_bytes, device_id "
                "FROM profile_events WHERE run_id = ? AND step = ? ORDER BY duration_us DESC",
                [run_id, step],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT step, timestamp, name, category, duration_us, cuda_time_us, "
                "cpu_time_us, input_shapes, memory_bytes, device_id "
                "FROM profile_events WHERE run_id = ? ORDER BY step, duration_us DESC",
                [run_id],
            ).fetchall()

    result = []
    for r in rows:
        d = dict(zip(cols, r))
        d["input_shapes"] = _parse_json(d["input_shapes"]) or []
        result.append(d)
    return result


def db_get_profile_steps(run_id: str) -> list[int]:
    """Return the sorted list of steps for which profile events exist."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT DISTINCT step FROM profile_events WHERE run_id = ? ORDER BY step",
            [run_id],
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# System snapshots
# ---------------------------------------------------------------------------

def db_insert_system_snapshot(run_id: str, snapshot: dict) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO system_snapshots (run_id, timestamp, gpu_utilization, memory_used, memory_total, "
            "temperature, power_draw, fan_speed, sm_clock_mhz, memory_clock_mhz, process_memory_used, "
            "process_exclusive, attributed_utilization, attributed_power, gpu_index) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                snapshot["timestamp"],
                snapshot.get("gpu_utilization"),
                snapshot.get("memory_used"),
                snapshot.get("memory_total"),
                snapshot.get("temperature"),
                snapshot.get("power_draw"),
                snapshot.get("fan_speed"),
                snapshot.get("sm_clock_mhz"),
                snapshot.get("memory_clock_mhz"),
                snapshot.get("process_memory_used"),
                snapshot.get("process_exclusive"),
                snapshot.get("attributed_utilization"),
                snapshot.get("attributed_power"),
                snapshot.get("gpu_index") or 0,
            ],
        )


def db_gpu_count(run_id: str) -> int:
    """Number of distinct GPUs that reported snapshots for a run (old rows count as GPU 0)."""
    with _lock:
        row = get_conn().execute(
            "SELECT COUNT(DISTINCT COALESCE(gpu_index, 0)) FROM system_snapshots WHERE run_id = ?",
            [run_id],
        ).fetchone()
    return int(row[0]) if row else 0


def db_get_system_snapshots(run_id: str) -> list[dict]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            """
            SELECT timestamp, gpu_utilization, memory_used, memory_total,
                   temperature, power_draw, fan_speed, sm_clock_mhz, memory_clock_mhz,
                   process_memory_used, process_exclusive,
                   attributed_utilization, attributed_power, COALESCE(gpu_index, 0)
            FROM system_snapshots
            WHERE run_id = ?
            ORDER BY timestamp, 14
            """,
            [run_id],
        ).fetchall()

    cols = [
        "timestamp", "gpu_utilization", "memory_used", "memory_total",
        "temperature", "power_draw", "fan_speed", "sm_clock_mhz", "memory_clock_mhz",
        "process_memory_used", "process_exclusive",
        "attributed_utilization", "attributed_power", "gpu_index",
    ]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Param stats (gradient & weight monitoring)
# ---------------------------------------------------------------------------

_PARAM_STAT_COLS = [
    "step", "timestamp", "param_name",
    "grad_norm", "grad_min", "grad_max", "grad_mean",
    "weight_norm", "weight_min", "weight_max", "weight_mean",
    "grad_histogram", "weight_histogram",
]


def db_insert_param_stats(run_id: str, stats: list[dict]) -> int:
    rows = [
        (
            run_id,
            s["step"],
            s["timestamp"],
            s["param_name"],
            s["grad_norm"],
            s["grad_min"],
            s["grad_max"],
            s["grad_mean"],
            s["weight_norm"],
            s["weight_min"],
            s["weight_max"],
            s["weight_mean"],
            json.dumps(s.get("grad_histogram") or []),
            json.dumps(s.get("weight_histogram") or []),
        )
        for s in stats
    ]
    with _lock:
        conn = get_conn()
        conn.executemany(
            "INSERT INTO param_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
    return len(rows)


def db_get_param_stats(run_id: str, param_name: str | None = None) -> list[dict]:
    with _lock:
        conn = get_conn()
        if param_name:
            rows = conn.execute(
                "SELECT step, timestamp, param_name, grad_norm, grad_min, grad_max, grad_mean, "
                "weight_norm, weight_min, weight_max, weight_mean, grad_histogram, weight_histogram "
                "FROM param_stats WHERE run_id = ? AND param_name = ? ORDER BY step",
                [run_id, param_name],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT step, timestamp, param_name, grad_norm, grad_min, grad_max, grad_mean, "
                "weight_norm, weight_min, weight_max, weight_mean, grad_histogram, weight_histogram "
                "FROM param_stats WHERE run_id = ? ORDER BY param_name, step",
                [run_id],
            ).fetchall()
    result = []
    for r in rows:
        d = dict(zip(_PARAM_STAT_COLS, r))
        d["grad_histogram"] = _parse_json(d["grad_histogram"]) or []
        d["weight_histogram"] = _parse_json(d["weight_histogram"]) or []
        result.append(d)
    return result


def db_get_param_stat_names(run_id: str) -> list[str]:
    """Return param names in insertion order (preserves model.named_parameters() order)."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT param_name FROM param_stats WHERE run_id = ? "
            "GROUP BY param_name ORDER BY min(step), param_name",
            [run_id],
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Events (anomaly flags, custom milestones)
# ---------------------------------------------------------------------------

def db_insert_event(run_id: str, event: dict) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                event["timestamp"],
                event.get("step"),
                event["event_type"],
                event["severity"],
                event["message"],
                json.dumps(event.get("metadata") or {}),
            ],
        )


def db_get_events(run_id: str) -> list[dict]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT timestamp, step, event_type, severity, message, metadata "
            "FROM events WHERE run_id = ? ORDER BY timestamp",
            [run_id],
        ).fetchall()
    cols = ["timestamp", "step", "event_type", "severity", "message", "metadata"]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        d["metadata"] = _parse_json(d["metadata"]) or {}
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Logs (stdout/stderr capture)
# ---------------------------------------------------------------------------

def db_insert_logs(run_id: str, entries: list[dict]) -> int:
    rows = [
        (run_id, e["timestamp"], e["stream"], e["line"], e.get("step"))
        for e in entries
    ]
    with _lock:
        conn = get_conn()
        conn.executemany("INSERT INTO logs VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def db_get_logs(run_id: str, stream: str | None = None) -> list[dict]:
    with _lock:
        conn = get_conn()
        if stream:
            rows = conn.execute(
                "SELECT timestamp, stream, line, step FROM logs "
                "WHERE run_id = ? AND stream = ? ORDER BY timestamp",
                [run_id, stream],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT timestamp, stream, line, step FROM logs "
                "WHERE run_id = ? ORDER BY timestamp",
                [run_id],
            ).fetchall()
    cols = ["timestamp", "stream", "line", "step"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Artifacts (checkpoint / file tracking)
# ---------------------------------------------------------------------------

def db_insert_artifact(run_id: str, artifact: dict) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                artifact["timestamp"],
                artifact.get("step"),
                artifact["artifact_type"],
                artifact["path"],
                artifact.get("size_bytes"),
                json.dumps(artifact.get("metadata") or {}),
            ],
        )


def db_get_artifacts(run_id: str) -> list[dict]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT timestamp, step, artifact_type, path, size_bytes, metadata "
            "FROM artifacts WHERE run_id = ? ORDER BY timestamp",
            [run_id],
        ).fetchall()
    cols = ["timestamp", "step", "artifact_type", "path", "size_bytes", "metadata"]
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        d["metadata"] = _parse_json(d["metadata"]) or {}
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Media (images, histograms, figures)
# ---------------------------------------------------------------------------

def db_insert_media(run_id: str, item: dict) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO media VALUES (?, ?, ?, ?, ?, ?)",
            [
                run_id,
                item["step"],
                item["timestamp"],
                item["name"],
                item["media_type"],
                item["data"],
            ],
        )


def db_get_media(run_id: str, name: str | None = None) -> list[dict]:
    with _lock:
        conn = get_conn()
        if name:
            rows = conn.execute(
                "SELECT step, timestamp, name, media_type, data "
                "FROM media WHERE run_id = ? AND name = ? ORDER BY step",
                [run_id, name],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT step, timestamp, name, media_type, data "
                "FROM media WHERE run_id = ? ORDER BY name, step",
                [run_id],
            ).fetchall()
    cols = ["step", "timestamp", "name", "media_type", "data"]
    return [dict(zip(cols, r)) for r in rows]


def db_get_media_names(run_id: str) -> list[str]:
    """Return media names in insertion order."""
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT name FROM media WHERE run_id = ? "
            "GROUP BY name ORDER BY min(step), name",
            [run_id],
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Samples (prompt / completion / reward dicts per step)
# ---------------------------------------------------------------------------

def db_insert_samples(run_id: str, name: str, step: int, timestamp: float, samples: list[dict]) -> int:
    """Store sample dicts for (name, step), appending after any already stored. Returns rows inserted."""
    import json as _json

    rows = []
    with _lock:
        conn = get_conn()
        start = conn.execute(
            "SELECT count(*) FROM samples WHERE run_id = ? AND name = ? AND step = ?",
            [run_id, name, step],
        ).fetchone()[0]
        for i, smp in enumerate(samples):
            r = smp.get("reward")
            reward = float(r) if isinstance(r, (int, float)) and not isinstance(r, bool) else None
            rows.append([run_id, step, timestamp, name, start + i, reward, _json.dumps(smp, default=str)])
        if rows:
            conn.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def db_get_sample_steps(run_id: str) -> list[dict]:
    """Per (name, step) sample counts, ordered by name then step."""
    with _lock:
        rows = get_conn().execute(
            "SELECT name, step, count(*), min(timestamp) FROM samples WHERE run_id = ? "
            "GROUP BY name, step ORDER BY name, step",
            [run_id],
        ).fetchall()
    return [{"name": r[0], "step": r[1], "count": r[2], "timestamp": r[3]} for r in rows]


def db_get_samples(run_id: str, name: str, step: int, limit: int = 50, offset: int = 0) -> dict:
    """One page of samples at (name, step): ``{"total", "items"}`` with each item's dict parsed."""
    import json as _json

    with _lock:
        conn = get_conn()
        total = conn.execute(
            "SELECT count(*) FROM samples WHERE run_id = ? AND name = ? AND step = ?",
            [run_id, name, step],
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT idx, data FROM samples WHERE run_id = ? AND name = ? AND step = ? "
            "ORDER BY idx LIMIT ? OFFSET ?",
            [run_id, name, step, limit, offset],
        ).fetchall()
    return {"total": total, "items": [{"idx": r[0], "sample": _json.loads(r[1])} for r in rows]}


def db_get_sample_extremes(run_id: str, n: int = 3) -> list[dict]:
    """Latest sample step per name with its top and bottom ``n`` samples by reward."""
    import json as _json

    out = []
    with _lock:
        conn = get_conn()
        latest = conn.execute(
            "SELECT name, max(step), count(DISTINCT step) FROM samples WHERE run_id = ? GROUP BY name ORDER BY name",
            [run_id],
        ).fetchall()
        for name, step, n_steps in latest:
            base = ("SELECT data FROM samples WHERE run_id = ? AND name = ? AND step = ? "
                    "AND reward IS NOT NULL ORDER BY reward ")
            top = conn.execute(base + "DESC LIMIT ?", [run_id, name, step, n]).fetchall()
            bottom = conn.execute(base + "ASC LIMIT ?", [run_id, name, step, n]).fetchall()
            out.append({
                "name": name, "latest_step": step, "n_steps": n_steps,
                "top": [_json.loads(r[0]) for r in top],
                "bottom": [_json.loads(r[0]) for r in bottom],
            })
    return out


# ---------------------------------------------------------------------------
# Run summaries (derived stats + efficiency score)
# ---------------------------------------------------------------------------

def db_compute_run_summary(run_id: str) -> dict | None:
    """Compute derived stats for a run and persist them in run_summaries.

    Safe to call for both finished and in-progress runs. Idempotent — calling
    multiple times overwrites the previous summary.
    """
    now = time.time()

    with _lock:
        conn = get_conn()

        # Run metadata
        meta = conn.execute(
            "SELECT created_at, finished_at, gpu_memory_total FROM runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        if meta is None:
            return None
        created_at, finished_at, gpu_memory_total = meta

        # Training time (wall-clock)
        total_training_time_s: float | None = None
        if created_at is not None and finished_at is not None:
            total_training_time_s = finished_at - created_at

        # Total steps + throughput from the metrics time range
        # Use MAX(step) as total_steps; use timestamp span for true throughput.
        step_row = conn.execute(
            "SELECT MAX(step), MAX(timestamp) - MIN(timestamp) FROM metrics WHERE run_id = ?",
            [run_id],
        ).fetchone()
        total_steps: int | None = int(step_row[0]) if step_row and step_row[0] is not None else None
        metric_time_span: float | None = step_row[1] if step_row else None
        steps_per_sec: float | None = None
        if total_steps and metric_time_span and metric_time_span > 0:
            steps_per_sec = total_steps / metric_time_span

        # GPU utilization / memory. If this run has per-process numbers, use only those rows (the GPUs this
        # process actually runs on, so an idle GPU 1 does not drag the average down); otherwise fall back to
        # device-wide values across every monitored GPU. Memory is summed over GPUs per poll, then maxed.
        has_proc = conn.execute(
            "SELECT COUNT(*) FROM system_snapshots WHERE run_id = ? "
            "AND (attributed_utilization IS NOT NULL OR process_memory_used IS NOT NULL)",
            [run_id],
        ).fetchone()[0] > 0
        if has_proc:
            util_row = conn.execute(
                "SELECT AVG(attributed_utilization) FROM system_snapshots WHERE run_id = ?",
                [run_id],
            ).fetchone()
            mem_row = conn.execute(
                "SELECT MAX(m) FROM (SELECT SUM(process_memory_used) AS m FROM system_snapshots "
                "WHERE run_id = ? AND process_memory_used IS NOT NULL GROUP BY timestamp)",
                [run_id],
            ).fetchone()
        else:
            util_row = conn.execute(
                "SELECT AVG(gpu_utilization) FROM system_snapshots WHERE run_id = ?",
                [run_id],
            ).fetchone()
            mem_row = conn.execute(
                "SELECT MAX(m) FROM (SELECT SUM(memory_used) AS m FROM system_snapshots "
                "WHERE run_id = ? AND memory_used IS NOT NULL GROUP BY timestamp)",
                [run_id],
            ).fetchone()
        avg_gpu_utilization: float | None = util_row[0] if util_row else None
        peak_process_memory_bytes: int | None = (
            int(mem_row[0]) if mem_row and mem_row[0] is not None else None
        )

        # Anomaly event count
        anomaly_row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'anomaly_detected'",
            [run_id],
        ).fetchone()
        anomaly_count: int = int(anomaly_row[0]) if anomaly_row else 0

        # Final metric values — last logged value per metric name
        final_rows = conn.execute(
            """
            SELECT name, value FROM (
                SELECT name, value,
                       ROW_NUMBER() OVER (PARTITION BY name ORDER BY step DESC, timestamp DESC) AS rn
                FROM metrics WHERE run_id = ?
            ) sub WHERE rn = 1
            """,
            [run_id],
        ).fetchall()
        final_metrics: dict[str, float] = {r[0]: round(r[1], 6) for r in final_rows}

        # Best metric values + the step at which they occurred
        # Loss-like names → best = min; accuracy-like names → best = max.
        agg_rows = conn.execute(
            "SELECT name, MIN(value), MAX(value) FROM metrics WHERE run_id = ? GROUP BY name",
            [run_id],
        ).fetchall()
        best_metrics: dict[str, dict] = {}
        for name, min_val, max_val in agg_rows:
            if any(k in name.lower() for k in ("loss", "error", "perplexity", "mse", "mae", "rmse")):
                best_val = min_val
                direction = "min"
            else:
                best_val = max_val
                direction = "max"
            # Find the step for the best value
            step_for_best = conn.execute(
                f"SELECT step FROM metrics WHERE run_id = ? AND name = ? "
                f"ORDER BY value {'ASC' if direction == 'min' else 'DESC'} LIMIT 1",
                [run_id, name],
            ).fetchone()
            best_metrics[name] = {
                "value": round(best_val, 6),
                "step": step_for_best[0] if step_for_best else None,
                "direction": direction,
            }

    # Efficiency score — computed outside the lock (pure Python)
    # Components:
    #   gpu_utilization: avg attributed util / 85 (capped at 1.0)
    #   stability:       1.0 − anomaly_count × 0.15 (floored at 0)
    efficiency_details: dict[str, float] = {}
    if avg_gpu_utilization is not None:
        efficiency_details["gpu_utilization"] = min(1.0, avg_gpu_utilization / 85.0)
    efficiency_details["stability"] = max(0.0, 1.0 - anomaly_count * 0.15)

    avg_eff = sum(efficiency_details.values()) / len(efficiency_details)
    if avg_eff >= 0.80:
        efficiency_score = "A"
    elif avg_eff >= 0.65:
        efficiency_score = "B"
    elif avg_eff >= 0.50:
        efficiency_score = "C"
    elif avg_eff >= 0.35:
        efficiency_score = "D"
    else:
        efficiency_score = "F"

    summary = {
        "run_id": run_id,
        "computed_at": now,
        "total_training_time_s": total_training_time_s,
        "total_steps": total_steps,
        "steps_per_sec": round(steps_per_sec, 2) if steps_per_sec else None,
        "avg_gpu_utilization": round(avg_gpu_utilization, 1) if avg_gpu_utilization is not None else None,
        "peak_process_memory_bytes": peak_process_memory_bytes,
        "anomaly_count": anomaly_count,
        "final_metrics": final_metrics,
        "best_metrics": best_metrics,
        "efficiency_score": efficiency_score,
        "efficiency_details": {k: round(v, 3) for k, v in efficiency_details.items()},
    }

    # Upsert
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM run_summaries WHERE run_id = ?", [run_id])
        conn.execute(
            "INSERT INTO run_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                now,
                total_training_time_s,
                total_steps,
                steps_per_sec,
                avg_gpu_utilization,
                peak_process_memory_bytes,
                anomaly_count,
                json.dumps(final_metrics),
                json.dumps(best_metrics),
                efficiency_score,
                json.dumps({k: round(v, 3) for k, v in efficiency_details.items()}),
            ],
        )

    return summary


def db_get_run_summary(run_id: str) -> dict | None:
    """Return the stored run summary, or None if not yet computed."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            """
            SELECT run_id, computed_at, total_training_time_s, total_steps, steps_per_sec,
                   avg_gpu_utilization, peak_process_memory_bytes, anomaly_count,
                   final_metrics, best_metrics, efficiency_score, efficiency_details
            FROM run_summaries WHERE run_id = ?
            """,
            [run_id],
        ).fetchone()
    if row is None:
        return None
    cols = [
        "run_id", "computed_at", "total_training_time_s", "total_steps", "steps_per_sec",
        "avg_gpu_utilization", "peak_process_memory_bytes", "anomaly_count",
        "final_metrics", "best_metrics", "efficiency_score", "efficiency_details",
    ]
    d = dict(zip(cols, row))
    d["final_metrics"] = _parse_json(d["final_metrics"]) or {}
    d["best_metrics"] = _parse_json(d["best_metrics"]) or {}
    d["efficiency_details"] = _parse_json(d["efficiency_details"]) or {}
    return d


# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------

_ALERT_COLS = "id, name, kind, url, format, project, min_severity, metric, op, threshold, enabled, created_at, created_by"


def _row_to_alert_rule(r: tuple) -> dict:
    return {
        "id": r[0], "name": r[1], "kind": r[2], "url": r[3], "format": r[4], "project": r[5],
        "min_severity": r[6], "metric": r[7], "op": r[8], "threshold": r[9],
        "enabled": bool(r[10]), "created_at": r[11], "created_by": r[12],
    }


def db_list_alert_rules() -> list[dict]:
    """All alert rules, including the (secret) webhook ``url``. Callers must mask it."""
    with _lock:
        rows = get_conn().execute(f"SELECT {_ALERT_COLS} FROM alert_rules ORDER BY created_at").fetchall()
    return [_row_to_alert_rule(r) for r in rows]


def db_get_alert_rule(rule_id: str) -> dict | None:
    with _lock:
        row = get_conn().execute(f"SELECT {_ALERT_COLS} FROM alert_rules WHERE id = ?", [rule_id]).fetchone()
    return _row_to_alert_rule(row) if row else None


def db_create_alert_rule(rule: dict, created_by: str) -> dict:
    rule_id = uuid.uuid4().hex[:12]
    with _lock:
        get_conn().execute(
            f"INSERT INTO alert_rules ({_ALERT_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [rule_id, rule["name"], rule["kind"], rule["url"], rule["format"], rule.get("project"),
             rule.get("min_severity"), rule.get("metric"), rule.get("op"), rule.get("threshold"),
             rule.get("enabled", True), time.time(), created_by],
        )
    return db_get_alert_rule(rule_id)


def db_update_alert_rule(rule_id: str, fields: dict) -> bool:
    """Update the given columns (a subset of the rule's fields). False if the rule does not exist."""
    allowed = {"name", "url", "format", "project", "min_severity", "metric", "op", "threshold", "enabled"}
    cols = [c for c in fields if c in allowed]
    with _lock:
        conn = get_conn()
        if conn.execute("SELECT 1 FROM alert_rules WHERE id = ?", [rule_id]).fetchone() is None:
            return False
        if cols:
            conn.execute(
                f"UPDATE alert_rules SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                [fields[c] for c in cols] + [rule_id],
            )
    return True


def db_delete_alert_rule(rule_id: str) -> bool:
    with _lock:
        row = get_conn().execute("DELETE FROM alert_rules WHERE id = ? RETURNING id", [rule_id]).fetchone()
    return row is not None


def db_matching_alert_rules(kinds: list[str], project: str | None, metrics: list[str] | None = None) -> list[dict]:
    """Enabled rules of the given kinds that apply to ``project`` (unscoped rules apply everywhere).

    With ``metrics``, ``metric_threshold`` rules are narrowed to those metric names.
    """
    sql = f"SELECT {_ALERT_COLS} FROM alert_rules WHERE enabled AND kind IN ({', '.join('?' * len(kinds))}) AND (project IS NULL OR project = ?)"
    params: list = [*kinds, project]
    if metrics is not None:
        sql += f" AND metric IN ({', '.join('?' * len(metrics))})"
        params += metrics
    with _lock:
        rows = get_conn().execute(sql, params).fetchall()
    return [_row_to_alert_rule(r) for r in rows]


def db_get_run_for_alert(run_id: str) -> dict | None:
    """Just what an alert may reveal about a run: id, name, project."""
    with _lock:
        row = get_conn().execute("SELECT run_id, name, project FROM runs WHERE run_id = ?", [run_id]).fetchone()
    return {"run_id": row[0], "name": row[1] or row[0], "project": row[2]} if row else None
