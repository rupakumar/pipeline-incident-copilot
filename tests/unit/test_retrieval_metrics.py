import pytest

from evals.metrics.retrieval_metrics import score


def test_perfect_ranking():
    m = score(["a", "b", "x"], {"a", "b"}, k=5)
    assert m == {"hit": 1.0, "recall": 1.0, "mrr": 1.0, "ndcg": pytest.approx(1.0)}


def test_first_relevant_at_rank_three():
    m = score(["x", "y", "a"], {"a"}, k=5)
    assert m["hit"] == 1.0 and m["mrr"] == pytest.approx(1 / 3)


def test_miss():
    assert score(["x", "y"], {"a"}, k=5)["hit"] == 0.0
