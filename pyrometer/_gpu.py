"""NVML sampling for the system monitor: every GPU visible to the process, plus per-process attribution."""

from __future__ import annotations

import os
import time
from typing import Any


def _resolve_handles(nvml: Any, visible: str | None) -> list[tuple[int, Any]]:
    """Return ``(position, handle)`` for each GPU the process can use.

    ``position`` is the CUDA ordinal (what the user sees as ``cuda:N``), not the NVML index, so a process
    started with ``CUDA_VISIBLE_DEVICES=2,3`` reports GPUs 0 and 1. Entries may be NVML indices or UUIDs;
    like CUDA, parsing stops at the first entry that does not exist. An empty value means no GPUs.
    """
    if visible is None:
        return [(i, nvml.nvmlDeviceGetHandleByIndex(i)) for i in range(nvml.nvmlDeviceGetCount())]
    handles: list[tuple[int, Any]] = []
    for pos, token in enumerate(t.strip() for t in visible.split(",") if t.strip()):
        try:
            if token.isdigit():
                handle = nvml.nvmlDeviceGetHandleByIndex(int(token))
            else:
                handle = nvml.nvmlDeviceGetHandleByUUID(token)
        except Exception:
            break
        handles.append((pos, handle))
    return handles


def resolve_handles(nvml: Any) -> list[tuple[int, Any]]:
    """Handles for the GPUs visible to this process (honours ``CUDA_VISIBLE_DEVICES``)."""
    return _resolve_handles(nvml, os.environ.get("CUDA_VISIBLE_DEVICES"))


def process_pids(own_pid: int) -> set[int]:
    """This PID plus its descendants (when psutil is installed; otherwise just this PID)."""
    pids = {own_pid}
    try:
        import psutil

        pids.update(c.pid for c in psutil.Process(own_pid).children(recursive=True))
    except Exception:
        pass
    return pids


def sample_gpu(nvml: Any, handle: Any, gpu_index: int, pids: set[int]) -> dict[str, Any]:
    """One snapshot of a device. ``process_*`` / ``attributed_*`` fields are None when unavailable.

    Per-process numbers: VRAM from nvmlDeviceGetComputeRunningProcesses, SM utilization from
    nvmlDeviceGetProcessUtilization, both summed over ``pids``. Without per-process utilization the
    device value is used when we are the only process, else scaled by our share of VRAM (an estimate).
    """
    mem = nvml.nvmlDeviceGetMemoryInfo(handle)
    # MIG instances support memory info only: utilization / temperature / power stay None there.
    util_rates = _call(nvml.nvmlDeviceGetUtilizationRates, handle)
    temp = _call(nvml.nvmlDeviceGetTemperature, handle, nvml.NVML_TEMPERATURE_GPU)
    power_mw = _call(nvml.nvmlDeviceGetPowerUsage, handle)
    power: float | None = power_mw / 1000.0 if power_mw is not None else None  # mW -> W

    fan: float | None = None
    try:
        fan = float(nvml.nvmlDeviceGetFanSpeed(handle))
    except Exception:
        pass

    sm_clock: int | None = None
    mem_clock: int | None = None
    try:
        sm_clock = nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_SM)
        mem_clock = nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_MEM)
    except Exception:
        pass

    process_memory_used: int | None = None
    process_exclusive: bool | None = None
    attributed_utilization: float | None = None
    attributed_power: float | None = None
    device_util: float | None = float(util_rates.gpu) if util_rates is not None else None

    our_vram = 0
    total_vram = 0
    try:
        procs = nvml.nvmlDeviceGetComputeRunningProcesses(handle)
        ours = [p for p in procs if p.pid in pids]
        if ours:
            our_vram = sum(p.usedGpuMemory or 0 for p in ours)
            total_vram = sum(p.usedGpuMemory or 0 for p in procs)
            process_memory_used = our_vram
            process_exclusive = len(ours) == len(procs)
    except Exception:
        pass

    try:
        samples = nvml.nvmlDeviceGetProcessUtilization(handle, 0)
        latest: dict[int, Any] = {}
        for s in samples:
            if s.pid in pids and (s.pid not in latest or s.timeStamp > latest[s.pid].timeStamp):
                latest[s.pid] = s
        if latest:
            attributed_utilization = min(100.0, float(sum(s.smUtil for s in latest.values())))
    except Exception:
        pass

    if attributed_utilization is None and device_util is not None:
        if process_exclusive:
            attributed_utilization = device_util
        elif our_vram > 0 and total_vram > 0:
            attributed_utilization = device_util * our_vram / total_vram

    if power is None:
        attributed_power = None
    elif process_exclusive:
        attributed_power = power
    elif attributed_utilization is not None and device_util:
        attributed_power = power * min(1.0, attributed_utilization / device_util)
    elif our_vram > 0 and total_vram > 0:
        attributed_power = power * our_vram / total_vram

    return {
        "gpu_index": gpu_index,
        "gpu_utilization": device_util,
        "memory_used": mem.used,
        "memory_total": mem.total,
        "temperature": float(temp) if temp is not None else None,
        "power_draw": power,
        "fan_speed": fan,
        "sm_clock_mhz": sm_clock,
        "memory_clock_mhz": mem_clock,
        "process_memory_used": process_memory_used,
        "process_exclusive": process_exclusive,
        "attributed_utilization": attributed_utilization,
        "attributed_power": attributed_power,
    }


