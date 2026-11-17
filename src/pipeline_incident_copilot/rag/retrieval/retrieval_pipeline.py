"""Configurable retrieval pipeline → optional cross-encoder rerank.

Modes:
- dense / bm25:   single retriever
- hybrid:         dense + bm25, fused by OUR reciprocal_rank_fusion (fusion/)
- hybrid_native:  dense + bm25, fused by Qdrant server-side (Fusion.RRF)

Tracing: `retrieve-knowledge` (retriever) → one child retriever per search route and query,
then `fuse-rankings` and `rerank-documents`, each showing the documents it produced.
"""

from dataclasses import dataclass

from langchain_core.documents import Document

from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.rag.embedding.bge_dense_embedder import MODEL as EMBEDDING_MODEL
from pipeline_incident_copilot.rag.retrieval.fusion.reciprocal_rank_fusion import reciprocal_rank_fusion
from pipeline_incident_copilot.rag.retrieval.reranking.cross_encoder_reranker import MODEL as RERANK_MODEL
from pipeline_incident_copilot.rag.retrieval.reranking.cross_encoder_reranker import rerank
from pipeline_incident_copilot.rag.retrieval.routes.dense_search_route import DenseSearchRoute
from pipeline_incident_copilot.rag.retrieval.routes.keyword_search_route import KeywordSearchRoute
from pipeline_incident_copilot.rag.retrieval.routes.native_hybrid_route import NativeHybridRoute

TRACE_TOP_N = 10  # documents shown per step in a trace


def summarize(docs: list[Document], n: int = TRACE_TOP_N) -> list[dict]:
    """Compact, readable view of ranked documents for trace output."""
    return [
        {
            "rank": i + 1,
            "parent_id": d.metadata.get("parent_id"),
            "source": d.metadata.get("source"),
            "score": round(float(d.metadata.get("rerank_score", d.metadata.get("score", 0.0))), 4),
            "text": d.page_content[:160],
        }
        for i, d in enumerate(docs[:n])
    ]


@dataclass(frozen=True)
class RetrievalConfig:
    strategy: str = "structure"  # chunking strategy / index to use
    mode: str = "hybrid"  # dense | bm25 | hybrid | hybrid_native
    rerank: bool = True
    candidates: int = 30  # per first-stage retriever

    @property
    def name(self) -> str:
        return f"{self.strategy}/{self.mode}{'+rerank' if self.rerank else ''}"


class RetrievalPipeline:
    def __init__(self, config: RetrievalConfig):
        self.config = config
        self.dense = DenseSearchRoute(config.strategy) if config.mode in ("dense", "hybrid") else None
        self.bm25 = KeywordSearchRoute(config.strategy) if config.mode in ("bm25", "hybrid") else None
        self.native = NativeHybridRoute(config.strategy) if config.mode == "hybrid_native" else None

    def _search(self, route_name: str, search, query: str) -> list[Document]:
        with get_langfuse().start_as_current_observation(
            as_type="retriever",
            name=route_name,
            input=query,
            metadata={"index": f"kb_{self.config.strategy}", "limit": self.config.candidates},
        ) as observation:
            docs = search(query)
            observation.update(output=summarize(docs), metadata={"returned": len(docs)})
        return docs

    def retrieve(self, query: str | list[str], k: int = 5) -> list[Document]:
        """Accepts one query or several (e.g. multi-query rewrites); all rankings get fused."""
        queries = [query] if isinstance(query, str) else query
        candidates = self.config.candidates
        langfuse = get_langfuse()
        with langfuse.start_as_current_observation(
            as_type="retriever",
            name="retrieve-knowledge",
            input={"queries": queries, "k": k},
            metadata={"config": self.config.name, "embedding_model": EMBEDDING_MODEL},
        ) as root:
            rankings = []
            for q in queries:
                if self.dense:
                    rankings.append(self._search("search-dense", lambda x: self.dense.search(x, candidates), q))
                if self.bm25:
                    rankings.append(self._search("search-keyword", lambda x: self.bm25.search(x, candidates), q))
                if self.native:
                    rankings.append(
                        self._search("search-hybrid-native", lambda x: self.native.search(x, candidates, candidates), q)
                    )

            if len(rankings) == 1:
                fused = rankings[0]
            else:
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="fuse-rankings",
                    input={"rankings": len(rankings)},
                    metadata={"method": "rrf", "k": 60},
                ) as fusion:
                    fused = reciprocal_rank_fusion(rankings)
                    fusion.update(output=summarize(fused))

            if self.config.rerank:
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="rerank-documents",
                    input={"query": queries[0], "candidates": min(len(fused), candidates)},
                    metadata={"model": RERANK_MODEL},
                ) as reranking:
                    results = rerank(queries[0], fused[:candidates], k)
                    reranking.update(output=summarize(results))
            else:
                results = fused[:k]
            root.update(output=summarize(results, k))
        return results
