"""LogAnalysisAgent: facts from the distilled log, and the search query (no model call).

The query-transform eval showed the distilled log itself is the best search query (hybrid +
rerank: MRR 1.00, runbook@5 1.00), so an LLM rewrite only costs time here.
"""

import re

from pipeline_incident_copilot.rag.retrieval.routes.error_code_route import known_error_codes
from pipeline_incident_copilot.schemas.agent_outputs import LogAnalysis
from pipeline_incident_copilot.schemas.triage_state import TriageState

EXCEPTION = re.compile(r"\b([A-Z]\w*(?:Exception|Error))\b")
QUERY_LINES = 4


def failing_line(distilled: str) -> str:
    """First line of our own code in the distilled log (not the error message or exception name)."""
    for line in distilled.splitlines():
        s = line.strip()
        if s and not s.startswith("[") and not EXCEPTION.fullmatch(s) and ":" not in s[:30]:
            return s[:200]
    return ""


class LogAnalysisAgent:
    name = "log_analysis"

    def run(self, state: TriageState) -> dict:
        ev = state["evidence"]
        text = f"{ev.error or ''}\n{ev.distilled_log}"
        codes = known_error_codes(text)
        exception = EXCEPTION.search(text)
        operation = failing_line(ev.distilled_log)
        result = LogAnalysis(
            error_code=codes[0] if codes else None,
            exception_type=exception.group(1) if exception else None,
            failing_operation=operation or f"task {ev.failed_task}",
            failing_task=ev.failed_task,
            key_lines=ev.distilled_log.splitlines()[:QUERY_LINES],
            search_query=" ".join(ev.distilled_log.splitlines()[:QUERY_LINES])[:1000],
        )
        return {"log_analysis": result, "trail": [f"error {result.error_code}: {result.failing_operation[:80]}"]}
