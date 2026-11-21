"""DuckDB access layer — all queries live here, never in route handlers."""

import base64
import binascii
import datetime
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from urllib.parse import quote as _urlquote
from collections.abc import Callable
from pathlib import Path

import duckdb

_DB_PATH = os.environ.get(
    "PYROMETER_DB_PATH",
    str(Path.home() / ".pyrometer" / "data.duckdb"),
)

_lock = threading.RLock()  # re-entrant: ingest holds it across claim-batch + insert
_conn: duckdb.DuckDBPyConnection | None = None


def get_conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = duckdb.connect(_DB_PATH)
        _init_schema(_conn)
    return _conn


def get_lock() -> threading.RLock:
    return _lock


def media_root() -> Path:
    """Directory holding image/figure payloads: ``<data dir>/media`` (``PYROMETER_MEDIA_DIR`` overrides)."""
    override = os.environ.get("PYROMETER_MEDIA_DIR")
    return Path(override) if override else Path(_DB_PATH).parent / "media"


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
    # Organisation: last activity (any ingest / status / metadata change), sweep group, archive stamp.
    conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS updated_at DOUBLE")
    conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS run_group VARCHAR")
    conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS archived_at DOUBLE")
    conn.execute("UPDATE runs SET project = 'default' WHERE project IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            name       VARCHAR PRIMARY KEY,
            visibility VARCHAR NOT NULL,
            created_at DOUBLE,
            created_by VARCHAR
        )
    """)
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at DOUBLE")
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS retention JSON")  # NULL = keep everything
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_members (
            project  VARCHAR,
            username VARCHAR,
            PRIMARY KEY (project, username)
        )
    """)
    conn.execute(
        "INSERT INTO projects (name, visibility, created_at, created_by) VALUES ('default', 'public', ?, 'system') "
        "ON CONFLICT DO NOTHING",
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
    # Image/figure payloads live in files under media_root(); ``path`` is relative to it and ``data`` NULL.
    conn.execute("ALTER TABLE media ADD COLUMN IF NOT EXISTS path VARCHAR")

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
    # seen_batches rows live as long as their run (deleted with it): a replay of any age stays a no-op.

    # Webhook alert rules (admin-managed). ``url`` is a secret: never returned by list/get helpers' callers unmasked.
    # A rule has one or more triggers (``triggers`` JSON list). The flat kind/min_severity/metric/op/threshold
    # columns are the pre-multi-trigger layout: migrated into ``triggers`` below, unused afterwards.
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
    conn.execute("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS triggers JSON")
    conn.execute("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS updated_at DOUBLE")
    conn.execute("UPDATE alert_rules SET updated_at = created_at WHERE updated_at IS NULL")
    for rid, kind, sev, metric, op, thr in conn.execute(
        "SELECT id, kind, min_severity, metric, op, threshold FROM alert_rules WHERE triggers IS NULL"
    ).fetchall():
        legacy = {"kind": kind, "min_severity": sev, "metric": metric, "op": op, "threshold": thr}
        conn.execute("UPDATE alert_rules SET triggers = ? WHERE id = ?", [json.dumps([legacy]), rid])

    # Alert delivery state, persisted so a restart neither re-fires nor forgets: when each alert was sent
    # (dedupe window + per-minute rate limit) and which runs are currently past a metric threshold.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_sent (
            rule_id VARCHAR,
            dkey    VARCHAR,
            sent_at DOUBLE
        )
    """)
    conn.execute("ALTER TABLE alert_sent ADD COLUMN IF NOT EXISTS run_id VARCHAR")  # NULL for rows from before
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_violations (
            rule_id     VARCHAR,
            trigger_key VARCHAR,
            run_id      VARCHAR,
            violating   BOOLEAN,
            PRIMARY KEY (rule_id, trigger_key, run_id)
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

    # Per-user pinned runs (dashboard users only).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pinned_runs (
            username  VARCHAR,
            run_id    VARCHAR,
            pinned_at DOUBLE,
            PRIMARY KEY (username, run_id)
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, done_at DOUBLE)")

    _backfill_updated_at(conn)
    _migrate_legacy_running(conn)
    _repair_duplicates(conn)
    _migrate_media_to_files(conn)


# Every table with a ``timestamp`` column per run: the evidence a run was alive at some moment.
_ACTIVITY_TABLES = (
    "metrics", "system_snapshots", "events", "logs", "param_stats", "profile_events",
    "artifacts", "media", "samples",
)


def _migrate_legacy_running(conn: duckdb.DuckDBPyConnection) -> None:
    """One-time cleanup of runs that predate heartbeats and are still marked ``running``.

    The server never auto-crashes a run without ``last_heartbeat``, so a run whose process died
    before heartbeats existed stays ``running`` forever. Find the newest data of each such run
    (any table); if that is older than the heartbeat timeout the run is marked ``crashed``
    (``finished_at`` = last data seen). Runs with recent data keep ``running`` and get their last
    data time as ``last_heartbeat``, so the normal sweep takes over from there. No alerts are sent
    for these transitions (they are history, not news). Idempotent: afterwards no running run has a
    NULL heartbeat.
    """
    ids = [r[0] for r in conn.execute(
        "SELECT run_id FROM runs WHERE status = 'running' AND last_heartbeat IS NULL"
    ).fetchall()]
    if not ids:
        return
    created = dict(conn.execute("SELECT run_id, created_at FROM runs WHERE run_id IN (SELECT unnest(?::VARCHAR[]))", [ids]).fetchall())
    last = {i: created.get(i) or 0.0 for i in ids}
    for table in _ACTIVITY_TABLES:
        try:
            rows = conn.execute(
                f"SELECT run_id, max(timestamp) FROM {table} WHERE run_id IN (SELECT unnest(?::VARCHAR[])) GROUP BY run_id",
                [ids],
            ).fetchall()
        except Exception:  # table without the expected shape: ignore as evidence
            continue
        for run_id, ts in rows:
            if ts is not None and ts > last[run_id]:
                last[run_id] = ts
    cutoff = time.time() - heartbeat_timeout()
    for run_id, ts in last.items():
        if ts < cutoff:
            conn.execute(
                "UPDATE runs SET status = 'crashed', finished_at = ?, last_heartbeat = ? WHERE run_id = ?",
                [ts, ts, run_id],
            )
        else:
            conn.execute("UPDATE runs SET last_heartbeat = ? WHERE run_id = ?", [ts, run_id])


def _backfill_updated_at(conn: duckdb.DuckDBPyConnection) -> None:
    """Give runs without ``updated_at`` (databases from before it existed) their newest data timestamp."""
    ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE updated_at IS NULL").fetchall()]
    if not ids:
        return
    conn.execute(
        "UPDATE runs SET updated_at = GREATEST(COALESCE(created_at, 0), COALESCE(finished_at, 0), "
        "COALESCE(last_heartbeat, 0)) WHERE run_id IN (SELECT unnest(?::VARCHAR[]))",
        [ids],
    )
    for table in _ACTIVITY_TABLES:
        conn.execute(
            f"UPDATE runs SET updated_at = t.m FROM (SELECT run_id, max(timestamp) AS m FROM {table} "
            "WHERE run_id IN (SELECT unnest(?::VARCHAR[])) GROUP BY run_id) t "
            "WHERE runs.run_id = t.run_id AND t.m > runs.updated_at",
            [ids],
        )


def _meta_done(conn: duckdb.DuckDBPyConnection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM schema_meta WHERE key = ?", [key]).fetchone() is not None


def _meta_mark(conn: duckdb.DuckDBPyConnection, key: str) -> None:
    conn.execute("INSERT INTO schema_meta VALUES (?, ?) ON CONFLICT DO NOTHING", [key, time.time()])


def _repair_duplicates(conn: duckdb.DuckDBPyConnection) -> None:
    """One-time: keep only the newest row (by client timestamp) per key where legacy data has duplicates.

    Before last-write-wins existed, a resumed run or a replayed spool could store the same
    (run, name, step) twice in ``metrics`` / ``param_stats`` / ``media``. Writes dedupe now, so this
    runs once (recorded in ``schema_meta``) and a clean table costs one aggregate scan.
    """
    if _meta_done(conn, "dedupe_v1"):
        return
    for table, key in (("metrics", "name"), ("param_stats", "param_name"), ("media", "name")):
        dup = conn.execute(
            f"SELECT 1 FROM (SELECT 1 FROM {table} GROUP BY run_id, {key}, step HAVING count(*) > 1 LIMIT 1)"
        ).fetchone()
        if dup is None:
            continue
        conn.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM (SELECT rowid, row_number() OVER "
            f"(PARTITION BY run_id, {key}, step ORDER BY timestamp DESC, rowid DESC) AS rn FROM {table}) "
            "WHERE rn > 1)"
        )
    _meta_mark(conn, "dedupe_v1")


MEDIA_FILE_TYPES = ("image", "figure")


def _media_relpath(run_id: str, name: str, step: int) -> str:
    return f"{run_id}/{hashlib.sha256(name.encode()).hexdigest()[:16]}-{step}.png"


def _write_media_file(relpath: str, raw: bytes) -> None:
    dest = media_root() / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, dest)


def _decode_b64(data: str) -> bytes | None:
    try:
        return base64.b64decode(data, validate=True) if data else None
    except (binascii.Error, ValueError):
        return None


def _migrate_media_to_files(conn: duckdb.DuckDBPyConnection) -> None:
    """Move base64 image/figure payloads still stored in the table into files (batches, idempotent).

    Rows whose payload is not valid base64 stay inline. DuckDB reuses the freed space but the database
    file only shrinks after a checkpoint/compaction.
    """
    skipped: list[int] = [-1]
    while True:
        rows = conn.execute(
            "SELECT rowid, run_id, name, step, data FROM media "
            "WHERE path IS NULL AND data IS NOT NULL AND media_type IN ('image', 'figure') "
            "AND rowid NOT IN (SELECT unnest(?::BIGINT[])) LIMIT 50",
            [skipped],
        ).fetchall()
        moved = 0
        for rowid, run_id, name, step, data in rows:
            raw = _decode_b64(data)
            if raw is None:
                skipped.append(rowid)
                continue
            rel = _media_relpath(run_id, name, step)
            _write_media_file(rel, raw)
            conn.execute("UPDATE media SET path = ?, data = NULL WHERE rowid = ?", [rel, rowid])
            moved += 1
        if not rows:
            return


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


_LIST_COLS = (
    "r.run_id, r.name, r.status, r.created_at, r.finished_at, r.hyperparams, r.gpu_model, r.tags, "
    "r.project, r.last_heartbeat, r.updated_at, r.run_group, r.archived_at, p.archived_at"
)


def _row_to_run_summary(row: tuple) -> dict:
    return {
        "project": row[8],
        "last_heartbeat": _ts_to_iso(row[9]),
        "run_id": row[0],
        "name": row[1],
        "status": row[2],
        "created_at": _ts_to_iso(row[3]),
        "finished_at": _ts_to_iso(row[4]),
        "updated_at": _ts_to_iso(row[10] if row[10] is not None else row[3]),
        "hyperparams": _parse_json(row[5]) or {},
        "gpu_model": row[6],
        "tags": _parse_json(row[7]) or [],
        "group": row[11],
        "archived": row[12] is not None,
        "project_archived": row[13] is not None,
        "pinned": False,  # filled in per user by the caller
    }


_DETAIL_COLS = [
    "run_id", "name", "status", "created_at", "finished_at",
    "hyperparams", "git_hash", "git_dirty", "gpu_model", "gpu_memory_total",
    "cuda_version", "pytorch_version", "python_version", "hostname", "tags", "notes",
    "project", "last_heartbeat", "updated_at", "run_group", "archived_at",
]


def _row_to_run_detail(row: tuple) -> dict:
    d = dict(zip(_DETAIL_COLS, row))
    d["hyperparams"] = _parse_json(d["hyperparams"]) or {}
    d["tags"] = _parse_json(d["tags"]) or []
    d["created_at"] = _ts_to_iso(d["created_at"])
    d["finished_at"] = _ts_to_iso(d["finished_at"])
    d["last_heartbeat"] = _ts_to_iso(d["last_heartbeat"])
    d["updated_at"] = _ts_to_iso(d["updated_at"] if d["updated_at"] is not None else row[3])
    d["group"] = d.pop("run_group")
    d["archived"] = d.pop("archived_at") is not None
    d["pinned"] = False
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
    group: str | None = None,
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
                hostname, tags, notes, project, last_heartbeat, updated_at, run_group
            ) VALUES (
                ?, ?, 'running', ?, NULL,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                time.time(),  # updated_at
                group,
            ],
        )

    return {
        "run_id": run_id, "name": resolved_name, "status": "running",
        "created_at": _ts_to_iso(now), "project": project, "group": group,
    }


RUN_SORTS = ("updated", "created", "name")


def db_list_runs(
    projects: list[str] | None = None,
    sort: str = "updated",
    include_archived: bool = False,
    group: str | None = None,
    project: str | None = None,
    status: str | None = None,
    pinned_by: str | None = None,
    run_ids: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Runs the caller may see; see ``db_list_runs_page`` (this drops the ``total``)."""
    return db_list_runs_page(projects, sort, include_archived, group, project, status, pinned_by, run_ids, limit, offset)[0]


