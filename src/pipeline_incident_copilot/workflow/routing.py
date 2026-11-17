"""Decisions on the graph's edges."""

from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.tuning.tunables import get_tunables

CONFIDENCE_THRESHOLD = get_tunables().routing_confidence_threshold  # below → a person decides


def by_confidence(state: TriageState) -> str:
    """Route on the evidence-calibrated confidence (the model's own number alone is ~0.95 for everything)."""
    rc = state["root_cause"]
    confidence = state.get("confidence", {}).get("calibrated", rc.confidence)
    return "resolution" if confidence >= CONFIDENCE_THRESHOLD and not rc.needs_human else "escalate"


def by_action(state: TriageState) -> str:
    """After the proposal: a safe action goes to a person for approval; otherwise the run ends (escalated)."""
    return "approval" if state["resolution"].action is not None else "end"


def by_auto_approval(state: TriageState) -> str:
    """A known fix the policy approved runs now; everything else waits for a person."""
    auto = state.get("auto_approval")
    return "remediate" if auto is not None and auto.approved else "approval"


def by_decision(state: TriageState) -> str:
    return "remediate" if state["approval"].decision == "approve" else "end"


def by_rerun_started(state: TriageState) -> str:
    return "verify" if state["remediation"].started else "fix_failed"


def by_verification(state: TriageState, max_attempts: int) -> str:
    if state["remediation"].succeeded:
        return "end"
    return "root_cause" if state.get("fix_attempts", 0) < max_attempts else "fix_failed"
