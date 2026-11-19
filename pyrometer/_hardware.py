import socket
import subprocess
import sys


def fingerprint() -> dict:
    info = {
        "python_version": sys.version.split()[0],
        "pytorch_version": None,
        "cuda_version": None,
        "gpu_model": None,
        "gpu_memory_total": None,
        "hostname": socket.gethostname(),
        "git_hash": None,
        "git_dirty": False,
    }

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

    try:
        import torch
        info["pytorch_version"] = torch.__version__
        if torch.cuda.is_available():
            info["cuda_version"] = torch.version.cuda
            info["gpu_model"] = torch.cuda.get_device_name(0)
            info["gpu_memory_total"] = torch.cuda.get_device_properties(0).total_memory
    except ImportError:
        pass

    # pynvml fallback when torch isn't available or has no CUDA
    if info["gpu_model"] is None:
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info["gpu_model"] = pynvml.nvmlDeviceGetName(handle)
            info["gpu_memory_total"] = pynvml.nvmlDeviceGetMemoryInfo(handle).total
        except Exception:
            pass

    return info