def db_list_runs_page(
    projects: list[str] | None = None,
    sort: str = "updated",
    include_archived: bool = False,
    group: str | None = None,
    project: str | None = None,
    status: str | None = None,
    pinned_by: str | None = None,
    run_ids: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """``(runs, total)``: runs the caller may see (``projects``: ``None`` = no restriction), newest activity first.

    ``total`` counts every match, ignoring ``limit``/``offset``. ``limit=None`` returns all of them. Every
    sort ends in ``run_id`` so ties page stably.

    ``sort``: ``updated`` (``updated_at`` desc, the default), ``created`` (desc) or ``name`` (a-z).
    Archived runs, and runs of archived projects, are hidden unless ``include_archived``.
    ``pinned_by`` fills each run's ``pinned`` flag for that user; ``run_ids`` restricts to those runs.
    """
    db_mark_stale_runs()
    clauses: list[str] = []
    params: list = []
    if projects is not None:
        if not projects:
            return [], 0
        clauses.append(f"r.project IN ({', '.join('?' * len(projects))})")
        params += projects
    if not include_archived:
        clauses.append("r.archived_at IS NULL AND p.archived_at IS NULL")
    if group is not None:
        clauses.append("r.run_group = ?")
        params.append(group)
    if project is not None:
        clauses.append("r.project = ?")
        params.append(project)
    if status is not None:
        clauses.append("r.status = ?")
        params.append(status)
    if run_ids is not None:
        if not run_ids:
            return [], 0
        clauses.append("r.run_id IN (SELECT unnest(?::VARCHAR[]))")
        params.append(run_ids)
    order = {
        "created": "r.created_at DESC, r.run_id",
        "name": "lower(r.name) ASC, r.created_at DESC, r.run_id",
    }.get(sort, "COALESCE(r.updated_at, r.created_at) DESC, r.created_at DESC, r.run_id")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    page = ""
    if limit is not None:
        page = f" LIMIT {int(limit)} OFFSET {max(int(offset), 0)}"
    elif offset:
        page = f" LIMIT 9223372036854775807 OFFSET {max(int(offset), 0)}"
    frm = "FROM runs r LEFT JOIN projects p ON p.name = r.project"
    with _lock:
        total = get_conn().execute(f"SELECT count(*) {frm} {where}", params).fetchone()[0]
        rows = get_conn().execute(f"SELECT {_LIST_COLS} {frm} {where} ORDER BY {order}{page}", params).fetchall()
        pinned = (
            {r[0] for r in get_conn().execute("SELECT run_id FROM pinned_runs WHERE username = ?", [pinned_by]).fetchall()}
            if pinned_by else set()
        )
    out = [_row_to_run_summary(r) for r in rows]
    for run in out:
        run["pinned"] = run["run_id"] in pinned
    return out, int(total)


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
        "archived": row[5] is not None if len(row) > 5 else False,
        "archived_at": _ts_to_iso(row[5]) if len(row) > 5 else None,
        "last_activity": _ts_to_iso(row[6]) if len(row) > 6 else None,
        "archived_runs": row[7] if len(row) > 7 else 0,
    }


