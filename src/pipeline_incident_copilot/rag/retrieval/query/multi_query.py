"""Multi-query: the LLM writes alternative phrasings; all are retrieved and fused."""

import re

from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.rag.retrieval.query.query_rewriter import TRIAGE_SYSTEM_PROMPT, QueryPlan, plan_to_query


def multi_query(plan: QueryPlan, n: int = 3) -> list[str]:
    raw = model_router.text(
        "multi_query",
        f"Problem: {plan_to_query(plan)}\n\nWrite {n} different short search queries for this problem, "
        "each from a different angle (error meaning, likely root cause, code that failed). "
        "One per line, no numbering.",
        system=TRIAGE_SYSTEM_PROMPT,
    )
    lines = [re.sub(r"^[\s\-\d.)*]+", "", line).strip() for line in raw.splitlines()]
    return [line for line in lines if line][:n]
