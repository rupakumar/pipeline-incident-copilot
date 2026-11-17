"""Markdown knowledge documents (architecture docs, runbooks) → one Document per file.

A YAML front-matter block (`--- ... ---`) at the top becomes metadata. With
`parent_per_section=True` the chunker gives each section its own parent_id (for long
reference documents); otherwise the whole file is one relevance unit (runbooks).
"""

import re
from pathlib import Path

import yaml
from langchain_core.documents import Document

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def split_front_matter(text: str) -> tuple[dict, str]:
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, text[match.end() :]


class MarkdownDocumentLoader:
    metadata_keys: tuple[str, ...] = ("failure_class", "owner", "last_reviewed")

    def __init__(self, directory: Path, source_type: str, parent_per_section: bool = False):
        self.directory = directory
        self.source_type = source_type
        self.parent_per_section = parent_per_section

    def load(self) -> list[Document]:
        docs = []
        for path in sorted(self.directory.glob("*.md")):
            meta, body = split_front_matter(path.read_text())
            docs.append(
                Document(
                    page_content=body.strip(),
                    metadata={
                        "source": self.source_type,
                        "source_type": self.source_type,
                        "file": path.name,
                        "title": meta.get("title", path.stem),
                        "parent_id": f"{self.source_type}:{path.stem}",
                        "parent_per_section": self.parent_per_section,
                        **{k: str(v) for k, v in meta.items() if k in self.metadata_keys},
                        # codes this document is the reference for: the exact error-code route finds it by them
                        **({"error_class": sorted(meta["error_signatures"])} if meta.get("error_signatures") else {}),
                    },
                )
            )
        return docs
