"""Replay spooled payloads (see ``_transport``) to a Pyrometer server.

Run this after the producing process has exited: it consumes the same files a live run owns.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from pyrometer._transport import (
    BATCH_HEADER, BLOB_DIR, DEAD_FILE, META_FILE, POS_FILE, QUEUE_FILE, Spool, _retryable,
    materialize_body, spool_root,
)


@dataclass
class SyncResult:
    """Outcome of syncing one run's spool directory."""

    run_id: str
    sent: int = 0
    remaining: int = 0
    error: str | None = None
    complete: bool = False
    messages: list[str] = field(default_factory=list)


def find_spools(target: str | None) -> list[Path]:
    """Resolve a CLI target (nothing, a spool root, one run dir, or a run id) to run directories."""
    if target is None:
        root = spool_root()
        return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    path = Path(target).expanduser()
    if path.is_dir():
        if any((path / f).exists() for f in (META_FILE, QUEUE_FILE, DEAD_FILE)):
            return [path]
        return sorted(p for p in path.iterdir() if p.is_dir())
    by_id = spool_root() / target
    return [by_id] if by_id.is_dir() else []


def sync_run_dir(run_dir: Path, client: httpx.Client | None = None, server: str | None = None,
                 api_key: str | None = None) -> SyncResult:
    """Replay one run's spool. Deletes the directory when everything was accepted."""
    run_id = run_dir.name
    result = SyncResult(run_id)
    meta = Spool(run_id, run_dir.parent).read_meta()
    own_client = client is None
    if own_client:
        base = (server or os.environ.get("PYROMETER_SERVER") or meta.get("server")
                or "http://localhost:8321").rstrip("/")
        key = api_key or os.environ.get("PYROMETER_API_KEY")
        client = httpx.Client(base_url=base, timeout=30,
                              headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        _replay(run_dir, run_id, meta, client, result)
    finally:
        if own_client:
            client.close()
    return result


def _replay(run_dir: Path, run_id: str, meta: dict, client: httpx.Client, result: SyncResult) -> None:
    spool = Spool(run_id, run_dir.parent)

    if not meta.get("created", True):
        try:
            resp = client.post("/runs", json=meta["create"])
        except httpx.HTTPError as e:
            result.error = f"server unreachable: {e}"
            return
        if resp.status_code in (401, 403):
            result.error = f"server rejected the API key (HTTP {resp.status_code})"
            return
        if resp.status_code >= 300:
            result.error = f"could not create the run (HTTP {resp.status_code})"
            return
        spool.write_meta(created=True)

    try:
        spool.recover_wal()
    except OSError:
        pass
    queue, bad_q = spool.pending()
    dead, bad_d = spool.dead()
    if bad_q + bad_d:
        result.messages.append(f"skipped {bad_q + bad_d} corrupt spool lines")

    kept_q: list[dict] = []
    kept_d: list[dict] = []
    unreachable = False
    # Dead letters first (they are older than the journal tail), the journal (status PATCH last) after.
    for source, item in [("dead", i) for i in dead] + [("queue", i) for i in queue]:
        keep = kept_d if source == "dead" else kept_q
        if unreachable:
            keep.append(item)
            continue
        body = materialize_body(item, run_dir)
        if item.get("blob") and body is None:
            result.messages.append(f"{item['path']}: media file missing, dropped")
            continue
        try:
            kwargs = {"timeout": item["timeout"]} if "timeout" in item else {}
            resp = client.request(item["method"], item["path"], json=body,
                                  headers={BATCH_HEADER: item["id"]}, **kwargs)
        except httpx.HTTPError as e:
            result.error = f"server unreachable: {e}"
            unreachable = True
            keep.append(item)
            continue
        if resp.status_code < 300:
            result.sent += 1
            if item.get("blob"):
                (run_dir / BLOB_DIR / item["blob"]["file"]).unlink(missing_ok=True)
        elif _retryable(resp.status_code):
            result.error = f"server error (HTTP {resp.status_code})"
            unreachable = True
            keep.append(item)
        else:
            kept_d.append(item)  # refused for good: park it in the dead-letter file
            result.messages.append(f"{item['path']} rejected (HTTP {resp.status_code})")

    result.remaining = len(kept_q) + len(kept_d)
    if result.remaining:
        _rewrite(run_dir / QUEUE_FILE, kept_q)
        (run_dir / POS_FILE).unlink(missing_ok=True)
        _rewrite(run_dir / DEAD_FILE, kept_d)
        if result.error is None:
            result.error = f"{result.remaining} payloads rejected by the server; kept in {run_dir}"
        return
    result.complete = True
    spool.cleanup()


def _rewrite(path: Path, items: list[dict]) -> None:
    if items:
        path.write_text("".join(json.dumps(i) + "\n" for i in items), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
