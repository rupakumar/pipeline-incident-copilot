"""Guards for unattended runs.

- single_instance(name): a second copy of a scheduled job exits instead of running twice
- heavy_work(name): one model-heavy job at a time across processes (the watcher's diagnoses and the nightly eval
  share it, so they never load models together)
- on_ac_power(), enough_memory(): preconditions for the nightly eval
- rotate_log(path): launchd appends forever; keep the last few MB
"""

import fcntl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pipeline_incident_copilot.config import RUNTIME_DIR
from pipeline_incident_copilot.guardrails.resource.memory_guard import MIN_FREE_PCT, free_memory_pct

LOCK_DIR = RUNTIME_DIR / "locks"
LOG_DIR = RUNTIME_DIR / "logs"


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def _lock(name: str, wait_seconds: float) -> Iterator[None]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_DIR / f"{name}.lock", "w") as f:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AlreadyRunning(f"{name} is held by another copilot process") from None
                time.sleep(2)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def single_instance(name: str):
    """Exclusive, no waiting: raises AlreadyRunning if another copy holds it."""
    return _lock(f"job-{name}", 0)


def heavy_work(wait_seconds: float = 3600):
    """Exclusive across processes for model-heavy work; waits (default up to 1 h) for the other job to finish."""
    return _lock("heavy-work", wait_seconds)


def on_ac_power() -> bool:
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    return "AC Power" in out or "Battery Power" not in out  # desktops report no battery at all


def enough_memory(minimum: int = MIN_FREE_PCT) -> tuple[bool, int]:
    free = free_memory_pct()
    return free >= minimum, free


def rotate_log(path: Path, max_bytes: int = 5_000_000, keep: int = 3) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for n in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{n}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{n + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    path.touch()
