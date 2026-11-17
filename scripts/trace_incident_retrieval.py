"""Run the knowledge-retrieval path for real failed runs, traced in Langfuse.

One trace per failed run: distill-log → rewrite-query → generate-query-variants →
retrieve-knowledge (search routes → fuse-rankings → rerank-documents). Retrieval quality
against our labels is attached as trace scores.

Usage: python -m scripts.trace_incident_retrieval [scenario_id | failure_class ...]   (default: all exported runs)
"""

import sys

from langfuse import propagate_attributes

from evals.dataset_loader import load_failed_runs, relevant_documents
from evals.metrics.retrieval_metrics import ranked_parents, score
from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse, tracing_enabled
from pipeline_incident_copilot.rag.retrieval.query.log_distiller import distill
from pipeline_incident_copilot.rag.retrieval.query.multi_query import multi_query
from pipeline_incident_copilot.rag.retrieval.query.query_rewriter import llm_rewrite, plan_to_query
from pipeline_incident_copilot.rag.retrieval.retrieval_pipeline import RetrievalConfig, RetrievalPipeline, summarize

CONFIG = RetrievalConfig(strategy="structure", mode="hybrid", rerank=True)
K = 5


def trace_run(run: dict, pipeline: RetrievalPipeline, relevant: set[str]) -> str:
    langfuse = get_langfuse()
    with langfuse.start_as_current_observation(as_type="chain", name="retrieve-incident-knowledge") as root:
        with propagate_attributes(
            session_id=f"incident-{run['run_id']}",
            trace_name="retrieve-incident-knowledge",
            tags=["knowledge-retrieval"],
            metadata={"run_id": str(run["run_id"]), "job": "loghub_etl", "source": str(run.get("source"))},
        ):
            distilled = distill(run["error"] or "", run["error_trace"] or "")
            root.update(input={"failed_run": run["run_page_url"], "evidence": distilled})

            plan = llm_rewrite(distilled)
            queries = [plan_to_query(plan), *multi_query(plan)]
            model_router.unload_local()  # never hold the LLM and the retrieval models in memory together
            docs = pipeline.retrieve(queries, k=K * 4)

            parents = ranked_parents(docs)
            root.update(
                output={"query_plan": plan.model_dump(), "top_documents": summarize(docs, K)},
                # ground truth is evaluation context, kept out of input/output
                metadata={
                    "eval_scenario": run["scenario_id"],
                    "eval_failure_class": run["failure_class"],
                    "eval_expected": sorted(relevant),
                },
            )
            metrics = score(parents, relevant, K)
            root.score_trace(name="retrieval_hit_at_5", value=metrics["hit"], data_type="NUMERIC")
            root.score_trace(name="retrieval_mrr", value=metrics["mrr"], data_type="NUMERIC")
            root.score_trace(name="retrieval_recall_at_5", value=metrics["recall"], data_type="NUMERIC")
        return root.trace_id


def main() -> None:
    if not tracing_enabled():
        sys.exit("Langfuse keys not found in .env (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY) — nothing to trace.")
    langfuse = get_langfuse()
    if not langfuse.auth_check():
        sys.exit("Langfuse rejected the credentials — check the keys and LANGFUSE_BASE_URL region in .env.")

    runs = load_failed_runs()
    wanted = set(sys.argv[1:])
    runs = [r for r in runs if not wanted or r["scenario_id"] in wanted or r["failure_class"] in wanted]

    pipeline = RetrievalPipeline(CONFIG)
    try:
        for run in runs:
            trace_id = trace_run(run, pipeline, set(relevant_documents(run["failure_class"])))
            print(f"{run['scenario_id']:<8} {langfuse.get_trace_url(trace_id=trace_id)}")
    finally:
        model_router.unload_local()  # free the local model's memory right away
        langfuse.flush()  # short-lived script: send everything before exit


if __name__ == "__main__":
    main()
