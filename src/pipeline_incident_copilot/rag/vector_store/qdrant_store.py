"""Qdrant vector store (local mode, on disk).

One collection per chunking strategy, each point carrying two named vectors:
- "dense": bge-small embedding (semantic)
- "bm25":  sparse BM25 term weights (keyword), IDF applied by Qdrant
Swap `QdrantClient(path=...)` for `url=...` to use a Qdrant server.

Local mode locks the folder for the process that opens it, so several copilot processes (watcher,
approval console, CLI) take turns: each releases the client after its graph run (`release_qdrant`) and
a process that finds the folder busy waits for it instead of failing.
"""

import atexit
import os
import shutil
import time
from functools import lru_cache
from pathlib import Path

from langchain_core.documents import Document
from qdrant_client import QdrantClient, models

from pipeline_incident_copilot.config import RUNTIME_DIR

QDRANT_PATH = Path(os.getenv("COPILOT_QDRANT", str(RUNTIME_DIR / "qdrant")))  # evals point this at a copy
DENSE_DIM = 384  # bge-small-en-v1.5
LOCK_WAIT_SECONDS = 60  # another process holds the folder only for one diagnosis or one memory write
LOCK_RETRY_SECONDS = 1.0


def _is_locked(error: RuntimeError) -> bool:
    return "already accessed by another instance" in str(error)


@lru_cache(maxsize=1)
def get_qdrant() -> QdrantClient:
    """One client per process (local mode holds a file lock); waits while another process holds it."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            return QdrantClient(path=str(QDRANT_PATH))
        except RuntimeError as e:
            if not _is_locked(e) or time.monotonic() > deadline:
                raise
            time.sleep(LOCK_RETRY_SECONDS)


def release_qdrant() -> None:
    """Close this process's client so other copilot processes can open the folder; reopens on next use."""
    if get_qdrant.cache_info().currsize:
        get_qdrant().close()
        get_qdrant.cache_clear()


atexit.register(release_qdrant)  # close before interpreter teardown (avoids noisy __del__)


def snapshot_store(target: Path, source: Path | None = None) -> Path:
    """A private copy of the store for batch jobs (evals), so they never hold the live store's lock while the watcher
    or the console need it. The copy is fresh each time; the live lock file is not copied."""
    source = source or RUNTIME_DIR / "qdrant"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".lock"))
    return target


def collection_name(strategy: str) -> str:
    return f"kb_{strategy}"


def recreate_collection(name: str) -> None:
    client = get_qdrant()
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        name,
        vectors_config={"dense": models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )


def to_document(point) -> Document:
    payload = dict(point.payload)
    return Document(page_content=payload.pop("text"), metadata={**payload, "score": point.score})
