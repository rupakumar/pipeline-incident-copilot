"""Tunables: the knobs of the diagnosis pipeline, read from config/tunables.yaml (or COPILOT_TUNABLES).

The self-improvement loop proposes a new value for ONE knob at a time, inside SEARCH_SPACE, and validates it with
the diagnosis eval in a separate process that reads the candidate file through COPILOT_TUNABLES. Modules read the
values once at import (a process sees one configuration).
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from pipeline_incident_copilot.config import PROJECT_ROOT

TUNABLES_FILE = PROJECT_ROOT / "config" / "tunables.yaml"


class Tunables(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    routing_confidence_threshold: float = 0.7
    root_cause_sure: float = 0.7
    knowledge_relevant_score: float = 0.0
    knowledge_min_relevant: int = 3
    knowledge_top_k: int = 8
    knowledge_cite: int = 4
    knowledge_memory_k: int = 2
    calibrator_model_weight: float = 0.4


@dataclass(frozen=True)
class Knob:
    low: float
    high: float
    step: float
    measured: bool = True  # False: the eval runs without it (e.g. memory is off), so the loop may not tune it


SEARCH_SPACE: dict[str, Knob] = {
    "routing_confidence_threshold": Knob(0.6, 0.8, 0.05),
    "root_cause_sure": Knob(0.6, 0.85, 0.05),
    "knowledge_relevant_score": Knob(-1.0, 1.0, 0.5),
    "knowledge_min_relevant": Knob(2, 4, 1),
    "knowledge_top_k": Knob(6, 12, 2),
    "knowledge_cite": Knob(3, 5, 1),
    "knowledge_memory_k": Knob(1, 3, 1, measured=False),
    "calibrator_model_weight": Knob(0.2, 0.6, 0.1),
}


def tunables_path() -> Path:
    return Path(os.getenv("COPILOT_TUNABLES", str(TUNABLES_FILE)))


def read_tunables(path: Path) -> Tunables:
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    tunables = Tunables.model_validate(data or {})
    for name, knob in SEARCH_SPACE.items():
        value = getattr(tunables, name)
        if not knob.low <= value <= knob.high:
            raise ValueError(f"{name}={value} is outside its allowed range {knob.low}..{knob.high} ({path})")
    return tunables


@lru_cache(maxsize=1)
def get_tunables() -> Tunables:
    return read_tunables(tunables_path())


def write_tunables(tunables: Tunables, path: Path) -> None:
    """Same layout as config/tunables.yaml: the comments of the original file are kept for unchanged keys."""
    original = TUNABLES_FILE.read_text().splitlines() if TUNABLES_FILE.exists() else []
    values = tunables.model_dump()
    lines, seen = [], set()
    for line in original:
        key = line.split(":", 1)[0].strip()
        if key in values and not line.startswith("#"):
            comment = line[line.index("#") :] if "#" in line else ""
            lines.append(f"{key}: {values[key]}".ljust(36) + comment)
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{k}: {v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")
