"""HyDE (Hypothetical Document Embeddings): the LLM drafts what the answer document might
say; we search with that, because it looks like the documents we index."""

from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.rag.retrieval.query.query_rewriter import TRIAGE_SYSTEM_PROMPT, QueryPlan, plan_to_query


def hyde(plan: QueryPlan) -> str:
    return model_router.text(
        "hyde",
        f"Problem: {plan_to_query(plan)}\n\nWrite a short (3-4 sentence) reference entry, as it would "
        "appear in a Spark/Delta error catalog or troubleshooting guide, explaining this error and its usual cause.",
        system=TRIAGE_SYSTEM_PROMPT,
        max_tokens=200,
    )
