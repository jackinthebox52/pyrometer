"""Run — the handle returned by pyrometer.init() that the user interacts with."""

import atexit
import logging
import os
import signal
import sys
import threading
import uuid
import time
from contextlib import contextmanager

import httpx

from pyrometer import _gpu
from pyrometer._anomaly import AnomalyConfig, GradDetector, MetricDetector
from pyrometer._transport import Spool, Transport

log = logging.getLogger(__name__)


class _TeeWriter:
    """Forwards writes to an original stream while buffering complete lines.

    Installed on sys.stdout / sys.stderr by Run.__init__ so console output is
    both displayed normally and shipped to the server as structured log lines.
    Thread-safe: a per-instance lock serialises write() and flush() calls.
    """

    def __init__(self, original, stream_name: str, on_line) -> None:
        self._orig = original
        self._name = stream_name
        self._on_line = on_line  # callable(line: str, stream: str)
        self._buf = ""
        self._lock = threading.RLock()

    def write(self, text: str) -> int:
        with self._lock:
            self._orig.write(text)
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line:
                    try:
                        self._on_line(line, self._name)
                    except Exception:
                        pass
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                try:
                    self._on_line(self._buf, self._name)
                except Exception:
                    pass
                self._buf = ""
            self._orig.flush()

    def fileno(self) -> int:
        return self._orig.fileno()

    def isatty(self) -> bool:
        return self._orig.isatty()

    def __getattr__(self, name):
        return getattr(self._orig, name)


HEARTBEAT_INTERVAL = 30.0
_INLINE_MEDIA_MAX = 32 * 1024  # larger string media (histograms) go to a blob file

# Runs that have not finished yet. A single excepthook / sys.exit / SIGTERM handler (installed on first
# use) decides how they end: uncaught exception or sys.exit(nonzero) -> failed, Ctrl-C or SIGTERM -> killed.
_active_runs: "set[Run]" = set()
_excepthook_installed = False
_our_sigterm = None  # the SIGTERM handler we installed (so a second init does not wrap it again)


def _exit_code_status(code) -> str:
    """Run status implied by a ``SystemExit`` code: ``None``/0 -> completed, anything else -> failed."""
    return "completed" if code is None or code == 0 else "failed"


def _install_exit_hooks() -> None:
    global _excepthook_installed
    if not _excepthook_installed:
        _excepthook_installed = True
        prev_hook = sys.excepthook

        def excepthook(exc_type, exc, tb) -> None:
            status = "killed" if issubclass(exc_type, KeyboardInterrupt) else "failed"
            for run in list(_active_runs):
                run._exit_status, run._exit_from_sys_exit = status, False
            prev_hook(exc_type, exc, tb)

        sys.excepthook = excepthook

        # An uncaught SystemExit never reaches excepthook, so sys.exit() is where its code is visible.
        # (``raise SystemExit(1)`` written out by hand bypasses it and ends as completed.)
        prev_exit = sys.exit

        def exit_(status=None) -> None:
            for run in list(_active_runs):
                run._exit_status, run._exit_from_sys_exit = _exit_code_status(status), True
            prev_exit(status)

        sys.exit = exit_
    _install_sigterm()


def _install_sigterm() -> None:
    """Chain our SIGTERM handling in front of whatever the application installed.

    Ours marks every live run ``killed`` and finishes it, then hands over to the previous
    handler (or to the default action: terminate). ``SIG_IGN`` is respected: a process that ignores
    SIGTERM keeps running, so its runs must not be closed. Needs the main thread; a ``init()`` from
    another thread leaves it for a later one.
    """
    global _our_sigterm
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        prev = signal.getsignal(signal.SIGTERM)
        if prev is _our_sigterm and _our_sigterm is not None:
            return
        if prev is signal.SIG_IGN:
            return

        def on_term(signum, frame) -> None:
            for run in list(_active_runs):
                run._exit_status, run._exit_from_sys_exit = "killed", False
                run._finish("killed")
            if callable(prev):
                prev(signum, frame)
            else:  # SIG_DFL (or None: handler not installed from Python): behave as if we were not here
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTERM)

        signal.signal(signal.SIGTERM, on_term)
        _our_sigterm = on_term
    except (ValueError, OSError, AttributeError):
        pass  # platform without SIGTERM, or not allowed here


_MAX_SAMPLE_BYTES = 60 * 1024  # server limit is 64 KB per sample
_MAX_SAMPLES_PER_REQUEST = 200


