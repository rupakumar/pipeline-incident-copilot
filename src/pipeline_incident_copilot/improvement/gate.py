"""Regression rules shared by the CI gate (evals/regression_gate.py) and the self-improvement validator."""

HIGHER_IS_BETTER = ["class_ok", "fact_ok", "action_ok", "all_ok", "context_recall", "context_precision", "faithfulness"]
LOWER_IS_BETTER = ["brier_calibrated", "errors"]
TOLERANCE = 0.02  # small noise allowance on rates
SLOWDOWN_ALLOWED = 1.25  # mean seconds may grow by 25%


def check(report: dict, baseline: dict) -> list[str]:
    """What got worse in `report` compared with `baseline` (both {"summary": {...}}); empty = no regression."""
    now, base = report["summary"], baseline["summary"]
    problems = []
    for key in HIGHER_IS_BETTER:
        if key in base and now.get(key, 0) < base[key] - TOLERANCE:
            problems.append(f"{key}: {now.get(key)} < baseline {base[key]}")
    for key in LOWER_IS_BETTER:
        if base.get(key) is not None and now.get(key) is not None and now[key] > base[key] + TOLERANCE:
            problems.append(f"{key}: {now[key]} > baseline {base[key]}")
    if base.get("mean_seconds") and now.get("mean_seconds", 0) > base["mean_seconds"] * SLOWDOWN_ALLOWED:
        problems.append(f"mean_seconds: {now['mean_seconds']} > {SLOWDOWN_ALLOWED}× baseline {base['mean_seconds']}")
    return problems
