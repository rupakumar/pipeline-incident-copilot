"""Local embedding model (no API key). BGE expects an instruction prefix on queries only."""

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from sentence_transformers import SentenceTransformer

MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class BgeEmbeddings(Embeddings):
    def __init__(self, model_name: str = MODEL):
        self.model = SentenceTransformer(model_name)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True, batch_size=64).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.model.encode(QUERY_PREFIX + text, normalize_embeddings=True).tolist()


@lru_cache(maxsize=1)
def get_embeddings() -> BgeEmbeddings:
    return BgeEmbeddings()
