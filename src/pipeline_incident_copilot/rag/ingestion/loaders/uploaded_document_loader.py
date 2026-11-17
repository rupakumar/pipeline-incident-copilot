"""Company documents uploaded in the console (data/runtime/knowledge_uploads), read back for a full index rebuild.

KnowledgeLibrary stores each upload already converted to Markdown, with a JSON sidecar describing it, so a rebuild
(`python -m scripts.build_index`) indexes exactly what the incremental upload indexed.
"""

import json
from pathlib import Path

from langchain_core.documents import Document


class UploadedDocumentLoader:
    def __init__(self, directory: Path):
        self.directory = directory

    def load(self) -> list[Document]:
        from pipeline_incident_copilot.rag.ingestion.knowledge_library import as_document

        docs = []
        for meta_file in sorted(self.directory.glob("*.json")):
            text_file = meta_file.with_suffix(".md")
            if text_file.exists():
                docs.append(as_document(json.loads(meta_file.read_text()), text_file.read_text()))
        return docs
