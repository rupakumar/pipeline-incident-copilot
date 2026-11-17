"""An incident: one failed pipeline run, from detection to resolution."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from pipeline_incident_copilot.schemas.connection import DEFAULT_CONNECTION_ID, RunId


class IncidentStatus(StrEnum):
    OPEN = "open"  # detected, evidence being collected
    INVESTIGATING = "investigating"  # agents working on it
    AWAITING_APPROVAL = "awaiting_approval"  # fix proposed, waiting for a human
    REMEDIATING = "remediating"  # approved fix running; waiting for the rerun to finish
    RESOLVED = "resolved"  # fix applied and verified
    ESCALATED = "escalated"  # handed to a human (low confidence or fix failed)
    REJECTED = "rejected"  # human rejected the proposed fix; nothing changed
    CLOSED = "closed"  # an escalated ticket a person handled outside the copilot and closed


class FailedRun(BaseModel):
    """What the watcher knows about a failed job run, before any evidence is collected."""

    # which saved connection found it; incidents from before connectors existed belong to the Databricks one
    connection_id: str = DEFAULT_CONNECTION_ID
    platform: str = "databricks"
    job_id: RunId  # Databricks job id, Jenkins job name, Glue job name, ...
    job_name: str
    run_id: RunId  # Databricks run id, Jenkins build number, Glue job-run id, ...
    run_page_url: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    result_state: str
    failed_task: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)

    @property
    def run_key(self) -> str:
        """Unique across platforms: run numbers repeat between Jenkins jobs and between connections."""
        return f"{self.connection_id}:{self.job_id}:{self.run_id}"


class Incident(BaseModel):
    incident_id: str  # INC-0001
    run: FailedRun
    status: IncidentStatus = IncidentStatus.OPEN
    opened_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def source(self) -> str | None:
        return self.run.parameters.get("source")

    @property
    def run_date(self) -> str | None:
        return self.run.parameters.get("run_date")
