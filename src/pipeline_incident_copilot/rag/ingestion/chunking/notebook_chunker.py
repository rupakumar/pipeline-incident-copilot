"""Databricks notebook chunking: one chunk per code cell (Python-aware split if a cell is big).

Also tags any notebook chunk with the section (markdown heading) it starts in. The parent stays
the notebook (`code:silver_parse`): each notebook is one job task, the unit labels refer to.
"""

import re

from langchain_core.documents import Document
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

CELL_SEPARATOR = "# COMMAND ----------"
HEADING = re.compile(r"^# MAGIC %md\s+(#+)\s*(.+)$|^# MAGIC (#+)\s*(.+)$", re.M)
MAX_CELL_CHARS = 1200


def _section_slug(level: int, title: str) -> str:
    if level == 1:
        return "setup"
    return re.sub(r"[^a-z0-9]+", "-", title.split(":")[0].lower()).strip("-")


def notebook_sections(text: str) -> list[tuple[int, str]]:
    """(char offset, section slug) for each markdown heading in a notebook."""
    out = []
    for m in HEADING.finditer(text):
        hashes, title = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        out.append((m.start(), _section_slug(len(hashes), title)))
    return out


def tag_section(chunk: Document, sections: list[tuple[int, str]]) -> Document:
    """Record the notebook section a chunk starts in (metadata only; parent_id is unchanged)."""
    start = chunk.metadata.get("start_index", 0)
    slug = "setup"
    for offset, s in sections:
        if offset <= start:
            slug = s
    chunk.metadata["section"] = slug
    return chunk


def chunk_notebook(doc: Document, max_cell: int = MAX_CELL_CHARS) -> list[Document]:
    py_splitter = RecursiveCharacterTextSplitter.from_language(Language.PYTHON, chunk_size=max_cell, chunk_overlap=0)
    text = doc.page_content
    sections = notebook_sections(text)
    chunks, offset = [], 0
    for cell in text.split(CELL_SEPARATOR):
        start = text.index(cell, offset)
        offset = start + len(cell)
        if not cell.strip() or cell.strip().startswith("# MAGIC %md"):
            continue  # skip empty + pure-markdown cells
        pieces = [cell] if len(cell) <= max_cell else py_splitter.split_text(cell)
        for piece in pieces:
            meta = {**doc.metadata, "start_index": start + cell.find(piece)}
            chunks.append(tag_section(Document(page_content=piece.strip(), metadata=meta), sections))
    return chunks
