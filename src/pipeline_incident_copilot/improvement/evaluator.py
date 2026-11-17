"""Evaluator: run the diagnosis eval for one configuration in its own process and return its summary.

A separate process per run, so a candidate's tunables / prompts (COPILOT_TUNABLES, COPILOT_PROMPTS_DIR) never leak
into the loop or the next run. The eval itself works on a copy of the incident store and with memory off.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline_incident_copilot.config import EVALS_DIR, PROJECT_ROOT

REPORTS = EVALS_DIR / "reports"
TIMEOUT_SECONDS = 45 * 60


@dataclass
class EvalResult:
    split: str
    summary: dict
    rows: list[dict]


class Evaluator:
    def __init__(self, limit: int | None = None, python: str = sys.executable):
        self.limit = limit  # quick runs (demo): first N incidents per split
        self.python = python

    def run(self, split: str, name: str, tunables: Path | None = None, prompts_dir: Path | None = None) -> EvalResult:
        env = dict(os.environ)
        if tunables:
            env["COPILOT_TUNABLES"] = str(tunables)
        if prompts_dir:
            env["COPILOT_PROMPTS_DIR"] = str(prompts_dir)
        cmd = [self.python, "-m", "evals.runners.diagnosis_eval", "--split", split, "--report-name", f"{name}.json"]
        if self.limit:
            cmd += ["--limit", str(self.limit)]
        proc = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        report_path = REPORTS / f"{name}.json"
        if proc.returncode != 0 or not report_path.exists():
            tail = (proc.stderr or proc.stdout)[-600:]
            raise RuntimeError(f"eval {name} failed (exit {proc.returncode}): {tail}")
        report = json.loads(report_path.read_text())
        return EvalResult(split, report["summary"], report.get("rows", []))
