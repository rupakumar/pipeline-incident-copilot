"""Validator: accept a change only if it is better beyond noise on the tuning split and nothing regresses.

1. tuning split (train): the candidate's mean over N runs vs the baseline's mean over N runs
   - no metric may regress (the regression gate's rules), and
   - the quality score must rise by more than max(MIN_GAIN, the baseline's own run-to-run spread)
2. holdout split (test, never used to choose changes): the candidate must pass the regression gate against the
   versioned baseline in evals/baselines/
Every verdict carries the reason and the metric deltas.
"""

from dataclasses import dataclass, field

from pipeline_incident_copilot.improvement.gate import HIGHER_IS_BETTER, LOWER_IS_BETTER, check

MIN_GAIN = 0.02
QUALITY = ("all_ok", "context_recall", "context_precision", "faithfulness")  # minus brier_calibrated


@dataclass
class Verdict:
    accepted: bool
    reason: str
    deltas: dict = field(default_factory=dict)


def mean_summary(summaries: list[dict]) -> dict:
    keys = [k for k in (*HIGHER_IS_BETTER, *LOWER_IS_BETTER, "mean_seconds", "escalated") if k in summaries[0]]
    out = {}
    for k in keys:
        values = [s[k] for s in summaries if s.get(k) is not None]
        out[k] = round(sum(values) / len(values), 4) if values else None
    return out


def quality(summary: dict) -> float:
    return sum(summary.get(k) or 0 for k in QUALITY) / len(QUALITY) - (summary.get("brier_calibrated") or 0)


def noise(summaries: list[dict]) -> float:
    """How much the quality score moved between identical baseline runs (0 with a single run)."""
    scores = [quality(s) for s in summaries]
    return round(max(scores) - min(scores), 4) if len(scores) > 1 else 0.0


def deltas(candidate: dict, baseline: dict) -> dict:
    return {
        k: round(candidate[k] - baseline[k], 4)
        for k in candidate
        if isinstance(candidate.get(k), (int, float)) and isinstance(baseline.get(k), (int, float))
    }


def judge_tuning(baseline_runs: list[dict], candidate_runs: list[dict]) -> Verdict:
    """Stage 1: better beyond noise on the tuning split, and no metric regresses."""
    base, cand = mean_summary(baseline_runs), mean_summary(candidate_runs)
    d = deltas(cand, base)
    d["quality"] = round(quality(cand) - quality(base), 4)
    problems = check({"summary": cand}, {"summary": base})
    if problems:
        return Verdict(False, "regresses on the tuning split: " + "; ".join(problems), d)
    needed = max(MIN_GAIN, noise(baseline_runs))
    if d["quality"] < needed:
        return Verdict(False, f"no real gain: quality {d['quality']:+.3f} < {needed:.3f} (min gain or noise)", d)
    return Verdict(True, f"quality {d['quality']:+.3f} on the tuning split (needed {needed:.3f})", d)


def judge_holdout(tuning: Verdict, holdout: dict, holdout_reference: dict) -> Verdict:
    """Stage 2 (only after stage 1 passed): the holdout split must not regress against tonight's current config."""
    d = {**tuning.deltas, "holdout": deltas(holdout, holdout_reference)}
    problems = check({"summary": holdout}, {"summary": holdout_reference})
    if problems:
        return Verdict(False, "better on tuning but regresses on the holdout: " + "; ".join(problems), d)
    return Verdict(True, f"{tuning.reason}; holdout passes the regression gate", d)
