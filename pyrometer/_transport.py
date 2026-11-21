"""Durable delivery of SDK payloads: write-ahead journal, background retries, dead letters.

Every payload the SDK ships (metrics, logs, events, media, ...) goes through a
:class:`Transport`. ``submit`` appends the payload to the run's on-disk journal
(``queue.jsonl``) *before* anything else and only enqueues a pointer for the sender
thread, so it never blocks the training loop on the network. The sender delivers
journal entries strictly in order and advances an acknowledgement cursor after each
success; when it has caught up the journal is truncated. Because every payload is on
disk until acknowledged:

* a server outage is just a growing backlog; the sender drains it by itself when the
  connection returns (no ``pyrometer sync`` needed while the process is alive),
* a SIGKILL loses nothing that ``submit`` already returned from; ``pyrometer sync`` (or
  the next ``init`` of the same run id) replays the unacknowledged tail,
* the memory queue is only a bounded cache of the journal.

Each payload carries a client-generated batch id (``X-Pyrometer-Batch``) that stays the
same in the journal, the dead-letter file and every replay. The server remembers ids per
run and ignores repeats, so a re-send after a lost response never double-inserts.

Files in ``<spool>/<run_id>/``: ``meta.json`` (server, run-create payload),
``queue.jsonl`` (journal), ``queue.pos`` (ack cursor, only while a backlog exists),
``metrics.wal`` / ``logs.wal`` / ``param_stats.wal`` (write-ahead copies of metrics, stdout/stderr lines
and gradient stats that were recorded but not yet batched into the journal), ``dead.jsonl`` (poison
payloads), ``blobs/`` (media bytes referenced from the journal), ``owner.lock`` (held, with the pid, by the
process that owns the run; ``pyrometer sync`` refuses to touch a run whose lock is held).
"""

import base64
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BATCH_HEADER = "X-Pyrometer-Batch"
QUEUE_FILE = "queue.jsonl"
POS_FILE = "queue.pos"
META_FILE = "meta.json"
DEAD_FILE = "dead.jsonl"
WAL_FILE = "metrics.wal"  # kept for the original name; see WAL_KINDS
LOCK_FILE = "owner.lock"
# Write-ahead logs: kind -> (file, route suffix the recovered batch is POSTed to)
WAL_KINDS: dict[str, tuple[str, str]] = {
    "metrics": ("metrics.wal", "metrics"),
    "logs": ("logs.wal", "logs"),
    "param-stats": ("param_stats.wal", "param-stats"),
}
BLOB_DIR = "blobs"
_BACKOFF_START = 1.0
_BACKOFF_CAP = 60.0
_FSYNC_INTERVAL = 1.0  # journal is fsync'd at most this often (by the sender thread)
_POS_INTERVAL = 0.5  # ack cursor is persisted at most this often while a backlog exists
_POISON_ATTEMPTS = 6  # failed attempts (with a healthy server) before a payload is dead-lettered
_CONNECT_ERRORS = (httpx.TransportError, httpx.InvalidURL)


def spool_root() -> Path:
    """Directory holding one sub-directory per run with undelivered payloads."""
    return Path(os.environ.get("PYROMETER_SPOOL_DIR") or Path.home() / ".pyrometer" / "spool")


def _retryable(status: int) -> bool:
    return status in (408, 425, 429) or status >= 500


def _record(item: dict) -> dict:
    """The persistent fields of a payload (drops the transport's private ``_`` keys)."""
    return {k: v for k, v in item.items() if not k.startswith("_")}


_NONFINITE = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}


def _finite_json(obj):
    """``obj`` with NaN/Infinity floats replaced by the strings ``"NaN"`` / ``"Infinity"`` / ``"-Infinity"``
    (strict JSON has no such numbers; the server's float fields parse these strings back)."""
    if isinstance(obj, float):
        return obj if obj - obj == 0.0 else _NONFINITE[str(obj)]  # nan/inf: x - x is nan
    if isinstance(obj, dict):
        return {k: _finite_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite_json(v) for v in obj]
    return obj


