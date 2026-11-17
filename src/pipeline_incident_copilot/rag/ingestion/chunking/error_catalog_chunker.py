"""Error catalog chunking: one chunk per error class (the loader already emits records)."""

from langchain_core.documents import Document


def chunk_error_record(doc: Document) -> list[Document]:
    return [doc]
