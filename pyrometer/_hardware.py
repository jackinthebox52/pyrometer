"""Hardware / environment fingerprint of a run.

``import torch`` and ``nvmlInit`` each cost seconds, so :func:`fingerprint_fast` collects everything
that is cheap (and uses torch only if the program has imported it already) and
:func:`fingerprint_slow` does the rest; ``init`` runs the latter in a background thread and PATCHes
the run when it is done. :func:`fingerprint` is the complete, blocking version.
"""

import socket
import subprocess
import sys
from importlib import metadata


def _git() -> dict:
    info: dict = {"git_hash": None, "git_dirty": False}
    try:
        info["git_hash"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "diff", "--stat"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        info["git_dirty"] = bool(dirty)
    except Exception:
        pass
    return info


def _nvml_gpu() -> dict:
    """Every visible GPU via pynvml (no torch; ``nvmlInit`` itself can take seconds).

    Honours ``CUDA_VISIBLE_DEVICES`` (indices, GPU UUIDs or MIG UUIDs), is MIG-aware and returns the
    list in ``gpus`` plus the compat single-GPU fields (``gpu_model``/``gpu_memory_total`` = GPU 0).
    """
    try:
        import pynvml

        from pyrometer import _gpu

        pynvml.nvmlInit()
        gpus = _gpu.gpu_inventory(pynvml, _gpu.resolve_handles(pynvml))
        return _from_gpus(gpus)
    except Exception:
        return {}


def _from_gpus(gpus: list[dict]) -> dict:
    if not gpus:
        return {}
    first = gpus[0]
    return {
        "gpus": gpus,
        "gpu_count": len(gpus),
        "driver_version": first.get("driver_version"),
        "gpu_model": first.get("name"),
        "gpu_memory_total": first.get("memory_total"),
    }


def fingerprint_slow() -> dict:
    """Fields that need torch or NVML: torch/CUDA versions, the list of visible GPUs (``gpus``), driver version.

    ``gpu_model`` / ``gpu_memory_total`` stay populated with GPU 0 for compatibility. Imports torch and
    initialises NVML (each can take seconds). Returns only what was found; NVML is preferred for the GPU
    list, torch is the fallback when NVML is unavailable.
    """
    info = _torch_fields()
    nvml = _nvml_gpu()
    if nvml:
        info.update(nvml)
    elif info.get("_torch_gpus"):
        info.update(_from_gpus(info["_torch_gpus"]))
    info.pop("_torch_gpus", None)
    return info


def _torch_fields() -> dict:
    info: dict = {}
    try:
        import torch

        info["pytorch_version"] = torch.__version__
        if torch.cuda.is_available():
            info["cuda_version"] = torch.version.cuda
            gpus = []
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                gpus.append({
                    "index": i, "name": props.name, "uuid": str(getattr(props, "uuid", "") or "") or None,
                    "memory_total": props.total_memory, "driver_version": None,
                    "mig_mode": None, "is_mig": False, "mig_devices": [],
                })
            info["_torch_gpus"] = gpus
            info["gpu_model"] = torch.cuda.get_device_name(0)
            info["gpu_memory_total"] = torch.cuda.get_device_properties(0).total_memory
    except Exception:  # ImportError, or a broken CUDA install
        pass
    return info


def fingerprint_fast() -> dict:
    """Everything cheap: python, host, git, torch version (package metadata, or the module if already imported).

    ``cuda_version`` is known only if the program imported torch already; GPU model/memory are left to
    :func:`fingerprint_slow` (initialising CUDA or NVML can take seconds).
    """
    info = {
        "python_version": sys.version.split()[0],
        "pytorch_version": None,
        "cuda_version": None,
        "gpu_model": None,
        "gpu_memory_total": None,
        "gpus": None,
        "gpu_count": None,
        "driver_version": None,
        "hostname": socket.gethostname(),
        **_git(),
    }
    torch = sys.modules.get("torch")  # never imported here; reading attributes of one the program has is free
    try:
        info["pytorch_version"] = getattr(torch, "__version__", None) or metadata.version("torch")
        info["cuda_version"] = getattr(getattr(torch, "version", None), "cuda", None)
    except Exception:
        pass
    return info


def fingerprint() -> dict:
    """The complete fingerprint (blocks while torch imports and NVML initialises)."""
    info = fingerprint_fast()
    info.update(fingerprint_slow())
    return info
