"""Token counts with the embedding model's own tokenizer (bge-small reads at most 512 tokens)."""

from functools import lru_cache

from transformers import AutoTokenizer

from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import MODEL

MAX_EMBEDDING_TOKENS = 512
CHUNK_TOKEN_BUDGET = 480  # leaves room for special tokens and the query-side prefix


@lru_cache(maxsize=1)
def _tokenizer():
    return AutoTokenizer.from_pretrained(MODEL)


def count_tokens(text: str) -> int:
    return len(_tokenizer().encode(text, add_special_tokens=False))
