from langchain_core.documents import Document

from pipeline_incident_copilot.rag.retrieval.fusion.reciprocal_rank_fusion import reciprocal_rank_fusion


def _docs(*ids):
    return [Document(page_content=i, metadata={"chunk_id": i}) for i in ids]


def test_documents_ranked_well_in_both_lists_win():
    fused = reciprocal_rank_fusion([_docs("a", "b", "c"), _docs("b", "a", "d")])
    assert [d.metadata["chunk_id"] for d in fused][:2] in (["a", "b"], ["b", "a"])
    assert {d.metadata["chunk_id"] for d in fused} == {"a", "b", "c", "d"}


def test_single_ranking_keeps_order():
    fused = reciprocal_rank_fusion([_docs("x", "y", "z")])
    assert [d.metadata["chunk_id"] for d in fused] == ["x", "y", "z"]
