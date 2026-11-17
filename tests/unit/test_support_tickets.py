from evals.dataset_loader import load_failed_runs, own_ticket_ids, relevant_for_run, scenarios_by_id, ticket_index
from evals.generate_ticket_history import held_out_values


def test_tickets_come_only_from_train_runs():
    train_run_ids = {str(r["run_id"]) for r in load_failed_runs("train")}
    assert ticket_index(), "no tickets generated"
    assert {t["source_run_id"] for t in ticket_index()} <= train_run_ids


def test_a_run_never_counts_its_own_ticket_as_relevant():
    for run in load_failed_runs("train"):
        assert not (own_ticket_ids(run) & relevant_for_run(run))


def test_no_test_scenario_details_in_any_ticket():
    from pipeline_incident_copilot.rag.ingestion.loaders import KNOWLEDGE_DIR

    text = "".join(p.read_text() for p in (KNOWLEDGE_DIR / "tickets").glob("TCK-*.md"))
    assert not [v for v in held_out_values(scenarios_by_id()) if v in text]
