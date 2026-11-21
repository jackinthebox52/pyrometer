"""Webhook alerts: rule matching, formatting, rate limiting and asynchronous delivery.

Ingestion only ever calls the ``notify_*`` / ``check_metrics`` functions, which put a job on a small
bounded queue and return. A single daemon worker thread does the rule lookups, evaluation and HTTP
calls, so a slow or dead webhook never delays ingestion. A rule has one or more triggers and fires at
most once per matching event. Rate-limit, dedupe and threshold-crossing state live in the database, so
a restart neither re-fires nor forgets. Failed deliveries are retried a few times with backoff (as timer
jobs, never blocking the worker). Webhook URLs are secrets: they are never logged, and failures are
reported without them.
"""

import hashlib
import logging
import operator
import os
import queue
import threading
import time

import httpx

from pyrometer.server import db

_log = logging.getLogger("pyrometer.server.alerts")

KINDS = ("run_crashed", "run_failed", "event", "anomaly", "metric_threshold")
FORMATS = ("generic", "ntfy", "discord", "slack")
SEVERITIES = ("info", "warning", "error", "critical")
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}

TIMEOUT_S = 5.0
DEDUPE_WINDOW_S = 300.0
RETRY_BACKOFF_S = (2.0, 10.0, 30.0)  # delays before retry 1, 2, 3 (so up to 4 attempts in total)
_QUEUE_MAX = 1000

_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_pending_retries = 0                    # delivery retries waiting on a timer (guarded by _worker_lock)


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
    if "\u2026" in url:  # the masked form shown by the API: saving it back would destroy the real secret
        return "Webhook URL looks masked; send the full URL"
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
    """Wait until queued alert jobs (and pending delivery retries) are processed (tests and shutdown)."""
    deadline = time.time() + timeout
    while (_queue.unfinished_tasks or _pending_retries) and time.time() < deadline:
        time.sleep(0.01)


def reset_state() -> None:
    """Forget all persisted rate-limit/dedupe/threshold state (tests)."""
    db.db_alert_clear_state()


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
    if kind == "deliver":
        _attempt(*job[1:])
        return
    run = db.db_get_run_for_alert(job[1])
    if run is None:
        return
    run_id, project = run["run_id"], run["project"]
    rules = db.db_matching_alert_rules(project)
    if kind == "status":
        status = job[2]
        rule_kind = "run_crashed" if status == "crashed" else "run_failed"
        for rule in rules:
            if any(t["kind"] == rule_kind for t in rule["triggers"]):
                title = f"Run {status}: {run['name']}"
                body = f"Run {run['name']!r} in project {project!r} {status}."
                _fire(rule, run, title, body, "error", dedupe=(rule_kind, run_id))
    elif kind == "event":
        ev = job[2]
        sev = ev.get("severity") or "info"
        is_anomaly = ev.get("event_type") == "anomaly_detected"
        for rule in rules:
            if any(_event_matches(t, sev, is_anomaly) for t in rule["triggers"]):  # once per rule, however many match
                title = f"{'Anomaly' if is_anomaly else 'Event'} ({sev}): {run['name']}"
                body = f"{ev.get('message', '')} (run {run['name']!r}, project {project!r})"
                _fire(rule, run, title, body, sev, dedupe=("event", run_id, ev.get("event_type"), ev.get("message")))
    elif kind == "metrics":
        _check_thresholds(run, rules, job[2])


def _event_matches(trigger: dict, sev: str, is_anomaly: bool) -> bool:
    if trigger["kind"] == "anomaly":
        return is_anomaly
    if trigger["kind"] == "event":
        return _sev_rank(sev) >= _sev_rank(trigger.get("min_severity") or "error")
    return False


def _sev_rank(sev: str) -> int:
    return SEVERITIES.index(sev) if sev in SEVERITIES else 0


def _check_thresholds(run: dict, rules: list[dict], points: list[dict]) -> None:
    for rule in rules:
        for trig in rule["triggers"]:
            cmp = OPS.get(trig.get("op") or "")
            if trig["kind"] != "metric_threshold" or cmp is None or trig.get("threshold") is None:
                continue
            tkey = f"{trig['metric']}{trig['op']}{trig['threshold']!r}"
            for p in sorted((p for p in points if p["name"] == trig["metric"]), key=lambda p: p["step"]):
                now_over = bool(cmp(p["value"], trig["threshold"]))
                was_over = db.db_alert_swap_violating(rule["id"], tkey, run["run_id"], now_over)
                if now_over and not was_over:  # fire on crossing only, not on every point beyond it
                    title = f"Metric threshold: {run['name']}"
                    body = (f"{trig['metric']} = {p['value']:.6g} {trig['op']} {trig['threshold']:g} "
                            f"at step {p['step']} (run {run['name']!r}, project {run['project']!r})")
                    _fire(rule, run, title, body, "warning", dedupe=("metric_threshold", run["run_id"], tkey))


def _fire(rule: dict, run: dict, title: str, body: str, severity: str, dedupe: tuple) -> None:
    """Apply dedupe and rate limit (persisted), then send."""
    dkey = hashlib.sha1(repr(dedupe).encode()).hexdigest()
    verdict = db.db_alert_acquire(rule["id"], dkey, DEDUPE_WINDOW_S, rate_limit_per_min(), run_id=run["run_id"])
    if verdict == "rate":
        _log.warning("alert rate limit reached for rule %s; dropping alert", rule["id"])
    if verdict != "ok":
        return
    base = public_url()
    link = f"{base}/#/runs/{run['run_id']}" if base else None
    _attempt(rule["id"], _build_alert(rule, title, body, link, severity), dkey, 0)


def _attempt(rule_id: str, req: dict, dkey: str, attempt: int) -> None:
    """One delivery attempt; a retryable failure schedules the next one on a timer."""
    global _pending_retries
    rule = db.db_get_alert_rule(rule_id)  # re-read: it may have been deleted or disabled meanwhile
    if rule is None or not rule["enabled"]:
        return
    error, retryable = _deliver_once(rule, req)
    if error is None:
        return
    if retryable and attempt < len(RETRY_BACKOFF_S):
        with _worker_lock:
            _pending_retries += 1
        timer = threading.Timer(RETRY_BACKOFF_S[attempt], _retry, (rule_id, req, dkey, attempt + 1))
        timer.daemon = True
        timer.start()
        return
    _log.warning("alert delivery failed for rule %s after %d attempt(s): %s", rule_id, attempt + 1, error)
    db.db_alert_release(rule_id, dkey)  # nothing arrived: let an identical alert try again


def _retry(rule_id: str, req: dict, dkey: str, attempt: int) -> None:
    global _pending_retries
    _submit(("deliver", rule_id, req, dkey, attempt))
    with _worker_lock:
        _pending_retries -= 1


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


def _deliver_once(rule: dict, req: dict) -> tuple[str | None, bool]:
    """POST to the webhook. Returns ``(error, retryable)``; the error text never contains the URL."""
    try:
        resp = _post(rule["url"], req)
    except Exception as e:
        return type(e).__name__, True  # message may embed the URL; network failures are worth retrying
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code}", resp.status_code in (408, 425, 429) or resp.status_code >= 500
    return None, False


def _deliver(rule: dict, req: dict) -> str | None:
    """Single attempt (the test endpoint). Returns an error description, or ``None`` on success."""
    return _deliver_once(rule, req)[0]


def _post(url: str, req: dict) -> httpx.Response:
    return httpx.post(url, timeout=TIMEOUT_S, follow_redirects=False, **req)
