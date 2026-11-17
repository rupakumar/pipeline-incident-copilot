"""Spark / Delta error catalogs (JSON) → one Document per error class.

Every Document gets a stable `parent_id` — the unit relevance is judged on, no matter
how a chunking strategy later splits it.
"""

import json
from pathlib import Path

from langchain_core.documents import Document


def _flatten(message) -> str:
    return " ".join(message) if isinstance(message, list) else str(message)


def load_error_catalog(path: Path, source: str) -> list[Document]:
    """One Document per error class (JSON is already record-structured)."""
    docs = []
    for name, spec in json.loads(path.read_text()).items():
        lines = [f"Error class: {name}"]
        if spec.get("sqlState"):
            lines.append(f"SQLSTATE: {spec['sqlState']}")
        lines.append(_flatten(spec.get("message", "")))
        for sub, sub_spec in (spec.get("subClass") or {}).items():
            lines.append(f"- {name}.{sub}: {_flatten(sub_spec.get('message', ''))}")
        docs.append(
            Document(
                page_content="\n".join(lines),
                metadata={
                    "source": source,
                    "source_type": "error_catalog",
                    "parent_id": f"{source}:{name}",
                    "error_class": name,
                },
            )
        )
    return docs


class ErrorCatalogLoader:
    source_type = "error_catalog"

    def __init__(self, path: Path, source: str):
        self.path = path
        self.source = source

    def load(self) -> list[Document]:
        return load_error_catalog(self.path, self.source)
