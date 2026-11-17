"""Compare query strategies (raw / distill / LLM rewrite / multi-query / HyDE) per retrieval config.

Usage: python -m evals.runners.query_transform_eval [-v]
"""

import os
import sys
import time

# Bulk eval runs would create hundreds of traces; these become Langfuse experiments later.
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from evals.dataset_loader import load_failed_runs, write_report  # noqa: E402
from evals.runners.retrieval_eval import build_query, evaluate  # noqa: E402
from pipeline_incident_copilot.llm import model_router  # noqa: E402
from pipeline_incident_copilot.rag.retrieval.query.hyde import hyde  # noqa: E402
from pipeline_incident_copilot.rag.retrieval.query.log_distiller import distill  # noqa: E402
from pipeline_incident_copilot.rag.retrieval.query.multi_query import multi_query  # noqa: E402
from pipeline_incident_copilot.rag.retrieval.query.query_rewriter import llm_rewrite, plan_to_query  # noqa: E402
from pipeline_incident_copilot.rag.retrieval.retrieval_pipeline import RetrievalConfig  # noqa: E402

runs = load_failed_runs()

# Precompute LLM outputs once per run (cached on disk by the router).
t = time.time()
plans = {r["run_id"]: llm_rewrite(distill(r["error"] or "", r["error_trace"] or "")) for r in runs}
multi = {rid: multi_query(p) for rid, p in plans.items()}
hyde = {rid: hyde(p) for rid, p in plans.items()}
print(f"LLM transforms ready in {time.time() - t:.1f}s")
model_router.unload_local()  # free ~2.5 GB before loading embedding + reranker models
if "-v" in sys.argv:
    for r in runs:
        rid = r["run_id"]
        print(f"\n[{r['scenario_id']}] plan: {plans[rid].model_dump()}")
        print(f"  multi: {multi[rid]}")
        print(f"  hyde: {hyde[rid][:200]}")

QUERIES = {
    "raw": build_query,
    "distill": lambda r: distill(r["error"] or "", r["error_trace"] or ""),
    "llm_rewrite": lambda r: plan_to_query(plans[r["run_id"]]),
    "multi_query": lambda r: [plan_to_query(plans[r["run_id"]]), *multi[r["run_id"]]],
    "hyde": lambda r: [plan_to_query(plans[r["run_id"]]), hyde[r["run_id"]]],
}
CONFIGS = [
    RetrievalConfig("structure", "bm25", rerank=False),
    RetrievalConfig("structure", "hybrid_native", rerank=False),
    RetrievalConfig("structure", "hybrid_native", rerank=True),
    RetrievalConfig("structure", "hybrid", rerank=True),
]


def held_out_average(result: dict, metric: str) -> float:
    held_out = [q for q in result["per_query"].values() if q["split"] == "test"]
    return sum(q[metric] for q in held_out) / len(held_out)


results = []
header = (
    f"{'query':<12} {'config':<30} {'hit@5':>6} {'rec@5':>6} {'MRR':>6} {'runbk@5':>8}"
    f" | {'test rec@5':>10} {'test MRR':>8}"
)
print("\n" + header)
for qname, qfn in QUERIES.items():
    for cfg in CONFIGS:
        r = evaluate(cfg, query_fn=qfn)
        results.append({"query": qname, **r})
        print(
            f"{qname:<12} {r['config']:<30} {r['hit']:>6.2f} {r['recall']:>6.2f} {r['mrr']:>6.2f}"
            f" {r['runbook_hit']:>8.2f}"
            f" | {held_out_average(r, 'recall'):>10.2f} {held_out_average(r, 'mrr'):>8.2f}"
        )

write_report("query_transform_results.json", results)
llm_calls = [s for s in model_router.stats if not s["cached"]]
print(f"\nLLM calls (uncached): {len(llm_calls)}, total {sum(s['seconds'] for s in llm_calls):.1f}s")
