"""Replay spooled payloads (see ``_transport``) to a Pyrometer server."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from pyrometer._transport import BATCH_HEADER, META_FILE, QUEUE_FILE, Spool, _retryable, spool_root


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
        if (path / META_FILE).exists() or (path / QUEUE_FILE).exists():
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
    queue_path = run_dir / QUEUE_FILE
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

    items: list[dict] = []
    if queue_path.exists():
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    items.append(json.loads(line))
                except ValueError:
                    result.messages.append("skipped a corrupt spool line")

    kept: list[dict] = []
    unreachable = False
    for i, item in enumerate(items):
        if unreachable:
            kept.append(item)
            continue
        try:
            kwargs = {"timeout": item["timeout"]} if "timeout" in item else {}
            resp = client.request(item["method"], item["path"], json=item["json"],
                                  headers={BATCH_HEADER: item["id"]}, **kwargs)
        except httpx.HTTPError as e:
            result.error = f"server unreachable: {e}"
            unreachable = True
            kept.append(item)
            continue
        if resp.status_code < 300:
            result.sent += 1
        else:
            kept.append(item)
            if _retryable(resp.status_code):
                result.error = f"server error (HTTP {resp.status_code})"
                unreachable = True
            else:
                result.messages.append(f"{item['path']} rejected (HTTP {resp.status_code})")

    result.remaining = len(kept)
    if kept:
        queue_path.write_text("".join(json.dumps(k) + "\n" for k in kept), encoding="utf-8")
        if result.error is None:
            result.error = f"{len(kept)} payloads rejected by the server; kept in {run_dir}"
        return
    result.complete = True
    for f in (queue_path, run_dir / META_FILE):
        f.unlink(missing_ok=True)
    try:
        run_dir.rmdir()
    except OSError:
        pass