def wire_json(body):
    """``body`` ready for a strict JSON encoder (httpx refuses NaN/Infinity, which a diverging loss produces,
    and would otherwise fail the whole batch it is in)."""
    try:
        json.dumps(body, allow_nan=False)
        return body
    except ValueError:
        return _finite_json(body)


def materialize_body(item: dict, run_dir: Path):
    """The JSON body to send for ``item``, with its blob (if any) inlined as text.

    Returns ``None`` when the blob file is missing.
    """
    blob = item.get("blob")
    if not blob:
        return wire_json(item.get("json"))
    try:
        raw = (run_dir / BLOB_DIR / blob["file"]).read_bytes()
    except OSError:
        return None
    body = dict(item.get("json") or {})
    body[blob["field"]] = (
        base64.b64encode(raw).decode("ascii") if blob.get("enc", "b64") == "b64" else raw.decode("utf-8")
    )
    return wire_json(body)


def read_jsonl(path: Path, start: int = 0) -> tuple[list[dict], int]:
    """Complete JSON lines of ``path`` from byte offset ``start``; returns ``(items, corrupt_count)``."""
    items: list[dict] = []
    corrupt = 0
    try:
        with open(path, "rb") as f:
            f.seek(start)
            for line in f:
                if not line.endswith(b"\n"):
                    corrupt += 1  # torn write from a killed process
                    continue
                if not line.strip():
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    corrupt += 1
    except OSError:
        pass
    return items, corrupt


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists (best effort; used where flock is unavailable)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


try:
    import fcntl as _fcntl
except ImportError:  # Windows: fall back to a pid file
    _fcntl = None


class SpoolLock:
    """Ownership lock for a run's spool directory (``owner.lock``).

    The process that writes a run's journal holds it for its lifetime. On Linux/macOS this is an
    ``flock`` on the file (the kernel drops it when the process dies, so a crashed owner never leaves
    a stale lock behind); the pid is written into the file for the message. Without ``fcntl``
    (Windows) the file just holds the pid and a lock counts as held while that process is alive.
    """

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / LOCK_FILE
        self._fh = None

    def acquire(self) -> bool:
        """Take the lock; ``False`` if another live process holds it."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if _fcntl is None:
                owner = self.owner_pid()
                if owner is not None and owner != os.getpid() and _pid_alive(owner):
                    return False
                self.path.write_text(str(os.getpid()))
                self._fh = False  # marker: held via pid file
                return True
            fh = open(self.path, "a+")
            try:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            self._fh = fh
            return True
        except OSError:
            return True  # cannot create the lock (read-only dir): do not block the run over it

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh:
            try:
                fh.close()  # closing drops the flock
            except OSError:
                pass
        elif fh is False:
            try:
                if self.owner_pid() == os.getpid():
                    self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def owner_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    @classmethod
    def held_by_other(cls, directory: Path) -> int | None:
        """Pid of a live process that owns ``directory``'s run, else ``None`` (no lock, or a stale one)."""
        path = Path(directory) / LOCK_FILE
        if not path.exists():
            return None
        probe = cls(directory)
        pid = probe.owner_pid()
        if _fcntl is None:
            return pid if pid is not None and pid != os.getpid() and _pid_alive(pid) else None
        try:
            fh = open(path, "a+")
        except OSError:
            return None
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return pid if pid is not None else -1  # held; owner's pid unknown
        else:
            return None  # we could take it: nobody owns it (stale file from a dead process)
        finally:
            fh.close()


