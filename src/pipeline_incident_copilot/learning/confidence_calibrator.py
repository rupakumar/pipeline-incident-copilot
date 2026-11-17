"""Confidence from evidence, not only from the model (a small model says ~0.95 for everything).

Signals, each 0 or 1:
- explained_by_change : the cause names a parameter (or its new value) that changed since the last success
- confirmed_by_check  : a live check agrees (e.g. the missing path really does not exist)
- exact_error_doc     : the error's own catalog entry was found
- knowledge_ok        : the knowledge search did not come back weak

calibrated = MODEL_WEIGHT * model confidence + (1 - MODEL_WEIGHT) * mean(signals). With no evidence
for the cause the result drops below the routing threshold, so a person decides.
"""

from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.tuning.tunables import get_tunables

MODEL_WEIGHT = get_tunables().calibrator_model_weight


def signals(state: TriageState) -> dict[str, bool]:
    ev, rc, k = state["evidence"], state["root_cause"], state.get("knowledge")
    cause = rc.cause.lower()
    changed = ev.comparison.changed_parameters if ev.comparison else {}
    explained = any(
        name.lower() in cause or name.lower().replace("_", " ") in cause or (new and new.lower() in cause)
        for name, (_old, new) in changed.items()
    )  # "input_path" may be written "input path" in the cause
    checks = [*ev.tool_results, *state.get("extra_checks", [])]
    path_missing = any(r.tool == "check_path_exists" and r.data.get("exists") is False for r in checks)
    says_missing = any(w in cause for w in ("missing", "not exist", "does not exist", "never arrived", "not found"))
    no_permission = any(r.tool == "check_permissions" and r.data.get("has_needed") is False for r in checks)
    says_permission = any(w in cause for w in ("permission", "privilege", "not allowed", "unauthorized"))
    compared = any(r.tool == "compare_with_last_success" and r.ok for r in checks)
    confirmed = (path_missing and says_missing) or (no_permission and says_permission) or (explained and compared)
    exact = bool(k) and any(c.score >= 10.0 for c in k.citations)  # the exact error-code route's score
    return {
        "explained_by_change": explained,
        "confirmed_by_check": bool(confirmed),
        "exact_error_doc": exact,
        "knowledge_ok": bool(k) and not k.weak,
    }


def calibrate(state: TriageState) -> tuple[float, dict[str, bool]]:
    s = signals(state)
    evidence = sum(s.values()) / len(s)
    return round(MODEL_WEIGHT * state["root_cause"].confidence + (1 - MODEL_WEIGHT) * evidence, 3), s
