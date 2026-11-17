"""Graph wiring, routing and guardrails, with fake agents (no models, no Databricks)."""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from pipeline_incident_copilot.guardrails.action.safe_actions import CAUSES, check
from pipeline_incident_copilot.rag.retrieval.routes.error_code_route import error_codes_in
from pipeline_incident_copilot.remediation.action_executor import rerun_parameters
from pipeline_incident_copilot.schemas.agent_outputs import (
    IncidentClassification,
    KnowledgeContext,
    LogAnalysis,
    ProposedAction,
    Resolution,
    RootCause,
)
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle
from pipeline_incident_copilot.schemas.incident import FailedRun, IncidentStatus
from pipeline_incident_copilot.schemas.remediation import RemediationResult
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository
from pipeline_incident_copilot.workflow.graph import Agents, Remediation, build_graph


def rerun(**params):
    return ProposedAction(name="rerun_job", parameters=params, reason="test")


def test_a_reset_must_touch_a_parameter_that_can_cause_the_failure_class():
    write_mode = rerun(silver_write_mode="append")  # differs from the last good run, but cannot null timestamps
    assert "cannot cause a data_quality failure" in check(write_mode, failure_class="data_quality", causes=CAUSES)
    assert check(write_mode, failure_class="schema_change", causes=CAUSES) is None
    assert check(rerun(timestamp_format=""), failure_class="data_quality", causes=CAUSES) is None
    assert check(rerun(debug_sample_rows="0"), failure_class="resource_exhaustion", causes=CAUSES) is None
    assert check(write_mode, failure_class="data_quality") is None  # no cause map (other jobs): not applied


def test_safe_actions_allow_resets_and_own_tables_only():
    assert check(None) is None
    assert check(rerun(timestamp_format="", debug_sample_rows="0", run_date="2017-06-12")) is None
    assert check(rerun(gold_table="workspace.pipeline_demo.spark_logs_gold_hourly")) is None
    assert "not allowed" in check(rerun(gold_table="samples.nyctaxi.x"))
    assert "not allowed" in check(rerun(debug_sample_rows="500000000"))
    assert "not allowed" in check(rerun(input_path="/Volumes/workspace/pipeline_demo/raw_logs/../secrets"))
    assert "cannot be changed" in check(rerun(owner="me"))
    failed = {"source": "spark", "run_date": "2017-06-12", "timestamp_format": "MM/dd/yy"}
    assert "changes nothing" in check(rerun(source="spark", run_date="2017-06-12"), failed)
    assert check(rerun(timestamp_format=""), failed) is None
    path = "/Volumes/workspace/pipeline_demo/raw_logs/spark/2017-06-12/Spark_2k.log"
    effective = {**failed, "input_path": path}  # the default path the failed run actually read
    assert "changes nothing" in check(rerun(run_date="2017-06-12", input_path=path), effective)
    good = {"source": "hdfs", "run_date": "2008-11-10", "timestamp_format": ""}
    bad = {"source": "hdfs", "run_date": "2008-11-12", "timestamp_format": ""}
    assert "new value" in check(rerun(timestamp_format="yyyy-MM-dd HH:mm:ss"), bad, good)  # invented
    assert check(rerun(timestamp_format=""), {**bad, "timestamp_format": "x"}, good) is None  # reset
    assert check(rerun(run_date="2008-11-10"), bad, good) is None  # back to the last working value


def test_error_codes_are_read_from_brackets():
    err = "[DELTA_METADATA_MISMATCH] A metadata mismatch ... [PATH_NOT_FOUND.X] ... [DELTA_METADATA_MISMATCH]"
    assert error_codes_in(err) == ["DELTA_METADATA_MISMATCH", "PATH_NOT_FOUND"]
    assert error_codes_in("AssertionError: DATA_QUALITY_FAILED: 100%") == []


class Fake:
    def __init__(self, name, update):
        self.name, self.update = name, update

    def run(self, state):
        return self.update


class FakeKnowledge:
    """Corrective-RAG steps: first pass finds 1 relevant doc, so it must retry once."""

    name = "knowledge"

    def retrieve(self, s):
        return {"docs": s.get("docs", []) + [f"doc{len(s['queries'])}"]}

    def grade(self, s):
        return {"relevant": s["docs"]}

    def needs_retry(self, s):
        return len(s["relevant"]) < 2 and s.get("retries", 0) < 1

    def rewrite(self, s):
        return {"queries": [*s["queries"], "hyde"], "retries": s.get("retries", 0) + 1}

    def cite(self, s):
        return {"knowledge": KnowledgeContext(citations=[], queries=s["queries"], retries=s["retries"])}


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, incident, action, effective, last_good=None):
        self.calls.append(action.parameters)
        return RemediationResult(rerun_run_id=100 + len(self.calls), parameters=action.parameters, started=True)


