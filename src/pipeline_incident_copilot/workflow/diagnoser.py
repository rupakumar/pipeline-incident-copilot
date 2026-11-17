"""IncidentDiagnoser: run the agent graph for one incident, save the result, tell people."""

from uuid import uuid4

from langfuse import propagate_attributes
from langgraph.types import Command

from pipeline_incident_copilot.llm import model_router
from pipeline_incident_copilot.notifications.base import EventKind, IncidentEvent, Notifier
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.rag.vector_store.qdrant_store import release_qdrant
from pipeline_incident_copilot.schemas.agent_outputs import Resolution, RootCause
from pipeline_incident_copilot.schemas.incident import IncidentStatus
from pipeline_incident_copilot.schemas.remediation import ApprovalDecision, RemediationResult
from pipeline_incident_copilot.schemas.triage_state import TriageState
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository

AGENT_FIELDS = (
    "classification",
    "log_analysis",
    "knowledge",
    "root_cause",
    "resolution",
    "auto_approval",
    "approval",
    "remediation",
)


def diagnosis_summary(state: TriageState) -> dict:
    out = {f: state[f].model_dump(mode="json") for f in AGENT_FIELDS if f in state}
    out["extra_checks"] = [r.model_dump(mode="json") for r in state.get("extra_checks", [])]
    if state.get("confidence"):  # {model, calibrated, signals}: the desk shows why a score is high or low
        out["confidence"] = state["confidence"]
    if state.get("auto_approval"):
        out["auto_approval"] = state["auto_approval"].model_dump(mode="json")
    out["trail"] = state.get("trail", [])
    return out


def note_for_people(state: TriageState) -> str:
    rc, res = state.get("root_cause"), state.get("resolution")
    lines = []
    if rc:
        lines.append(f"Root cause (confidence {rc.confidence:.2f}): {rc.cause}")
    if res:
        lines.append(f"Proposed fix: {res.summary}")
        lines += [f"  {i}. {step}" for i, step in enumerate(res.steps, 1)]
        auto = state.get("auto_approval")
        if res.action and auto is not None and auto.approved:
            lines.append(f"Automated action (no approval needed, {auto.known_fix}): rerun_job {res.action.parameters}")
        elif res.action:
            lines.append(f"Automated action (needs approval): rerun_job {res.action.parameters}")
            if auto is not None and auto.memory_id:
                lines.append(f"Matches a {auto.known_fix}; not applied automatically: {auto.reason}.")
            iid = state["incident"].incident_id
            lines.append(f"Approve: copilot approve {iid}    Reject: copilot reject {iid} --note '…'")
    elif rc:
        lines.append("Escalated: a person needs to decide. Evidence and history are in the incident.")
    return "\n".join(lines)


def note_from_saved(incident_id: str, d: dict) -> str:
    """The same note as note_for_people, rebuilt from a saved diagnosis (for re-sending)."""
    rc, res = d.get("root_cause"), d.get("resolution")
    lines = []
    if rc:
        lines.append(f"Root cause (confidence {rc['confidence']:.2f}): {rc['cause']}")
    if res:
        lines.append(f"Proposed fix: {res['summary']}")
        lines += [f"  {i}. {step}" for i, step in enumerate(res["steps"], 1)]
        if res.get("action"):
            lines.append(f"Automated action (needs approval): rerun_job {res['action']['parameters']}")
            lines.append(f"Approve: copilot approve {incident_id}    Reject: copilot reject {incident_id} --note '…'")
    return "\n".join(lines)


class AlreadyDecided(ValueError):
    """Someone else decided this approval first (two approvers, or a person and the auto-apply re-check)."""