def db_get_project(name: str) -> dict | None:
    """One project (with its ``retention`` policy, which the list omits)."""
    with _lock:
        row = get_conn().execute(
            "SELECT p.name, p.visibility, p.created_at, p.created_by, "
            "(SELECT count(*) FROM runs r WHERE r.project = p.name), p.archived_at, "
            "(SELECT max(r.updated_at) FROM runs r WHERE r.project = p.name AND r.archived_at IS NULL), "
            "(SELECT count(*) FROM runs r WHERE r.project = p.name AND r.archived_at IS NOT NULL), p.retention "
            "FROM projects p WHERE p.name = ?",
            [name],
        ).fetchone()
    if not row:
        return None
    return {**_row_to_project(row[:8]), "retention": _parse_json(row[8])}


def db_list_projects(
    names: list[str] | None = None, sort: str = "activity", include_archived: bool = False
) -> list[dict]:
    """Projects with run counts and ``last_activity``; ``names`` restricts the result (``None`` = all).

    ``sort``: ``activity`` (newest run activity first; projects without runs fall back to their
    creation time) or ``name``. Archived projects are hidden unless ``include_archived``.
    """
    clauses: list[str] = []
    params: list = []
    if names is not None:
        if not names:
            return []
        clauses.append(f"p.name IN ({', '.join('?' * len(names))})")
        params += names
    if not include_archived:
        clauses.append("p.archived_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "p.name" if sort == "name" else "COALESCE(max(CASE WHEN r.archived_at IS NULL THEN r.updated_at END), p.created_at) DESC, p.name"
    with _lock:
        rows = get_conn().execute(
            f"""
            SELECT p.name, p.visibility, p.created_at, p.created_by, COUNT(r.run_id), p.archived_at,
                   max(CASE WHEN r.archived_at IS NULL THEN r.updated_at END),
                   COUNT(CASE WHEN r.archived_at IS NOT NULL THEN 1 END)
            FROM projects p LEFT JOIN runs r ON r.project = p.name
            {where}
            GROUP BY p.name, p.visibility, p.created_at, p.created_by, p.archived_at
            ORDER BY {order}
            """,
            params,
        ).fetchall()
    return [_row_to_project(r) for r in rows]


def db_create_project(name: str, visibility: str, created_by: str) -> dict | None:
    """Create a project; returns ``None`` if the name is taken."""
    with _lock:
        conn = get_conn()
        inserted = conn.execute(
            "INSERT INTO projects (name, visibility, created_at, created_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING RETURNING name",
            [name, visibility, time.time(), created_by],
        ).fetchone()
    return db_get_project(name) if inserted else None


def db_set_project_visibility(name: str, visibility: str) -> bool:
    with _lock:
        row = get_conn().execute(
            "UPDATE projects SET visibility = ? WHERE name = ? RETURNING name", [visibility, name]
        ).fetchone()
    return row is not None


def db_set_project_archived(name: str, archived: bool) -> bool:
    with _lock:
        row = get_conn().execute(
            "UPDATE projects SET archived_at = ? WHERE name = ? RETURNING name",
            [time.time() if archived else None, name],
        ).fetchone()
    return row is not None


def db_delete_project(name: str) -> dict:
    """Remove a project row, its membership and the alert rules scoped to it (its runs must be gone
    already; see ``db_delete_run``). Returns ``{"alert_rules": n}`` removed."""
    with _lock:
        conn = get_conn()
        rule_ids = [r[0] for r in conn.execute("SELECT id FROM alert_rules WHERE project = ?", [name]).fetchall()]
        for rid in rule_ids:
            db_delete_alert_rule(rid)
        conn.execute("DELETE FROM project_members WHERE project = ?", [name])
        conn.execute("DELETE FROM projects WHERE name = ?", [name])
    return {"alert_rules": len(rule_ids)}


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
        row = conn.execute(
            f"SELECT {', '.join(_DETAIL_COLS)} FROM runs WHERE run_id = ?", [run_id]
        ).fetchone()
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
            UPDATE runs SET status = 'crashed', finished_at = last_heartbeat, updated_at = ?
            WHERE status = 'running' AND last_heartbeat IS NOT NULL AND last_heartbeat < ?
            RETURNING run_id
            """,
            [time.time(), now - heartbeat_timeout()],
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
                updated_at = CASE WHEN status = 'crashed' THEN ? ELSE updated_at END,
                status = CASE WHEN status = 'crashed' THEN 'running' ELSE status END,
                finished_at = CASE WHEN status = 'crashed' THEN NULL ELSE finished_at END
            WHERE run_id = ?
            """,
            [time.time(), time.time(), run_id],
        )


def db_resume_run(run_id: str) -> dict:
    """Reattach to an existing run: status back to ``running``, heartbeat refreshed.

    Original name, hyperparams and tags are kept. Returns what the SDK needs to continue:
    ``last_step`` (highest logged step, ``None`` if nothing was logged), ``previous_status``.
    """
    with _lock:
        conn = get_conn()
        prev, archived_at = conn.execute("SELECT status, archived_at FROM runs WHERE run_id = ?", [run_id]).fetchone()
        conn.execute(
            "UPDATE runs SET status = 'running', finished_at = NULL, last_heartbeat = ?, updated_at = ?, archived_at = NULL "
            "WHERE run_id = ?",
            [time.time(), time.time(), run_id],
        )
        last_step = conn.execute("SELECT MAX(step) FROM metrics WHERE run_id = ?", [run_id]).fetchone()[0]
    run = db_get_run(run_id)
    return {
        **{k: run[k] for k in ("run_id", "name", "status", "created_at", "project", "hyperparams", "tags")},
        "resumed": True,
        "previous_status": prev,
        "unarchived": archived_at is not None,
        "last_step": last_step,
    }


