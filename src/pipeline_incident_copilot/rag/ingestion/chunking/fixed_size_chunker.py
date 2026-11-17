"""Baseline strategy: size-based recursive character splitting for every source."""

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from pipeline_incident_copilot.rag.ingestion.chunking.markdown_section_chunker import markdown_sections, slugify
from pipeline_incident_copilot.rag.ingestion.chunking.notebook_chunker import notebook_sections, tag_section


def fixed_size(docs: list[Document], size: int = 500, overlap: int = 50) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=overlap, add_start_index=True)
    chunks = []
    for doc in docs:
        parts = splitter.split_documents([doc])
        if doc.metadata["source"] == "code":
            sections = notebook_sections(doc.page_content)
            parts = [tag_section(p, sections) for p in parts]
        elif doc.metadata.get("parent_per_section"):
            parts = [_tag_markdown_section(p, markdown_sections(doc.page_content)) for p in parts]
        chunks.extend(parts)
    return chunks


def _tag_markdown_section(chunk: Document, sections: list[tuple[int, int, str]]) -> Document:
    """Give a fixed-size chunk the parent_id of the markdown section it starts in."""
    start = chunk.metadata.get("start_index", 0)
    title = next((t for s, e, t in sections if s <= start < e), "overview")
    chunk.metadata["section"] = slugify(title)
    chunk.metadata["parent_id"] = f"{chunk.metadata['parent_id']}#{slugify(title)}"
    return chunk