class IncidentDiagnoser:
    def __init__(self, repo: IncidentRepository, graph, notifier: Notifier, unload_after: bool = True):
        self.repo = repo
        self.graph = graph
        self.notifier = notifier
        self.unload_after = unload_after  # False during a batch (eval): unload once at the end instead
        self.last_trace_id: str | None = None  # Langfuse trace of the last diagnosis (evals attach scores)

    def diagnose(self, incident_id: str) -> TriageState:
        incident, evidence = self.repo.get(incident_id), self.repo.get_evidence(incident_id)
        if incident is None or evidence is None:
            raise KeyError(f"{incident_id}: no incident or no evidence yet")
        thread_id = f"{incident_id}:{uuid4().hex[:8]}"  # a fresh checkpoint thread per attempt (failed ones too)
        start_tokens = len(model_router.token_usage)
        langfuse = get_langfuse()
        with langfuse.start_as_current_observation(as_type="chain", name="diagnose-incident") as root:
            with propagate_attributes(
                session_id=incident_id,
                trace_name="diagnose-incident",
                tags=["diagnosis", incident.run.job_name],
                metadata={"run_id": str(incident.run.run_id), "thread_id": thread_id},
            ):
                root.update(
                    input={"incident": incident_id, "failed_task": evidence.failed_task, "error": evidence.error}
                )
                try:
                    state = self.graph.invoke(
                        {"incident": incident, "evidence": evidence},
                        config={"configurable": {"thread_id": thread_id}},
                    )
                finally:
                    self._release()
                root.update(output=note_for_people(state))
            self.last_trace_id = root.trace_id
        self._finish(incident_id, thread_id, state, evidence, start_tokens)
        return state

    def diagnose_or_escalate(self, incident_id: str) -> TriageState | None:
        """For unattended runs (the watcher): a diagnosis that crashes escalates its incident with the error
        instead of stopping the loop, so one bad incident never blocks the next ones."""
        try:
            return self.diagnose(incident_id)
        except Exception as e:  # noqa: BLE001 — any failure: hand it to a person, keep watching
            why = f"diagnosis failed: {e.__class__.__name__}: {str(e)[:300]}"
            self.repo.set_status(incident_id, IncidentStatus.ESCALATED, why)
            delivered = self.notifier.send(
                IncidentEvent(
                    kind=EventKind.ESCALATED,
                    incident=self.repo.get(incident_id),
                    evidence=self.repo.get_evidence(incident_id),
                    note=f"The copilot could not finish the diagnosis, so a person needs to look.\n{why}",
                )
            )
            self.repo.add_event(incident_id, "notified" if delivered else "notify_failed", EventKind.ESCALATED)
            return None

    def resume(self, incident_id: str, decision: ApprovalDecision) -> TriageState:
        """Continue a paused incident with a person's decision (same checkpoint thread)."""
        thread_id = self.repo.latest_thread(incident_id)
        incident = self.repo.get(incident_id)
        if thread_id is None or incident is None or incident.status is not IncidentStatus.AWAITING_APPROVAL:
            raise ValueError(f"{incident_id} is not waiting for approval")
        if holder := self.repo.claim_decision(incident_id, decision.by, decision.decision):
            raise AlreadyDecided(f"{incident_id} was already decided: {holder}")
        start_tokens = len(model_router.token_usage)
        with get_langfuse().start_as_current_observation(as_type="chain", name="resume-incident") as root:
            with propagate_attributes(
                session_id=incident_id, trace_name="resume-incident", tags=["approval", decision.decision]
            ):
                root.update(input=decision.model_dump(mode="json"))
                try:
                    state = self.graph.invoke(
                        Command(resume=decision.model_dump(mode="json")),
                        config={"configurable": {"thread_id": thread_id}},
                    )
                finally:
                    self._release()
                root.update(output=self.repo.get(incident_id).status.value)
        self._finish(incident_id, thread_id, state, self.repo.get_evidence(incident_id), start_tokens)
        return state

    def auto_apply_waiting(self, incident_id: str, policy) -> bool:
        """Ask the auto-apply policy again for a ticket waiting for approval (a rule may pass now, e.g. the cooldown
        ended or the connection's settings changed). If it approves, continue the paused graph as an automatic
        approval: the same guardrail, rerun, verification and learning as any approved fix. True if it ran."""
        if self.repo.get(incident_id).status is not IncidentStatus.AWAITING_APPROVAL:
            return False
        state = self.saved_state(incident_id)
        if not state.get("resolution") or not state["resolution"].action:
            return False
        auto = policy.decide(state)
        if not auto.approved:
            return False
        decision = ApprovalDecision(decision="approve", by="copilot (auto-apply)", note=auto.reason, automatic=True)
        if self.repo.claim_decision(incident_id, decision.by, decision.decision):
            return False  # a person decided first
        self.repo.add_event(incident_id, "auto_approved", f"{auto.reason} (re-checked while waiting for approval)")
        self.resume(incident_id, decision)
        return True

    def _release(self) -> None:
        """After each graph run (also a failed one): free the local model and the knowledge-base lock, so the
        watcher, the approval console and the CLI can take turns. Batches (evals) keep both until the end."""
        if self.unload_after:
            model_router.unload_all()
            release_qdrant()

    def saved_state(self, incident_id: str) -> TriageState:
        """The incident's latest graph state, from its checkpoint thread."""
        thread_id = self.repo.latest_thread(incident_id)
        if thread_id is None:
            raise KeyError(f"{incident_id} has no diagnosis yet")
        return self.graph.get_state({"configurable": {"thread_id": thread_id}}).values

    def current_event(self, incident_id: str) -> IncidentEvent:
        """The notification event for the incident's current status, rebuilt from what is saved."""
        incident = self.repo.get(incident_id)
        if incident is None:
            raise KeyError(incident_id)
        kind = {
            IncidentStatus.AWAITING_APPROVAL: EventKind.DIAGNOSIS_READY,
            IncidentStatus.RESOLVED: EventKind.RESOLVED,
            IncidentStatus.ESCALATED: EventKind.ESCALATED,
        }.get(incident.status, EventKind.INCIDENT_OPENED)
        saved = self.repo.latest_diagnosis(incident_id) or {}
        note = note_from_saved(incident_id, saved)
        if kind is EventKind.RESOLVED:  # keep the "auto-fixed" headline when the email is sent again
            auto = [e for e in self.repo.events(incident_id) if e["kind"] == "auto_approved"]
            if auto:
                known = auto[-1]["detail"].removeprefix("approved automatically: ")
                note = f"Auto-fixed without approval: {known}.\n{note}"
        return IncidentEvent(
            kind=kind,
            incident=incident,
            evidence=self.repo.get_evidence(incident_id),
            note=note,
            root_cause=RootCause.model_validate(saved["root_cause"]) if saved.get("root_cause") else None,
            resolution=Resolution.model_validate(saved["resolution"]) if saved.get("resolution") else None,
            remediation=RemediationResult.model_validate(saved["remediation"]) if saved.get("remediation") else None,
            confidence=saved.get("confidence"),
            auto_approval=saved.get("auto_approval"),
        )

    def renotify(self, incident_id: str) -> bool:
        """Send the notification for the incident's current status again."""
        event = self.current_event(incident_id)
        delivered = self.notifier.send(event)
        self.repo.add_event(incident_id, "notified" if delivered else "notify_failed", f"{event.kind} (re-sent)")
        return delivered

    def _finish(self, incident_id: str, thread_id: str, state: TriageState, evidence, start_tokens: int) -> None:
        summary = diagnosis_summary(state)
        summary["model_tokens"] = model_router.token_usage[start_tokens:]
        summary["paused_for_approval"] = bool(state.get("__interrupt__"))
        self.repo.save_diagnosis(incident_id, thread_id, summary)
        current = self.repo.get(incident_id)
        kind = {
            IncidentStatus.AWAITING_APPROVAL: EventKind.DIAGNOSIS_READY,
            IncidentStatus.RESOLVED: EventKind.RESOLVED,
        }.get(current.status, EventKind.ESCALATED)
        if current.status is IncidentStatus.REJECTED:
            return  # the person who rejected it already knows
        note = note_for_people(state)
        if current.status is IncidentStatus.RESOLVED:
            r = state["remediation"]
            note = f"Fixed: rerun {r.rerun_run_id} with {r.parameters} succeeded.\n{r.run_page_url}\n\n{note}"
            auto, approval = state.get("auto_approval"), state.get("approval")
            if auto is not None and auto.approved:
                note = f"Auto-fixed without approval: {auto.known_fix}.\n{note}"
            elif approval is not None and approval.automatic:  # approved automatically after waiting
                note = f"Auto-fixed without approval: {approval.note.removeprefix('approved automatically: ')}.\n{note}"
        delivered = self.notifier.send(
            IncidentEvent(
                kind=kind,
                incident=current,
                evidence=evidence,
                note=note,
                root_cause=state.get("root_cause"),
                resolution=state.get("resolution"),
                remediation=state.get("remediation"),
                confidence=state.get("confidence"),
                auto_approval=summary.get("auto_approval"),
            )
        )
        self.repo.add_event(incident_id, "notified" if delivered else "notify_failed", kind)
