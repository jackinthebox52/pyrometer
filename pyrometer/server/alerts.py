"""Webhook alerts: rule matching, formatting, rate limiting and asynchronous delivery.

Ingestion only ever calls the ``notify_*`` / ``check_metrics`` functions, which put a job on a small
bounded queue and return. A single daemon worker thread does the rule lookups, evaluation and HTTP
calls, so a slow or dead webhook never delays ingestion. Webhook URLs are secrets: they are never
logged, and failures are reported without them.
"""

import logging
import operator
import os
import queue
import threading
import time
from collections import deque

import httpx

from pyrometer.server import db

_log = logging.getLogger("pyrometer.server.alerts")

KINDS = ("run_crashed", "run_failed", "event", "anomaly", "metric_threshold")
FORMATS = ("generic", "ntfy", "discord", "slack")
SEVERITIES = ("info", "warning", "error", "critical")
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}

TIMEOUT_S = 5.0
DEDUPE_WINDOW_S = 300.0
_QUEUE_MAX = 1000

_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_sent: dict[str, deque] = {}            # rule id -> send timestamps (rate limit)
_recent: dict[tuple, float] = {}        # (rule id, dedupe key) -> last sent (dedupe)
_violating: dict[tuple[str, str], bool] = {}  # (rule id, run id) -> currently over the threshold


def public_url() -> str | None:
    """Dashboard base URL for links (``PYROMETER_PUBLIC_URL``); ``None`` omits links."""
    url = os.environ.get("PYROMETER_PUBLIC_URL", "").strip().rstrip("/")
    return url or None


def rate_limit_per_min() -> int:
    """Max alerts per rule per minute (``PYROMETER_ALERT_RATE``, default 10)."""
    try:
        return max(1, int(os.environ.get("PYROMETER_ALERT_RATE", "10")))
    except ValueError:
        return 10


def mask_url(url: str) -> str:
    """A non-secret hint of a webhook URL (scheme and host only)."""
    try:
        parsed = httpx.URL(url)
        return f"{parsed.scheme}://{parsed.host}/…"
    except Exception:
        return "…"


def public_rule(rule: dict) -> dict:
    """The rule as admins see it after creation: the URL is masked."""
    return {**rule, "url": mask_url(rule["url"])}


def validate_url(url: str) -> str | None:
    """An error message if ``url`` is not an http(s) URL, else ``None``."""
    try:
        parsed = httpx.URL(url)
    except Exception:
        return "Invalid webhook URL"
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return "Webhook URL must be http(s)"
    return None


# ---------------------------------------------------------------------------
# Public entry points (called from request handlers / threads; never block)
# ---------------------------------------------------------------------------

def notify_run_status(run_id: str, status: str) -> None:
    """A run just transitioned to ``status``; alerts for ``crashed`` and ``failed``."""
    if status in ("crashed", "failed"):
        _submit(("status", run_id, status))


def notify_event(run_id: str, event: dict) -> None:
    """A new event was ingested (not a deduped duplicate)."""
    _submit(("event", run_id, event))


def check_metrics(run_id: str, metrics: list[dict]) -> None:
    """Newly ingested metric points, for threshold rules."""
    _submit(("metrics", run_id, [{"step": m["step"], "name": m["name"], "value": m["value"]} for m in metrics]))


def send_test(rule: dict) -> str | None:
    """Send a test message synchronously. Returns an error string, or ``None`` on success."""
    return _deliver(rule, _build_alert(rule, "Pyrometer test alert", "This is a test alert from Pyrometer.", None, "info"))


def flush(timeout: float = 5.0) -> None:
    """Wait until queued alert jobs are processed (used by tests and shutdown)."""
    deadline = time.time() + timeout
    while _queue.unfinished_tasks and time.time() < deadline:
        time.sleep(0.01)


def _submit(job: tuple) -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="pyrometer-alerts", daemon=True)
            _worker.start()
    try:
        _queue.put_nowait(job)
    except queue.Full:
        _log.warning("alert queue full; dropping a job")


def _run_worker() -> None:
    while True:
        job = _queue.get()
        try:
            _handle(job)
        except Exception:
            _log.exception("alert job failed")  # exception text could embed a URL only via httpx; see _deliver
        finally:
            _queue.task_done()


# ---------------------------------------------------------------------------
# Job handling
# ---------------------------------------------------------------------------

