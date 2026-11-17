"""Retrieval metrics at the parent-document level (hit@k, recall@k, MRR, nDCG@k).

Chunks are mapped to their parent_id and de-duplicated (keeping best rank), so strategies
that split documents differently are compared fairly.
"""

import math


def ranked_parents(docs) -> list[str]:
    seen = []
    for d in docs:
        if d.metadata["parent_id"] not in seen:
            seen.append(d.metadata["parent_id"])
    return seen


def score(ranked: list[str], relevant: set[str], k: int) -> dict:
    top = ranked[:k]
    hits = [p in relevant for p in top]
    first = next((i for i, p in enumerate(ranked) if p in relevant), None)
    dcg = sum(1 / math.log2(i + 2) for i, h in enumerate(hits) if h)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return {
        "hit": float(any(hits)),
        "recall": sum(hits) / len(relevant),
        "mrr": 1 / (first + 1) if first is not None else 0.0,
        "ndcg": dcg / idcg,
    }
