from pipeline_incident_copilot.learning.incident_memory import IncidentMemory, pattern_of
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle, RunComparison
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.memory_repository import MemoryRepository


def test_one_memory_per_pattern_with_a_track_record(tmp_path):
    repo = MemoryRepository(connect(tmp_path / "t.db"))
    mid = repo.insert(
        "UNAUTHORIZED_ACCESS|gold_aggregate|gold_table",
        "INC-0020",
        "v1",
        "permissions",
        "UNAUTHORIZED_ACCESS",
        "gold_aggregate",
    )
    repo.add_occurrence(mid, "INC-0031", "v2")
    repo.add_occurrence(mid, "INC-0031", "v2")  # same incident twice: counted once
    m = repo.get(mid)
    assert m["incident_ids"] == ["INC-0020", "INC-0031"] and m["seen_count"] == 2 and m["text"] == "v2"
    repo.record_outcome(mid, worked=False)
    assert not IncidentMemory.trusted(repo.get(mid))  # 0 worked, 1 failed → no longer offered
    repo.record_outcome(mid, worked=True)
    assert IncidentMemory.trusted(repo.get(mid))


def test_pattern_is_error_code_task_and_changed_parameters():
    ev = EvidenceBundle(
        incident_id="INC-1",
        failed_task="gold_aggregate",
        error="[UNAUTHORIZED_ACCESS] Unauthorized access:",
        error_trace_chars=0,
        distilled_log="",
        parameters={},
        comparison=RunComparison(
            failed=None, last_success=None, changed_parameters={"gold_table": ("", "samples.a.b")}
        ),
    )
    assert pattern_of({"evidence": ev}) == "UNAUTHORIZED_ACCESS|gold_aggregate|gold_table"


def test_citations_keep_one_of_each_kind_before_near_duplicates():
    from langchain_core.documents import Document

    from pipeline_incident_copilot.agents.knowledge_agent import diverse

    def doc(pid, kind, score):
        return Document(page_content=pid, metadata={"parent_id": pid, "source_type": kind, "rerank_score": score})

    ranked = [
        doc("databricks:A", "error_catalog", 10),
        doc("databricks:B", "error_catalog", 0.96),
        doc("databricks:C", "error_catalog", 0.95),
        doc("ticket:T1", "ticket", 0.99),
        doc("memory:1", "incident", 0.90),
        doc("runbook:r", "runbook", 0.90),
        doc("code:g", "code", 0.99),
    ]
    picked = [d.metadata["parent_id"] for d in diverse(ranked, 4)]
    # memory + runbook are reserved even though near-duplicate error docs score higher; then the top scores
    assert {"memory:1", "runbook:r", "databricks:A"} <= set(picked) and "databricks:C" not in picked


def oom_state(platform="databricks", job="loghub_etl"):
    from types import SimpleNamespace

    ev = EvidenceBundle(
        incident_id="INC-0014",
        failed_task="gold_aggregate",
        error="Execution ran out of memory.",
        error_trace_chars=0,
        distilled_log="",
        parameters={},
        comparison=RunComparison(
            failed=None,
            last_success=None,
            changed_parameters={"debug_sample_rows": ("0", "500000000"), "silver_write_mode": ("append", "overwrite")},
        ),
    )
    run = SimpleNamespace(platform=platform, job_name=job, connection_id="c")
    return {"evidence": ev, "root_cause": SimpleNamespace(failure_class="resource_exhaustion"),
            "incident": SimpleNamespace(run=run)}  # fmt: skip


def test_an_unrelated_change_in_the_same_run_does_not_split_the_lesson():
    # INC-0014: silver_write_mode also changed, but it can't cause an out-of-memory → same pattern as INC-0023
    assert pattern_of(oom_state()) == "-|gold_aggregate|debug_sample_rows"
    # a job without a cause map keeps every changed parameter (nothing is hidden)
    assert pattern_of(oom_state(platform="jenkins", job="x")) == "-|gold_aggregate|debug_sample_rows,silver_write_mode"


def test_consolidate_merges_memories_of_the_same_lesson_and_keeps_different_fixes_apart(tmp_path, monkeypatch):
    from pipeline_incident_copilot.learning import incident_memory

    repo = MemoryRepository(connect(tmp_path / "t.db"))
    key, fix = "databricks-loghub:loghub_etl", {"debug_sample_rows": "0"}
    a = repo.insert("-|gold_aggregate|debug_sample_rows", "INC-0023", "x Seen 1 time(s).", "resource_exhaustion",
                    None, "gold_aggregate", key, fix)  # fmt: skip
    repo.add_occurrence(a, "INC-0034", "x Seen 2 time(s).", fix)
    b = repo.insert("-|gold_aggregate|debug_sample_rows,silver_write_mode", "INC-0014", "y", "resource_exhaustion",
                    None, "gold_aggregate", key, fix)  # fmt: skip
    c = repo.insert("DELTA_METADATA_MISMATCH|silver_parse|enrich_columns,silver_write_mode", "INC-0007", "z",
                    "schema_change", None, "silver_parse", key, {"enrich_columns": ""})  # fmt: skip
    monkeypatch.setattr(IncidentMemory, "_upsert_point", lambda self, m, t: None)
    monkeypatch.setattr(
        incident_memory, "get_qdrant", lambda: type("Q", (), {"collection_exists": lambda s, n: False})()
    )
    memory = IncidentMemory(repo, collection="test")
    assert memory.consolidate(lambda cid: "databricks") == [(a, b)]
    kept = repo.get(a)
    assert kept["incident_ids"] == ["INC-0023", "INC-0034", "INC-0014"] and kept["fix_successes"] == 3
    assert kept["seen_count"] == 3 and kept["text"] == "x Seen 3 time(s)." and repo.get(b) is None
    assert repo.get(c)["pattern"].endswith("enrich_columns,silver_write_mode")  # both can cause a schema change
    assert memory.consolidate(lambda cid: "databricks") == []  # idempotent
