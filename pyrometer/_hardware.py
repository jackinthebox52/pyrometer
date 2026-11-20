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
    """GPU 0 model and memory via pynvml (no torch; ``nvmlInit`` itself can take seconds)."""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode()
        return {"gpu_model": name, "gpu_memory_total": pynvml.nvmlDeviceGetMemoryInfo(handle).total}
    except Exception:
        return {}


def fingerprint_slow() -> dict:
    """Fields that need torch or NVML: torch/CUDA versions, GPU 0 model and memory.

    Imports torch and initialises NVML (each can take seconds). Returns only what was found.
    """
    info = _torch_fields()
    if info.get("gpu_model") is None:
        info.update(_nvml_gpu())
    return info


def _torch_fields() -> dict:
    info: dict = {}
    try:
        import torch

        info["pytorch_version"] = torch.__version__
        if torch.cuda.is_available():
            info["cuda_version"] = torch.version.cuda
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
