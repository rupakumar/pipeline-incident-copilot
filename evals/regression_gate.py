"""Regression gate: fail (exit 1) if a diagnosis report scores worse than the accepted baseline.

Usage:
  python -m evals.regression_gate evals/reports/diagnosis_test_memory-off.json            # check
  python -m evals.regression_gate evals/reports/diagnosis_test_memory-off.json --accept   # make it the baseline
Baselines live in evals/baselines/ (versioned): a change that lowers a score is visible in review.
"""

import json
import sys
from pathlib import Path

from pipeline_incident_copilot.config import EVALS_DIR
from pipeline_incident_copilot.improvement.gate import check  # the rules, shared with the self-improvement loop

BASELINES = EVALS_DIR / "baselines"


def main() -> None:
    path = Path(sys.argv[1])
    report = json.loads(path.read_text())
    baseline_path = BASELINES / path.name
    if "--accept" in sys.argv:
        BASELINES.mkdir(exist_ok=True)
        baseline_path.write_text(json.dumps({"summary": report["summary"]}, indent=2) + "\n")
        print(f"baseline updated: {baseline_path}")
        return
    if not baseline_path.exists():
        sys.exit(f"no baseline {baseline_path}; run with --accept to create it")
    problems = check(report, json.loads(baseline_path.read_text()))
    if problems:
        print("REGRESSION:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print(f"ok: {path.name} is at least as good as the baseline")


if __name__ == "__main__":
    main()
