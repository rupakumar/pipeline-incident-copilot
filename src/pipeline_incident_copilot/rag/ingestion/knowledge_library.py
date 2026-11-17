"""KnowledgeLibrary: company documents uploaded in the console, indexed into the live knowledge base.

add():    convert (PDF / Word / Markdown / text / HTML) → mask secrets → refuse instruction-like text (prompt
          injection) → section chunks with a context header → dense + BM25 vectors → upsert into every knowledge
          collection (kb_structure is the one the diagnosis searches). No full rebuild; takes seconds.
remove(): drop the document's points, its file and its row.

Files live in data/runtime/knowledge_uploads/ (git-ignored: company documents never enter the repository), each
with a JSON sidecar (type, title, failure class), and `UploadedDocumentLoader` reads them back, so a full
`build_index` keeps them. Document types decide how retrieval treats them:
- architecture → source_type "architecture", one parent per section (long design documents)
- runbook      → "runbook": gets the knowledge agent's reserved runbook slot; tag a failure class to target it
- product      → "product_doc": product / platform documentation
- postmortem   → "postmortem": past incident write-ups
"""

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.documents import Document
from qdrant_client import models

from pipeline_incident_copilot.config import RUNTIME_DIR
from pipeline_incident_copilot.guardrails.input.injection_detector import suspicious_lines
from pipeline_incident_copilot.guardrails.input.secret_redactor import redact
from pipeline_incident_copilot.rag.ingestion.chunking.markdown_section_chunker import chunk_markdown
from pipeline_incident_copilot.rag.ingestion.document_converter import to_markdown

UPLOAD_DIR = RUNTIME_DIR / "knowledge_uploads"
STRATEGIES = ("structure", "fixed")  # every knowledge collection; the diagnosis searches kb_structure
DOC_TYPES = {
    "architecture": ("architecture", True, "Architecture & design"),
    "runbook": ("runbook", False, "Runbook / SOP"),
    "product": ("product_doc", True, "Product documentation"),
    "postmortem": ("postmortem", False, "Postmortem / past incident"),
}
FAILURE_CLASSES = ("missing_input", "data_quality", "schema_change", "permissions", "resource_exhaustion")
POINT_NAMESPACE = uuid.UUID("5f0c1c8e-2a4b-4d7e-9a61-6f2b1d0c7e11")


class RefusedDocument(ValueError):
    pass


def doc_slug(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:48] or "document"


def as_document(meta: dict, text: str) -> Document:
    source_type, per_section, _ = DOC_TYPES[meta["doc_type"]]
    return Document(
        page_content=text,
        metadata={
            "source": "company_upload",
            "source_type": source_type,
            "file": meta["filename"],
            "title": meta["title"],
            "parent_id": f"{source_type}:{meta['doc_id']}",
            "parent_per_section": per_section,
            "doc_id": meta["doc_id"],
            **({"failure_class": meta["failure_class"]} if meta.get("failure_class") else {}),
        },
    )


def chunks_for(doc: Document, strategy: str) -> list[Document]:
    """Same chunking as the built-in knowledge (sections + context header); fixed = plain size split."""
    if strategy == "fixed":
        from pipeline_incident_copilot.rag.ingestion.chunking.fixed_size_chunker import fixed_size

        parts = fixed_size([doc])
    else:
        parts = chunk_markdown(doc)
    for i, c in enumerate(parts):
        c.metadata["chunk_id"] = f"{strategy}-upload-{doc.metadata['doc_id']}-{i}"
    return parts


