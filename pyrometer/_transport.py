"""Durable delivery of SDK payloads: background retries, bounded memory, disk spool.

Every payload the SDK ships (metrics, logs, events, media, ...) goes through a
:class:`Transport`. ``submit`` only enqueues, so it never blocks the training
loop. A sender thread delivers in order and retries failures with exponential
backoff. If the queue grows past its cap, or delivery is still failing when the
run finishes, payloads are appended to a JSONL spool on disk that
``pyrometer sync`` replays later.

Each payload carries a client-generated batch id (``X-Pyrometer-Batch``). The
server remembers ids per run and ignores repeats, so a retry after a lost
response never double-inserts.
"""

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BATCH_HEADER = "X-Pyrometer-Batch"
QUEUE_FILE = "queue.jsonl"
META_FILE = "meta.json"
_BACKOFF_START = 1.0
_BACKOFF_CAP = 60.0


def spool_root() -> Path:
    """Directory holding one sub-directory per run with undelivered payloads."""
    return Path(os.environ.get("PYROMETER_SPOOL_DIR") or Path.home() / ".pyrometer" / "spool")


def _retryable(status: int) -> bool:
    return status in (408, 425, 429) or status >= 500


class Spool:
    """On-disk spool for one run: ``meta.json`` plus an append-only ``queue.jsonl``."""

    def __init__(self, run_id: str, root: Path | None = None) -> None:
        self.run_id = run_id
        self.dir = (root or spool_root()) / run_id
        self._lock = threading.Lock()

    def read_meta(self) -> dict:
        try:
            return json.loads((self.dir / META_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def write_meta(self, **fields) -> None:
        """Merge ``fields`` into ``meta.json`` (never stores the API key)."""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            meta = {**self.read_meta(), **fields}
            (self.dir / META_FILE).write_text(json.dumps(meta))

    def append(self, items: list[dict]) -> None:
        if not items:
            return
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / QUEUE_FILE, "a", encoding="utf-8") as f:
                for item in items:
                    f.write(json.dumps(item) + "\n")


class Transport:
    """Ordered, retrying, non-blocking sender for one run.

    Args:
        run_id: The run all payloads belong to.
        client: HTTP client (base URL and auth already configured); ``None`` when offline.
        spool: Where undeliverable payloads go.
        offline: If true nothing touches the network: every payload goes to the spool.
        max_queue: In-memory cap; payloads beyond it are spilled to disk immediately.
        meta_defaults: Written to the spool's ``meta.json`` before the first spill.
    """

    def __init__(
        self,
        run_id: str,
        client: httpx.Client | None,
        spool: Spool,
        offline: bool = False,
        max_queue: int = 500,
        meta_defaults: dict | None = None,
    ) -> None:
        self.run_id = run_id
        self._client = client
        self._spool = spool
        self._offline = offline or client is None
        self._max_queue = max_queue
        self._meta_defaults = meta_defaults or {}
        self._queue: deque[dict] = deque()
        self._cv = threading.Condition()
        self._closing = False
        self._abandon = False
        self.spilled = 0  # payloads that ended up on disk because delivery failed
        self.offline_saved = 0  # payloads written to disk by design (offline mode)
        self._meta_written = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="pyrometer-sender")
        self._thread.start()

    # -- public --

    def submit(
        self, method: str, path: str, body, timeout: float | None = None, ephemeral: bool = False
    ) -> None:
        """Queue one request. Never performs network I/O on the caller's thread.

        ``ephemeral`` requests (heartbeats) only mean something right now: they are never spooled
        or retried (dropped on failure, offline, overflow and close), and at most one per path
        waits in the queue.
        """
        item = {"id": uuid.uuid4().hex, "method": method, "path": path, "json": body}
        if timeout is not None:
            item["timeout"] = timeout
        if ephemeral:
            item["ephemeral"] = True
            if self._offline:
                return
        with self._cv:
            if ephemeral:
                if self._closing or any(q.get("ephemeral") and q["path"] == path for q in self._queue):
                    return
                if len(self._queue) < self._max_queue:
                    self._queue.append(item)
                    self._cv.notify_all()
                return
            if self._closing and not self._thread.is_alive():
                overflow = True
            else:
                overflow = len(self._queue) >= self._max_queue
                if not overflow:
                    self._queue.append(item)
                    self._cv.notify_all()
        if overflow:
            self._spill([item])

    def close(self, timeout: float = 10.0) -> int:
        """Try to deliver what is queued for up to ``timeout`` s, then spill the rest.

        Returns the number of payloads left on disk (for this run, in total, since init).
        """
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        self._thread.join(timeout)
        with self._cv:
            self._abandon = True
            rest = list(self._queue)
            self._queue.clear()
            self._cv.notify_all()
        self._spill([i for i in rest if not i.get("ephemeral")])
        return self.spilled

    # -- internals --

    def _spill(self, items: list[dict]) -> None:
        if not items:
            return
        try:
            if not self._meta_written:
                existing = self._spool.read_meta()
                self._spool.write_meta(**{k: v for k, v in self._meta_defaults.items() if k not in existing})
                self._meta_written = True
            self._spool.append(items)
            if self._offline:
                self.offline_saved += len(items)
            else:
                self.spilled += len(items)
        except OSError:
            log.exception("Could not write %d payloads to the spool at %s", len(items), self._spool.dir)

    def _deliver(self, item: dict) -> str:
        """Return ``"ok"`` (delivered or handled), or ``"retry"``."""
        if self._offline:
            self._spill([item])
            return "ok"
        kwargs: dict = {}
        if "timeout" in item:
            kwargs["timeout"] = item["timeout"]
        try:
            resp = self._client.request(  # type: ignore[union-attr]
                item["method"], item["path"], json=item["json"],
                headers={BATCH_HEADER: item["id"]}, **kwargs,
            )
        except (httpx.TransportError, httpx.InvalidURL):
            return "ok" if item.get("ephemeral") else "retry"
        except Exception:
            log.exception("Unexpected error delivering %s", item["path"])
            return "ok" if item.get("ephemeral") else "retry"
        if resp.status_code < 300:
            return "ok"
        if item.get("ephemeral"):
            return "ok"  # a missed heartbeat is not worth keeping
        if _retryable(resp.status_code):
            return "retry"
        # The server rejected this payload outright (auth, validation, unknown run): retrying
        # cannot help, so keep it on disk instead of dropping it.
        log.warning("Server rejected %s with HTTP %d; saved to the spool", item["path"], resp.status_code)
        self._spill([item])
        return "ok"

    def _loop(self) -> None:
        backoff = _BACKOFF_START
        while True:
            with self._cv:
                while not self._queue and not self._closing:
                    self._cv.wait()
                if self._abandon or not self._queue:
                    return
                item = self._queue[0]
            outcome = self._deliver(item)
            with self._cv:
                if self._abandon:
                    return
                if outcome == "ok":
                    if self._queue and self._queue[0] is item:
                        self._queue.popleft()
                    backoff = _BACKOFF_START
                    continue
                if self._closing:
                    return  # final drain: one failed attempt is enough, close() spills the rest
                deadline = time.monotonic() + backoff
                while not self._closing and not self._abandon:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cv.wait(remaining)
                backoff = min(backoff * 2, _BACKOFF_CAP)
