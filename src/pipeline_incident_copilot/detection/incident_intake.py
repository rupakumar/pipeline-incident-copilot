"""IncidentIntake: what happens to every new failed run, in order. Traced as one Langfuse trace per incident.

Evidence comes from the connector of the platform the run failed on (ConnectorHub).
"""

from langfuse import propagate_attributes

from pipeline_incident_copilot.connectors.hub import ConnectorHub
from pipeline_incident_copilot.notifications.base import EventKind, IncidentEvent, Notifier
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle
from pipeline_incident_copilot.schemas.incident import FailedRun, Incident
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository


class IncidentIntake:
    def __init__(self, repo: IncidentRepository, hub: ConnectorHub, notifier: Notifier):
        self.repo = repo
        self.hub = hub
        self.notifier = notifier

    def process(self, run: FailedRun) -> tuple[Incident, EvidenceBundle] | None:
        """None when the run already has an incident (another watcher or process opened it first)."""
        incident = self.repo.open_if_new(run)  # saved first: a crash below never loses the failure
        if incident is None:
            return None
        langfuse = get_langfuse()
        with langfuse.start_as_current_observation(as_type="chain", name="incident-intake") as root:
            with propagate_attributes(
                session_id=incident.incident_id,
                trace_name="incident-intake",
                tags=["incident-intake", run.platform, run.job_name],
                metadata={
                    "run_id": str(run.run_id),
                    "failed_task": str(run.failed_task),
                    "connection_id": run.connection_id,
                    "platform": run.platform,
                },
            ):
                root.update(input=run.model_dump(mode="json"))
                evidence = self.hub.for_incident(incident).collect_evidence(incident)
                self.repo.save_evidence(evidence)
                root.update(output={"tools": [r.summary for r in evidence.tool_results], "problems": evidence.problems})
        delivered = self.notifier.send(
            IncidentEvent(kind=EventKind.INCIDENT_OPENED, incident=incident, evidence=evidence)
        )
        self.repo.add_event(
            incident.incident_id, "notified" if delivered else "notify_failed", EventKind.INCIDENT_OPENED
        )
        return incident, evidence
