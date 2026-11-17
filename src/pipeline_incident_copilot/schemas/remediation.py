"""The human decision on a proposed fix, and what happened when it ran."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from pipeline_incident_copilot.schemas.connection import DEFAULT_CONNECTION_ID, RunId


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    by: str
    note: str = ""
    automatic: bool = False  # approved by the auto-apply policy (a known, verified fix), not a person
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RemediationResult(BaseModel):
    connection_id: str = DEFAULT_CONNECTION_ID  # where the rerun runs (the verifier waits on the same connector)
    job_id: RunId | None = None
    rerun_run_id: RunId | None = None
    run_page_url: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)  # job parameters the rerun used
    started: bool = False
    succeeded: bool = False
    result_state: str | None = None
    failed_task: str | None = None
    error: str | None = None
    refused: str | None = None  # why the executor refused to run the action


class AutoApproval(BaseModel):
    """The auto-apply policy's answer for one proposed fix (learning/auto_approval.py)."""

    approved: bool
    reason: str  # one line for the history, the email and the dashboards
    memory_id: int | None = None  # the known fix this ticket matches, if any
    from_incidents: list[str] = Field(default_factory=list)  # incidents where that fix worked
    successes: int = 0
    failures: int = 0

    @property
    def known_fix(self) -> str:
        if self.memory_id is None:
            return ""
        return (
            f"known fix from {', '.join(self.from_incidents[-3:])} (memory {self.memory_id}, worked {self.successes}×)"
        )