def sample_all(nvml: Any, handles: list[tuple[int, Any]], own_pid: int) -> list[dict[str, Any]]:
    """Snapshot every GPU with one shared timestamp; a failing device is skipped, not fatal."""
    pids = process_pids(own_pid)
    now = time.time()
    out: list[dict[str, Any]] = []
    for index, handle in handles:
        try:
            snap = sample_gpu(nvml, handle, index, pids)
        except Exception:
            continue
        snap["timestamp"] = now
        out.append(snap)
    return out


# NVML clocks-throttle-reason bits (nvmlClocksThrottleReason*), documented in the NVML reference.
THROTTLE_REASONS: dict[int, str] = {
    0x1: "gpu_idle",
    0x2: "applications_clocks_setting",
    0x4: "sw_power_cap",
    0x8: "hw_slowdown",
    0x10: "sync_boost",
    0x20: "sw_thermal_slowdown",
    0x40: "hw_thermal_slowdown",
    0x80: "hw_power_brake_slowdown",
    0x100: "display_clock_setting",
}
THROTTLE_SLOWDOWN_BITS = 0x4 | 0x8 | 0x20 | 0x40 | 0x80  # reasons that actually cost performance


def decode_throttle(mask: int) -> list[str]:
    """Names of the throttle reasons set in an NVML bitmask (unknown bits are ignored)."""
    return [name for bit, name in THROTTLE_REASONS.items() if mask & bit]


def throttle_mask(nvml: Any, handle: Any) -> int | None:
    """Current NVML clock-throttle-reason bitmask of a device, or ``None`` if the driver cannot tell."""
    for fn in ("nvmlDeviceGetCurrentClocksEventReasons", "nvmlDeviceGetCurrentClocksThrottleReasons"):
        f = getattr(nvml, fn, None)
        if f is None:
            continue
        try:
            return int(f(handle))
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Hardware inventory (run-level): every visible GPU, MIG-aware
# ---------------------------------------------------------------------------

MIG_UTILIZATION_NOTE = (
    "NVML does not report utilization, temperature or power for MIG instances; "
    "those fields are null for MIG devices (memory is reported per instance)."
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:
        return None


def is_mig_handle(nvml: Any, handle: Any, uuid: str | None = None) -> bool:
    """True if ``handle`` is a MIG instance (not a whole GPU)."""
    f = getattr(nvml, "nvmlDeviceIsMigDeviceHandle", None)
    if f is not None:
        res = _call(f, handle)
        if res is not None:
            return bool(res)
    return bool(uuid and uuid.startswith("MIG-"))


def mig_children(nvml: Any, handle: Any) -> list[dict[str, Any]]:
    """MIG instances of a MIG-enabled parent GPU (empty if none or unsupported)."""
    out: list[dict[str, Any]] = []
    count = _call(getattr(nvml, "nvmlDeviceGetMaxMigDeviceCount", lambda h: 0), handle) or 0
    for i in range(int(count)):
        try:
            child = nvml.nvmlDeviceGetMigDeviceHandleByIndex(handle, i)
        except Exception:
            continue  # unused slot
        mem = _call(nvml.nvmlDeviceGetMemoryInfo, child)
        out.append({
            "index": i,
            "uuid": _text(_call(nvml.nvmlDeviceGetUUID, child)),
            "name": _text(_call(nvml.nvmlDeviceGetName, child)),
            "memory_total": int(mem.total) if mem is not None else None,
            "utilization": None,  # not available per MIG instance via NVML
        })
    return out


def gpu_inventory(nvml: Any, handles: list[tuple[int, Any]]) -> list[dict[str, Any]]:
    """Static description of each visible GPU: ``index`` (CUDA ordinal), ``name``, ``uuid``, ``memory_total``,
    ``driver_version``, ``mig_mode`` (``"enabled"``/``"disabled"``/``None`` if unknown), ``is_mig``
    (the visible device itself is a MIG instance, e.g. ``CUDA_VISIBLE_DEVICES=MIG-...``) and ``mig_devices``
    (instances of a MIG-enabled GPU, with per-instance memory and ``utilization: null``).
    A device NVML cannot describe is skipped.
    """
    driver = _text(_call(nvml.nvmlSystemGetDriverVersion))
    out: list[dict[str, Any]] = []
    for index, handle in handles:
        try:
            uuid = _text(_call(nvml.nvmlDeviceGetUUID, handle))
            mem = nvml.nvmlDeviceGetMemoryInfo(handle)
            entry: dict[str, Any] = {
                "index": index,
                "name": _text(_call(nvml.nvmlDeviceGetName, handle)),
                "uuid": uuid,
                "memory_total": int(mem.total),
                "driver_version": driver,
                "mig_mode": None,
                "is_mig": False,
                "mig_devices": [],
            }
            if is_mig_handle(nvml, handle, uuid):
                entry["is_mig"] = True
                entry["mig_mode"] = "enabled"
                entry["note"] = MIG_UTILIZATION_NOTE
            else:
                modes = _call(getattr(nvml, "nvmlDeviceGetMigMode", lambda h: None), handle)
                if modes is not None:
                    current = modes[0] if isinstance(modes, (tuple, list)) else modes
                    enabled = int(current) == int(getattr(nvml, "NVML_DEVICE_MIG_ENABLE", 1))
                    entry["mig_mode"] = "enabled" if enabled else "disabled"
                    if enabled:
                        entry["mig_devices"] = mig_children(nvml, handle)
                        entry["note"] = MIG_UTILIZATION_NOTE
            out.append(entry)
        except Exception:
            continue
    return out
