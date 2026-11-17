"""Keyword search: BM25 via sparse vectors (great for exact error codes)."""

from langchain_core.documents import Document
from qdrant_client import models

from pipeline_incident_copilot.rag.embedding.bm25_sparse_encoder import query_vector
from pipeline_incident_copilot.rag.vector_store.qdrant_store import collection_name, get_qdrant, to_document


class KeywordSearchRoute:
    def __init__(self, strategy: str):
        self.collection = collection_name(strategy)

    def search(self, query: str, k: int) -> list[Document]:
        idx, vals = query_vector(query)
        res = get_qdrant().query_points(
            self.collection, query=models.SparseVector(indices=idx, values=vals), using="bm25", limit=k
        )
        return [to_document(p) for p in res.points]
