"""Chunking strategies, selected by name (compared in evals/runners/retrieval_eval.py).

- fixed:     size-based splitting for everything (baseline)
- structure: one chunk per error class; notebook split on cell boundaries
"""

from langchain_core.documents import Document

from pipeline_incident_copilot.rag.ingestion.chunking.fixed_size_chunker import fixed_size
from pipeline_incident_copilot.rag.ingestion.chunking.structure_aware_chunker import structure_aware

STRATEGIES = {"fixed": fixed_size, "structure": structure_aware}


def chunk(docs: list[Document], strategy: str) -> list[Document]:
    chunks = STRATEGIES[strategy](docs)
    for i, c in enumerate(chunks):
        c.metadata["chunk_id"] = f"{strategy}-{i}"
    return chunks