# Hardware/environment columns the SDK may fill in after creating the run (see PATCH /runs/{id}).
RUN_INFO_FIELDS = (
    "git_hash", "git_dirty", "gpu_model", "gpu_memory_total", "cuda_version",
    "pytorch_version", "python_version", "hostname",
)


RUN_META_FIELDS = ("name", "tags", "run_group")  # organisation fields (rename / retag / regroup)


def db_touch_run(run_id: str, ts: float | None = None) -> None:
    """Stamp ``updated_at`` (called on every successful ingest)."""
    with _lock:
        get_conn().execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", [ts or time.time(), run_id])


def db_update_run(
    run_id: str, status: str | None, notes: str | None, info: dict | None = None, meta: dict | None = None
) -> None:
    """Update status / notes / hardware info / name, tags, group. Setting ``running`` also counts as a
    heartbeat, so a run revived by a status PATCH (or a resume) is not immediately swept back to
    ``crashed``. Any change bumps ``updated_at``. ``meta`` keys: ``name``, ``tags`` (list), ``run_group``
    (``None`` clears)."""
    with _lock:
        conn = get_conn()
        now = time.time()
        changed = False
        if status is not None:
            changed = True
            finished_at = now if status in TERMINAL_STATUSES else None
            if status == "running":
                conn.execute(
                    "UPDATE runs SET status = ?, finished_at = NULL, last_heartbeat = ? WHERE run_id = ?",
                    [status, now, run_id],
                )
            else:
                conn.execute(
                    "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                    [status, finished_at, run_id],
                )
        if notes is not None:
            changed = True
            conn.execute("UPDATE runs SET notes = ? WHERE run_id = ?", [notes, run_id])
        for col, val in (info or {}).items():
            if col in RUN_INFO_FIELDS and val is not None:  # whitelist: the column name is interpolated
                changed = True
                conn.execute(f"UPDATE runs SET {col} = ? WHERE run_id = ?", [val, run_id])
        for col, val in (meta or {}).items():
            if col in RUN_META_FIELDS:
                changed = True
                conn.execute(
                    f"UPDATE runs SET {col} = ? WHERE run_id = ?",
                    [json.dumps(val) if col == "tags" else val, run_id],
                )
        if changed:
            conn.execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", [now, run_id])


def db_set_run_archived(run_id: str, archived: bool) -> bool:
    """Archive/unarchive a run. ``False`` if it does not exist. Archiving also bumps ``updated_at``."""
    with _lock:
        row = get_conn().execute(
            "UPDATE runs SET archived_at = ?, updated_at = ? WHERE run_id = ? RETURNING run_id",
            [time.time() if archived else None, time.time(), run_id],
        ).fetchone()
    return row is not None


def db_move_run(run_id: str, project: str) -> bool:
    with _lock:
        row = get_conn().execute(
            "UPDATE runs SET project = ?, updated_at = ? WHERE run_id = ? RETURNING run_id",
            [project, time.time(), run_id],
        ).fetchone()
    return row is not None


# Every table with a ``run_id`` column except ``runs`` itself: everything a hard delete must clear.
RUN_CHILD_TABLES = (
    "metrics", "profile_events", "system_snapshots", "param_stats", "events", "logs", "artifacts",
    "media", "samples", "seen_batches", "run_summaries", "alert_violations", "alert_sent", "pinned_runs",
)


def _remove_media_dir(run_id: str) -> None:
    if run_id and "/" not in run_id and "\\" not in run_id and run_id not in (".", ".."):
        shutil.rmtree(media_root() / run_id, ignore_errors=True)


def db_delete_run(run_id: str) -> bool:
    """Hard-delete a run: its row, every child table (incl. seen batch ids, samples, summaries, alert
    state, pins) and its media files on disk. ``False`` if it did not exist."""
    with _lock:
        conn = get_conn()
        exists = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", [run_id]).fetchone() is not None
        for table in RUN_CHILD_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE run_id = ?", [run_id])
        conn.execute("DELETE FROM runs WHERE run_id = ?", [run_id])
        _remove_media_dir(run_id)
    return exists


def db_list_run_ids(project: str) -> list[str]:
    with _lock:
        return [r[0] for r in get_conn().execute("SELECT run_id FROM runs WHERE project = ?", [project]).fetchall()]


def db_list_groups(project: str, include_archived: bool = False) -> list[dict]:
    """Groups of a project (``init(group=...)``), most recently active first."""
    where = "" if include_archived else " AND archived_at IS NULL"
    with _lock:
        rows = get_conn().execute(
            "SELECT run_group, count(*), max(COALESCE(updated_at, created_at)), min(created_at), "
            "count(CASE WHEN status = 'running' THEN 1 END) FROM runs "
            f"WHERE project = ? AND run_group IS NOT NULL{where} GROUP BY run_group "
            "ORDER BY max(COALESCE(updated_at, created_at)) DESC, run_group",
            [project],
        ).fetchall()
    return [
        {"group": r[0], "runs": r[1], "last_activity": _ts_to_iso(r[2]), "first_created": _ts_to_iso(r[3]), "running": r[4]}
        for r in rows
    ]


# --- pins ------------------------------------------------------------------

def db_pin_run(username: str, run_id: str) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO pinned_runs VALUES (?, ?, ?) ON CONFLICT DO NOTHING", [username, run_id, time.time()]
        )


def db_unpin_run(username: str, run_id: str) -> None:
    with _lock:
        get_conn().execute("DELETE FROM pinned_runs WHERE username = ? AND run_id = ?", [username, run_id])


def db_pinned_run_ids(username: str) -> list[str]:
    """The user's pinned run ids, most recently pinned first."""
    with _lock:
        rows = get_conn().execute(
            "SELECT run_id FROM pinned_runs WHERE username = ? ORDER BY pinned_at DESC", [username]
        ).fetchall()
    return [r[0] for r in rows]


def db_is_pinned(username: str, run_id: str) -> bool:
    with _lock:
        return get_conn().execute(
            "SELECT 1 FROM pinned_runs WHERE username = ? AND run_id = ?", [username, run_id]
        ).fetchone() is not None


# ---------------------------------------------------------------------------
# Read-side downsampling (min/max/first/last per bucket, so spikes survive)
# ---------------------------------------------------------------------------

MIN_POINTS, MAX_POINTS_CAP = 8, 100_000


def clamp_max_points(n: int) -> int:
    return max(MIN_POINTS, min(int(n), MAX_POINTS_CAP))


def _range_where(
    axis_step: bool, step_min: int | None, step_max: int | None, t_min: float | None, t_max: float | None
) -> tuple[str, list]:
    """``AND ...`` fragment limiting rows to the requested step and/or time window."""
    sql, params = "", []
    for cond, col, val in (
        (axis_step and step_min is not None, "step >= ?", step_min),
        (axis_step and step_max is not None, "step <= ?", step_max),
        (t_min is not None, "timestamp >= ?", t_min),
        (t_max is not None, "timestamp <= ?", t_max),
    ):
        if cond:
            sql += f" AND {col}"
            params.append(val)
    return sql, params


