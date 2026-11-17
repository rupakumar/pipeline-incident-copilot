"""Databricks error-class reference page (HTML) → one Document per Databricks-specific error.

The page repeats the open-source Spark and Delta classes, which are already indexed from
their own catalogs; those are skipped (`exclude_classes`) so each error has one parent.
Each entry is an <h3> with the class name, a "SQLSTATE: …" paragraph, then the message.
"""

import json
from html.parser import HTMLParser
from pathlib import Path

from langchain_core.documents import Document

SOURCE = "databricks"


class _ErrorClassPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries: dict[str, list[str]] = {}
        self._current: str | None = None
        self._in = None  # "h3" | "p" | None
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3"):
            self._current = None
        if tag in ("h3", "p"):
            self._in, self._buffer = tag, []

    def handle_endtag(self, tag):
        if tag != self._in:
            return
        text = " ".join("".join(self._buffer).split()).replace("​", "").strip()
        if tag == "h3" and text.isupper() and " " not in text:
            self._current = text
            self.entries[text] = []
        elif tag == "p" and self._current and text:
            self.entries[self._current].append(text)
        self._in = None

    def handle_data(self, data):
        if self._in:
            self._buffer.append(data)


def parse_error_reference(html: str) -> dict[str, dict]:
    parser = _ErrorClassPageParser()
    parser.feed(html)
    catalog = {}
    for name, paragraphs in parser.entries.items():
        sqlstate = next((p.removeprefix("SQLSTATE:").strip() for p in paragraphs if p.startswith("SQLSTATE:")), None)
        message = " ".join(p for p in paragraphs if not p.startswith("SQLSTATE:"))
        if message:
            catalog[name] = {"sqlState": sqlstate, "message": message}
    return catalog


def known_classes(*catalog_paths: Path) -> set[str]:
    return {name for path in catalog_paths if path.exists() for name in json.loads(path.read_text())}


class DatabricksErrorReferenceLoader:
    source_type = "error_catalog"

    def __init__(self, path: Path, exclude_classes: set[str] = frozenset()):
        self.path = path
        self.exclude_classes = exclude_classes

    def load(self) -> list[Document]:
        if not self.path.exists():
            return []
        docs = []
        for name, spec in parse_error_reference(self.path.read_text()).items():
            if name in self.exclude_classes:
                continue
            lines = [f"Error class: {name}"]
            if spec["sqlState"]:
                lines.append(f"SQLSTATE: {spec['sqlState']}")
            lines.append(spec["message"])
            docs.append(
                Document(
                    page_content="\n".join(lines),
                    metadata={
                        "source": SOURCE,
                        "source_type": "error_catalog",
                        "parent_id": f"{SOURCE}:{name}",
                        "error_class": name,
                    },
                )
            )
        return docs
