"""Structure-aware strategy: route each source to the chunker that respects its structure."""

from langchain_core.documents import Document

from pipeline_incident_copilot.rag.ingestion.chunking.error_catalog_chunker import chunk_error_record
from pipeline_incident_copilot.rag.ingestion.chunking.markdown_section_chunker import chunk_markdown
from pipeline_incident_copilot.rag.ingestion.chunking.notebook_chunker import chunk_notebook

CHUNKERS = {
    "error_catalog": chunk_error_record,
    "code": chunk_notebook,
    "runbook": chunk_markdown,
    "architecture": chunk_markdown,
    "ticket": chunk_markdown,
    "product_doc": chunk_markdown,  # company documents uploaded in the console
    "postmortem": chunk_markdown,
}


def structure_aware(docs: list[Document]) -> list[Document]:
    return [chunk for doc in docs for chunk in CHUNKERS[doc.metadata["source_type"]](doc)]