class Spool:
    """On-disk state for one run (see the module docstring for the layout)."""

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
        """Append payloads to the journal (used by tests and tools; the transport writes directly)."""
        if not items:
            return
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / QUEUE_FILE, "ab") as f:
                for item in items:
                    f.write((json.dumps(item) + "\n").encode("utf-8"))

    def read_pos(self) -> int:
        try:
            return max(0, int((self.dir / POS_FILE).read_text().strip()))
        except (OSError, ValueError):
            return 0

    def write_pos(self, pos: int) -> None:
        try:
            (self.dir / POS_FILE).write_text(str(pos))
        except OSError:
            pass

    def pending(self) -> tuple[list[dict], int]:
        """Unacknowledged journal entries (from the ack cursor), and the number of corrupt lines."""
        return read_jsonl(self.dir / QUEUE_FILE, self.read_pos())

    def dead(self) -> tuple[list[dict], int]:
        return read_jsonl(self.dir / DEAD_FILE)

    def recover_wal(self) -> int:
        """Turn leftover write-ahead lines (metrics, log lines, gradient stats never batched, e.g. after
        SIGKILL) into journal entries.

        Entries keep the batch id they were logged under, so if that batch had in fact been
        journaled and delivered the server ignores this copy. Returns the number of batches.
        """
        total = 0
        for _kind, (fname, route) in WAL_KINDS.items():
            wal = self.dir / fname
            if not wal.exists():
                continue
            lines, _ = read_jsonl(wal)
            batches: dict[str, list] = {}
            for line in lines:
                if isinstance(line, dict) and "b" in line and "e" in line:
                    batches.setdefault(line["b"], []).extend(line["e"])
            self.append([
                {"id": b, "method": "POST", "path": f"/runs/{self.run_id}/{route}", "json": entries}
                for b, entries in batches.items()
            ])
            wal.unlink(missing_ok=True)
            total += len(batches)
        return total

    def cleanup(self) -> None:
        """Delete every file of a fully delivered run and the directory itself."""
        for name in (QUEUE_FILE, POS_FILE, DEAD_FILE, META_FILE, *(f for f, _ in WAL_KINDS.values())):
            (self.dir / name).unlink(missing_ok=True)
        blobs = self.dir / BLOB_DIR
        if blobs.is_dir():
            for f in blobs.iterdir():
                f.unlink(missing_ok=True)
            try:
                blobs.rmdir()
            except OSError:
                pass
        try:
            self.dir.rmdir()
        except OSError:
            pass


