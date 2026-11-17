"""IncidentAgent: what kind of failure, how severe.

Rules first: error codes already name the class (as in the runbooks' "typical errors"), so no model
call is needed for known errors. Unknown errors fall back to the local model.
"""

import re

from pipeline_incident_copilot.agents.base import evidence_brief, load_prompt
from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.schemas.agent_outputs import IncidentClassification
from pipeline_incident_copilot.schemas.triage_state import TriageState

# (pattern in the error text, class) — first match wins
RULES = [
    (r"PATH_NOT_FOUND|UC_VOLUME_NOT_FOUND|FileNotFoundException|No such file", "missing_input"),
    (r"DELTA_METADATA_MISMATCH|SCHEMA_MISMATCH|DELTA_FAILED_TO_MERGE_FIELDS|CANNOT_MERGE_SCHEMA", "schema_change"),
    (r"UNAUTHORIZED_ACCESS|PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS|does not have \w+ privilege", "permissions"),
    (r"DATA_QUALITY_FAILED", "data_quality"),
    (
        r"out of memory|OutOfMemory|driver has been killed|DRIVER_UNRESPONSIVE|kernel is unresponsive",
        "resource_exhaustion",
    ),  # fmt: skip
]
SEVERITY = "sev2"  # a failed daily load: dashboards go stale


def classify_by_rules(error: str) -> IncidentClassification | None:
    for pattern, failure_class in RULES:
        if m := re.search(pattern, error or "", re.I):
            return IncidentClassification(
                failure_class=failure_class, severity=SEVERITY, rationale=f"error contains {m.group(0)}"
            )
    return None


class IncidentAgent:
    name = "incident"

    def run(self, state: TriageState) -> dict:
        ev = state["evidence"]
        result = classify_by_rules(f"{ev.error or ''}\n{ev.distilled_log}")
        how = "rule"
        if result is None:
            how = "model"
            result = model_router.structured(
                "classify",
                f"{evidence_brief(state)}\n\nClassify this failure.",
                IncidentClassification,
                system=load_prompt("incident"),
                max_tokens=200,
            )
        return {
            "classification": result,
            "trail": [f"classified as {result.failure_class} ({result.severity}, by {how})"],
        }
