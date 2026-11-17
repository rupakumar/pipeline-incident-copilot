"""Hybrid done by Qdrant itself: prefetch dense + sparse, fuse server-side with RRF."""

from langchain_core.documents import Document
from qdrant_client import models

from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import get_embeddings
from pipeline_incident_copilot.rag.embedding.bm25_sparse_encoder import query_vector
from pipeline_incident_copilot.rag.vector_store.qdrant_store import collection_name, get_qdrant, to_document


class NativeHybridRoute:
    def __init__(self, strategy: str):
        self.collection = collection_name(strategy)

    def search(self, query: str, k: int, candidates: int = 30) -> list[Document]:
        idx, vals = query_vector(query)
        res = get_qdrant().query_points(
            self.collection,
            prefetch=[
                models.Prefetch(query=get_embeddings().embed_query(query), using="dense", limit=candidates),
                models.Prefetch(query=models.SparseVector(indices=idx, values=vals), using="bm25", limit=candidates),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=k,
        )
        return [to_document(p) for p in res.points]
