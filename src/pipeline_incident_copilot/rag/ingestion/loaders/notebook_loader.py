"""Databricks notebook sources → one Document per notebook (one per job task); chunkers split them."""

from pathlib import Path

from langchain_core.documents import Document


def load_notebook(path: Path) -> list[Document]:
    return [
        Document(
            page_content=path.read_text(),
            metadata={"source": "code", "source_type": "code", "file": path.name, "parent_id": f"code:{path.stem}"},
        )
    ]


class NotebookLoader:
    """Every notebook in a directory, e.g. databricks/notebooks/ (bronze_ingest, silver_parse, …)."""

    source_type = "code"

    def __init__(self, directory: Path):
        self.directory = directory

    def load(self) -> list[Document]:
        return [doc for path in sorted(self.directory.glob("*.py")) for doc in load_notebook(path)]
