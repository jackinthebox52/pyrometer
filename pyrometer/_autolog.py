"""Autolog engine: automatic step counter, learning rate, loss, throughput and (optionally) gradient health.

One :class:`_AutologEngine` per :class:`~pyrometer._run.Run`. It is started by ``pyrometer.init`` (so it
covers ``with pyrometer.init(...) as run:`` and plain ``run = init(...)`` alike) and stopped by
``Run._finish``. It attaches only to *global* PyTorch hooks, never to the user's objects:

* ``torch.optim.optimizer.register_optimizer_step_post_hook``: a step counter, learning rates,
  steps/sec, token/sample counters (one cheap callback per ``optimizer.step()``).
* A scoped wrap of ``torch.Tensor.backward`` and ``torch.autograd.backward``: records a *detached
  reference* to each scalar loss (no GPU sync). Values are read lazily, one logging cadence later.
* Optional (config, default off): global forward-hook discovery of parameters for gradient statistics
  (the machinery of ``Run.watch``) and a cheap global gradient-norm / NaN check.

Everything it logs lives under the ``auto/`` prefix (plus the ``tokens_seen`` / ``samples_seen`` axis metrics).
Callbacks never raise into user code.
"""

from __future__ import annotations

import collections
import functools
import logging
import os
import sys
import threading
import time
import weakref
from typing import Any, Callable

log = logging.getLogger(__name__)

# Backwards (of one loss stream) without any optimizer step before the loss fallback starts logging. Far above any
# realistic gradient-accumulation window, so a program that does step an optimizer never triggers it.
_FALLBACK_AFTER = 1024
_LOSS_PENDING_MAX = 4096  # per loss stream, between two optimizer steps (guards a loop that never steps)


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------


class AutologConfig:
    """Live-editable autolog settings (``run.config``). Assigning an option takes effect immediately.

    Options:
        log_every: Log the auto metrics every N driving optimizer steps (and at step 1). Default 10.
        loss: Capture losses from ``backward()``. Default True.
        lr: Log per-param-group learning rates. Default True.
        throughput: Log steps/sec, tokens/sec, samples/sec and data-wait. Default True.
        step_optimizer: The optimizer whose ``step()`` drives the step counter. ``None`` (default): the
            first optimizer that steps.
        grad_norm: Log the global gradient L2 norm (``auto/grad_norm``) at cadence; NaN/Inf in it is
            reported by anomaly detection. One foreach-norm per logged step, read lazily. Default False.
        grad_health: Per-parameter gradient/weight statistics for every module (the ``run.watch``
            machinery via global hooks, or just the model passed to ``run.track(model=...)``).
            Expensive; default False.
        grad_log_freq: Backward passes between per-parameter statistics (grad_health). Default 100.
        profile_every: Profile one whole iteration every N driving steps (``None`` = never). Default None.
        capture_grad: Also capture ``torch.autograd.grad(loss, ...)`` calls as losses (for hand-rolled
            update rules). Off by default because libraries and gradient penalties call it with scalar
            outputs that are not training losses. Default False.
        episode_window: Episodes in the rolling window of ``run.log_episode`` statistics. Default 100.
        episode_log_every: ``run.log_episode`` logs the statistics every N episodes. Default 10.
    """

    _DEFAULTS: dict[str, Any] = {
        "log_every": 10,
        "loss": True,
        "lr": True,
        "throughput": True,
        "step_optimizer": None,
        "grad_norm": False,
        "grad_health": False,
        "grad_log_freq": 100,
        "profile_every": None,
        "capture_grad": False,
        "episode_window": 100,
        "episode_log_every": 10,
    }

    def __init__(self, **overrides: Any) -> None:
        object.__setattr__(self, "_listeners", [])
        for k, v in self._DEFAULTS.items():
            object.__setattr__(self, k, v)
        for k, v in overrides.items():
            setattr(self, k, v)

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in self._DEFAULTS:
            raise AttributeError(f"unknown autolog option {name!r}; valid options: {sorted(self._DEFAULTS)}")
        if name in ("log_every", "grad_log_freq", "episode_window", "episode_log_every"):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        elif name == "profile_every":
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise ValueError("profile_every must be a positive integer or None")
        elif name != "step_optimizer":
            value = bool(value)
        object.__setattr__(self, name, value)
        for fn in list(self._listeners):
            fn(name)

    def as_dict(self) -> dict[str, Any]:
        """The current options (``step_optimizer`` is shown by class name)."""
        d = {k: getattr(self, k) for k in self._DEFAULTS}
        if d["step_optimizer"] is not None:
            d["step_optimizer"] = type(d["step_optimizer"]).__name__
        return d

    def __repr__(self) -> str:
        return f"AutologConfig({', '.join(f'{k}={v!r}' for k, v in self.as_dict().items())})"