class KnowledgeLibrary:
    def __init__(self, conn: sqlite3.Connection, upload_dir: Path = UPLOAD_DIR):
        self.conn = conn
        self.dir = upload_dir

    # --- catalogue ---------------------------------------------------------------------------------
    def all(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM knowledge_documents ORDER BY uploaded_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get(self, doc_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM knowledge_documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def _unique_id(self, base: str) -> str:
        candidate, n = base, 1
        while self.get(candidate):
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    # --- add / remove ------------------------------------------------------------------------------
    def add(
        self, filename: str, data: bytes, doc_type: str, title: str = "", failure_class: str = "", by: str = ""
    ) -> dict:
        filename = Path(filename).name.strip() or "document.txt"
        if doc_type not in DOC_TYPES:
            raise RefusedDocument("pick a document type")
        if failure_class and failure_class not in FAILURE_CLASSES:
            raise RefusedDocument("unknown failure class")
        sha = hashlib.sha256(data).hexdigest()
        if dup := self.conn.execute("SELECT doc_id FROM knowledge_documents WHERE sha256 = ?", (sha,)).fetchone():
            raise RefusedDocument(f"this file is already in the library ({dup[0]})")
        text = to_markdown(filename, data)  # raises UnsupportedDocument with a reason
        if hits := suspicious_lines(text, limit=3):
            raise RefusedDocument(
                "the document contains text that reads like instructions to an AI, so it was not indexed: "
                + " | ".join(hits)
            )
        text = redact(text)  # tokens / passwords in a pasted config never reach the index
        meta = {
            "doc_id": self._unique_id(doc_slug(filename)),
            "filename": filename,
            "title": title.strip() or _title_of(text, filename),
            "doc_type": doc_type,
            "failure_class": failure_class or None,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{meta['doc_id']}.md").write_text(text)
        (self.dir / f"{meta['doc_id']}.json").write_text(json.dumps(meta, indent=1))
        counts = self._index(as_document(meta, text))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "INSERT INTO knowledge_documents (doc_id, filename, title, doc_type, failure_class, sha256, bytes,"
                " chars, sections, chunks, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (meta["doc_id"], filename, meta["title"], doc_type, meta["failure_class"], sha, len(data), len(text),
                 text.count("\n#") + 1, counts.get("structure", 0), by, now),
            )  # fmt: skip
        return self.get(meta["doc_id"])

    def remove(self, doc_id: str) -> None:
        self._unindex(doc_id)
        for ext in (".md", ".json"):
            (self.dir / f"{doc_id}{ext}").unlink(missing_ok=True)
        with self.conn:
            self.conn.execute("DELETE FROM knowledge_documents WHERE doc_id = ?", (doc_id,))

    # --- vector index ------------------------------------------------------------------------------
    def _index(self, doc: Document) -> dict[str, int]:
        from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import get_embeddings
        from pipeline_incident_copilot.rag.embedding.bm25_sparse_encoder import doc_vector, tokenize
        from pipeline_incident_copilot.rag.vector_store.qdrant_store import (
            collection_name,
            get_qdrant,
            recreate_collection,
            release_qdrant,
        )

        counts = {}
        try:
            client = get_qdrant()
            for strategy in STRATEGIES:
                name = collection_name(strategy)
                if not client.collection_exists(name):
                    if strategy != "structure":
                        continue
                    recreate_collection(name)  # a fresh install: the uploaded documents are the first knowledge
                parts = chunks_for(doc, strategy)
                tokens = [tokenize(c.page_content) for c in parts]
                avgdl = sum(map(len, tokens)) / max(1, len(tokens))
                dense = get_embeddings().embed_documents([c.page_content for c in parts])
                client.upsert(
                    name,
                    points=[
                        models.PointStruct(
                            id=str(uuid.uuid5(POINT_NAMESPACE, c.metadata["chunk_id"])),
                            vector={"dense": vec, "bm25": _sparse(doc_vector(t, avgdl))},
                            payload={"text": c.page_content, **c.metadata},
                        )
                        for c, vec, t in zip(parts, dense, tokens, strict=True)
                    ],
                )
                counts[strategy] = len(parts)
        finally:
            release_qdrant()  # the watcher's next diagnosis needs the folder
        return counts

    def _unindex(self, doc_id: str) -> None:
        from pipeline_incident_copilot.rag.vector_store.qdrant_store import collection_name, get_qdrant, release_qdrant

        match = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])
        try:
            client = get_qdrant()
            for strategy in STRATEGIES:
                if client.collection_exists(name := collection_name(strategy)):
                    client.delete(name, points_selector=models.FilterSelector(filter=match))
        finally:
            release_qdrant()

    def search(self, question: str, k: int = 5) -> list[dict]:
        """What the knowledge agent would find for a question (hybrid dense + BM25 on kb_structure, no rerank)."""
        from pipeline_incident_copilot.rag.retrieval.routes.native_hybrid_route import NativeHybridRoute
        from pipeline_incident_copilot.rag.vector_store.qdrant_store import release_qdrant

        try:
            docs = NativeHybridRoute("structure").search(question, k)
        finally:
            release_qdrant()
        return [
            {
                "title": str(d.metadata.get("title") or d.metadata.get("parent_id")),
                "source_type": d.metadata.get("source_type", "?"),
                "parent_id": d.metadata.get("parent_id", "?"),
                "uploaded": d.metadata.get("source") == "company_upload",
                "text": d.page_content[:420],
            }
            for d in docs
        ]


def _sparse(vector: tuple[list[int], list[float]]) -> models.SparseVector:
    return models.SparseVector(indices=vector[0], values=vector[1])


def _title_of(text: str, filename: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return (m.group(1).strip() if m else Path(filename).stem.replace("_", " ").replace("-", " ").strip())[:120]
