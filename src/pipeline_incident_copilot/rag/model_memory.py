"""Free the retrieval models (embedder + cross-encoder) between steps, so a local LLM can load next.

They reload on the next retrieval (a few seconds). Part of the Mac memory rule: never hold the
retrieval models and an LLM at the same time.
"""

import gc


def release_retrieval_models() -> None:
    from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import get_embeddings
    from pipeline_incident_copilot.rag.retrieval.reranking.cross_encoder_reranker import _model as reranker

    get_embeddings.cache_clear()
    reranker.cache_clear()
    gc.collect()
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except ImportError:
        pass