class Run:
    """Represents an active training run.

    Returned by :func:`pyrometer.init`. Not instantiated directly.

    Manages three background threads:
    - A flush thread that hands buffered metrics to the sender every 5 seconds
      (or immediately when the buffer reaches 100 entries).
    - A sender thread that delivers payloads in order from a write-ahead journal
      (``~/.pyrometer/spool/<run_id>/``), retrying failures with exponential backoff
      and replaying automatically when the server comes back. Anything still
      undelivered at exit can be uploaded with ``pyrometer sync``.
    - A system monitor thread that polls pynvml every 2 seconds and POSTs
      GPU utilization, memory, temperature, and power draw to the server.
    """

    def __init__(
        self,
        run_id: str,
        server_url: str,
        api_key: str | None = None,
        offline: bool = False,
        anomaly_detection: bool | dict | None = True,
        create: dict | None = None,
    ) -> None:
        self.run_id = run_id
        self._server = server_url
        self._offline = offline
        self._client: httpx.Client | None = None
        if not offline:
            self._client = httpx.Client(
                base_url=server_url,
                timeout=10,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
        self._spool = Spool(run_id)
        self._transport = Transport(
            run_id,
            self._client,
            self._spool,
            offline=offline,
            meta_defaults={"server": server_url, "run_id": run_id},
            create=create,
        )
        self._stop = threading.Event()

        self._buffer: list[dict] = []
        self._wal_batch = ""  # batch id of the metrics currently buffered (and in the write-ahead log)
        # Re-entrant: the SIGTERM handler runs finish() on the main thread, possibly while that very
        # thread is inside log()/print() holding one of these locks; a plain Lock would deadlock it.
        self._lock = threading.RLock()
        self._finished = False
        self._last_step = 0
        self._step_known = False
        self.resumed = False
        """True if this run reattached to an existing run (``init(resume=...)``)."""

        # watch() state — gradient/weight monitoring
        self._watch_hooks: list = []
        self._param_stats_buffer: list[dict] = []
        self._param_stats_batch = ""  # batch id of the buffered stats (and their write-ahead copy)
        self._param_stats_lock = threading.RLock()
        self._watch_grad_norm_history: dict[str, list[float]] = {}
        self._watch_low_norm_streak: dict[str, int] = {}

        # stdout/stderr capture
        self._log_buffer: list[dict] = []
        self._log_batch = ""  # batch id of the buffered lines (and their write-ahead copy)
        self._log_lock = threading.RLock()
        self._original_stdout = None
        self._original_stderr = None
        self._install_stdout_capture()

        # metric anomaly detection (loss spike, NaN)
        self._anomaly_cfg = AnomalyConfig.from_option(anomaly_detection)
        self._metric_detector = MetricDetector(self._anomaly_cfg)
        self._grad_detector = GradDetector(self._anomaly_cfg)
        self._anomaly_lock = threading.Lock()

        self._our_pid = os.getpid()

        # Liveness: a heartbeat every HEARTBEAT_INTERVAL s (sent immediately at start). Never spooled.
        self._last_heartbeat = float("-inf")  # monotonic() can be < HEARTBEAT_INTERVAL right after boot
        self._exit_status = "completed"
        self._probe_thread: threading.Thread | None = None
        self._exit_from_sys_exit = False  # status came from sys.exit(): cleared if the program carries on
        _active_runs.add(self)
        _install_exit_hooks()
        atexit.register(self._atexit)

        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def log(self, metrics: dict, step: int) -> None:
        """Log a dict of scalar metrics at a given training step.

        Buffers entries in memory and flushes to the server every 5 seconds or
        when the buffer reaches 100 entries — whichever comes first. This is
        safe to call inside a tight training loop without hammering the API.

        Args:
            metrics: Dict mapping metric names to float values.
                     Names can use slash notation, e.g. ``"train/loss"``.
            step: Global training step (integer). Used as the x-axis in charts.

        Example::

            run.log({"train/loss": loss.item(), "lr": scheduler.get_last_lr()[0]}, step=global_step)
        """
        if self._finished:
            return
        if self._exit_from_sys_exit:  # a caught sys.exit(): the program is evidently still running
            self._exit_status, self._exit_from_sys_exit = "completed", False
        self._last_step = step
        self._step_known = True
        ts = time.time()
        with self._lock:
            entries = [
                {"step": step, "timestamp": ts, "name": name, "value": float(value)}
                for name, value in metrics.items()
            ]
            if entries:
                if not self._buffer:
                    self._wal_batch = uuid.uuid4().hex
                self._buffer.extend(entries)
                self._transport.wal_append(self._wal_batch, entries)
            if len(self._buffer) >= 100:
                self._flush_locked()
        # Anomaly checks run outside the buffer lock; HTTP calls happen in spawned threads.
        for name, value in metrics.items():
            try:
                self._check_metric_anomaly(name, float(value), step)
            except (ValueError, TypeError):
                pass

    @property
    def server(self) -> str:
        """Base URL of the Pyrometer server this run reports to (as resolved by :func:`pyrometer.init`)."""
        return self._server

    @property
    def last_step(self) -> int | None:
        """The most recent step known for this run, or ``None`` if there is none yet.

        Fresh run: the last step you passed to :meth:`log`. Resumed run: starts as the highest
        step already stored on the server, so you can continue with ``range(run.last_step + 1, ...)``.
        ``None`` for a new run before the first :meth:`log`, and for a run resumed offline (the
        server is not consulted).
        """
        return self._last_step if self._step_known else None

    def _start_hardware_probe(self, known: dict) -> None:
        """Fill in the torch/NVML-dependent hardware fields in the background.

        ``import torch`` and ``nvmlInit`` take seconds, so ``init`` does not wait for them: this thread runs them and
        PATCHes the run with whatever differs from the ``known`` (cheap) fingerprint. The PATCH goes
        through the journal like any payload, so it also works after an outage or offline.
        """
        def probe() -> None:
            try:
                from pyrometer import _hardware

                found = _hardware.fingerprint_slow()
                delta = {k: v for k, v in found.items() if v is not None and v != known.get(k)}
                if delta and not self._finished:
                    self._transport.submit("PATCH", f"/runs/{self.run_id}", delta)
            except Exception:
                log.debug("hardware probe failed", exc_info=True)

        self._probe_thread = threading.Thread(target=probe, daemon=True, name="pyrometer-hwprobe")
        self._probe_thread.start()

    def _seed_detectors(self) -> None:
        """Best-effort, non-blocking: preload metric anomaly history of a resumed run from the server.

        Without it a resumed process starts every detector cold and is blind for ``warmup`` samples.
        """
        if self._client is None or not self._anomaly_cfg.enabled:
            return

        def seed() -> None:
            try:
                resp = self._client.get(  # type: ignore[union-attr]
                    f"/runs/{self.run_id}/metrics/recent", params={"limit": self._anomaly_cfg.window}, timeout=5
                )
                if resp.status_code >= 300:
                    return
                history = resp.json().get("data") or {}
                with self._anomaly_lock:
                    for name, values in history.items():
                        self._metric_detector.seed(name, [v for v in values if isinstance(v, (int, float))])
            except Exception:
                log.debug("could not seed anomaly detectors", exc_info=True)

        threading.Thread(target=seed, daemon=True, name="pyrometer-seed").start()

    def _mark_resumed(self, last_step: int | None, previous_status: str | None, changes: dict | None) -> None:
        self.resumed = True
        self._seed_detectors()
        if last_step is not None:
            self._last_step = int(last_step)
            self._step_known = True
        meta: dict = {"previous_status": previous_status, "last_step": last_step}
        if changes:
            meta["config_changes"] = changes
        self.event("run resumed", event_type="run_resumed", severity="info", metadata=meta)

    def watch(self, model, log_freq: int = 100) -> None:
        """Monitor gradient and weight statistics for all trainable parameters.

        Registers a backward hook on every ``requires_grad`` parameter. Every
        ``log_freq`` backward passes, computes per-parameter gradient and weight
        statistics (L2 norm, min, max, mean, 32-bin histogram) and ships them to
        the server. Anomaly detection fires alert events automatically when:

        - NaN or Inf is detected in gradients or weights (reported once per parameter
          until it recovers).
        - A gradient norm exceeds 1000× its rolling median after a 20-sample warmup
          (rate-limited per parameter and per run).
        - A gradient norm stays below 1e-8 for 200 consecutive samples (dead layer).

        Thresholds are tunable via ``init(anomaly_detection={...})``; pass ``False`` to disable.

        Call once after :func:`pyrometer.init` and before the training loop::

            run.watch(model, log_freq=50)  # log every 50th backward pass

        Args:
            model: A ``torch.nn.Module`` whose parameters will be monitored.
            log_freq: How often (in number of backward passes per parameter)
                      to compute and ship statistics. Default 100.
        """
        try:
            import torch  # noqa: F401
        except ImportError:
            return

        self._remove_watch_hooks()

        def make_hook(param_name: str, param):
            call_count = [0]

            def hook(grad):
                if grad is None:
                    return
                call_count[0] += 1
                if call_count[0] % log_freq != 0:
                    return
                if self._finished:
                    return

                step = self._last_step
                ts = time.time()
                try:
                    import torch as _torch

                    # Move to CPU for all computations (safe across CUDA/CPU models)
                    g = grad.detach().float().cpu()
                    w = param.data.detach().float().cpu()
                    flat_g = g.flatten()
                    flat_w = w.flatten()

                    grad_norm = float(flat_g.norm(2))
                    has_nan_inf = (
                        not bool(_torch.isfinite(g).all())
                        or not bool(_torch.isfinite(w).all())
                    )

                    try:
                        gh = _torch.histc(flat_g, bins=32).tolist()
                        wh = _torch.histc(flat_w, bins=32).tolist()
                    except Exception:
                        gh, wh = [], []

                    stat = {
                        "step": step,
                        "timestamp": ts,
                        "param_name": param_name,
                        "grad_norm": grad_norm,
                        "grad_min": float(flat_g.min()),
                        "grad_max": float(flat_g.max()),
                        "grad_mean": float(flat_g.mean()),
                        "weight_norm": float(flat_w.norm(2)),
                        "weight_min": float(flat_w.min()),
                        "weight_max": float(flat_w.max()),
                        "weight_mean": float(flat_w.mean()),
                        "grad_histogram": gh,
                        "weight_histogram": wh,
                    }

                    with self._param_stats_lock:
                        if not self._param_stats_buffer:
                            self._param_stats_batch = uuid.uuid4().hex
                        self._param_stats_buffer.append(stat)
                        self._transport.wal_append(self._param_stats_batch, [stat], "param-stats")

                    threading.Thread(
                        target=self._check_grad_anomaly,
                        args=(param_name, grad_norm, step, has_nan_inf),
                        daemon=True,
                    ).start()
                except Exception:
                    log.exception("Error computing param stats for %s", param_name)

            return hook

        count = 0
        for name, param in model.named_parameters():
            if param.requires_grad:
                h = param.register_hook(make_hook(name, param))
                self._watch_hooks.append(h)
                count += 1
        log.debug("Watching %d parameters (log_freq=%d)", count, log_freq)

    def event(
        self,
        message: str,
        event_type: str = "custom",
        severity: str = "info",
        metadata: dict | None = None,
    ) -> None:
        """Log a structured event on the training timeline.

        Events appear as vertical marker lines on all metric charts in the
        dashboard. Hover a marker to see the event message and metadata.

        Args:
            message: Human-readable description of the event.
            event_type: Category string. Built-in values: ``"checkpoint_saved"``,
                        ``"lr_scheduled"``, ``"anomaly_detected"``, ``"cuda_error"``,
                        ``"oom_warning"``, ``"thermal_throttle"``, ``"custom"``.
            severity: One of ``"info"``, ``"warning"``, ``"error"``.
            metadata: Optional JSON-serialisable dict of extra context.

        Example::

            run.event("switched to phase 2 training", metadata={"phase": 2})
            run.event("checkpoint saved", event_type="checkpoint_saved",
                      metadata={"path": "/ckpt/step_5000.pt"})
        """
        if self._finished:
            return
        self._transport.submit("POST", f"/runs/{self.run_id}/events", {
            "timestamp": time.time(),
            "step": self._last_step,
            "event_type": event_type,
            "severity": severity,
            "message": message,
            "metadata": metadata or {},
        })

    def log_artifact(
        self,
        path: str,
        artifact_type: str = "checkpoint",
        metadata: dict | None = None,
    ) -> None:
        """Track a saved file artifact (model checkpoint, config, etc.).

        Does **not** upload the file — only records its path, on-disk size,
        timestamp, and associated metadata so you can see what was saved and
        when in the dashboard.

        Args:
            path: Absolute or relative path to the saved file.
            artifact_type: Category string, e.g. ``"checkpoint"``, ``"config"``,
                           ``"dataset"``, ``"export"``.
            metadata: Optional JSON-serialisable dict, e.g.
                      ``{"step": 5000, "val_loss": 0.23}``.

        Example::

            torch.save(model.state_dict(), ckpt_path)
            run.log_artifact(ckpt_path, artifact_type="checkpoint",
                             metadata={"val_loss": val_loss})
        """
        if self._finished:
            return
        import os
        try:
            size: int | None = os.path.getsize(path) if os.path.exists(path) else None
        except Exception:
            size = None
        self._transport.submit("POST", f"/runs/{self.run_id}/artifacts", {
            "timestamp": time.time(),
            "step": self._last_step,
            "artifact_type": artifact_type,
            "path": str(path),
            "size_bytes": size,
            "metadata": metadata or {},
        })

    def log_image(self, name: str, image, step: int | None = None) -> None:
        """Log an image at a given training step.

        Accepts PIL Images, NumPy arrays, or PyTorch tensors. Converts to PNG
        and is journaled to disk as a binary file and shipped to the server base64-encoded. Float tensors are
        clamped to [0, 1] and scaled to uint8 automatically.

        Args:
            name: Display name for this image stream, e.g. ``"predictions"``.
            image: One of:
                   - ``PIL.Image.Image``
                   - NumPy array with shape ``(H, W)`` or ``(H, W, C)``
                   - ``torch.Tensor`` with shape ``(C, H, W)``, ``(H, W)``,
                     or ``(N, C, H, W)`` (first image in batch is used).
            step: Training step. Defaults to the last step passed to
                  :meth:`log`.

        Example::

            run.log_image("predictions", output_image_tensor, step=global_step)
        """
        if self._finished:
            return
        try:
            data = self._image_to_png(image)
        except Exception:
            log.exception("Failed to encode image '%s'", name)
            return
        self._post_media(name, "image", data, step)

    def log_histogram(
        self,
        name: str,
        data,
        step: int | None = None,
        num_bins: int = 32,
    ) -> None:
        """Log a histogram of a tensor or array at a given training step.

        Flattens the input, computes a ``num_bins``-bin histogram, and ships
        it to the server as JSON. Useful for monitoring activation distributions,
        logit ranges, or any tensor distribution you care about beyond the
        automatic per-layer gradient histograms from :meth:`watch`.

        Args:
            name: Display name, e.g. ``"activation_distribution"``.
            data: ``torch.Tensor``, NumPy array, or plain Python list.
            step: Training step. Defaults to the last step passed to
                  :meth:`log`.
            num_bins: Number of histogram bins (default 32).

        Example::

            run.log_histogram("logits", model_output.detach(), step=global_step)
        """
        if self._finished:
            return
        import json as _json
        try:
            hist_json = self._compute_histogram(data, num_bins)
        except Exception:
            log.exception("Failed to compute histogram '%s'", name)
            return
        self._post_media(name, "histogram", _json.dumps(hist_json), step)

    def log_figure(self, name: str, figure, step: int | None = None) -> None:
        """Log a matplotlib figure at a given training step.

        Renders the figure to a PNG, journals it as a binary file, and ships it
        to the server. Useful for confusion matrices, precision-recall curves,
        or any custom matplotlib visualisation you want tracked over training.

        Args:
            name: Display name, e.g. ``"confusion_matrix"``.
            figure: A ``matplotlib.figure.Figure`` instance.
            step: Training step. Defaults to the last step passed to
                  :meth:`log`.

        Example::

            fig, ax = plt.subplots()
            ax.imshow(conf_matrix)
            run.log_figure("confusion_matrix", fig, step=global_step)
            plt.close(fig)
        """
        if self._finished:
            return
        try:
            data = self._figure_to_png(figure)
        except Exception:
            log.exception("Failed to encode figure '%s'", name)
            return
        self._post_media(name, "figure", data, step)

    def log_samples(
        self,
        step: int,
        samples: list[dict],
        name: str = "samples",
        mode: str = "replace",
    ) -> None:
        """Log generation samples (prompt / completion / reward ...) for a step.

        Each sample is an arbitrary JSON-able ``dict``; common keys are ``prompt``,
        ``completion``, ``reward`` and ``reference``. The dashboard's Samples tab
        builds its columns from whatever keys you log and sorts by ``reward`` when
        present. Non-JSON values are stringified. Limits: 64 KB per sample (the
        longest string values are truncated with a warning) and 200 samples per
        request (larger lists are split automatically). Log a few dozen samples
        every N steps rather than every step.

        Args:
            step: Training step these samples belong to.
            samples: List of dicts.
            name: Sample stream name, e.g. ``"eval_generations"``.
            mode: ``"replace"`` (default): the samples replace whatever is already stored for this
                ``(name, step)`` - so a resumed run that re-logs a step does not duplicate it, and
                calling ``log_samples`` again for the same step *overwrites* the earlier call.
                ``"append"``: add to the samples already stored for the step (log one step's
                samples in several calls).

        Example::

            run.log_samples(step, [{"prompt": p, "completion": c, "reward": r}], name="rollouts")
        """
        if self._finished or not samples:
            return
        if mode not in ("replace", "append"):
            raise ValueError(f"log_samples: mode must be 'replace' or 'append', not {mode!r}")
        import json as _json

        cleaned: list[dict] = []
        truncated = 0
        for smp in samples:
            if not isinstance(smp, dict):
                log.warning("log_samples: skipping non-dict sample of type %s", type(smp).__name__)
                continue
            smp = {str(k): v for k, v in smp.items()}
            for _ in range(8):  # shrink the longest string until the sample fits
                size = len(_json.dumps(smp, default=str))
                if size <= _MAX_SAMPLE_BYTES:
                    break
                key = max(smp, key=lambda k: len(smp[k]) if isinstance(smp[k], str) else 0)
                val = smp[key]
                if not isinstance(val, str) or len(val) < 100:
                    smp = {"error": "sample too large; dropped"}
                    break
                keep = max(50, len(val) - (size - _MAX_SAMPLE_BYTES) - 64)
                smp[key] = val[:keep] + "...[truncated]"
                truncated += 1
            cleaned.append(smp)
        if truncated:
            log.warning("log_samples: truncated %d oversized string value(s) to fit %d bytes per sample",
                        truncated, _MAX_SAMPLE_BYTES)
        for i in range(0, len(cleaned), _MAX_SAMPLES_PER_REQUEST):
            self._transport.submit(
                "POST",
                f"/runs/{self.run_id}/samples",
                {
                    "name": name,
                    "step": step,
                    "timestamp": time.time(),
                    "samples": cleaned[i:i + _MAX_SAMPLES_PER_REQUEST],
                    # a large list is split into several requests: only the first may replace
                    "mode": mode if i == 0 else "append",
                },
                timeout=30,
            )

    def finish(self) -> None:
        """Mark the run as completed, flush remaining metrics, and close the HTTP session.

        Always call this at the end of training — it flips the run status from
        ``running`` to ``completed`` in the database and ensures no metrics are lost.
        If you forget, the run is still closed at interpreter exit: ``completed`` on a
        normal exit, ``failed`` after an uncaught exception, ``killed`` on Ctrl-C or
        SIGTERM. (A hard kill, e.g. SIGKILL or power loss, is detected by the server:
        no heartbeat for 5 minutes marks the run ``crashed``.)
        Undelivered data gets a short final delivery attempt; anything the server
        still did not accept is saved to the spool directory and a single warning
        names it (upload later with ``pyrometer sync``). In offline mode the whole
        run is in the spool.

        Example::

            run.finish()
        """
        self._finish("completed")

    def _finish(self, status: str) -> None:
        if self._finished:
            return
        if self._probe_thread is not None and status != "killed":
            # let a nearly finished hardware probe still report (it submits only while the run is open)
            self._probe_thread.join(timeout=2)
        self._finished = True
        _active_runs.discard(self)
        try:
            atexit.unregister(self._atexit)
        except Exception:
            pass
        self._stop.set()
        self._flush_thread.join(timeout=5)
        # Stop sampling before the transport closes, or a late snapshot would be spilled to disk.
        if self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=3)
        self._flush()
        self._restore_stdout_capture()
        self._remove_watch_hooks()
        self._transport.submit("PATCH", f"/runs/{self.run_id}", {"status": status})
        left = self._transport.close(timeout=10.0)
        if self._client is not None:
            self._client.close()
        if self._offline:
            print(
                f"pyrometer: run {self.run_id} saved offline in {self._spool.dir}. "
                "Upload it with: pyrometer sync",
                file=sys.stderr,
            )
        elif left or self._transport.dead:
            extra = f" ({self._transport.dead} dead-lettered after repeated server errors)" if self._transport.dead else ""
            print(
                f"pyrometer: {left} payloads were not delivered{extra}; "
                f"saved in {self._spool.dir}. Upload them with: pyrometer sync",
                file=sys.stderr,
            )

    @contextmanager
    def profile(self, step: int):
        """Profile a single training step using torch.profiler.

        Activates ``torch.profiler.profile`` with CPU + CUDA activity capture,
        input shape recording, and memory profiling for exactly the code block
        inside the ``with`` statement. After the block exits, aggregated
        operator events (name, category, CPU/CUDA time, input shapes, memory
        delta) are shipped to the server in a background thread so the training
        loop is not blocked.

        Only profile periodically (e.g. every 100 steps) — each profiling call
        adds measurable overhead and can produce thousands of events.

        If ``torch`` is not importable the context manager is a silent no-op,
        so non-PyTorch code paths are unaffected.

        Args:
            step: The training step being profiled. Stored alongside all events
                  so the dashboard can correlate profiler data with metric charts.

        Example::

            with run.profile(step=global_step):
                loss = val_step(model, batch)
        """
        try:
            import torch.profiler as _tp
        except ImportError:
            yield
            return

        ts = time.time()
        with _tp.profile(
            activities=[_tp.ProfilerActivity.CPU, _tp.ProfilerActivity.CUDA],
            record_shapes=True,
            profile_memory=True,
            with_stack=False,
        ) as _prof:
            yield

        # Ship events off the hot path — failures are silently swallowed.
        threading.Thread(
            target=self._ship_profile,
            args=(_prof, step, ts),
            daemon=True,
        ).start()

    def _ship_profile(self, prof, step: int, ts: float) -> None:
        """Extract key_averages from a finished profiler session and POST to server."""
        try:
            events = []
            for evt in prof.key_averages():
                cpu_us = float(evt.self_cpu_time_total or 0)
                # self_cuda_time_total was renamed to self_device_time_total in PyTorch 2.x
                device_us = float(
                    getattr(evt, "self_device_time_total", None)
                    or getattr(evt, "self_cuda_time_total", None)
                    or 0
                )
                total_us = cpu_us + device_us
                if total_us < 1.0:
                    continue  # skip sub-microsecond bookkeeping noise

                category = "cuda_kernel" if device_us > 0 else "cpu_op"

                # Coerce input_shapes (may contain torch.Size) to plain lists.
                try:
                    shapes = [[int(s) for s in shape] for shape in (evt.input_shapes or [])]
                except Exception:
                    shapes = []

                # Net memory delta: device preferred, fall back to CPU.
                mem = (
                    getattr(evt, "cuda_memory_usage", None)
                    or getattr(evt, "self_device_memory_usage", None)
                    or evt.cpu_memory_usage
                    or None
                )

                events.append({
                    "step": step,
                    "timestamp": ts,
                    "name": evt.key,
                    "category": category,
                    "duration_us": total_us,
                    "cuda_time_us": device_us,
                    "cpu_time_us": cpu_us,
                    "input_shapes": shapes,
                    "memory_bytes": int(mem) if mem else None,
                    "device_id": 0,
                })

            if events:
                self._transport.submit("POST", f"/runs/{self.run_id}/profile", events, timeout=30)
                log.debug("Shipped %d profile events for step %d", len(events), step)
            else:
                log.warning("Profile step %d produced no events (all sub-microsecond)", step)
        except Exception:
            log.exception("Failed to ship profile events for step %d", step)

    # --- internals ---

    # -- stdout/stderr capture --

    def _install_stdout_capture(self) -> None:
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = _TeeWriter(sys.stdout, "stdout", self._on_log_line)
        sys.stderr = _TeeWriter(sys.stderr, "stderr", self._on_log_line)

    def _restore_stdout_capture(self) -> None:
        if self._original_stdout is not None:
            sys.stdout = self._original_stdout
            self._original_stdout = None
        if self._original_stderr is not None:
            sys.stderr = self._original_stderr
            self._original_stderr = None

    def _on_log_line(self, line: str, stream: str) -> None:
        ts = time.time()
        should_flush = False
        with self._log_lock:
            entry = {
                "timestamp": ts,
                "stream": stream,
                "line": line,
                "step": self._last_step,
            }
            if not self._log_buffer:
                self._log_batch = uuid.uuid4().hex
            self._log_buffer.append(entry)
            self._transport.wal_append(self._log_batch, [entry], "logs")
            should_flush = len(self._log_buffer) >= 50
        if should_flush:
            threading.Thread(target=self._flush_logs, daemon=True).start()

    def _flush_logs(self) -> None:
        with self._log_lock:
            if not self._log_buffer:
                return
            batch, bid = self._log_buffer[:], self._log_batch
            self._log_buffer.clear()
        self._transport.submit("POST", f"/runs/{self.run_id}/logs", batch, item_id=bid)
        self._transport.wal_clear("logs", bid)

    # -- gradient/weight monitoring --

    def _remove_watch_hooks(self) -> None:
        for h in self._watch_hooks:
            try:
                h.remove()
            except Exception:
                pass
        self._watch_hooks.clear()

    def _check_grad_anomaly(
        self, param_name: str, grad_norm: float, step: int, has_nan_inf: bool
    ) -> None:
        with self._anomaly_lock:
            msg = self._grad_detector.observe(param_name, grad_norm, step, has_nan_inf)
        if msg:
            self._fire_anomaly_event(param_name, step, msg)

    def _fire_anomaly_event(self, param_name: str, step: int, message: str) -> None:
        if self._finished:
            return
        self._transport.submit("POST", f"/runs/{self.run_id}/events", {
            "timestamp": time.time(),
            "step": step,
            "event_type": "anomaly_detected",
            "severity": "warning",
            "message": f"{param_name}: {message}",
            "metadata": {"param_name": param_name},
        })

    def _check_metric_anomaly(self, name: str, value: float, step: int) -> None:
        """Run the (conservative, deterministic) metric detector and report any finding."""
        with self._anomaly_lock:
            found = self._metric_detector.observe(name, value, step)
        if found:
            severity, message = found
            self._fire_metric_spike_event(name, step, message, severity)

    def _fire_metric_spike_event(self, name: str, step: int, message: str, severity: str) -> None:
        if self._finished:
            return
        self._transport.submit("POST", f"/runs/{self.run_id}/events", {
            "timestamp": time.time(),
            "step": step,
            "event_type": "anomaly_detected",
            "severity": severity,
            "message": message,
            "metadata": {"metric": name},
        })

    def _flush_param_stats(self) -> None:
        with self._param_stats_lock:
            if not self._param_stats_buffer:
                return
            batch, bid = self._param_stats_buffer[:], self._param_stats_batch
            self._param_stats_buffer.clear()
        self._transport.submit("POST", f"/runs/{self.run_id}/param-stats", batch, item_id=bid)
        self._transport.wal_clear("param-stats", bid)

    def _flush(self) -> None:
        with self._lock:
            self._flush_locked()
        self._flush_param_stats()
        self._flush_logs()

    def _flush_locked(self) -> None:
        """Must be called with self._lock held."""
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        self._transport.submit("POST", f"/runs/{self.run_id}/metrics", batch, item_id=self._wal_batch)
        self._transport.wal_clear("metrics", self._wal_batch)

    # ------------------------------------------------------------------
    # Media helpers
    # ------------------------------------------------------------------

    def _image_to_png(self, image) -> bytes:
        """Convert an image-like object to PNG bytes."""
        import io

        pil_img = self._to_pil(image)
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    def _to_pil(self, image):
        """Normalise various image formats to a PIL Image."""
        try:
            from PIL import Image as _PILImage
        except ImportError as exc:
            raise ImportError(
                "Pillow is required for log_image / log_figure. "
                "Install it with: pip install Pillow"
            ) from exc

        # Already a PIL Image
        if hasattr(image, "save") and hasattr(image, "mode"):
            return image

        # torch.Tensor
        try:
            import torch as _torch
            if isinstance(image, _torch.Tensor):
                t = image.detach().cpu()
                if t.ndim == 4:  # (N, C, H, W) — take first
                    t = t[0]
                if t.ndim == 3:  # (C, H, W) → (H, W, C)
                    t = t.permute(1, 2, 0)
                # Now (H, W) or (H, W, C)
                if t.is_floating_point():
                    t = (t.clamp(0, 1) * 255).byte()
                arr = t.numpy()
                if arr.ndim == 3 and arr.shape[2] == 1:
                    arr = arr[:, :, 0]
                return _PILImage.fromarray(arr)
        except ImportError:
            pass

        # NumPy array
        import numpy as _np
        arr = _np.asarray(image)
        if arr.dtype != _np.uint8:
            if arr.max() <= 1.0:
                arr = (arr * 255).clip(0, 255).astype(_np.uint8)
            else:
                arr = arr.clip(0, 255).astype(_np.uint8)
        return _PILImage.fromarray(arr)

    def _figure_to_png(self, figure) -> bytes:
        """Render a matplotlib figure to PNG bytes."""
        import io

        buf = io.BytesIO()
        figure.savefig(buf, format="png", bbox_inches="tight", dpi=96)
        return buf.getvalue()

    def _compute_histogram(self, data, num_bins: int) -> dict:
        """Compute a histogram dict with keys 'edges' and 'counts'."""
        try:
            import torch as _torch
            if isinstance(data, _torch.Tensor):
                flat = data.detach().float().cpu().flatten()
                vmin, vmax = float(flat.min()), float(flat.max())
                if vmin == vmax:
                    vmax = vmin + 1.0
                counts = _torch.histc(flat, bins=num_bins, min=vmin, max=vmax).tolist()
                edges = [vmin + i * (vmax - vmin) / num_bins for i in range(num_bins + 1)]
                return {"edges": edges, "counts": counts}
        except ImportError:
            pass

        import numpy as _np
        arr = _np.asarray(data, dtype=float).flatten()
        counts, edges = _np.histogram(arr, bins=num_bins)
        return {"edges": edges.tolist(), "counts": counts.tolist()}

    def _post_media(self, name: str, media_type: str, data: str | bytes, step: int | None) -> None:
        """Queue a media item for delivery.

        Raw bytes (images, figures) and large strings are journaled/spooled as separate binary
        files referenced from the JSONL; base64 happens only on the wire.
        """
        if self._finished:
            return
        resolved_step = step if step is not None else self._last_step
        body = {
            "step": resolved_step,
            "timestamp": time.time(),
            "name": name,
            "media_type": media_type,
        }
        blob: tuple[str, bytes, str] | None = None
        if isinstance(data, bytes):
            blob = ("data", data, "b64")
        elif len(data) > _INLINE_MEDIA_MAX:
            blob = ("data", data.encode("utf-8"), "utf8")
        else:
            body["data"] = data
        self._transport.submit(
            "POST",
            f"/runs/{self.run_id}/media",
            body,
            timeout=30,  # images can be large; use a longer timeout
            blob=blob,
        )

    def _flush_loop(self) -> None:
        self._heartbeat()
        while not self._stop.wait(5):
            self._flush()
            self._heartbeat()
        self._flush()  # drain on exit

    def _heartbeat(self) -> None:
        now = time.monotonic()
        if self._offline or self._finished or now - self._last_heartbeat < HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        self._transport.submit("POST", f"/runs/{self.run_id}/heartbeat", None, timeout=5, ephemeral=True)

    def _atexit(self) -> None:
        """Interpreter is exiting without finish(): close the run with the status the exit implies."""
        self._finish(self._exit_status)

    def _monitor_loop(self) -> None:
        try:
            import pynvml
            pynvml.nvmlInit()
            handles = _gpu.resolve_handles(pynvml)
        except Exception:
            return  # no GPU or pynvml unavailable
        if not handles:
            return

        while not self._stop.is_set():
            try:
                for snap in _gpu.sample_all(pynvml, handles, self._our_pid):
                    if self._stop.is_set():
                        break
                    self._transport.submit("POST", f"/runs/{self.run_id}/system", snap)
            except Exception:
                pass
            self._stop.wait(2)