class Transport:
    """Journaled, ordered, retrying, non-blocking sender for one run.

    Args:
        run_id: The run all payloads belong to.
        client: HTTP client (base URL and auth already configured); ``None`` when offline.
        spool: Where the journal lives.
        offline: If true nothing touches the network: every payload stays in the journal.
        max_queue: In-memory cache cap. Payloads beyond it are served from the journal.
        meta_defaults: Written to the spool's ``meta.json`` (for keys not already present).
        create: Run-creation payload still to be POSTed (``init`` could not reach the server).
            The sender creates the run first (idempotent), then drains the journal.
    """

    def __init__(
        self,
        run_id: str,
        client: httpx.Client | None,
        spool: Spool,
        offline: bool = False,
        max_queue: int = 500,
        meta_defaults: dict | None = None,
        create: dict | None = None,
    ) -> None:
        self.run_id = run_id
        self._client = client
        self._spool = spool
        self._offline = offline or client is None
        self._max_queue = max_queue
        self._meta_defaults = meta_defaults or {}
        self._create = None if self._offline else create
        self._cv = threading.Condition()
        self._closing = False
        self._abandon = False
        self._halted = False  # the server refused the run itself: stop sending, keep everything on disk
        self._cache: deque[dict] = deque()  # journal entries starting at the ack cursor, in order
        self._mem: deque[dict] = deque()  # payloads the disk refused (read-only home, full disk)
        self._eph: deque[dict] = deque()  # heartbeats: memory only, never journaled
        self._fh = None  # journal, raw append-only
        self._wal_fh: dict[str, object] = {}  # kind -> open write-ahead file
        self._wal_closed = False
        self._wal_last: dict[str, str] = {}  # kind -> id of the newest batch written to that file
        self._wal_lock = threading.RLock()
        self._dirty = False
        self._last_sync = time.monotonic()
        self._last_pos_write = 0.0
        self._fail_id: str | None = None
        self._fail_n = 0
        self._warned_disk = False
        self._warned_dead = False
        self._warned_halt = False
        self.overflowed = 0  # payloads served from disk because the memory cache was full
        self.offline_saved = 0  # payloads written to disk by design (offline mode)
        self.dead = 0  # payloads moved to the dead-letter file this session
        self._meta_written = False

        # Mark this process as the run's owner (``pyrometer sync`` stays away while we are alive). Taken
        # before recovering leftovers so a concurrent sync cannot replay the same files.
        self._owner = SpoolLock(spool.dir)
        if not self._owner.acquire():
            log.warning("Spool %s is owned by another live process; two writers may interleave", spool.dir)

        # Anything a previous process left behind is replayed first (same batch ids, so safe).
        try:
            spool.recover_wal()
        except OSError:
            log.exception("Could not recover %s", spool.dir / WAL_FILE)
        qpath = spool.dir / QUEUE_FILE
        self._tail = qpath.stat().st_size if qpath.exists() else 0
        self._pos = min(spool.read_pos(), self._tail)
        if self._create is not None:
            self._ensure_meta()

        self._thread = threading.Thread(target=self._loop, daemon=True, name="pyrometer-sender")
        self._thread.start()

    # -- public --

    def submit(
        self,
        method: str,
        path: str,
        body,
        timeout: float | None = None,
        ephemeral: bool = False,
        blob: tuple[str, bytes, str] | None = None,
        item_id: str | None = None,
    ) -> None:
        """Journal one request and queue it for delivery. Never touches the network on the caller's thread.

        ``ephemeral`` requests (heartbeats) only mean something right now: they are never
        journaled or retried (dropped on failure, offline and close), and at most one per path
        waits in the queue.

        ``blob`` is ``(field, raw_bytes, "b64" | "utf8")``: the bytes are written to a separate
        file in the spool and only inlined (base64 for ``b64``) into ``body[field]`` on the wire.
        ``item_id`` fixes the batch id (otherwise a fresh one is generated).
        """
        item: dict = {"id": item_id or uuid.uuid4().hex, "method": method, "path": path, "json": body}
        if timeout is not None:
            item["timeout"] = timeout
        if ephemeral:
            item["ephemeral"] = True
            if self._offline:
                return
            with self._cv:
                if self._closing or any(q["path"] == path for q in self._eph):
                    return
                self._eph.append(item)
                self._cv.notify_all()
            return
        if blob is not None:
            field, raw, enc = blob
            try:
                bdir = self._spool.dir / BLOB_DIR
                bdir.mkdir(parents=True, exist_ok=True)
                (bdir / f"{item['id']}.bin").write_bytes(raw)
                item["blob"] = {"field": field, "file": f"{item['id']}.bin", "enc": enc}
            except OSError:
                item["json"] = {
                    **(body or {}),
                    field: base64.b64encode(raw).decode("ascii") if enc == "b64" else raw.decode("utf-8"),
                }
        with self._cv:
            if self._journal(item):
                if self._offline:
                    self.offline_saved += 1
                else:
                    cache_end = self._cache[-1]["_end"] if self._cache else self._pos
                    if cache_end == item["_off"] and len(self._cache) < self._max_queue:
                        self._cache.append(item)
                    else:
                        self.overflowed += 1
            elif not self._offline and len(self._mem) < self._max_queue:
                self._mem.append(item)
            self._cv.notify_all()

    def wal_append(self, batch_id: str, entries: list[dict], kind: str = "metrics") -> None:
        """Append not-yet-batched entries to the write-ahead log ``kind`` (one cheap write, no fsync).

        ``kind`` is a key of :data:`WAL_KINDS`. Entries with the same ``batch_id`` are replayed as
        one batch (and keep that id on the wire, so a batch that was in fact delivered is deduped).
        """
        line = (json.dumps({"b": batch_id, "e": entries}) + "\n").encode("utf-8")
        with self._wal_lock:
            if self._wal_closed:
                return
            try:
                fh = self._wal_fh.get(kind)
                if fh is None:
                    self._spool.dir.mkdir(parents=True, exist_ok=True)
                    fh = self._wal_fh[kind] = open(self._spool.dir / WAL_KINDS[kind][0], "ab", buffering=0)
                fh.write(line)  # type: ignore[attr-defined]
                self._wal_last[kind] = batch_id
            except OSError:
                self._disk_warning()

    def wal_clear(self, kind: str = "metrics", batch_id: str | None = None) -> None:
        """Forget write-ahead entries of ``kind``: their batch is now journaled.

        With ``batch_id`` the file is only emptied if that batch is the newest one written, so a
        batch started concurrently (no lock shared with the caller) is never lost; older, already
        journaled batches that stay behind are harmless (the server ignores their repeated ids).
        """
        with self._wal_lock:
            if batch_id is not None and self._wal_last.get(kind) != batch_id:
                return
            fh = self._wal_fh.get(kind)
            if fh is not None:
                try:
                    os.ftruncate(fh.fileno(), 0)  # type: ignore[attr-defined]
                except OSError:
                    pass

    def close(self, timeout: float = 10.0) -> int:
        """Try to deliver what is journaled for up to ``timeout`` s, then stop.

        Returns the number of payloads (including a still-pending run creation) left
        on disk for ``pyrometer sync``.
        """
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        self._thread.join(timeout)
        with self._cv:
            self._abandon = True
            self._eph.clear()
            self._cv.notify_all()
            left = self._count_pending() + len(self._mem) + (1 if self._create is not None else 0)
            self._persist_pos(force=True)
            if self._fh is not None:
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass
                self._fh.close()
                self._fh = None
        with self._wal_lock:
            self._wal_closed = True
            for kind, fh in list(self._wal_fh.items()):
                try:
                    fh.close()  # type: ignore[attr-defined]
                except OSError:
                    pass
                wal = self._spool.dir / WAL_KINDS[kind][0]
                try:
                    if wal.exists() and wal.stat().st_size == 0:
                        wal.unlink()
                except OSError:
                    pass
            self._wal_fh.clear()
        try:
            if not self._offline and left == 0:
                if self._tail:
                    (self._spool.dir / QUEUE_FILE).unlink(missing_ok=True)
                    (self._spool.dir / POS_FILE).unlink(missing_ok=True)
                if not (self._spool.dir / DEAD_FILE).exists():
                    self._spool.cleanup()
        finally:
            self._owner.release()
            self._owner.path.unlink(missing_ok=True)
            try:
                self._spool.dir.rmdir()  # only succeeds when nothing is left in it
            except OSError:
                pass
        return left

    # -- journal --

    def _ensure_meta(self) -> None:
        if self._meta_written:
            return
        existing = self._spool.read_meta()
        fields = {k: v for k, v in self._meta_defaults.items() if k not in existing}
        if self._create is not None:
            fields.update(created=False, create=self._create)
        elif not self._offline:
            fields["created"] = True
        self._spool.write_meta(**fields)
        self._meta_written = True

    def _disk_warning(self) -> None:
        if not self._warned_disk:
            self._warned_disk = True
            log.warning("Could not write to the spool at %s; data is only kept in memory", self._spool.dir)

    def _journal(self, item: dict) -> bool:
        """Append ``item`` to the journal (cv held). Sets ``_off``/``_end``; False if the disk refused."""
        try:
            if self._fh is None:
                self._spool.dir.mkdir(parents=True, exist_ok=True)
                self._ensure_meta()
                qpath = self._spool.dir / QUEUE_FILE
                self._fh = open(qpath, "ab", buffering=0)
                self._tail = os.fstat(self._fh.fileno()).st_size
                if self._tail:
                    with open(qpath, "rb") as r:
                        r.seek(self._tail - 1)
                        if r.read(1) != b"\n":  # torn last line from a killed process: isolate it
                            self._fh.write(b"\n")
                            self._tail += 1
            line = (json.dumps(_record(item)) + "\n").encode("utf-8")
            off = self._tail
            self._fh.write(line)
            item["_off"], item["_end"] = off, off + len(line)
            self._tail = off + len(line)
            self._dirty = True
            return True
        except OSError:
            self._disk_warning()
            return False

    def _read_at(self, pos: int) -> dict | None:
        """Read the journal entry at ``pos`` (cv held). Corrupt/torn lines are skipped (``None``)."""
        try:
            with open(self._spool.dir / QUEUE_FILE, "rb") as f:
                f.seek(pos)
                line = f.readline()
        except OSError:
            self._pos = self._tail
            return None
        if not line.endswith(b"\n"):
            self._pos = self._tail
            return None
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or "path" not in item:
                raise ValueError
        except ValueError:
            self._pos = pos + len(line)
            return None
        item["_off"], item["_end"] = pos, pos + len(line)
        return item

    def _count_pending(self) -> int:
        if self._tail <= self._pos:
            return 0
        items, _ = read_jsonl(self._spool.dir / QUEUE_FILE, self._pos)
        return len(items)

    def _persist_pos(self, force: bool = False) -> None:
        if self._tail <= self._pos:
            return
        now = time.monotonic()
        if force or now - self._last_pos_write >= _POS_INTERVAL:
            self._spool.write_pos(self._pos)
            self._last_pos_write = now

    def _ack(self, item: dict, keep_blob: bool = False) -> None:
        """Mark ``item`` as handled (delivered or dead-lettered) and compact the journal (cv held)."""
        if "_off" not in item:
            if self._mem and self._mem[0] is item:
                self._mem.popleft()
        else:
            self._pos = max(self._pos, item["_end"])
            while self._cache and self._cache[0]["_end"] <= self._pos:
                self._cache.popleft()
        if item.get("blob") and not keep_blob:
            (self._spool.dir / BLOB_DIR / item["blob"]["file"]).unlink(missing_ok=True)
        if self._pos >= self._tail and self._tail > 0:
            try:  # caught up: everything in the journal is acknowledged
                os.truncate(self._spool.dir / QUEUE_FILE, 0)
                self._pos = self._tail = 0
                (self._spool.dir / POS_FILE).unlink(missing_ok=True)
            except OSError:
                pass
        else:
            self._persist_pos()

    def _fsync_if_due(self) -> None:
        with self._cv:
            if not self._dirty or self._fh is None or time.monotonic() - self._last_sync < _FSYNC_INTERVAL:
                return
            fd = self._fh.fileno()
            self._dirty = False
            self._last_sync = time.monotonic()
        try:
            os.fsync(fd)
        except OSError:
            pass

    def _dead_letter(self, item: dict, reason: str) -> None:
        rec = {**_record(item), "reason": reason}
        try:
            with open(self._spool.dir / DEAD_FILE, "ab") as f:
                f.write((json.dumps(rec) + "\n").encode("utf-8"))
        except OSError:
            log.exception("Could not write the dead-letter file")
            return
        self.dead += 1
        if not self._warned_dead:
            self._warned_dead = True
            print(
                f"pyrometer: a payload ({item['path']}) was repeatedly refused by the server ({reason}); "
                f"moved to {self._spool.dir / DEAD_FILE} and continuing. Retry it with: pyrometer sync",
                file=sys.stderr,
            )

    # -- sender --

    def _next(self):
        """Pick the next unit of work (cv held): ``(kind, item)`` or ``None``."""
        if self._create is not None:
            return "create", None
        if self._eph:
            return "eph", self._eph[0]
        while self._pos < self._tail:
            if self._cache and self._cache[0]["_off"] == self._pos:
                return "item", self._cache[0]
            self._cache.clear()
            item = self._read_at(self._pos)
            if item is not None:
                return "item", item
        if self._mem:
            return "item", self._mem[0]
        return None

    def _attempt(self, kind: str, item: dict | None) -> str:
        """One delivery attempt: ``"ok"``, ``"retry"`` or ``"reject"`` (permanent refusal)."""
        try:
            if kind == "create":
                resp = self._client.post("/runs", json=self._create)  # type: ignore[union-attr]
            else:
                assert item is not None
                body = materialize_body(item, self._spool.dir)
                if item.get("blob") and body is None:
                    log.warning("Media file for %s is missing; dropping the payload", item["path"])
                    return "ok"
                kwargs = {"timeout": item["timeout"]} if "timeout" in item else {}
                resp = self._client.request(  # type: ignore[union-attr]
                    item["method"], item["path"], json=body, headers={BATCH_HEADER: item["id"]}, **kwargs
                )
        except _CONNECT_ERRORS:
            return "retry"
        except Exception:
            log.exception("Unexpected error delivering %s", item["path"] if item else "/runs")
            return "retry"
        if resp.status_code < 300:
            return "ok"
        return "retry" if _retryable(resp.status_code) else "reject"

    def _healthy(self) -> bool:
        try:
            return self._client.get("/health", timeout=3).status_code < 300  # type: ignore[union-attr]
        except Exception:
            return False

    def _loop(self) -> None:
        backoff = _BACKOFF_START
        while True:
            self._fsync_if_due()
            with self._cv:
                if self._abandon:
                    return
                work = None if (self._offline or self._halted) else self._next()
                if work is None:
                    if self._closing:
                        return
                    self._cv.wait(_FSYNC_INTERVAL if self._dirty else None)
                    continue
            kind, item = work
            outcome = self._attempt(kind, item)
            if kind == "item" and outcome == "retry":
                self._note_failure(item)
            poison = (
                kind == "item" and outcome == "retry" and self._fail_n >= _POISON_ATTEMPTS
                and not self._closing and self._healthy()
            )
            with self._cv:
                if self._abandon:
                    return
                if kind == "eph":
                    if self._eph and self._eph[0] is item:
                        self._eph.popleft()
                    continue
                if kind == "create":
                    if outcome == "ok":
                        self._create = None
                        self._spool.write_meta(created=True)
                        backoff = _BACKOFF_START
                        continue
                    if outcome == "reject":
                        self._halted = True
                        if not self._warned_halt:
                            self._warned_halt = True
                            print(
                                "pyrometer: the server refused to create this run; nothing will be sent. "
                                f"Data stays in {self._spool.dir} (fix the cause, then run: pyrometer sync)",
                                file=sys.stderr,
                            )
                        continue
                elif outcome == "ok":
                    self._ack(item)
                    self._fail_id, self._fail_n = None, 0
                    backoff = _BACKOFF_START
                    continue
                elif outcome == "reject" or poison:
                    self._dead_letter(item, "rejected by the server" if outcome == "reject" else "repeated server errors")
                    self._ack(item, keep_blob=True)
                    self._fail_id, self._fail_n = None, 0
                    continue
                if self._closing:
                    return  # final drain: one failed attempt is enough, the rest stays journaled
                if kind == "item" and outcome == "retry" and self._fail_n >= _POISON_ATTEMPTS:
                    self._fail_n = 0  # the server is down, not the payload: start counting again
                deadline = time.monotonic() + backoff
            while True:  # back off, still fsyncing the journal while we wait
                self._fsync_if_due()
                with self._cv:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._closing or self._abandon:
                        break
                    self._cv.wait(min(remaining, _FSYNC_INTERVAL))
            backoff = min(backoff * 2, _BACKOFF_CAP)

    def _note_failure(self, item: dict) -> None:
        if self._fail_id == item["id"]:
            self._fail_n += 1
        else:
            self._fail_id, self._fail_n = item["id"], 1
