import pytest

from pipeline_incident_copilot.schemas.evidence import EvidenceBundle, ToolResult
from pipeline_incident_copilot.schemas.incident import FailedRun, IncidentStatus
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository


def _run(run_id: int, task: str = "bronze_ingest") -> FailedRun:
    return FailedRun(
        job_id=1,
        job_name="loghub_etl",
        run_id=run_id,
        result_state="FAILED",
        failed_task=task,
        parameters={"source": "spark", "run_date": "2017-06-12"},
    )


def test_open_numbers_incidents_and_dedupes_by_run(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    first, second = repo.open(_run(101)), repo.open(_run(102))
    assert (first.incident_id, second.incident_id) == ("INC-0001", "INC-0002")
    assert repo.exists_for_run(_run(101)) and not repo.exists_for_run(_run(999))
    assert first.source == "spark" and first.run_date == "2017-06-12"


def test_status_changes_and_evidence_are_kept_with_history(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    inc = repo.open(_run(101))
    repo.save_evidence(
        EvidenceBundle(
            incident_id=inc.incident_id,
            failed_task="bronze_ingest",
            error="[PATH_NOT_FOUND] ...",
            error_trace_chars=10,
            distilled_log="x",
            parameters={},
            tool_results=[ToolResult(tool="check_path_exists", args={}, ok=True, summary="does NOT exist")],
        )
    )
    repo.set_status(inc.incident_id, IncidentStatus.INVESTIGATING, "agents started")
    assert repo.get(inc.incident_id).status is IncidentStatus.INVESTIGATING
    assert repo.get_evidence(inc.incident_id).tool_results[0].summary == "does NOT exist"
    assert [e["kind"] for e in repo.events(inc.incident_id)] == ["opened", "evidence", "status:investigating"]
    assert [i.incident_id for i in repo.recent(IncidentStatus.INVESTIGATING)] == ["INC-0001"]


def _evidence(incident_id: str) -> EvidenceBundle:
    return EvidenceBundle(
        incident_id=incident_id, failed_task="gold_aggregate", error="[UNAUTHORIZED_ACCESS]", error_trace_chars=0,
        distilled_log="x", parameters={},
    )  # fmt: skip


def test_watcher_sweep_finds_only_recent_open_incidents_with_evidence_and_no_diagnosis(tmp_path):
    from datetime import UTC, datetime, timedelta

    repo = IncidentRepository(connect(tmp_path / "t.db"))
    ready, collecting, diagnosed, old = (repo.open(_run(n)) for n in (1, 2, 3, 4))
    for inc in (ready, diagnosed, old):
        repo.save_evidence(_evidence(inc.incident_id))
    repo.save_diagnosis(diagnosed.incident_id, "thread-1", {"source": "graph"})
    repo.conn.execute("UPDATE incidents SET opened_at = '2026-01-01T00:00:00+00:00' WHERE incident_id = ?",
                      (old.incident_id,))  # fmt: skip
    found = repo.undiagnosed(since=datetime.now(UTC) - timedelta(hours=24))
    assert [i.incident_id for i in found] == [ready.incident_id]  # not mid-intake, not done, not stale
    repo.set_status(ready.incident_id, IncidentStatus.ESCALATED, "x")
    assert repo.undiagnosed(since=datetime.now(UTC) - timedelta(hours=24)) == []


def test_a_rerun_the_copilot_started_as_a_fix_is_not_a_new_incident(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    original = repo.open(_run(101))
    repo.set_status(original.incident_id, IncidentStatus.REMEDIATING, "rerun 555 started with {'gold_table': ''}")
    assert repo.started_by_copilot(_run(555))  # its failure belongs to the original incident
    assert not repo.started_by_copilot(_run(5551)) and not repo.started_by_copilot(_run(556))
    other_job = _run(555).model_copy(update={"job_name": "another_job"})
    assert not repo.started_by_copilot(other_job)  # Jenkins build numbers repeat across jobs


def test_a_rerun_failing_before_its_id_is_recorded_is_still_recognised_as_the_copilots(tmp_path):
    from datetime import UTC, datetime, timedelta

    repo = IncidentRepository(connect(tmp_path / "t.db"))
    now = datetime.now(UTC)
    rerun = _run(777).model_copy(update={"started_at": now + timedelta(seconds=5)})
    earlier_failure = _run(776).model_copy(update={"started_at": now - timedelta(minutes=10)})
    with repo.rerun_pending(rerun.connection_id, rerun.job_name):  # started, id not recorded yet
        assert repo.started_by_copilot(rerun)
        assert not repo.started_by_copilot(earlier_failure)  # a real failure from before the fix still opens
        other_pipeline = rerun.model_copy(update={"job_name": "another_job"})
        assert not repo.started_by_copilot(other_pipeline)
    assert not repo.started_by_copilot(rerun)  # marker gone; from here the recorded id decides
    repo.set_state(repo._pending_key(rerun.connection_id, rerun.job_name), (now - timedelta(hours=1)).isoformat())
    assert not repo.started_by_copilot(rerun)  # a crash left the marker behind: it expires


def test_two_processes_opening_the_same_failed_run_make_one_incident(tmp_path):
    path = tmp_path / "t.db"
    first, second = IncidentRepository(connect(path)), IncidentRepository(connect(path))
    assert first.open_if_new(_run(42)).incident_id == "INC-0001"
    assert second.open_if_new(_run(42)) is None  # the other watcher got there first: skipped, no crash
    assert second.open_if_new(_run(43)).incident_id == "INC-0002"
    with pytest.raises(ValueError, match="already has an incident"):
        first.open(_run(42))


def test_an_incident_left_investigating_by_a_dead_process_is_found_for_a_retry(tmp_path):
    from datetime import UTC, datetime, timedelta

    repo = IncidentRepository(connect(tmp_path / "t.db"))
    stuck, busy = repo.open(_run(1)), repo.open(_run(2))
    for inc in (stuck, busy):
        repo.set_status(inc.incident_id, IncidentStatus.INVESTIGATING, "agents started")
    repo.conn.execute("UPDATE incident_events SET at = '2026-01-01T00:00:00+00:00' WHERE incident_id = ?",
                      (stuck.incident_id,))  # fmt: skip
    since = datetime(2020, 1, 1, tzinfo=UTC)
    found = repo.stalled(since=since, quiet_for=timedelta(minutes=15))
    assert [i.incident_id for i in found] == [stuck.incident_id]  # the busy one had activity just now
