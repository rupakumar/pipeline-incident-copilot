from evals.metrics.diagnosis_metrics import brier, context_scores, ece, faithfulness
from evals.regression_gate import check


def test_context_scores_and_faithfulness():
    assert context_scores(
        ["runbook:a", "ticket:T", "spark:X", "code:c"], {"runbook:a", "spark:X", "z", "y", "w"}, 4
    ) == (0.5, 0.5)
    cause = "gold_table changed to 'samples.bakehouse.hourly_events' on 2008-11-10"
    assert (
        faithfulness(cause, "changed: gold_table: '' → 'samples.bakehouse.hourly_events'; run_date 2008-11-10") == 1.0
    )
    assert faithfulness(cause, "gold_table changed") < 1.0  # the table and the date were not in the evidence
    assert faithfulness("the run failed", "") == 1.0  # no concrete claims to check


def test_calibration_scores():
    assert brier([1.0, 0.0], [True, False]) == 0.0
    assert brier([0.95, 0.95], [True, False]) == 0.4525
    assert ece([0.9, 0.9, 0.9, 0.9], [True, True, True, False]) == 0.15


def test_gate_flags_drops_but_not_noise():
    base = {"summary": {"all_ok": 1.0, "faithfulness": 0.9, "mean_seconds": 15.0, "errors": 0}}
    assert check({"summary": {"all_ok": 0.99, "faithfulness": 0.9, "mean_seconds": 16, "errors": 0}}, base) == []
    problems = check({"summary": {"all_ok": 0.89, "faithfulness": 0.9, "mean_seconds": 30, "errors": 0}}, base)
    assert any("all_ok" in p for p in problems) and any("mean_seconds" in p for p in problems)
