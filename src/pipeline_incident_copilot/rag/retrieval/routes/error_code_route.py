"""Exact error-code lookup: the catalog entry whose error_class matches the error (no ranking needed)."""

import re
from functools import lru_cache

from langchain_core.documents import Document
from qdrant_client import models

from pipeline_incident_copilot.config import PROJECT_ROOT
from pipeline_incident_copilot.rag.ingestion.loaders.markdown_document_loader import split_front_matter
from pipeline_incident_copilot.rag.vector_store.qdrant_store import collection_name, get_qdrant

ERROR_CODE = re.compile(r"\[([A-Z][A-Z0-9_]{3,})(?:\.[A-Z0-9_]+)?\]")


def error_codes_in(text: str) -> list[str]:
    """Bracketed error classes in order of appearance, e.g. '[PATH_NOT_FOUND] ...' -> ['PATH_NOT_FOUND']."""
    seen = []
    for code in ERROR_CODE.findall(text or ""):
        if code not in seen:
            seen.append(code)
    return seen


@lru_cache(maxsize=1)
def runbook_signatures() -> dict[str, tuple[str, ...]]:
    """code → exact error lines, from the runbooks' `error_signatures` front matter (the one place they are written)."""
    out: dict[str, tuple[str, ...]] = {}
    for path in sorted((PROJECT_ROOT / "knowledge" / "runbooks").glob("*.md")):
        meta, _ = split_front_matter(path.read_text())
        for code, lines in (meta.get("error_signatures") or {}).items():
            out[code] = tuple(out.get(code, ())) + tuple(lines)
    return out


def known_error_codes(text: str) -> list[str]:
    """Bracketed codes first (Databricks/Spark style); else the code of a runbook whose exact error line is in the
    text, which also catches unbracketed codes such as Jenkins' `ERROR: DATA_QUALITY_FAILED: ...`."""
    if codes := error_codes_in(text):
        return codes
    lowered = (text or "").lower()
    return [code for code, lines in runbook_signatures().items() if any(line.lower() in lowered for line in lines)]


class ErrorCodeLookupRoute:
    def __init__(self, strategy: str):
        self.collection = collection_name(strategy)

    def search(self, error_code: str, k: int = 3) -> list[Document]:
        points, _ = get_qdrant().scroll(
            self.collection,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="error_class", match=models.MatchValue(value=error_code))]
            ),
            limit=k,
            with_payload=True,
        )
        docs = []
        for p in points:  # scroll returns records (no score): an exact match scores 1.0
            payload = dict(p.payload)
            docs.append(Document(page_content=payload.pop("text"), metadata={**payload, "score": 1.0}))
        return docs
