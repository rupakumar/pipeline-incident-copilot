"""Auto-apply known fixes: memory fix track record, every rule of the policy, and the v1 → v2 backfill."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from pipeline_incident_copilot.learning.auto_approval import EVENT, AutoApprovalPolicy
from pipeline_incident_copilot.schemas.agent_outputs import ProposedAction, Resolution, RootCause
from pipeline_incident_copilot.schemas.connection import Connection
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle, RunComparison
from pipeline_incident_copilot.schemas.incident import FailedRun
from pipeline_incident_copilot.storage.connection_repository import ConnectionRepository
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository
from pipeline_incident_copilot.storage.memory_repository import MemoryRepository

PATTERN = "-|build step 'Execute shell'|MIN_ROWS"
FIX = {"MIN_ROWS": "1000"}


@pytest.fixture
def store(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    connections = ConnectionRepository(repo.conn)
    connections.save(
        Connection(connection_id="local-jenkins", name="Local Jenkins", platform="jenkins", pipelines=["export"])
    )
    return repo, connections, MemoryRepository(repo.conn)


def state_for(repo, fix=FIX, confidence=0.9, attempts=0, job="export", number=7):
    run = FailedRun(
        connection_id="local-jenkins", platform="jenkins", job_id=job, job_name=job, run_id=number, result_state="F"
    )
    inc = repo.open(run)
    ev = EvidenceBundle(
        incident_id=inc.incident_id,
        failed_task="build step 'Execute shell'",
        error="ERROR: DATA_QUALITY_FAILED: min_row_count expected >= 5000, got 2000",
        error_trace_chars=0,
        distilled_log="x",
        parameters={},
        comparison=RunComparison(failed=None, last_success=None, changed_parameters={"MIN_ROWS": ("1000", "5000")}),
    )
    rc = RootCause(
        cause="MIN_ROWS raised", failure_class="data_quality", confidence=0.9, evidence_refs=[], needs_human=False
    )
    action = ProposedAction(name="rerun_job", parameters=fix, reason="undo")
    res = Resolution(summary="reset", steps=[], action=action, risk="low", verify=[])
    return {
        "incident": inc,
        "evidence": ev,
        "root_cause": rc,
        "resolution": res,
        "confidence": {"calibrated": confidence},
        "fix_attempts": attempts,
    }


def remember(memories, successes=2, failures=0, key="local-jenkins:export", fix=FIX):
    mid = memories.insert(PATTERN, "INC-0001", "text", "data_quality", None, "x", key, fix)
    for n in range(2, successes + 1):
        memories.add_occurrence(mid, f"INC-000{n}", "text", fix)
    for _ in range(failures):
        memories.record_outcome(mid, worked=False)
    return mid


def switch_on(connections, minimum=2):
    c = connections.get("local-jenkins")
    connections.save(c.model_copy(update={"auto_apply": True, "auto_apply_min_successes": minimum}))


def test_memory_counts_successes_of_the_exact_fix_only(store):
    _, _, memories = store
    mid = remember(memories, successes=2)
    assert memories.get(mid)["fix_successes"] == 2 and memories.get(mid)["fix"] == FIX
    memories.add_occurrence(mid, "INC-0002", "text", FIX)  # same incident again: not a new success
    assert memories.get(mid)["fix_successes"] == 2
    memories.add_occurrence(mid, "INC-0009", "text", {"MIN_ROWS": ""})  # a different fix restarts the count
    assert memories.get(mid)["fix_successes"] == 1 and memories.get(mid)["fix"] == {"MIN_ROWS": ""}


def test_known_fix_with_enough_successes_is_approved_when_switched_on(store):
    repo, connections, memories = store
    remember(memories, successes=2)
    policy = AutoApprovalPolicy(memories, connections, repo, enabled=True)
    off = policy.decide(state_for(repo))
    assert not off.approved and "auto-apply is off for the connection" in off.reason and off.successes == 2
    switch_on(connections)
    on = policy.decide(state_for(repo, number=8))
    assert on.approved and on.reason.startswith("approved automatically: known fix from INC-0001, INC-0002")


@pytest.mark.parametrize(
    ("setup", "kwargs", "reason"),
    [
        ({"successes": 1}, {}, "worked 1 of the 2 times needed"),
        ({"failures": 1}, {}, "failed 1 time(s)"),
        ({"key": "local-jenkins:other"}, {}, "no verified fix on record"),
        ({}, {"fix": {"MIN_ROWS": ""}}, "differs from the known fix"),
        ({}, {"confidence": 0.8}, "confidence 0.80 is below 0.85"),
        ({}, {"attempts": 1}, "an earlier rerun of this incident failed"),
    ],
)
def test_every_rule_can_refuse(store, setup, kwargs, reason):
    repo, connections, memories = store
    remember(memories, **setup)
    switch_on(connections)
    answer = AutoApprovalPolicy(memories, connections, repo, enabled=True).decide(state_for(repo, **kwargs))
    assert not answer.approved and reason in answer.reason


def test_global_switch_and_rate_limit(store):
    repo, connections, memories = store
    remember(memories)
    switch_on(connections)
    assert (
        "switched off" in AutoApprovalPolicy(memories, connections, repo, enabled=False).decide(state_for(repo)).reason
    )
    policy = AutoApprovalPolicy(memories, connections, repo, enabled=True)
    first = state_for(repo, number=10)
    assert policy.decide(first).approved
    repo.add_event(first["incident"].incident_id, EVENT, "approved automatically")
    assert "at most 1 per 1 h" in policy.decide(state_for(repo, number=11)).reason  # default cooldown: 1 h
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    repo.conn.execute("UPDATE incident_events SET at = ? WHERE kind = ?", (old, EVENT))
    assert policy.decide(state_for(repo, number=12)).approved


def test_the_cooldown_is_set_per_connection_and_zero_means_no_limit(store):
    repo, connections, memories = store
    remember(memories)
    switch_on(connections)
    policy = AutoApprovalPolicy(memories, connections, repo, enabled=True)
    repo.add_event(state_for(repo, number=20)["incident"].incident_id, EVENT, "approved automatically")
    c = connections.get("local-jenkins")
    connections.save(c.model_copy(update={"auto_apply_cooldown_hours": 6}))
    assert "at most 1 per 6 h" in policy.decide(state_for(repo, number=21)).reason
    connections.save(c.model_copy(update={"auto_apply_cooldown_hours": 0}))
    assert policy.decide(state_for(repo, number=22)).approved


def test_a_waiting_ticket_is_auto_fixed_once_its_rule_passes(store):
    from pipeline_incident_copilot.schemas.incident import IncidentStatus
    from pipeline_incident_copilot.workflow.diagnoser import IncidentDiagnoser

    repo, connections, memories = store
    remember(memories)
    switch_on(connections)
    state = state_for(repo, number=30)
    iid = state["incident"].incident_id
    repo.set_status(iid, IncidentStatus.AWAITING_APPROVAL, "waiting")
    repo.add_event(iid, "auto_approval:no", "needs a person: cooldown (matches known fix from INC-0001)")
    assert [i.incident_id for i in repo.waiting_with_known_fix(datetime.now(UTC) - timedelta(hours=1))] == [iid]
    diagnoser = IncidentDiagnoser.__new__(IncidentDiagnoser)
    diagnoser.repo, resumed = repo, []
    diagnoser.saved_state = lambda incident_id: state
    diagnoser.resume = lambda incident_id, decision: resumed.append(decision)
    policy = AutoApprovalPolicy(memories, connections, repo, enabled=True)
    connections.save(connections.get("local-jenkins").model_copy(update={"auto_apply": False}))
    assert diagnoser.auto_apply_waiting(iid, policy) is False and resumed == []  # still held back: stays waiting
    switch_on(connections)
    assert diagnoser.auto_apply_waiting(iid, policy) is True
    assert resumed[0].automatic and resumed[0].by == "copilot (auto-apply)"
    assert any(e["kind"] == EVENT for e in repo.events(iid))


def test_v1_memories_get_their_fix_and_pipeline_backfilled(tmp_path, monkeypatch):
    from pipeline_incident_copilot.storage import database

    monkeypatch.setattr(database, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "v1.db"
    repo = IncidentRepository(connect(path))
    run = FailedRun(
        connection_id="local-jenkins",
        platform="jenkins",
        job_id="export",
        job_name="export",
        run_id=3,
        result_state="F",
    )
    repo.open(run)
    repo.conn.execute(
        "INSERT INTO memories (pattern, incident_ids, text, seen_count, created_at, updated_at)"
        " VALUES ('p', '[\"INC-0001\"]', ?, 1, 'x', 'x')",
        ("Root cause: x\nFix that worked: rerun_job {'MIN_ROWS': '1000'} (approved by operator)",),
    )
    repo.conn.commit()
    for col in ("pipeline_key", "fix_json", "fix_successes"):
        repo.conn.execute(f"ALTER TABLE memories DROP COLUMN {col}")
    for col in ("auto_apply", "auto_apply_min_successes"):
        repo.conn.execute(f"ALTER TABLE connections DROP COLUMN {col}")
    repo.conn.execute("PRAGMA user_version = 1")
    repo.conn.commit()
    repo.conn.close()

    conn = connect(path)
    m = MemoryRepository(conn).all()[0]
    assert (m["fix"], m["pipeline_key"], m["fix_successes"]) == (FIX, "local-jenkins:export", 1)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
    assert isinstance(conn, sqlite3.Connection) and list((tmp_path / "backups").glob("v1-v1-*.db"))


def test_v2_diagnoses_get_their_calibrated_confidence_back_from_the_trail(tmp_path, monkeypatch):
    import json

    from pipeline_incident_copilot.storage import database

    monkeypatch.setattr(database, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "v2.db"
    repo = IncidentRepository(connect(path))
    inc = repo.open(FailedRun(job_id="j", job_name="j", run_id=1, result_state="F"))
    graph = {"trail": ["root cause (model 0.95, evidence-calibrated 0.83): MIN_ROWS 1000 -> 5000"]}
    skill = {"source": "claude_skill", "root_cause": {"confidence": 0.88}, "trail": []}
    for d in (graph, skill, {"trail": ["escalated before root cause"]}):
        repo.save_diagnosis(inc.incident_id, "t", d)
    repo.conn.execute("PRAGMA user_version = 2")
    repo.conn.commit()
    repo.conn.close()

    conn = connect(path)
    rows = [json.loads(r[0]) for r in conn.execute("SELECT diagnosis_json FROM diagnoses ORDER BY id")]
    assert rows[0]["confidence"] == {"model": 0.95, "calibrated": 0.83, "signals": None}
    assert rows[1]["confidence"]["calibrated"] == 0.88  # the skill's own calibrated number
    assert "confidence" not in rows[2]  # nothing to recover: left as it was
