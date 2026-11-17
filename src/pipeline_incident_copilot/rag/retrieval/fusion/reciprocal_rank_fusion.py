"""Reciprocal Rank Fusion (Cormack et al., 2009).

score(d) = Σ over rankings of 1 / (k + rank(d))

Uses only ranks, not raw scores — so it can merge BM25 scores and cosine similarities,
which live on incomparable scales. k=60 is the value from the paper; it damps the
influence of any single list's top ranks.
"""

from langchain_core.documents import Document


def reciprocal_rank_fusion(rankings: list[list[Document]], k: int = 60) -> list[Document]:
    scores: dict[str, float] = {}
    docs: dict[str, Document] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            cid = doc.metadata["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            docs[cid] = doc
    return [docs[cid] for cid in sorted(scores, key=scores.get, reverse=True)]
