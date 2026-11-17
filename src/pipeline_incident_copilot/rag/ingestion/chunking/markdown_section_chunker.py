"""Markdown chunking: one chunk per heading section, with a context header.

The embedded text starts with a header naming where the chunk sits, e.g.
`[runbook] Missing input data › Diagnosis`, so a section still carries its document's topic.
Sections over the token budget are split at paragraph boundaries; the header repeats.
"""

import re

from langchain_core.documents import Document

from pipeline_incident_copilot.rag.ingestion.chunking.token_counter import CHUNK_TOKEN_BUDGET, count_tokens

HEADING = re.compile(r"^(#{1,3})\s+(.+)$", re.M)


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def markdown_sections(text: str) -> list[tuple[int, int, str]]:
    """(start, end, title) per section; text before the first H2 belongs to the document title."""
    starts = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in HEADING.finditer(text)]
    sections = []
    for i, (start, level, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        sections.append((start, end, "overview" if level == 1 else title))
    return sections or [(0, len(text), "overview")]


def _split_by_paragraph(body: str, budget: int) -> list[str]:
    parts, current = [], ""
    for paragraph in body.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}".strip()
        if current and count_tokens(candidate) > budget:
            parts.append(current)
            current = paragraph
        else:
            current = candidate
    return [*parts, current] if current else parts


def chunk_markdown(doc: Document, budget: int = CHUNK_TOKEN_BUDGET) -> list[Document]:
    meta = doc.metadata
    chunks = []
    for start, end, title in markdown_sections(doc.page_content):
        body = doc.page_content[start:end].strip()
        header = f"[{meta['source_type']}] {meta['title']} › {title}"
        section = slugify(title)
        parent_id = f"{meta['parent_id']}#{section}" if meta.get("parent_per_section") else meta["parent_id"]
        pieces = _split_by_paragraph(body, budget - count_tokens(header) - 2)
        for i, piece in enumerate(pieces):
            chunks.append(
                Document(
                    page_content=f"{header}\n{piece}",
                    metadata={**meta, "section": section, "parent_id": parent_id, "part": i + 1, "of": len(pieces)},
                )
            )
    return chunks
