"""KnowledgeAgent: corrective RAG over the knowledge base, as steps the workflow wires into a subgraph.

retrieve → grade → (enough relevant? done : rewrite with HyDE → retrieve → grade) → cite.
Grading uses the cross-encoder score, not the LLM, so the local model is never loaded at the same
time as the retrieval models (memory rule).
"""

import os

from langchain_core.documents import Document

from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.rag.retrieval.query.hyde import hyde
from pipeline_incident_copilot.rag.retrieval.query.query_rewriter import QueryPlan
from pipeline_incident_copilot.rag.retrieval.reranking.cross_encoder_reranker import rerank
from pipeline_incident_copilot.rag.retrieval.retrieval_pipeline import RetrievalConfig, RetrievalPipeline
from pipeline_incident_copilot.rag.retrieval.routes.error_code_route import ErrorCodeLookupRoute
from pipeline_incident_copilot.rag.retrieval.routes.memory_route import MemoryRoute
from pipeline_incident_copilot.schemas.agent_outputs import Citation, KnowledgeContext, LogAnalysis
from pipeline_incident_copilot.tuning.tunables import get_tunables

RELEVANT_SCORE = get_tunables().knowledge_relevant_score  # cross-encoder logit; above = judged relevant
MIN_RELEVANT = get_tunables().knowledge_min_relevant  # fewer after grading → rewrite the query, retry once
MAX_RETRIES = 1
TOP_K = get_tunables().knowledge_top_k
CITE = get_tunables().knowledge_cite
# reserved places for the two kinds the fix depends on (a past verified fix, the runbook); the rest
# by score. Reserving more kinds hurt: forcing an error-catalog doc pushed out the decisive past ticket
# for a corrupted delivery (dq-04 went wrong).
SLOTS = ["incident", "runbook"]
EXACT_SCORE = 10.0  # an exact error-class match outranks any similarity score
MEMORY_K = get_tunables().knowledge_memory_k  # past verified fixes offered per incident (hints only)


def memory_enabled() -> bool:
    """Off for fair evals (a test incident's own fix must not be in its memory): KNOWLEDGE_USE_MEMORY=false."""
    return os.getenv("KNOWLEDGE_USE_MEMORY", "true").lower() != "false"


def _best_per_parent(docs: list[Document]) -> list[Document]:
    best: dict[str, Document] = {}
    for d in docs:
        pid = d.metadata.get("parent_id", "?")
        score = d.metadata.get("rerank_score", d.metadata.get("score", 0.0))
        if pid not in best or score > best[pid].metadata.get("rerank_score", best[pid].metadata.get("score", 0.0)):
            best[pid] = d
    return sorted(best.values(), key=lambda d: d.metadata.get("rerank_score", 0.0), reverse=True)


def diverse(ranked: list[Document], n: int) -> list[Document]:
    """One per kind in SLOTS order (best of each), then the highest-scoring rest; kept in score order."""
    picked: list[Document] = []
    for kind in SLOTS:
        best = next((d for d in ranked if d.metadata.get("source_type") == kind and d not in picked), None)
        if best is not None and len(picked) < n:
            picked.append(best)
    picked += [d for d in ranked if d not in picked][: n - len(picked)]
    return sorted(picked, key=lambda d: d.metadata.get("rerank_score", 0.0), reverse=True)


def _citation(d: Document) -> Citation:
    m = d.metadata
    return Citation(
        parent_id=m.get("parent_id", "?"),
        source_type=m.get("source_type", m.get("source", "?")),
        title=str(m.get("title") or m.get("error_class") or m.get("parent_id", "?")),
        snippet=d.page_content[:500],
        score=round(float(m.get("rerank_score", 0.0)), 3),
    )


class KnowledgeAgent:
    name = "knowledge"

    def __init__(self, strategy: str = "structure", memory: MemoryRoute | None = None):
        self.pipeline = RetrievalPipeline(RetrievalConfig(strategy=strategy, mode="hybrid", rerank=True, candidates=20))
        self.exact = ErrorCodeLookupRoute(strategy)
        self.memory = memory or MemoryRoute()

    # --- subgraph steps (each takes and returns a plain dict: the KnowledgeState) ---------------
    def retrieve(self, s: dict) -> dict:
        la: LogAnalysis = s["log_analysis"]
        query = s["queries"][-1]
        docs = self.pipeline.retrieve(query, k=TOP_K)
        if la.error_code and len(s["queries"]) == 1:  # exact lookup once, on the first pass
            for d in self.exact.search(la.error_code, k=1):
                d.metadata["rerank_score"] = EXACT_SCORE
                docs.insert(0, d)
        if memory_enabled() and len(s["queries"]) == 1:  # scored by the same cross-encoder and query
            with get_langfuse().start_as_current_observation(
                as_type="retriever", name="search-memory", input=query, metadata={"collection": self.memory.collection}
            ) as obs:
                found = rerank(query, self.memory.search(query, k=MEMORY_K), MEMORY_K)
                obs.update(
                    output=[
                        {"parent_id": d.metadata.get("parent_id"), "score": d.metadata.get("rerank_score")}
                        for d in found
                    ]
                )
            docs += found
        return {"docs": _best_per_parent(s.get("docs", []) + docs)}

    def grade(self, s: dict) -> dict:
        relevant = [d for d in s["docs"] if d.metadata.get("rerank_score", 0.0) > RELEVANT_SCORE]
        return {"relevant": relevant}

    def needs_retry(self, s: dict) -> bool:
        return len(s["relevant"]) < MIN_RELEVANT and s.get("retries", 0) < MAX_RETRIES

    def rewrite(self, s: dict) -> dict:
        la: LogAnalysis = s["log_analysis"]
        plan = QueryPlan(
            error_code=la.error_code,
            exception_type=la.exception_type,
            failing_operation=la.failing_operation,
            search_query=la.search_query,
        )
        hypothetical = hyde(plan)
        model_router.unload_local()
        return {"queries": [*s["queries"], hypothetical], "retries": s.get("retries", 0) + 1}

    def cite(self, s: dict) -> dict:
        ranked = s["relevant"] or s["docs"]
        chosen = diverse(ranked, CITE)
        return {
            "knowledge": KnowledgeContext(
                citations=[_citation(d) for d in chosen],
                queries=s["queries"],
                retries=s.get("retries", 0),
                weak=len(s["relevant"]) < MIN_RELEVANT,
            )
        }
