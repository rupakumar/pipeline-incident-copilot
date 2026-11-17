"""Disk cache of model outputs keyed by (task, route, prompt, …).

Local decoding is greedy, so outputs are deterministic and safe to reuse across eval runs.
"""

import hashlib
import json
import os
from pathlib import Path


class ResponseCache:
    """Disk cache of model outputs. LLM_CACHE_ENABLED=false bypasses reads (writes still refresh)."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}
        self.enabled = os.getenv("LLM_CACHE_ENABLED", "true").lower() != "false"

    def key(self, *parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]

    def get(self, key):
        return self.data.get(key) if self.enabled else None

    def put(self, key, value) -> None:
        self.data[key] = value
        self.path.write_text(json.dumps(self.data, indent=1))
