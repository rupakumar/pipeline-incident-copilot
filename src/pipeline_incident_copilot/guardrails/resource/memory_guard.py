"""Guardrails so local models never strain the Mac (M4, 16 GB unified memory).

- refuse to load any local model (MLX or Ollama) if macOS reports < MIN_FREE_PCT memory free
- hard-cap MLX's memory and buffer cache
"""

import re
import subprocess
import time

MIN_FREE_PCT = 45
MEMORY_LIMIT_GB = 7.0  # MLX raises instead of pushing the system into swap
CACHE_LIMIT_GB = 1.0  # freed buffers MLX may keep around for reuse
GB = 1024**3


def free_memory_pct() -> int:
    out = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True).stdout
    m = re.search(r"free percentage:\s*(\d+)%", out)
    return int(m.group(1)) if m else 0


def swap_used_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    m = re.search(r"used = ([\d.]+)M", out)
    return float(m.group(1)) / 1024 if m else 0.0


def require_free_memory(wait_seconds: float = 30.0) -> int:
    """Free-memory %, waiting up to `wait_seconds` for it to recover (GPU memory from the retrieval
    models is returned a few seconds after release); refuse if it stays below MIN_FREE_PCT."""
    deadline = time.monotonic() + wait_seconds
    free = free_memory_pct()
    while free < MIN_FREE_PCT and time.monotonic() < deadline:
        time.sleep(2)
        free = free_memory_pct()
    if free < MIN_FREE_PCT:
        raise MemoryError(
            f"Only {free}% memory free (need ≥{MIN_FREE_PCT}%). Close some apps and retry — "
            "not loading the local model to avoid straining the Mac."
        )
    return free


def check_and_limit() -> None:
    import mlx.core as mx  # only the MLX path needs it

    free = require_free_memory()
    mx.set_memory_limit(int(MEMORY_LIMIT_GB * GB))
    mx.set_cache_limit(int(CACHE_LIMIT_GB * GB))
    print(f"[safety] {free}% memory free, swap {swap_used_gb():.2f} GB; MLX capped at {MEMORY_LIMIT_GB} GB")


def release() -> None:
    import mlx.core as mx

    mx.clear_cache()