# ---------------------------------------------------------------------------------------------
# Global backward wrapper (shared by every active engine, refcounted)
# ---------------------------------------------------------------------------------------------

_wrap_lock = threading.Lock()
_engines: tuple = ()  # copy-on-write, read lock-free by the wrappers
_orig_tensor_backward: Callable | None = None
_orig_autograd_backward: Callable | None = None
_tls = threading.local()  # .active: inside a wrapped backward (Tensor.backward calls autograd.backward)


def _w_tensor_backward(self, *args, **kwargs):
    if not getattr(_tls, "active", False):
        engines = _engines
        if engines:
            try:
                f = sys._getframe(1)
                key = (f.f_code, f.f_lineno)
                for e in engines:
                    e._on_backward(key, self)
            except Exception:
                pass
    prev = getattr(_tls, "active", False)
    _tls.active = True
    try:
        return _orig_tensor_backward(self, *args, **kwargs)  # type: ignore[misc]
    finally:
        _tls.active = prev


def _w_autograd_backward(tensors, *args, **kwargs):
    if not getattr(_tls, "active", False):
        engines = _engines
        if engines:
            try:
                import torch

                ts = (tensors,) if isinstance(tensors, torch.Tensor) else tuple(tensors)
                f = sys._getframe(1)
                key = (f.f_code, f.f_lineno)
                for t in ts:
                    for e in engines:
                        e._on_backward(key, t)
            except Exception:
                pass
    prev = getattr(_tls, "active", False)
    _tls.active = True
    try:
        return _orig_autograd_backward(tensors, *args, **kwargs)  # type: ignore[misc]
    finally:
        _tls.active = prev


def _install_backward(engine: "_AutologEngine") -> None:
    global _engines, _orig_tensor_backward, _orig_autograd_backward
    import torch

    with _wrap_lock:
        _engines = _engines + (engine,)
        if torch.Tensor.backward is not _w_tensor_backward:
            _orig_tensor_backward = torch.Tensor.backward
            torch.Tensor.backward = functools.wraps(_orig_tensor_backward)(_w_tensor_backward)  # type: ignore[method-assign]
        if torch.autograd.backward is not _w_autograd_backward:
            _orig_autograd_backward = torch.autograd.backward
            torch.autograd.backward = functools.wraps(_orig_autograd_backward)(_w_autograd_backward)  # type: ignore[assignment]


def _uninstall_backward(engine: "_AutologEngine") -> None:
    """Drop ``engine``; when no engine is left restore the original functions (unless somebody else wrapped
    on top of ours meanwhile: then our wrapper stays but is inert and the other wrapper keeps working)."""
    global _engines
    import torch

    with _wrap_lock:
        _engines = tuple(e for e in _engines if e is not engine)
        if _engines:
            return
        if torch.Tensor.backward is _w_tensor_backward and _orig_tensor_backward is not None:
            torch.Tensor.backward = _orig_tensor_backward  # type: ignore[method-assign]
        if torch.autograd.backward is _w_autograd_backward and _orig_autograd_backward is not None:
            torch.autograd.backward = _orig_autograd_backward  # type: ignore[assignment]


_grad_engines: tuple = ()  # engines with config.capture_grad on (copy-on-write)
_orig_autograd_grad: Callable | None = None


