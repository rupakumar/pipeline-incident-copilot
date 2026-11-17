"""Graph nodes: wrap each agent, keep the incident's status and history in step with the graph."""

from collections.abc import Callable

from langgraph.types import interrupt

from pipeline_incident_copilot.agents.base import Agent
from pipeline_incident_copilot.guardrails.resource.memory_guard import free_memory_pct
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.rag.model_memory import release_retrieval_models
from pipeline_incident_copilot.rag.retrieval.query.log_distiller import distill
from pipeline_incident_copilot.schemas.evidence import ToolResult
from pipeline_incident_copilot.schemas.incident import IncidentStatus
from pipeline_incident_copilot.schemas.remediation import ApprovalDecision
from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository

KEEP_MODELS_ABOVE_PCT = 35  # release the retrieval models only when memory is really low (CPU reranker)


def agent_node(agent: Agent, repo: IncidentRepository) -> Callable[[TriageState], dict]:
    def node(state: TriageState) -> dict:
        with get_langfuse().start_as_current_observation(as_type="span", name=f"node-{agent.name}"):
            update = agent.run(state)
        for line in update.get("trail", []):
            repo.add_event(state["incident"].incident_id, agent.name, line)
        return update

    return node


def knowledge_node(corrective_rag, repo: IncidentRepository) -> Callable[[TriageState], dict]:
    def node(state: TriageState) -> dict:
        la = state["log_analysis"]
        with get_langfuse().start_as_current_observation(as_type="span", name="node-knowledge"):
            out = corrective_rag.invoke({"log_analysis": la, "queries": [la.search_query], "retries": 0})
        if free_memory_pct() < KEEP_MODELS_ABOVE_PCT:  # the LLM steps come next: make room if memory is tight
            release_retrieval_models()
        k = out["knowledge"]
        line = (
            f"{len(k.citations)} documents ({'weak' if k.weak else 'good'} match, {k.retries} retries): "
            + ", ".join(c.parent_id for c in k.citations)
        )
        repo.add_event(state["incident"].incident_id, "knowledge", line)
        return {"knowledge": k, "trail": [line]}

    return node


def start_node(repo: IncidentRepository) -> Callable[[TriageState], dict]:
    def node(state: TriageState) -> dict:
        repo.set_status(state["incident"].incident_id, IncidentStatus.INVESTIGATING, "agents started")
        return {"trail": ["agents started"]}

    return node


def escalate_node(repo: IncidentRepository) -> Callable[[TriageState], dict]:
    def node(state: TriageState) -> dict:
        rc = state["root_cause"]
        conf = state.get("confidence", {}).get("calibrated", rc.confidence)
        why = (
            "needs a human decision" if rc.needs_human else f"evidence-calibrated confidence {conf:.2f} below threshold"
        )
        repo.set_status(state["incident"].incident_id, IncidentStatus.ESCALATED, why)
        return {"trail": [f"escalated: {why}"]}

    return node


def proposal_node(repo: IncidentRepository) -> Callable[[TriageState], dict]:
    """End of diagnosis: a safe fix goes to approval; otherwise the incident is escalated with steps."""

    def node(state: TriageState) -> dict:
        res = state["resolution"]
        if res.action is None:  # nothing to approve: a person has to act first (e.g. upstream delivery)
            repo.set_status(state["incident"].incident_id, IncidentStatus.ESCALATED, f"needs a person: {res.summary}")
            return {"trail": ["no safe automated fix: escalated with steps for a person"]}
        # not "awaiting approval" yet: the auto-apply policy decides first, and a ticket that fixes itself must never
        # show up in the approval queue (a person could approve a fix that is already running)
        return {"trail": [f"fix proposed: rerun_job {res.action.parameters}"]}

    return node


def auto_approval_node(repo: IncidentRepository, policy) -> Callable[[TriageState], dict]:
    """Ask the auto-apply policy first (learning/auto_approval.py). Its own node, so a resume after a person's
    decision never re-asks it: LangGraph resumes at the paused approval node."""

    def node(state: TriageState) -> dict:
        if policy is None:
            return {}
        iid = state["incident"].incident_id
        with get_langfuse().start_as_current_observation(as_type="guardrail", name="auto-approval-policy") as obs:
            auto = policy.decide(state)
            obs.update(output=auto.model_dump(mode="json"))
        if not auto.approved:
            note = f"needs a person: {auto.reason}" + (f" (matches {auto.known_fix})" if auto.memory_id else "")
            repo.add_event(iid, "auto_approval:no", note)
            return {"auto_approval": auto, "trail": [f"auto-apply: no, {auto.reason}"]}
        decision = ApprovalDecision(decision="approve", by="copilot (auto-apply)", note=auto.reason, automatic=True)
        repo.add_event(iid, "auto_approved", auto.reason)
        repo.add_event(iid, "approval:approve", f"by {decision.by}: {decision.note}")
        return {"auto_approval": auto, "approval": decision, "trail": [auto.reason]}

    return node


MAX_FIX_ATTEMPTS = 2  # the first approved rerun, plus one retry after a re-diagnosis


