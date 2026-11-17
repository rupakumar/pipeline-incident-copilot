"""Loader interface: every knowledge source turns into LangChain Documents.

Every Document carries `source_type` (what kind of knowledge it is) and a stable
`parent_id` — the unit relevance is judged on, however a chunker later splits it.
"""

from typing import Protocol

from langchain_core.documents import Document


class DocumentLoader(Protocol):
    source_type: str

    def load(self) -> list[Document]: ...