def _w_autograd_grad(outputs, *args, **kwargs):
    if not getattr(_tls, "active", False):
        engines = _grad_engines
        if engines:
            try:
                import torch

                ts = (outputs,) if isinstance(outputs, torch.Tensor) else tuple(outputs)
                f = sys._getframe(1)
                key = (f.f_code, f.f_lineno)
                for t in ts:
                    if isinstance(t, torch.Tensor):
                        for e in engines:
                            e._on_backward(key, t)
            except Exception:
                pass
    return _orig_autograd_grad(outputs, *args, **kwargs)  # type: ignore[misc]


def _install_grad(engine: "_AutologEngine") -> None:
    global _grad_engines, _orig_autograd_grad
    import torch

    with _wrap_lock:
        if engine in _grad_engines:
            return
        _grad_engines = _grad_engines + (engine,)
        if torch.autograd.grad is not _w_autograd_grad:
            _orig_autograd_grad = torch.autograd.grad
            torch.autograd.grad = functools.wraps(_orig_autograd_grad)(_w_autograd_grad)  # type: ignore[assignment]


def _uninstall_grad(engine: "_AutologEngine") -> None:
    """Same contract as :func:`_uninstall_backward` for the ``torch.autograd.grad`` wrapper."""
    global _grad_engines
    import torch

    with _wrap_lock:
        _grad_engines = tuple(e for e in _grad_engines if e is not engine)
        if _grad_engines:
            return
        if torch.autograd.grad is _w_autograd_grad and _orig_autograd_grad is not None:
            torch.autograd.grad = _orig_autograd_grad  # type: ignore[assignment]


def name_loss(name: str, loss):
    """Tag a loss tensor so its ``backward`` is logged as ``auto/loss/<name>``; returns the tensor."""
    try:
        loss._pyro_loss_name = str(name)
    except Exception:
        pass
    return loss


# ---------------------------------------------------------------------------------------------
# Dataloader wrapper
# ---------------------------------------------------------------------------------------------


def _infer_batch_size(batch: Any, depth: int = 0) -> int | None:
    shape = getattr(batch, "shape", None)
    if shape is not None:
        try:
            return int(shape[0])
        except Exception:
            return None
    if depth < 3:
        if isinstance(batch, dict) and batch:
            return _infer_batch_size(next(iter(batch.values())), depth + 1)
        if isinstance(batch, (list, tuple)) and batch:
            return _infer_batch_size(batch[0], depth + 1)
    return None


class _TrackedLoader:
    """Iterable wrapper that reports each batch and the time spent waiting for it."""

    def __init__(self, engine: "_AutologEngine", loader: Any) -> None:
        self._engine = engine
        self._loader = loader

    def __iter__(self):
        it = iter(self._loader)
        clock = time.perf_counter
        while True:
            t0 = clock()
            try:
                batch = next(it)
            except StopIteration:
                return
            wait = clock() - t0
            try:
                self._engine._on_batch(batch, wait)
            except Exception:
                pass
            yield batch

    def __len__(self) -> int:
        return len(self._loader)

    def __getattr__(self, name: str):
        return getattr(self._loader, name)


# ---------------------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------------------


class _Stream:
    """One loss stream: all backwards from one call site (or one ``name_loss`` tag)."""

    __slots__ = ("order", "tag", "pending", "count", "metric", "total", "fb")

    def __init__(self, order: int, tag: str | None) -> None:
        self.order = order
        self.tag = tag
        self.pending: collections.deque = collections.deque(maxlen=_LOSS_PENDING_MAX)
        self.count = 0  # backwards since the last driving step (micro-batches)
        self.metric: str | None = None  # fixed at the first emission
        self.total = 0  # backwards of this stream ever (the loss fallback's step)
        self.fb = False  # the loss fallback is active for this stream