def approval_node(repo: IncidentRepository) -> Callable[[TriageState], dict]:
    """Pause the graph (saved by the checkpointer) until a person approves or rejects the fix."""

    def node(state: TriageState) -> dict:
        res = state["resolution"]
        iid = state["incident"].incident_id
        if repo.get(iid).status is not IncidentStatus.AWAITING_APPROVAL:  # once: a resume re-runs this node
            repo.set_status(iid, IncidentStatus.AWAITING_APPROVAL, res.summary)
        decision = ApprovalDecision.model_validate(
            interrupt(
                {
                    "incident_id": state["incident"].incident_id,
                    "summary": res.summary,
                    "action": res.action.model_dump() if res.action else None,
                }
            )
        )  # on resume, LangGraph re-runs this node from the top and interrupt() returns the decision
        iid = state["incident"].incident_id
        repo.add_event(iid, f"approval:{decision.decision}", f"by {decision.by}: {decision.note}".strip(": "))
        if decision.decision == "reject":
            repo.set_status(iid, IncidentStatus.REJECTED, decision.note or "fix rejected; nothing was changed")
        return {"approval": decision, "trail": [f"{decision.decision}d by {decision.by}"]}

    return node


def remediate_node(repo: IncidentRepository, executor) -> Callable[[TriageState], dict]:
    def node(state: TriageState) -> dict:
        from pipeline_incident_copilot.agents.resolution_agent import effective_parameters

        iid = state["incident"].incident_id
        with get_langfuse().start_as_current_observation(as_type="tool", name="rerun-job") as obs:
            c = state["evidence"].comparison
            last_good = c.last_success.parameters if c and c.last_success else None
            run = state["incident"].run
            with repo.rerun_pending(run.connection_id, run.job_name):  # its failure is ours, not a new incident
                result = executor.execute(
                    state["incident"], state["resolution"].action, effective_parameters(state), last_good
                )
                if not result.refused:
                    repo.set_status(
                        iid, IncidentStatus.REMEDIATING, f"rerun {result.rerun_run_id} started with {result.parameters}"
                    )
            obs.update(input=state["resolution"].action.parameters, output=result.model_dump(mode="json"))
        if result.refused:
            repo.add_event(iid, "remediation_refused", result.refused)
            return {"remediation": result, "trail": [f"rerun refused: {result.refused}"]}
        attempts = state.get("fix_attempts", 0) + 1
        return {"remediation": result, "fix_attempts": attempts, "trail": [f"rerun {result.rerun_run_id} started"]}

    return node


def verify_node(repo: IncidentRepository, verifier) -> Callable[[TriageState], dict]:
    def node(state: TriageState) -> dict:
        iid = state["incident"].incident_id
        with get_langfuse().start_as_current_observation(as_type="span", name="verify-rerun") as obs:
            result = verifier.verify(state["remediation"])
            obs.update(output=result.model_dump(mode="json"))
        if result.succeeded:
            repo.set_status(iid, IncidentStatus.RESOLVED, f"rerun {result.rerun_run_id} succeeded")
            return {"remediation": result, "trail": [f"verified: rerun {result.rerun_run_id} succeeded"]}
        first_line = (result.error or "").strip().splitlines()[0][:300] if result.error else "no error text"
        repo.add_event(
            iid, "verify_failed", f"rerun {result.rerun_run_id} failed in {result.failed_task}: {first_line}"
        )
        ev = state["evidence"]
        rerun_check = ToolResult(
            tool="approved_rerun",
            args=result.parameters,
            ok=True,
            summary=f"the approved rerun {result.rerun_run_id} with {result.parameters} FAILED in "
            f"{result.failed_task}: {first_line}",
        )
        new_evidence = ev.model_copy(
            update={
                "error": result.error or ev.error,
                "failed_task": result.failed_task or ev.failed_task,
                "distilled_log": distill(result.error or "", "") or ev.distilled_log,
                "tool_results": [*ev.tool_results, rerun_check],
            }
        )
        return {"remediation": result, "evidence": new_evidence, "trail": [f"rerun failed: {first_line}"]}

    return node


def _cited(state: TriageState) -> list[str]:
    k = state.get("knowledge")
    return [c.parent_id for c in k.citations] if k else []


def learn_node(repo: IncidentRepository, memory) -> Callable[[TriageState], dict]:
    """A verified fix becomes a memory; memories cited in this diagnosis get a 'worked' mark."""

    def node(state: TriageState) -> dict:
        iid = state["incident"].incident_id
        if memory is None:
            return {"trail": ["learning disabled"]}
        saved = memory.record(state)
        credited = memory.record_outcome(_cited(state), worked=True)
        line = f"learned: memory:{saved['id']} (seen {saved['seen_count']}×)" + (
            f"; past memories that helped: {credited}" if credited else ""
        )
        repo.add_event(iid, "learned", line)
        return {"trail": [line]}

    return node


def fix_failed_node(repo: IncidentRepository, memory=None) -> Callable[[TriageState], dict]:
    def node(state: TriageState) -> dict:
        r = state.get("remediation")
        why = r.refused if r and r.refused else f"{state.get('fix_attempts', 0)} approved rerun(s) did not fix it"
        repo.set_status(state["incident"].incident_id, IncidentStatus.ESCALATED, why)
        if memory is not None and not (r and r.refused):
            if blamed := memory.record_outcome(_cited(state), worked=False):
                repo.add_event(
                    state["incident"].incident_id, "memory_failed", f"memories whose fix did not work: {blamed}"
                )
        return {"trail": [f"escalated: {why}"]}

    return node
