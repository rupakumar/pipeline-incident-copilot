"""Calibrated confidence: the model's number blended with evidence checks, each 0 or 1."""

from types import SimpleNamespace

from pipeline_incident_copilot.learning.confidence_calibrator import calibrate, signals
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle, RunComparison, ToolResult


def state(cause: str, exact_doc: bool = True, changed=None):
    evidence = EvidenceBundle(
        incident_id="INC-1", failed_task="bronze_ingest", error="x", error_trace_chars=0, distilled_log="x",
        parameters={},
        comparison=RunComparison(failed=None, last_success=None,
                                 changed_parameters=changed or {"input_path": ("", "/Volumes/w/p/raw/x.log.gz")}),
        tool_results=[ToolResult(tool="compare_with_last_success", args={}, ok=True, summary="changed")],
    )  # fmt: skip
    citation = SimpleNamespace(score=10.0 if exact_doc else 0.9)
    return {
        "evidence": evidence,
        "root_cause": SimpleNamespace(cause=cause, confidence=0.95),
        "knowledge": SimpleNamespace(citations=[citation], weak=False),
    }


def test_a_cause_naming_the_changed_parameter_in_plain_words_counts_as_explained():
    for cause in ("input_path now points at a .gz file", "Input path changed to a .gz file that was never delivered"):
        s = signals(state(cause))
        assert s["explained_by_change"] and s["confirmed_by_check"], cause


def test_confidence_drops_below_the_routing_line_without_evidence_for_the_cause():
    sure, _ = calibrate(state("Input path changed to a .gz file"))
    unsure, s = calibrate(state("the cluster had a transient network problem", exact_doc=False))
    assert sure == 0.98 and not s["explained_by_change"] and unsure < 0.7  # a person decides