def _handle(job: tuple) -> None:
    kind = job[0]
    run_id = job[1]
    run = db.db_get_run_for_alert(run_id)
    if run is None:
        return
    project = run["project"]
    if kind == "status":
        status = job[2]
        rule_kind = "run_crashed" if status == "crashed" else "run_failed"
        for rule in db.db_matching_alert_rules([rule_kind], project):
            title = f"Run {status}: {run['name']}"
            body = f"Run {run['name']!r} in project {project!r} {status}."
            _fire(rule, run, title, body, "error", dedupe=(rule_kind, run_id))
    elif kind == "event":
        ev = job[2]
        sev = ev.get("severity") or "info"
        is_anomaly = ev.get("event_type") == "anomaly_detected"
        wanted = ["event", "anomaly"] if is_anomaly else ["event"]
        for rule in db.db_matching_alert_rules(wanted, project):
            if rule["kind"] == "event" and _sev_rank(sev) < _sev_rank(rule["min_severity"] or "error"):
                continue
            title = f"{'Anomaly' if is_anomaly else 'Event'} ({sev}): {run['name']}"
            body = f"{ev.get('message', '')} (run {run['name']!r}, project {project!r})"
            _fire(rule, run, title, body, sev, dedupe=(rule["kind"], run_id, ev.get("event_type"), ev.get("message")))
    elif kind == "metrics":
        _check_thresholds(run, job[2])


def _sev_rank(sev: str) -> int:
    return SEVERITIES.index(sev) if sev in SEVERITIES else 0


def _check_thresholds(run: dict, points: list[dict]) -> None:
    names = sorted({p["name"] for p in points})
    rules = db.db_matching_alert_rules(["metric_threshold"], run["project"], names)
    for rule in rules:
        cmp = OPS.get(rule["op"] or "")
        if cmp is None or rule["threshold"] is None:
            continue
        key = (rule["id"], run["run_id"])
        for p in sorted((p for p in points if p["name"] == rule["metric"]), key=lambda p: p["step"]):
            now_over = bool(cmp(p["value"], rule["threshold"]))
            was_over = _violating.get(key, False)
            _violating[key] = now_over
            if now_over and not was_over:  # fire on crossing only, not on every point beyond it
                title = f"Metric threshold: {run['name']}"
                body = (f"{rule['metric']} = {p['value']:.6g} {rule['op']} {rule['threshold']:g} "
                        f"at step {p['step']} (run {run['name']!r}, project {run['project']!r})")
                _fire(rule, run, title, body, "warning", dedupe=("metric_threshold", run["run_id"], rule["metric"]))


def _fire(rule: dict, run: dict, title: str, body: str, severity: str, dedupe: tuple) -> None:
    """Apply dedupe and rate limit, then send."""
    now = time.time()
    dkey = (rule["id"], *dedupe)
    if now - _recent.get(dkey, 0.0) < DEDUPE_WINDOW_S:
        return
    sent = _sent.setdefault(rule["id"], deque())
    while sent and now - sent[0] > 60:
        sent.popleft()
    if len(sent) >= rate_limit_per_min():
        _log.warning("alert rate limit reached for rule %s; dropping alert", rule["id"])
        return
    for k in [k for k, t in _recent.items() if now - t >= DEDUPE_WINDOW_S]:
        del _recent[k]
    _recent[dkey] = now
    sent.append(now)
    base = public_url()
    link = f"{base}/#/runs/{run['run_id']}" if base else None
    error = _deliver(rule, _build_alert(rule, title, body, link, severity))
    if error:
        _log.warning("alert delivery failed for rule %s: %s", rule["id"], error)


# ---------------------------------------------------------------------------
# Formatting and delivery
# ---------------------------------------------------------------------------

def _build_alert(rule: dict, title: str, body: str, link: str | None, severity: str) -> dict:
    """A ready-to-send request: ``{"json": ...}`` or ``{"content": ..., "headers": ...}``."""
    text = f"{title}\n{body}" + (f"\n{link}" if link else "")
    fmt = rule["format"]
    if fmt == "slack":
        return {"json": {"text": text}}
    if fmt == "discord":
        return {"json": {"content": text[:2000]}}
    if fmt == "ntfy":
        headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": "4" if severity in ("error", "critical") else "3"}
        if link:
            headers["Click"] = link
        return {"content": body.encode(), "headers": headers}
    return {"json": {"title": title, "message": body, "severity": severity, "rule": rule["name"], "link": link}}


def _deliver(rule: dict, req: dict) -> str | None:
    """POST to the webhook. Returns an error description that never contains the URL."""
    try:
        resp = _post(rule["url"], req)
        if resp.status_code >= 400:
            return f"HTTP {resp.status_code}"
        return None
    except Exception as e:
        return type(e).__name__  # message may embed the URL


def _post(url: str, req: dict) -> httpx.Response:
    return httpx.post(url, timeout=TIMEOUT_S, follow_redirects=False, **req)
