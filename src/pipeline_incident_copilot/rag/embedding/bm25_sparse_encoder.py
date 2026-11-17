"""BM25 as sparse vectors, so keyword search runs inside Qdrant.

BM25(q, d) = Σ_t  IDF(t) · tf(t,d)·(k1+1) / (tf(t,d) + k1·(1 − b + b·|d|/avgdl))

We store the right-hand term per document (client-side, needs corpus avgdl); Qdrant
computes IDF(t) server-side via `Modifier.IDF`. The query vector is just 1.0 per term.
"""

import re
import zlib
from collections import Counter

K1 = 1.2
B = 0.75
STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "is",
    "and",
    "or",
    "for",
    "on",
    "be",
    "at",
    "by",
    "with",
    "this",
    "that",
}


def tokenize(text: str) -> list[str]:
    """Keep error codes whole (delta_metadata_mismatch) AND their parts (delta, metadata, ...)."""
    tokens = []
    for tok in re.findall(r"[a-z0-9_]+", text.lower()):
        if tok in STOPWORDS:
            continue
        tokens.append(tok)
        if "_" in tok:
            tokens.extend(p for p in tok.split("_") if p and p not in STOPWORDS)
    return tokens


def _index(term: str) -> int:
    return zlib.crc32(term.encode()) & 0x7FFFFFFF  # stable across runs, unlike hash()


def doc_vector(tokens: list[str], avgdl: float) -> tuple[list[int], list[float]]:
    tf = Counter(tokens)
    norm = K1 * (1 - B + B * len(tokens) / avgdl)
    by_index: dict[int, float] = {}
    for term, n in tf.items():
        i = _index(term)
        by_index[i] = by_index.get(i, 0.0) + n * (K1 + 1) / (n + norm)
    return list(by_index), list(by_index.values())


def query_vector(text: str) -> tuple[list[int], list[float]]:
    indices = sorted({_index(t) for t in tokenize(text)})
    return indices, [1.0] * len(indices)