class FakeVerifier:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)  # True = rerun succeeds

    def verify(self, result):
        ok = self.outcomes.pop(0)
        return result.model_copy(
            update={
                "succeeded": ok,
                "result_state": "SUCCESS" if ok else "FAILED",
                "failed_task": None if ok else "gold_aggregate",
                "error": None if ok else "[UNAUTHORIZED_ACCESS] still failing",
            }
        )


class FakeMemory:
    def __init__(self):
        self.recorded, self.outcomes = [], []

    def record(self, state):
        self.recorded.append(state["incident"].incident_id)
        return {"id": 1, "seen_count": 1}

    def record_outcome(self, cited, worked):
        self.outcomes.append(worked)
        return []


CONFIG = {"configurable": {"thread_id": "t1"}}


def _graph(tmp_path, confidence, action=None, outcomes=(True,), memory=None, auto_policy=None):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    run = FailedRun(job_id=1, job_name="loghub_etl", run_id=5, result_state="FAILED", failed_task="bronze_ingest")
    inc = repo.open(run)
    ev = EvidenceBundle(
        incident_id=inc.incident_id,
        failed_task="bronze_ingest",
        error="[PATH_NOT_FOUND] x",
        error_trace_chars=0,
        distilled_log="x",
        parameters={},
    )
    rc = RootCause(
        cause="file missing",
        failure_class="missing_input",
        confidence=confidence,
        evidence_refs=["check_path_exists"],
        needs_human=False,
    )
    res = Resolution(
        summary="wait for delivery", steps=["ask upstream"], action=action, risk="low", verify=["rerun ok"]
    )
    agents = Agents(
        incident=Fake(
            "incident",
            {"classification": IncidentClassification(failure_class="missing_input", severity="sev2", rationale="r")},
        ),
        log_analysis=Fake(
            "log_analysis",
            {"log_analysis": LogAnalysis(failing_operation="read", search_query="q", error_code="PATH_NOT_FOUND")},
        ),
        knowledge=FakeKnowledge(),
        root_cause=Fake("root_cause", {"root_cause": rc, "trail": ["rc"]}),
        resolution=Fake("resolution", {"resolution": res, "trail": ["res"]}),
    )
    executor = FakeExecutor()
    graph = build_graph(
        agents, repo, Remediation(executor, FakeVerifier(outcomes)), InMemorySaver(), memory, auto_policy
    )
    state = graph.invoke({"incident": inc, "evidence": ev}, CONFIG)
    return repo, inc.incident_id, state, graph, executor


def test_confident_diagnosis_without_safe_action_goes_to_a_person(tmp_path):
    repo, iid, state, *_ = _graph(tmp_path, confidence=0.95)
    assert state["knowledge"].retries == 1 and state["knowledge"].queries == ["q", "hyde"]
    assert state["resolution"].summary == "wait for delivery"
    assert repo.get(iid).status is IncidentStatus.ESCALATED  # no action to approve


def test_safe_action_pauses_for_approval_then_runs_and_resolves(tmp_path):
    repo, iid, state, graph, executor = _graph(tmp_path, confidence=0.95, action=rerun(timestamp_format=""))
    assert state["__interrupt__"] and repo.get(iid).status is IncidentStatus.AWAITING_APPROVAL
    assert executor.calls == []  # nothing runs before a person decides
    state = graph.invoke(Command(resume={"decision": "approve", "by": "operator"}), CONFIG)
    assert executor.calls == [{"timestamp_format": ""}]
    assert state["remediation"].succeeded and repo.get(iid).status is IncidentStatus.RESOLVED


class FakePolicy:
    def __init__(self, approved):
        from pipeline_incident_copilot.schemas.remediation import AutoApproval

        self.answer = AutoApproval(
            approved=approved,
            reason="approved automatically: known fix" if approved else "the known fix worked 1 of the 2 times needed",
            memory_id=5,
            from_incidents=["INC-0029"],
            successes=2 if approved else 1,
        )
        self.asked = 0

    def decide(self, state):
        self.asked += 1
        return self.answer


