"""Cross-encoder reranking.

Bi-encoders (embeddings) encode query and chunk separately — fast, but coarse. A
cross-encoder reads query+chunk together and scores relevance directly — much more
accurate, too slow for the whole corpus, so we only rerank the top candidates.
"""

import os
from functools import lru_cache

from langchain_core.documents import Document
from sentence_transformers import CrossEncoder

MODEL = "BAAI/bge-reranker-base"
BATCH_SIZE = 8
DEVICE = os.getenv("RERANK_DEVICE", "cpu")


@lru_cache(maxsize=1)
def _model() -> CrossEncoder:
    # CPU, not the GPU (MPS): GPU memory came back slowly and fragmented when the reranker was released
    # between incidents, which grew swap (6.4 → 10.8 GB over 21 incidents). CPU memory is predictable.
    return CrossEncoder(MODEL, max_length=512, device=DEVICE)


def rerank(query: str, docs: list[Document], k: int) -> list[Document]:
    if not docs:
        return []
    # small batches keep the GPU memory spike low (memory rule); speed is about the same
    scores = _model().predict([(query, d.page_content) for d in docs], batch_size=BATCH_SIZE)
    ranked = sorted(zip(docs, scores, strict=True), key=lambda x: x[1], reverse=True)
    out = []
    for doc, score in ranked[:k]:
        doc.metadata["rerank_score"] = float(score)
        out.append(doc)
    return out
