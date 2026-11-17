"""Search the copilot's own memory of verified fixes (collection `incident_memory`), trusted ones only."""

import os

from langchain_core.documents import Document
from qdrant_client import models

from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import get_embeddings
from pipeline_incident_copilot.rag.embedding.bm25_sparse_encoder import query_vector
from pipeline_incident_copilot.rag.vector_store.qdrant_store import get_qdrant, to_document


def memory_collection() -> str:
    """incident_memory, or another collection (MEMORY_COLLECTION) so evals never touch the real memory."""
    return os.getenv("MEMORY_COLLECTION", "incident_memory")


class MemoryRoute:
    def __init__(self, collection: str | None = None):
        self.collection = collection or memory_collection()

    def search(self, query: str, k: int = 3) -> list[Document]:
        client = get_qdrant()
        if not client.collection_exists(self.collection):
            return []  # nothing learned yet
        trusted = models.Filter(must=[models.FieldCondition(key="trusted", match=models.MatchValue(value=True))])
        idx, vals = query_vector(query)
        res = client.query_points(
            self.collection,
            prefetch=[
                models.Prefetch(query=get_embeddings().embed_query(query), using="dense", limit=10, filter=trusted),
                models.Prefetch(
                    query=models.SparseVector(indices=idx, values=vals), using="bm25", limit=10, filter=trusted
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=k,
        )
        return [to_document(p) for p in res.points]