def _query_series(
    conn, table: str, cols: list[str], where: str, params: list, part: str, axis: str,
    orderings: list[str], order_by: str, max_points: int | None,
) -> tuple[list[tuple], int]:
    """Rows of ``table`` matching ``where``; with ``max_points``, at most about that many per series.

    A series (``part`` expression, e.g. the metric name) with <= ``max_points`` rows is returned whole.
    A longer one is cut into ``max_points // len(orderings)`` equal-width buckets along ``axis`` and from each
    bucket the rows winning each of ``orderings`` (e.g. ``value DESC`` = the bucket's peak) are kept, so a
    one-step spike is never averaged away. Everything is one windowed scan in SQL. Returns
    ``(rows, total_rows_before_downsampling)``; ``cols`` are plain column names or ``expr AS alias``.
    """
    names = [c.split(" AS ")[-1] for c in cols]
    total = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]
    if max_points is None or total <= max_points:
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY {order_by}", params).fetchall()
        return rows, total
    nb = max(1, max_points // len(orderings))
    ranks = ", ".join(f"row_number() OVER (PARTITION BY _p, _b ORDER BY {o}) AS _r{i}" for i, o in enumerate(orderings))
    keep = " OR ".join(f"_r{i} = 1" for i in range(len(orderings)))
    sql = f"""
        WITH base AS (
            SELECT {', '.join(cols)}, {part} AS _p, {axis} AS _x FROM {table} WHERE {where}
        ), win AS (
            SELECT *, count(*) OVER (PARTITION BY _p) AS _c,
                   min(_x) OVER (PARTITION BY _p) AS _lo, max(_x) OVER (PARTITION BY _p) AS _hi
            FROM base
        ), bk AS (
            SELECT *, CASE WHEN _hi > _lo
                THEN least({nb - 1}, CAST(floor((_x - _lo)::DOUBLE / (_hi - _lo) * {nb}) AS INTEGER)) ELSE 0 END AS _b
            FROM win
        ), rk AS (SELECT *, {ranks} FROM bk)
        SELECT {', '.join(names)} FROM rk WHERE _c <= {int(max_points)} OR {keep} ORDER BY {order_by}
    """
    return conn.execute(sql, params).fetchall(), total


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


def _replace_newer(conn, table: str, run_id: str, name_col: str, rows: dict[tuple[str, int], tuple], ts_pos: int) -> list[tuple]:
    """Last write wins per (run, ``name_col``, step), judged by the client timestamp.

    ``rows`` maps (name, step) to the row tuple to insert (its timestamp is at ``ts_pos``). An existing
    row with the same key is replaced only if the incoming timestamp is >= its own, so a very late replay
    cannot overwrite newer values. Returns the rows to insert (those not beaten by a newer stored row)
    after deleting the superseded ones (``table``/``name_col`` are constants, never user input).

    Skipped, via DuckDB's per-row-group min/max statistics, when the run has nothing at or after the
    batch's first step: the normal case for a run that only moves forward.
    """
    if not rows:
        return []
    first = min(k[1] for k in rows)
    if conn.execute(f"SELECT 1 FROM {table} WHERE run_id = ? AND step >= ? LIMIT 1", [run_id, first]).fetchone() is None:
        return list(rows.values())
    keys = list(rows)
    existing = conn.execute(
        f"SELECT {name_col}, step, max(timestamp) FROM {table} WHERE run_id = ? AND ({name_col}, step) IN "
        "(SELECT n, s FROM (SELECT unnest(?::VARCHAR[]) AS n, unnest(?::INTEGER[]) AS s)) GROUP BY 1, 2",
        [run_id, [k[0] for k in keys], [k[1] for k in keys]],
    ).fetchall()
    newest = {(n, st): ts for n, st, ts in existing}
    keep, drop = [], []
    for key, row in rows.items():
        old = newest.get(key)
        if old is None:
            keep.append(row)
        elif row[ts_pos] >= old:
            keep.append(row)
            drop.append(key)
    if drop:
        conn.execute(
            f"DELETE FROM {table} WHERE run_id = ? AND ({name_col}, step) IN "
            "(SELECT n, s FROM (SELECT unnest(?::VARCHAR[]) AS n, unnest(?::INTEGER[]) AS s))",
            [run_id, [k[0] for k in drop], [k[1] for k in drop]],
        )
    return keep


def _newest_per_key(rows: list[tuple], key_of, ts_of) -> dict:
    """Collapse a batch to one row per key: the highest timestamp wins, arrival order breaks ties."""
    out: dict = {}
    for r in rows:
        k = key_of(r)
        if k not in out or ts_of(r) >= ts_of(out[k]):
            out[k] = r
    return out


def db_insert_metrics(run_id: str, metrics: list[dict]) -> int:
    """Store metric points, last write wins per (run, name, step) by client timestamp.

    A resumed run that logs a step again (or a replayed spool) replaces the earlier value instead of
    duplicating it, unless the stored value carries a newer timestamp (a late replay must not clobber
    fresher data). Deduping on write keeps every read (charts, summaries, alerts) a plain scan.
    Returns the number of points stored.
    """
    rows = [(run_id, m["step"], m["timestamp"], m["name"], m["value"]) for m in metrics]
    latest = _newest_per_key(rows, lambda r: (r[3], r[1]), lambda r: r[2])
    with _lock:
        conn = get_conn()
        keep = _replace_newer(conn, "metrics", run_id, "name", latest, 2)
        if keep:
            conn.executemany("INSERT INTO metrics VALUES (?, ?, ?, ?, ?)", keep)
    return len(keep)


def db_recent_metrics(run_id: str, limit: int = 100) -> dict[str, list[float]]:
    """The newest ``limit`` values of every metric (oldest first), for seeding SDK anomaly detectors on resume."""
    with _lock:
        rows = get_conn().execute(
            """
            SELECT name, value FROM (
                SELECT name, step, value, row_number() OVER (PARTITION BY name ORDER BY step DESC) AS rn
                FROM metrics WHERE run_id = ?
            ) WHERE rn <= ? ORDER BY name, step
            """,
            [run_id, limit],
        ).fetchall()
    out: dict[str, list[float]] = {}
    for name, value in rows:
        out.setdefault(name, []).append(value)
    return out


def db_get_metrics(
    run_id: str,
    name: str | None = None,
    max_points: int | None = None,
    step_min: int | None = None,
    step_max: int | None = None,
    t_min: float | None = None,
    t_max: float | None = None,
    with_total: bool = False,
):
    """Metric points ordered by step. ``name`` limits to one metric; ``step_*``/``t_*`` select a window
    (zoom). ``max_points`` downsamples each metric independently (see ``_query_series``: per bucket the
    min, max, first and last point). ``with_total`` returns ``(rows, points_in_window)``."""
    where, params = "run_id = ?", [run_id]
    if name:
        where += " AND name = ?"
        params.append(name)
    frag, extra = _range_where(True, step_min, step_max, t_min, t_max)
    where += frag
    params += extra
    with _lock:
        rows, total = _query_series(
            get_conn(), "metrics", ["step", "timestamp", "name", "value"], where, params, "name", "step",
            ["value ASC, step", "value DESC, step", "step ASC", "step DESC"], "step, name",
            None if max_points is None else clamp_max_points(max_points),
        )
    out = [{"step": r[0], "timestamp": r[1], "name": r[2], "value": r[3]} for r in rows]
    return (out, total) if with_total else out


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


_SYSTEM_COLS = [
    "timestamp", "gpu_utilization", "memory_used", "memory_total",
    "temperature", "power_draw", "fan_speed", "sm_clock_mhz", "memory_clock_mhz",
    "process_memory_used", "process_exclusive",
    "attributed_utilization", "attributed_power", "COALESCE(gpu_index, 0) AS gpu_index",
]


def db_get_system_snapshots(
    run_id: str,
    max_points: int | None = None,
    t_min: float | None = None,
    t_max: float | None = None,
    with_total: bool = False,
):
    """System snapshots ordered by time then GPU. ``t_min``/``t_max`` select a window; ``max_points``
    downsamples each GPU's series: per time bucket the first and last snapshot plus the ones with the
    peak utilization, lowest utilization, peak memory and peak temperature."""
    where, params = "run_id = ?", [run_id]
    frag, extra = _range_where(False, None, None, t_min, t_max)
    where += frag
    params += extra
    with _lock:
        rows, total = _query_series(
            get_conn(), "system_snapshots", _SYSTEM_COLS, where, params, "COALESCE(gpu_index, 0)", "timestamp",
            ["timestamp ASC", "timestamp DESC", "gpu_utilization DESC NULLS LAST, timestamp",
             "gpu_utilization ASC NULLS LAST, timestamp", "memory_used DESC NULLS LAST, timestamp",
             "temperature DESC NULLS LAST, timestamp"],
            "timestamp, gpu_index", None if max_points is None else clamp_max_points(max_points),
        )
    cols = [c.split(" AS ")[-1] for c in _SYSTEM_COLS]
    out = [dict(zip(cols, r)) for r in rows]
    return (out, total) if with_total else out


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
    # Last write wins per (run, param_name, step) by client timestamp, like metrics.
    latest = _newest_per_key(rows, lambda r: (r[3], r[1]), lambda r: r[2])
    with _lock:
        conn = get_conn()
        keep = _replace_newer(conn, "param_stats", run_id, "param_name", latest, 2)
        if keep:
            conn.executemany(
                "INSERT INTO param_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", keep
            )
    return len(keep)


def db_get_param_stats(
    run_id: str,
    param_name: str | None = None,
    max_points: int | None = None,
    step_min: int | None = None,
    step_max: int | None = None,
    t_min: float | None = None,
    t_max: float | None = None,
    with_total: bool = False,
):
    """Param stats ordered by parameter then step. Windows and ``max_points`` as for ``db_get_metrics``;
    downsampling is per parameter and keeps, per bucket, the first and last step and the steps with
    the peak gradient norm and peak weight norm."""
    where, params = "run_id = ?", [run_id]
    if param_name:
        where += " AND param_name = ?"
        params.append(param_name)
    frag, extra = _range_where(True, step_min, step_max, t_min, t_max)
    where += frag
    params += extra
    with _lock:
        rows, total = _query_series(
            get_conn(), "param_stats", _PARAM_STAT_COLS, where, params, "param_name", "step",
            ["step ASC", "step DESC", "grad_norm DESC NULLS LAST, step", "weight_norm DESC NULLS LAST, step"],
            "param_name, step", None if max_points is None else clamp_max_points(max_points),
        )
    result = []
    for r in rows:
        d = dict(zip(_PARAM_STAT_COLS, r))
        d["grad_histogram"] = _parse_json(d["grad_histogram"]) or []
        d["weight_histogram"] = _parse_json(d["weight_histogram"]) or []
        result.append(d)
    return (result, total) if with_total else result


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
    """Store a media item; replaces an earlier one with the same (run, name, step) unless that one
    carries a newer client timestamp. Image/figure payloads (base64 PNG) are written to
    ``<media root>/<run_id>/`` and only their path is kept in the table; anything else (histogram JSON,
    or a payload that is not valid base64) stays in the ``data`` column."""
    path, data = None, item["data"]
    if item["media_type"] in MEDIA_FILE_TYPES:
        raw = _decode_b64(data)
        if raw is not None:
            path, data = _media_relpath(run_id, item["name"], item["step"]), None
    row = (run_id, item["step"], item["timestamp"], item["name"], item["media_type"], data, path)
    with _lock:
        conn = get_conn()
        keep = _replace_newer(conn, "media", run_id, "name", {(item["name"], item["step"]): row}, 2)
        if keep:
            if path is not None:
                _write_media_file(path, raw)
            conn.execute("INSERT INTO media VALUES (?, ?, ?, ?, ?, ?, ?)", row)


def _media_rows(rows: list[tuple], run_id: str, inline: bool) -> list[dict]:
    out = []
    for step, ts, name, mtype, data, path in rows:
        item = {"step": step, "timestamp": ts, "name": name, "media_type": mtype, "data": data,
                "url": None}
        if path is not None:
            item["url"] = f"/runs/{run_id}/media/file?name={_urlquote(name)}&step={step}"
            if inline:
                try:
                    item["data"] = base64.b64encode((media_root() / path).read_bytes()).decode()
                except OSError:
                    item["data"] = None  # file lost (manual cleanup): metadata is still listed
            else:
                item["data"] = None
        out.append(item)
    return out


def db_get_media(run_id: str, name: str | None = None, inline: bool = True) -> list[dict]:
    """Media items of a run. File-backed images/figures carry a ``url`` (see ``GET /runs/{id}/media/file``);
    ``inline`` (default, backward compatible) also returns their base64 ``data``, ``False`` omits it."""
    with _lock:
        conn = get_conn()
        if name:
            rows = conn.execute(
                "SELECT step, timestamp, name, media_type, data, path "
                "FROM media WHERE run_id = ? AND name = ? ORDER BY step",
                [run_id, name],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT step, timestamp, name, media_type, data, path "
                "FROM media WHERE run_id = ? ORDER BY name, step",
                [run_id],
            ).fetchall()
    return _media_rows(rows, run_id, inline)


def db_get_media_file(run_id: str, name: str, step: int) -> Path | None:
    """Disk path of a stored image/figure, or ``None`` (unknown item, or payload kept inline)."""
    with _lock:
        row = get_conn().execute(
            "SELECT path FROM media WHERE run_id = ? AND name = ? AND step = ? AND path IS NOT NULL",
            [run_id, name, step],
        ).fetchone()
    if row is None:
        return None
    path = media_root() / row[0]
    return path if path.is_file() else None


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

def db_insert_samples(
    run_id: str, name: str, step: int, timestamp: float, samples: list[dict], mode: str = "replace"
) -> int:
    """Store sample dicts for (name, step). Returns rows inserted.

    ``replace`` (default) first deletes what is stored for this (run, name, step), so a resumed run that
    re-logs a step does not duplicate its samples; ``append`` adds after the existing ones.
    """
    import json as _json

    rows = []
    with _lock:
        conn = get_conn()
        if mode == "replace":
            newest = conn.execute(
                "SELECT max(timestamp) FROM samples WHERE run_id = ? AND name = ? AND step = ?", [run_id, name, step]
            ).fetchone()[0]
            if newest is not None and newest > timestamp:
                return 0  # a late replay must not overwrite newer samples
            conn.execute("DELETE FROM samples WHERE run_id = ? AND name = ? AND step = ?", [run_id, name, step])
            start = 0
        else:
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


def _norm_ws(text: str) -> str:
    """Trim and collapse runs of whitespace (prompt matching ignores formatting differences)."""
    return " ".join(text.split())


def db_get_samples(
    run_id: str,
    name: str,
    step: int,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
    desc: bool = False,
    q: str | None = None,
    prompt: str | None = None,
) -> dict:
    """One page of samples at (name, step): ``{"total", "items"}`` with each item's dict parsed.

    Filtering and sorting run over the whole step, not just the page. ``q`` is a case-insensitive
    substring match over every top-level value; ``prompt`` matches the ``prompt`` field exactly after
    whitespace normalization; ``sort`` is a top-level key (numeric values first, ordered numerically,
    then the rest as text; samples without the key last). The key is only ever bound as a JSON path
    parameter, never interpolated into SQL (``desc`` is a bool mapped to a keyword).
    """
    import json as _json

    where = "run_id = ? AND name = ? AND step = ?"
    params: list = [run_id, name, step]
    if q:
        where += (" AND list_bool_or(list_transform(json_extract_string(data, '$.*'), "
                  "lambda x: contains(lower(x), lower(?))))")
        params.append(q)
    if prompt is not None:
        where += " AND trim(regexp_replace(json_extract_string(data, '$.prompt'), '\\s+', ' ', 'g')) = ?"
        params.append(_norm_ws(prompt))
    order = "idx"
    order_params: list = []
    if sort:
        path = '$."' + sort.replace("\\", "\\\\").replace('"', '\\"') + '"'
        direction = "DESC" if desc else "ASC"
        order = (
            f"(TRY_CAST(json_extract_string(data, ?) AS DOUBLE) IS NULL), "
            f"TRY_CAST(json_extract_string(data, ?) AS DOUBLE) {direction}, "
            f"(json_extract_string(data, ?) IS NULL), "
            f"json_extract_string(data, ?) {direction}, idx"
        )
        order_params = [path] * 4
    with _lock:
        conn = get_conn()
        total = conn.execute(f"SELECT count(*) FROM samples WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT idx, data FROM samples WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, *order_params, limit, offset],
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

    # Upsert (unless the run was hard-deleted while this was computing: that must leave no row behind)
    with _lock:
        conn = get_conn()
        if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", [run_id]).fetchone() is None:
            return None
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

_ALERT_COLS = "id, name, url, format, project, enabled, created_at, created_by, triggers, kind, min_severity, metric, op, threshold, updated_at"


def _row_to_alert_rule(r: tuple) -> dict:
    triggers = _parse_json(r[8])
    if not triggers:  # defensive: a row written by an older version between migrations
        triggers = [{"kind": r[9], "min_severity": r[10], "metric": r[11], "op": r[12], "threshold": r[13]}]
    return {
        "id": r[0], "name": r[1], "url": r[2], "format": r[3], "project": r[4],
        "enabled": bool(r[5]), "created_at": r[6], "created_by": r[7], "triggers": triggers,
        "updated_at": r[14] if r[14] is not None else r[6],
    }


def db_list_alert_rules() -> list[dict]:
    """All alert rules, including the (secret) webhook ``url``. Callers must mask it."""
    with _lock:
        rows = get_conn().execute(f"SELECT {_ALERT_COLS} FROM alert_rules ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC").fetchall()
    return [_row_to_alert_rule(r) for r in rows]


def db_get_alert_rule(rule_id: str) -> dict | None:
    with _lock:
        row = get_conn().execute(f"SELECT {_ALERT_COLS} FROM alert_rules WHERE id = ?", [rule_id]).fetchone()
    return _row_to_alert_rule(row) if row else None


def db_create_alert_rule(rule: dict, created_by: str) -> dict:
    rule_id = uuid.uuid4().hex[:12]
    with _lock:
        get_conn().execute(
            "INSERT INTO alert_rules (id, name, url, format, project, enabled, created_at, created_by, triggers, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [rule_id, rule["name"], rule["url"], rule["format"], rule.get("project"),
             rule.get("enabled", True), time.time(), created_by, json.dumps(rule["triggers"]), time.time()],
        )
    return db_get_alert_rule(rule_id)


def db_update_alert_rule(rule_id: str, fields: dict) -> bool:
    """Update the given columns (a subset of the rule's fields). False if the rule does not exist."""
    allowed = {"name", "url", "format", "project", "enabled", "triggers"}
    cols = [c for c in fields if c in allowed]
    with _lock:
        conn = get_conn()
        if conn.execute("SELECT 1 FROM alert_rules WHERE id = ?", [rule_id]).fetchone() is None:
            return False
        if cols:
            values = [json.dumps(fields[c]) if c == "triggers" else fields[c] for c in cols]
            conn.execute(
                f"UPDATE alert_rules SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ? WHERE id = ?",
                values + [time.time(), rule_id],
            )
    return True


def db_delete_alert_rule(rule_id: str) -> bool:
    with _lock:
        conn = get_conn()
        row = conn.execute("DELETE FROM alert_rules WHERE id = ? RETURNING id", [rule_id]).fetchone()
        conn.execute("DELETE FROM alert_sent WHERE rule_id = ?", [rule_id])
        conn.execute("DELETE FROM alert_violations WHERE rule_id = ?", [rule_id])
    return row is not None


def db_matching_alert_rules(project: str | None) -> list[dict]:
    """Enabled rules that apply to ``project`` (unscoped rules apply everywhere); callers filter on triggers."""
    with _lock:
        rows = get_conn().execute(
            f"SELECT {_ALERT_COLS} FROM alert_rules WHERE enabled AND (project IS NULL OR project = ?) "
            "ORDER BY created_at",
            [project],
        ).fetchall()
    return [_row_to_alert_rule(r) for r in rows]


def db_alert_acquire(
    rule_id: str, dkey: str, window_s: float, per_minute: int, now: float | None = None, run_id: str | None = None
) -> str:
    """Decide whether an alert may be sent and record it, atomically.

    Returns ``"ok"`` (recorded: send it), ``"dup"`` (the same ``dkey`` was sent within ``window_s``) or
    ``"rate"`` (``per_minute`` alerts already went out for the rule in the last minute).
    """
    now = time.time() if now is None else now
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM alert_sent WHERE sent_at < ?", [now - max(window_s, 60.0)])
        if conn.execute(
            "SELECT 1 FROM alert_sent WHERE rule_id = ? AND dkey = ? AND sent_at >= ?", [rule_id, dkey, now - window_s]
        ).fetchone():
            return "dup"
        recent = conn.execute(
            "SELECT COUNT(*) FROM alert_sent WHERE rule_id = ? AND sent_at >= ?", [rule_id, now - 60.0]
        ).fetchone()[0]
        if recent >= per_minute:
            return "rate"
        conn.execute("INSERT INTO alert_sent (rule_id, dkey, sent_at, run_id) VALUES (?, ?, ?, ?)", [rule_id, dkey, now, run_id])
    return "ok"


def db_alert_release(rule_id: str, dkey: str) -> None:
    """Forget a recorded alert whose delivery finally failed, so an identical alert can be tried again."""
    with _lock:
        get_conn().execute("DELETE FROM alert_sent WHERE rule_id = ? AND dkey = ?", [rule_id, dkey])


def db_alert_clear_state() -> None:
    """Drop all rate-limit/dedupe/threshold state (tests)."""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM alert_sent")
        conn.execute("DELETE FROM alert_violations")


def db_alert_swap_violating(rule_id: str, trigger_key: str, run_id: str, now_over: bool) -> bool:
    """Store the run's current over-threshold state and return the previous one (``False`` if unseen)."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT violating FROM alert_violations WHERE rule_id = ? AND trigger_key = ? AND run_id = ?",
            [rule_id, trigger_key, run_id],
        ).fetchone()
        was = bool(row[0]) if row else False
        if row is None:
            conn.execute("INSERT INTO alert_violations VALUES (?, ?, ?, ?)", [rule_id, trigger_key, run_id, now_over])
        elif was != now_over:
            conn.execute(
                "UPDATE alert_violations SET violating = ? WHERE rule_id = ? AND trigger_key = ? AND run_id = ?",
                [now_over, rule_id, trigger_key, run_id],
            )
    return was


def db_get_run_for_alert(run_id: str) -> dict | None:
    """Just what an alert may reveal about a run: id, name, project."""
    with _lock:
        row = get_conn().execute("SELECT run_id, name, project FROM runs WHERE run_id = ?", [run_id]).fetchone()
    return {"run_id": row[0], "name": row[1] or row[0], "project": row[2]} if row else None


# ---------------------------------------------------------------------------
# Retention (optional per-project policy; off by default)
# ---------------------------------------------------------------------------

# Tables a policy may thin out by age (``timestamp`` < now - N days). ``runs`` is never touched;
# ``metrics`` is deletable only when a policy names it explicitly.
RETENTION_TABLES = (
    "system_snapshots", "logs", "profile_events", "param_stats", "events", "artifacts", "media", "samples", "metrics",
)


SUMMARY_TABLES = ("metrics", "system_snapshots", "events")  # what db_compute_run_summary reads


def validate_retention(policy: object) -> dict:
    """Normalise ``{"older_than_days": {table: days}}``; ``ValueError`` on anything else."""
    if not isinstance(policy, dict) or set(policy) - {"older_than_days"}:
        raise ValueError('retention must look like {"older_than_days": {"system_snapshots": 30}}')
    days = policy.get("older_than_days") or {}
    if not isinstance(days, dict):
        raise ValueError("older_than_days must be an object mapping table -> days")
    clean: dict[str, float] = {}
    for table, n in days.items():
        if table not in RETENTION_TABLES:
            raise ValueError(f"unknown retention table {table!r}; choose from {', '.join(RETENTION_TABLES)}")
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not 0 < n <= 36500:
            raise ValueError(f"days for {table} must be a number between 0 and 36500")
        clean[table] = n
    return {"older_than_days": clean}


def db_set_project_retention(name: str, policy: dict | None) -> bool:
    """Set (or with ``None``/empty clear) a project's retention policy."""
    clean = validate_retention(policy) if policy else {"older_than_days": {}}
    value = json.dumps(clean) if clean["older_than_days"] else None
    with _lock:
        row = get_conn().execute(
            "UPDATE projects SET retention = ? WHERE name = ? RETURNING name", [value, name]
        ).fetchone()
    return row is not None


def db_apply_retention(project: str | None = None, now: float | None = None) -> dict[str, dict[str, int]]:
    """Delete rows older than each project's policy (all projects with one, or just ``project``).

    Returns ``{project: {table: rows_deleted}}`` for what was removed. Media files of deleted media rows
    go too. Only the tables a policy names are touched. A run whose ``metrics``, ``system_snapshots`` or
    ``events`` lost rows has its stored summary recomputed (if it had one) so it never reports deleted data.
    """
    now = time.time() if now is None else now
    out: dict[str, dict[str, int]] = {}
    touched: set[str] = set()
    with _lock:
        conn = get_conn()
        where, params = "retention IS NOT NULL", []
        if project is not None:
            where += " AND name = ?"
            params.append(project)
        for name, raw in conn.execute(f"SELECT name, retention FROM projects WHERE {where}", params).fetchall():
            try:
                policy = validate_retention(_parse_json(raw))
            except ValueError:
                continue
            for table, days in policy["older_than_days"].items():
                cutoff = now - float(days) * 86400
                sub = "timestamp < ? AND run_id IN (SELECT run_id FROM runs WHERE project = ?)"
                if table == "media":
                    for (rel,) in conn.execute(
                        f"SELECT path FROM media WHERE {sub} AND path IS NOT NULL", [cutoff, name]
                    ).fetchall():
                        try:
                            (media_root() / rel).unlink()
                        except OSError:
                            pass
                gone = conn.execute(f"DELETE FROM {table} WHERE {sub} RETURNING run_id", [cutoff, name]).fetchall()
                if gone:
                    out.setdefault(name, {})[table] = len(gone)
                    if table in SUMMARY_TABLES:
                        touched.update(r[0] for r in gone)
        if touched:
            have = conn.execute(
                "SELECT run_id FROM run_summaries WHERE run_id IN (SELECT unnest(?::VARCHAR[]))", [sorted(touched)]
            ).fetchall()
            touched = {r[0] for r in have}
    for run_id in sorted(touched):  # each recompute takes the lock itself; do not hold it across all of them
        db_compute_run_summary(run_id)
    return out


# ---------------------------------------------------------------------------
# Export (Parquet)
# ---------------------------------------------------------------------------

EXPORT_TABLES = (
    "metrics", "system_snapshots", "param_stats", "profile_events", "events", "logs", "artifacts",
    "media", "samples", "run_summaries",
)


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def db_export_parquet(dest: Path, run_id: str | None = None, project: str | None = None) -> dict[str, int]:
    """Write one Parquet file per table (``runs`` plus ``EXPORT_TABLES``) for a run or a whole project.

    Returns ``{table: rows}``. ``dest`` must exist. Credentials, alert rules and the dedupe table are never
    exported. File-backed media is described by its ``path`` column (relative to the media directory); the
    caller adds the files themselves.

    The reads run on their own DuckDB cursor inside one transaction, **without the global lock**: DuckDB's
    MVCC gives the export a consistent snapshot while ingestion keeps writing on the main connection.
    """
    assert (run_id is None) != (project is None)
    if run_id is not None:
        runs_where = f"run_id = {_sql_str(run_id)}"
    else:
        runs_where = f"project = {_sql_str(project)}"
    counts: dict[str, int] = {}
    cur = get_conn().cursor()
    try:
        cur.execute("BEGIN TRANSACTION")
        for table in ("runs", *EXPORT_TABLES):
            where = runs_where if table == "runs" else f"run_id IN (SELECT run_id FROM runs WHERE {runs_where})"
            target = _sql_str(str(dest / f"{table}.parquet"))
            cur.execute(f"COPY (SELECT * FROM {table} WHERE {where}) TO {target} (FORMAT PARQUET)")
            counts[table] = cur.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]
        cur.execute("COMMIT")
    finally:
        cur.close()
    return counts


def db_export_media_files(run_ids: list[str]) -> list[tuple[Path, str]]:
    """``(disk path, relative path)`` of every stored media file of ``run_ids`` (for the export zip)."""
    if not run_ids:
        return []
    with _lock:
        rows = get_conn().execute(
            "SELECT path FROM media WHERE path IS NOT NULL AND run_id IN (SELECT unnest(?::VARCHAR[]))", [run_ids]
        ).fetchall()
    return [(media_root() / r[0], r[0]) for r in rows if (media_root() / r[0]).is_file()]
