"""Compare retrieval configurations on the real failed-run queries.

Usage: python -m evals.runners.retrieval_eval [-v]
"""

import os
import sys

# Bulk eval runs would create hundreds of traces; these become Langfuse experiments later.
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from evals.dataset_loader import load_failed_runs, own_ticket_ids, relevant_for_run, write_report  # noqa: E402
from evals.metrics.retrieval_metrics import ranked_parents, score  # noqa: E402
from pipeline_incident_copilot.rag.retrieval.retrieval_pipeline import RetrievalConfig, RetrievalPipeline  # noqa: E402

CONFIGS = [
    RetrievalConfig(strategy=s, mode=m, rerank=r)
    for s in ("fixed", "structure")
    for m, r in (
        ("dense", False),
        ("bm25", False),
        ("hybrid", False),
        ("hybrid_native", False),
        ("dense", True),
        ("hybrid", True),
    )
]


def build_query(run: dict, trace_chars: int = 1000) -> str:
    """Baseline query: raw error + head of the stack trace (what a naive system would send)."""
    return f"{run['error'] or ''}\n{(run['error_trace'] or '')[:trace_chars]}".strip()


def evaluate(
    config: RetrievalConfig, k: int = 5, query_fn=build_query, verbose: bool = False, split: str | None = None
) -> dict:
    runs = load_failed_runs(split)
    pipeline = RetrievalPipeline(config)
    per_query = {}
    for run in runs:
        relevant = relevant_for_run(run)
        excluded = own_ticket_ids(run)
        # retrieve extra chunks so we still have k distinct parents after de-duplication
        ranked = [p for p in ranked_parents(pipeline.retrieve(query_fn(run), k=k * 4)) if p not in excluded]
        per_query[run["scenario_id"]] = {
            **score(ranked, relevant, k),
            "top": ranked[:k],
            "split": run["split"],
            "ticket_hit": float(any(p.startswith("ticket:") and p in relevant for p in ranked[:k])),
            "runbook_hit": float(any(p.startswith("runbook:") and p in relevant for p in ranked[:k])),
        }
        if verbose:
            print(f"  {run['scenario_id']:<8} {run['failure_class']:<20} {ranked[:k]}")
    n = len(per_query)
    summary = {
        m: sum(q[m] for q in per_query.values()) / n
        for m in ("hit", "recall", "mrr", "ndcg", "runbook_hit", "ticket_hit")
    }
    return {"config": config.name, **summary, "per_query": per_query}


def main() -> None:
    verbose = "-v" in sys.argv
    results = []
    print(f"{'config':<28} {'hit@5':>6} {'rec@5':>6} {'MRR':>6} {'nDCG':>6} {'runbk@5':>8} {'ticket@5':>9}")
    for cfg in CONFIGS:
        if verbose:
            print(cfg.name)
        r = evaluate(cfg, verbose=verbose)
        results.append(r)
        print(
            f"{r['config']:<28} {r['hit']:>6.2f} {r['recall']:>6.2f} {r['mrr']:>6.2f} {r['ndcg']:>6.2f}"
            f" {r['runbook_hit']:>8.2f} {r['ticket_hit']:>9.2f}"
        )
    write_report("retrieval_results.json", results)


if __name__ == "__main__":
    main()
