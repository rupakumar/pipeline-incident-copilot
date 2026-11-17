"""Semantic search: embed the query, nearest neighbours by cosine."""

from langchain_core.documents import Document

from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import get_embeddings
from pipeline_incident_copilot.rag.vector_store.qdrant_store import collection_name, get_qdrant, to_document


class DenseSearchRoute:
    def __init__(self, strategy: str):
        self.collection = collection_name(strategy)

    def search(self, query: str, k: int) -> list[Document]:
        res = get_qdrant().query_points(
            self.collection, query=get_embeddings().embed_query(query), using="dense", limit=k
        )
        return [to_document(p) for p in res.points]
