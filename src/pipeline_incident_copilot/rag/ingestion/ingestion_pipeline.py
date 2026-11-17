"""Ingestion: load → chunk → embed (dense + sparse) → upsert into Qdrant.

Also writes the chunks to `data/runtime/index/<strategy>/chunks.jsonl` for inspection.
"""

import json
import shutil

from langchain_core.documents import Document
from qdrant_client import models

from pipeline_incident_copilot.config import RUNTIME_DIR
from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import get_embeddings
from pipeline_incident_copilot.rag.embedding.bm25_sparse_encoder import doc_vector, tokenize
from pipeline_incident_copilot.rag.ingestion.chunking import chunk
from pipeline_incident_copilot.rag.ingestion.loaders import load_all
from pipeline_incident_copilot.rag.vector_store.qdrant_store import collection_name, get_qdrant, recreate_collection

INDEX_DIR = RUNTIME_DIR / "index"
UPSERT_BATCH = 256


def build_index(strategy: str) -> int:
    chunks = chunk(load_all(), strategy)
    _write_chunks(strategy, chunks)

    name = collection_name(strategy)
    recreate_collection(name)
    token_lists = [tokenize(c.page_content) for c in chunks]
    avgdl = sum(map(len, token_lists)) / len(token_lists)
    embedder = get_embeddings()
    client = get_qdrant()
    for i in range(0, len(chunks), UPSERT_BATCH):
        part = chunks[i : i + UPSERT_BATCH]
        dense = embedder.embed_documents([c.page_content for c in part])
        points = []
        for j, (c, vec) in enumerate(zip(part, dense, strict=True)):
            idx, vals = doc_vector(token_lists[i + j], avgdl)
            points.append(
                models.PointStruct(
                    id=i + j,
                    vector={"dense": vec, "bm25": models.SparseVector(indices=idx, values=vals)},
                    payload={"text": c.page_content, **c.metadata},
                )
            )
        client.upsert(name, points=points)
    return len(chunks)


def _write_chunks(strategy: str, chunks: list[Document]) -> None:
    out = INDEX_DIR / strategy
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with (out / "chunks.jsonl").open("w") as f:
        for c in chunks:
            f.write(json.dumps({"text": c.page_content, "metadata": c.metadata}) + "\n")


def load_chunks(strategy: str) -> list[Document]:
    with (INDEX_DIR / strategy / "chunks.jsonl").open() as f:
        return [Document(page_content=r["text"], metadata=r["metadata"]) for r in map(json.loads, f)]