def test_known_fix_approved_by_the_policy_runs_without_pausing(tmp_path):
    policy = FakePolicy(approved=True)
    repo, iid, state, _, executor = _graph(tmp_path, 0.95, action=rerun(timestamp_format=""), auto_policy=policy)
    assert "__interrupt__" not in state and executor.calls == [{"timestamp_format": ""}]
    assert state["approval"].automatic and state["approval"].by == "copilot (auto-apply)"
    assert repo.get(iid).status is IncidentStatus.RESOLVED
    kinds = [e["kind"] for e in repo.events(iid)]
    assert "auto_approved" in kinds and kinds.index("auto_approved") < kinds.index("status:remediating")
    assert "status:awaiting_approval" not in kinds  # never in the approval queue, so nobody can approve it twice


def test_waiting_status_is_set_once_and_kept_through_the_resume(tmp_path):
    policy = FakePolicy(approved=False)
    repo, iid, state, graph, _ = _graph(tmp_path, 0.95, action=rerun(timestamp_format=""), auto_policy=policy)
    assert repo.get(iid).status is IncidentStatus.AWAITING_APPROVAL
    graph.invoke(Command(resume={"decision": "approve", "by": "operator"}), CONFIG)
    kinds = [e["kind"] for e in repo.events(iid)]
    assert kinds.count("status:awaiting_approval") == 1 and repo.get(iid).status is IncidentStatus.RESOLVED


def test_policy_refusal_waits_for_a_person_and_is_not_asked_again_on_resume(tmp_path):
    policy = FakePolicy(approved=False)
    repo, iid, state, graph, executor = _graph(tmp_path, 0.95, action=rerun(timestamp_format=""), auto_policy=policy)
    assert state["__interrupt__"] and executor.calls == []
    assert any("worked 1 of the 2" in (e["detail"] or "") for e in repo.events(iid) if e["kind"] == "auto_approval:no")
    graph.invoke(Command(resume={"decision": "reject", "by": "operator"}), CONFIG)
    assert policy.asked == 1 and executor.calls == [] and repo.get(iid).status is IncidentStatus.REJECTED


def test_rejected_fix_changes_nothing(tmp_path):
    repo, iid, _, graph, executor = _graph(tmp_path, confidence=0.95, action=rerun(timestamp_format=""))
    graph.invoke(Command(resume={"decision": "reject", "by": "operator", "note": "not now"}), CONFIG)
    assert executor.calls == [] and repo.get(iid).status is IncidentStatus.REJECTED


def test_failed_rerun_is_rediagnosed_reapproved_then_escalated(tmp_path):
    repo, iid, _, graph, executor = _graph(
        tmp_path, confidence=0.95, action=rerun(timestamp_format=""), outcomes=(False, False)
    )
    state = graph.invoke(Command(resume={"decision": "approve", "by": "operator"}), CONFIG)
    assert state["__interrupt__"]  # re-diagnosed with the rerun's error, waiting for approval again
    assert any(r.tool == "approved_rerun" for r in state["evidence"].tool_results)
    state = graph.invoke(Command(resume={"decision": "approve", "by": "operator"}), CONFIG)
    assert len(executor.calls) == 2 and repo.get(iid).status is IncidentStatus.ESCALATED


def test_rerun_parameters_keep_failed_settings_and_apply_the_fix():
    defaults = {
        "source": "spark",
        "run_date": "{{job.trigger.time.iso_date}}",
        "timestamp_format": "",
        "gold_table": "",
    }
    failed = {"source": "hdfs", "run_date": "2008-11-10", "timestamp_format": "yyyyMMdd HHmmss", "gold_table": ""}
    assert rerun_parameters(failed, defaults, {"timestamp_format": ""}) == {"source": "hdfs", "run_date": "2008-11-10"}


def test_unsure_diagnosis_is_escalated_without_a_fix(tmp_path):
    repo, iid, state, *_ = _graph(tmp_path, confidence=0.4)
    assert "resolution" not in state
    assert repo.get(iid).status is IncidentStatus.ESCALATED


def test_verified_fix_is_learned_and_failed_fixes_count_against_cited_memories(tmp_path):
    memory = FakeMemory()
    repo, iid, _, graph, _ = _graph(tmp_path, confidence=0.95, action=rerun(timestamp_format=""), memory=memory)
    graph.invoke(Command(resume={"decision": "approve", "by": "operator"}), CONFIG)
    assert memory.recorded == [iid] and memory.outcomes == [True]

    memory = FakeMemory()
    repo, iid, _, graph, _ = _graph(
        tmp_path / "b", confidence=0.95, action=rerun(timestamp_format=""), outcomes=(False, False), memory=memory
    )
    graph.invoke(Command(resume={"decision": "approve", "by": "operator"}), CONFIG)
    graph.invoke(Command(resume={"decision": "approve", "by": "operator"}), CONFIG)
    assert memory.recorded == [] and memory.outcomes == [False]