class _AutologEngine:
    def __init__(self, run: Any, enabled: bool, config: AutologConfig) -> None:
        self.run = run
        self.config = config
        self.enabled = enabled
        self._active = False
        self._lock = threading.RLock()

        self.step = 0  # optimizer steps completed (the driving optimizer)
        self.saw_step = False
        self._implicit = -1  # step handed to step-less log() calls while no optimizer has stepped
        self._hook_handle: Any = None
        self._driver_ref: weakref.ref | None = None
        self._opts: list[weakref.ref] = []
        self._opt_ids: set[int] = set()

        self._streams: dict[Any, _Stream] = {}
        self._lazy: list[tuple[int, float, dict[str, Any]]] = []  # (step, ts, {metric: device tensor})

        # window for rates
        self._win_t: float | None = None
        self._win_k = 0
        self._win_tokens = 0
        self._win_samples = 0
        self._last_emit_k = 0

        # track() hints and counters
        self._tokens_hint: int | Callable | None = None
        self._samples_hint: int | Callable | None = None
        self._model_ref: weakref.ref | None = None
        self._loader_tracked = False
        self.tokens_seen = 0
        self.samples_seen = 0
        self._wait_total = 0.0

        # gradient health
        self._gh_on = False
        self._fwd_handle: Any = None
        self._seen_modules: "weakref.WeakSet" = weakref.WeakSet()
        self._module_n = 0

        # episode statistics (run.log_episode)
        self._ep_lock = threading.Lock()
        self._ep_returns: collections.deque = collections.deque(maxlen=100)
        self._ep_lengths: collections.deque = collections.deque(maxlen=100)
        self._ep_info: dict[str, collections.deque] = {}
        self._ep_count = 0
        self._ep_since_emit = 0
        self._ep_step = 0  # step of the most recent episode (where the final flush logs)
        self.env_steps = 0

        self._prof: tuple | None = None
        config._listeners.append(self._on_config)

    # -- lifecycle --

    def start(self, base_step: int | None, resumed: bool) -> None:
        if not self.enabled or self._active:
            return
        try:
            import torch
            from torch.optim.optimizer import register_optimizer_step_post_hook
        except ImportError:
            self.enabled = False
            return
        self.step = int(base_step or 0)
        self._implicit = self.step if resumed else self.step - 1
        self._hook_handle = register_optimizer_step_post_hook(self._on_optimizer_step)
        self._active = True
        _install_backward(self)
        if self.config.capture_grad:
            _install_grad(self)
        self._sync_grad_health()

    def stop(self, resolve: bool = True) -> None:
        if not self._active:
            return
        self._active = False
        try:
            self._hook_handle.remove()
        except Exception:
            pass
        _uninstall_backward(self)
        _uninstall_grad(self)
        self._gh_off()
        if self._prof is not None:
            try:
                self._prof[0].stop()
            except Exception:
                pass
            self._prof = None
        try:
            if resolve:
                if self.saw_step and self.step != self._last_emit_k:
                    self._emit(self.step, None)
                elif not self.saw_step:
                    for s in list(self._streams.values()):
                        self._fallback_flush(s, True)
                self._resolve_lazy()
        except Exception:
            log.debug("autolog final flush failed", exc_info=True)
        try:
            self.config._listeners.remove(self._on_config)
        except ValueError:
            pass

    # -- step resolution for Run.log --

    def resolve_step(self) -> int:
        if not self._active:
            raise ValueError(
                "step is required: autolog is off, so there is no automatic step counter "
                "(pass step=... or leave autolog enabled)"
            )
        if self.saw_step:
            return self.step
        with self._lock:
            self._implicit += 1  # no optimizer has stepped yet: count the step-less log calls
            return self._implicit

    def set_step(self, n: int) -> None:
        with self._lock:
            self.step = int(n)
            self._implicit = self.step - 1
        self.run._last_step = self.step
        self.run._step_known = True

    # -- track() --

    def track(self, tokens_per_batch, samples_per_batch, model, dataloader):
        if tokens_per_batch is not None:
            if not callable(tokens_per_batch) and (not isinstance(tokens_per_batch, (int, float)) or tokens_per_batch < 0):
                raise ValueError("tokens_per_batch must be a non-negative number or a callable(batch) -> int")
            if callable(tokens_per_batch) and dataloader is None and not self._loader_tracked:
                raise ValueError("a callable tokens_per_batch needs dataloader= (it is called with each batch)")
            self._tokens_hint = tokens_per_batch if callable(tokens_per_batch) else int(tokens_per_batch)
        if samples_per_batch is not None:
            if not callable(samples_per_batch) and (not isinstance(samples_per_batch, (int, float)) or samples_per_batch < 0):
                raise ValueError("samples_per_batch must be a non-negative number or a callable(batch) -> int")
            self._samples_hint = samples_per_batch if callable(samples_per_batch) else int(samples_per_batch)
        if model is not None:
            self._model_ref = weakref.ref(model)
            if self._gh_on:
                self._gh_off()
                self._sync_grad_health()
        if dataloader is not None:
            if not self._active:
                return dataloader
            return _TrackedLoader(self, dataloader)
        return None

    def _on_batch(self, batch: Any, wait: float) -> None:
        tokens = self._tokens_hint
        samples = self._samples_hint
        if callable(tokens):
            tokens = tokens(batch)
        if callable(samples):
            samples = samples(batch)
        if samples is None:
            samples = _infer_batch_size(batch)
        with self._lock:
            self._loader_tracked = True
            self._wait_total += wait
            if tokens:
                self.tokens_seen += int(tokens)
            if samples:
                self.samples_seen += int(samples)

    # -- episode statistics --

    def log_episode(self, return_: float, length: int | None, step: int | None, info: dict[str, Any]) -> None:
        """Record one finished episode; every ``episode_log_every`` episodes log rolling statistics."""
        ret = float(return_)
        n = None if length is None else int(length)
        window = self.config.episode_window
        extras: dict[str, float] = {}
        for k, v in info.items():
            if isinstance(v, bool):
                v = float(v)
            if isinstance(v, (int, float)):
                extras[str(k)] = float(v)
            else:
                try:
                    extras[str(k)] = float(v)  # 0-d tensors / numpy scalars
                except (TypeError, ValueError):
                    pass
        with self._ep_lock:
            if self._ep_returns.maxlen != window:
                self._ep_returns = collections.deque(self._ep_returns, maxlen=window)
                self._ep_lengths = collections.deque(self._ep_lengths, maxlen=window)
                self._ep_info = {k: collections.deque(d, maxlen=window) for k, d in self._ep_info.items()}
            self._ep_returns.append(ret)
            if n is not None:
                self._ep_lengths.append(n)
                self.env_steps += n
            for k, v in extras.items():
                self._ep_info.setdefault(k, collections.deque(maxlen=window)).append(v)
            self._ep_count += 1
            self._ep_step = step if step is not None else self.step
            self._ep_since_emit += 1
            due = self._ep_since_emit >= self.config.episode_log_every
        if due:
            if step is None:
                step = self.resolve_step()
            self._emit_episodes(step)

    def flush_episodes(self) -> None:
        """Log the statistics of episodes recorded since the last emission (called when the run ends)."""
        if self._ep_since_emit:
            self._emit_episodes(self._ep_step)

    def _emit_episodes(self, step: int) -> None:
        with self._ep_lock:
            if not self._ep_returns:
                return
            self._ep_since_emit = 0
            r = list(self._ep_returns)
            metrics: dict[str, float] = {
                "episode/return_mean": sum(r) / len(r),
                "episode/return_min": min(r),
                "episode/return_max": max(r),
                "episode/count": float(self._ep_count),
            }
            if self._ep_lengths:
                metrics["episode/length_mean"] = sum(self._ep_lengths) / len(self._ep_lengths)
                metrics["env_steps"] = float(self.env_steps)
            for k, d in self._ep_info.items():
                if d:
                    metrics[f"episode/{k}_mean"] = sum(d) / len(d)
        self.run._log(metrics, step, time.time())

    # -- backward capture --

    def _on_backward(self, key: Any, t: Any) -> None:
        if not (self._active and self.config.loss):
            return
        try:
            if not (t.numel() == 1 and t.is_floating_point()):
                return
            tag = getattr(t, "_pyro_loss_name", None)
        except Exception:
            return
        skey = ("tag", tag) if tag else key
        d = t.detach()
        with self._lock:
            s = self._streams.get(skey)
            if s is None:
                s = self._streams[skey] = _Stream(len(self._streams), tag)
            s.pending.append(d)
            s.count += 1
            s.total += 1
            due = not self.saw_step and (s.fb or s.total >= _FALLBACK_AFTER) and s.total % self.config.log_every == 0
            if due:
                s.fb = True
        if due:
            self._fallback_flush(s, False)

    # -- optimizer step hook --

    def _on_optimizer_step(self, optimizer: Any, args: Any, kwargs: Any) -> None:
        if not self._active:
            return
        try:
            self._step_hook(optimizer)
        except Exception:
            log.debug("autolog step hook failed", exc_info=True)

    def _is_driver(self, optimizer: Any) -> bool:
        chosen = self.config.step_optimizer
        if chosen is not None:
            return optimizer is chosen
        d = self._driver_ref() if self._driver_ref is not None else None
        if d is None:
            self._driver_ref = weakref.ref(optimizer)
            return True
        return d is optimizer

    def _step_hook(self, optimizer: Any) -> None:
        now = time.perf_counter()
        with self._lock:
            if id(optimizer) not in self._opt_ids:
                self._opt_ids.add(id(optimizer))
                self._opts.append(weakref.ref(optimizer))
            if not self._is_driver(optimizer):
                return
            self.step = k = self.step + 1
            self.saw_step = True
            run = self.run
            run._last_step = k
            run._step_known = True
            micro = max((s.count for s in self._streams.values()), default=0) or 1
            if not self._loader_tracked:
                if isinstance(self._tokens_hint, int):
                    self.tokens_seen += self._tokens_hint * micro
                if isinstance(self._samples_hint, int):
                    self.samples_seen += self._samples_hint * micro
            cadence = k == 1 or k % self.config.log_every == 0
        if self._prof is not None or self.config.profile_every:
            self._profile_tick(k)
        if cadence:
            self._emit(k, now)
        else:
            with self._lock:
                for s in self._streams.values():
                    s.pending.clear()
                    s.count = 0

    def _emit(self, k: int, now: float | None) -> None:
        """Log the cadence metrics for step ``k``. ``now=None`` (final flush at stop) skips rate metrics."""
        cfg = self.config
        metrics: dict[str, float] = {}
        device_vals: dict[str, Any] = {}
        ts = time.time()
        with self._lock:
            self._last_emit_k = k
            dt = None if (self._win_t is None or now is None) else now - self._win_t
            ds = k - self._win_k
            if cfg.throughput and dt and dt > 0 and ds > 0:
                metrics["auto/steps_per_sec"] = ds / dt
                if self.tokens_seen:
                    metrics["auto/tokens_per_sec"] = (self.tokens_seen - self._win_tokens) / dt
                if self.samples_seen:
                    metrics["auto/samples_per_sec"] = (self.samples_seen - self._win_samples) / dt
                if self._loader_tracked:
                    metrics["auto/data_wait_ms"] = self._wait_total / ds * 1000.0
                    metrics["auto/data_wait_frac"] = min(1.0, self._wait_total / dt)
            if cfg.throughput:
                if self.tokens_seen:
                    metrics["tokens_seen"] = float(self.tokens_seen)
                if self.samples_seen:
                    metrics["samples_seen"] = float(self.samples_seen)
            if now is not None:
                self._win_t, self._win_k = now, k
            self._win_tokens, self._win_samples = self.tokens_seen, self.samples_seen
            self._wait_total = 0.0
            if cfg.lr:
                metrics.update(self._lr_metrics())
            if cfg.loss:
                device_vals = self._loss_values()
            for s in self._streams.values():
                s.pending.clear()
                s.count = 0
            opt = self._driver_opt()
        if cfg.grad_norm and opt is not None:
            gn = self._grad_norm_tensor(opt)
            if gn is not None:
                device_vals["auto/grad_norm"] = gn
        self._ship(k, ts, metrics, device_vals)

    def _ship(self, k: int, ts: float, metrics: dict[str, float], device_vals: dict[str, Any]) -> None:
        """Log ready values now; device tensors are queued and read at the next cadence (no GPU sync)."""
        # Values from earlier cadences are certainly computed by now: reading them cannot stall the GPU.
        self._resolve_lazy()
        if metrics:
            self.run._log(metrics, k, ts)
        ready: dict[str, float] = {}
        for name, tensor in device_vals.items():
            if tensor.device.type == "cpu":  # nothing to wait for
                try:
                    ready[name] = float(tensor)
                except Exception:
                    pass
        if ready:
            self.run._log(ready, k, ts)
        pending = {n: t for n, t in device_vals.items() if n not in ready}
        if pending:
            with self._lock:
                self._lazy.append((k, ts, pending))

    def _fallback_flush(self, s: "_Stream", final: bool) -> None:
        """Loss fallback for programs that never step an optimizer (manual updates, custom rules): the loss of
        each stream is logged as the mean of every ``log_every`` backwards, at the stream's own backward count.
        ``final`` also logs the trailing partial chunk (when the run ends)."""
        L = self.config.log_every
        with self._lock:
            vals = list(s.pending)
            n, total = len(vals), s.total
            first = total - n + 1
            chunks: list[tuple[int, list[Any]]] = []
            consumed = 0
            e = ((first - 1) // L + 1) * L  # first multiple of L covered by the pending values
            while e <= total:
                lo = max(e - L + 1, first)
                chunks.append((e, vals[lo - first:e - first + 1]))
                consumed = e - first + 1
                e += L
            if final and consumed < n:
                chunks.append((total, vals[consumed:]))
                consumed = n
            s.pending = collections.deque(vals[consumed:], maxlen=_LOSS_PENDING_MAX)
            metric = self._stream_metric(s)
        ts = time.time()
        for step, chunk in chunks:
            mean = self._mean(chunk)
            if mean is not None:
                self._ship(self.step + step, ts, {}, {metric: mean})

    def _stream_metric(self, s: "_Stream") -> str:
        if s.metric is None:
            if s.tag:
                s.metric = f"auto/loss/{s.tag}"
            elif len(self._streams) >= 2:
                s.metric = f"auto/loss/{s.order}"
            else:
                s.metric = "auto/loss"
        return s.metric

    @staticmethod
    def _mean(vals: list[Any]) -> Any:
        import torch

        try:
            return torch.stack([v.reshape(()).float() for v in vals]).mean()
        except Exception:  # tensors on different devices
            try:
                return torch.tensor(sum(float(v) for v in vals) / len(vals))
            except Exception:
                return None

    def _driver_opt(self) -> Any:
        chosen = self.config.step_optimizer
        if chosen is not None:
            return chosen
        return self._driver_ref() if self._driver_ref is not None else None

    def _lr_metrics(self) -> dict[str, float]:
        out: dict[str, float] = {}
        j = 0
        driver = self._driver_opt()
        for ref in self._opts:
            opt = ref()
            if opt is None:
                continue
            if opt is driver:
                prefix = "auto/lr/"
            else:
                j += 1
                prefix = f"auto/lr/opt{j}/"
            for i, g in enumerate(opt.param_groups):
                try:
                    out[f"{prefix}{i}"] = float(g["lr"])
                except Exception:
                    pass
        return out

    def _loss_values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in [s for s in self._streams.values() if s.pending]:
            metric = self._stream_metric(s)
            mean = self._mean(list(s.pending))
            if mean is not None:
                out[metric] = mean
        return out

    def _grad_norm_tensor(self, opt: Any) -> Any:
        try:
            import torch

            grads = [
                p.grad for g in opt.param_groups for p in g["params"]
                if p.grad is not None and not p.grad.is_sparse
            ]
            if not grads:
                return None
            norms = torch._foreach_norm([g.detach() for g in grads])
            dev = norms[0].device
            return torch.linalg.vector_norm(torch.stack([n.to(dev, torch.float32) for n in norms]))
        except Exception:
            return None

    def _resolve_lazy(self) -> None:
        """Read queued device values (one ``.item()`` each) and log them at their original step/time."""
        with self._lock:
            ready, self._lazy = self._lazy, []
        for k, ts, tensors in ready:
            vals: dict[str, float] = {}
            for name, t in tensors.items():
                try:
                    vals[name] = float(t)
                except Exception:
                    pass
            if vals:
                self.run._log(vals, k, ts)

    # -- profiling tied to the step counter --

    def _profile_tick(self, k: int) -> None:
        prof, self._prof = self._prof, None
        if prof is not None:
            p, step, ts = prof
            try:
                p.stop()
                self.run._ship_profile_async(p, step, ts)
            except Exception:
                log.debug("autolog profile stop failed", exc_info=True)
        n = self.config.profile_every
        if n and k % n == 0:  # profile the whole next iteration (forward, backward, step k + 1)
            try:
                import torch.profiler as tp

                p = tp.profile(
                    activities=[tp.ProfilerActivity.CPU, tp.ProfilerActivity.CUDA],
                    record_shapes=True, profile_memory=True,
                )
                p.start()
                self._prof = (p, k + 1, time.time())
            except Exception:
                log.debug("autolog profile start failed", exc_info=True)

    # -- gradient health --

    def _on_config(self, name: str) -> None:
        if name == "capture_grad" and self._active:
            if self.config.capture_grad:
                _install_grad(self)
            else:
                _uninstall_grad(self)
        if name in ("grad_health", "grad_log_freq") and self._active:
            self._gh_off()
            self._sync_grad_health()

    def _sync_grad_health(self) -> None:
        if not (self._active and self.config.grad_health) or self._gh_on:
            return
        model = self._model_ref() if self._model_ref is not None else None
        try:
            if model is not None:
                self.run.watch(model, log_freq=self.config.grad_log_freq)
            else:
                from torch.nn.modules.module import register_module_forward_hook

                self._fwd_handle = register_module_forward_hook(self._discover)
            self._gh_on = True
        except Exception:
            log.debug("could not enable gradient health", exc_info=True)

    def _gh_off(self) -> None:
        if not self._gh_on:
            return
        self._gh_on = False
        if self._fwd_handle is not None:
            try:
                self._fwd_handle.remove()
            except Exception:
                pass
            self._fwd_handle = None
        self.run._remove_watch_hooks()
        self._seen_modules = weakref.WeakSet()

    def _discover(self, module: Any, args: Any, output: Any) -> None:
        """Global forward hook: hook the parameters of every module the first time it runs."""
        if module in self._seen_modules:
            return
        try:
            self._seen_modules.add(module)
            self._module_n += 1
            base = f"{type(module).__name__}.{self._module_n}"
            for pname, p in module.named_parameters(recurse=False):
                if p.requires_grad:
                    h = p.register_hook(self.run._make_param_hook(f"{base}.{pname}", p, self.config.grad_log_freq))
                    self.run._watch_hooks.append(h)
        except Exception:
            log.debug("grad-health discovery failed", exc_info=True)


# ---------------------------------------------------------------------------------------------
# Rank awareness
# ---------------------------------------------------------------------------------------------


def rank_info() -> tuple[int | None, int]:
    """``(global_rank, world_size)`` from the launcher's environment (``RANK``/``LOCAL_RANK``/``WORLD_SIZE``);
    rank is ``None`` when not distributed."""
    def _int(name: str) -> int | None:
        try:
            return int(os.environ[name])
        except (KeyError, ValueError):
            return None

    world = _int("WORLD_SIZE") or 1
    rank = _int("RANK")
    if rank is None and world > 1:
        rank = _int("LOCAL_RANK")
    return rank, world


class NullRun:
    """Stand-in returned by ``init`` on non-zero ranks (``rank_zero_only``): accepts every ``Run`` call and does nothing."""

    run_id = None
    server = ""
    resumed = False
    last_step = None
    step = 0

    def __init__(self, config: AutologConfig | None = None) -> None:
        self.config = config or AutologConfig()

    def __enter__(self) -> "NullRun":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def profile(self, step: int | None = None):
        import contextlib

        return contextlib.nullcontext()

    def track(self, *, tokens_per_batch=None, samples_per_batch=None, model=None, dataloader=None):
        return dataloader

    def name_loss(self, name: str, loss):
        return loss

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None
