"""Evidence gathered for an incident. Every item is citable: it says where it came from."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Result of one read-only investigation tool call."""

    tool: str
    args: dict[str, Any]
    ok: bool  # the tool ran (not: the check passed)
    summary: str  # one line a human or an agent can quote
    data: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunSnapshot(BaseModel):
    """One row of the pipeline_runs audit table."""

    job_run_id: str
    run_date: str | None
    source: str | None
    status: str
    failed_task: str | None
    error_summary: str | None
    bronze_rows: int | None
    silver_rows: int | None
    gold_rows: int | None
    input_path: str | None
    code_version: str | None
    parameters: dict[str, str]
    started_at: datetime | None
    duration_seconds: float | None


class RunComparison(BaseModel):
    """The failed run next to the last successful run of the same source."""

    failed: RunSnapshot | None
    last_success: RunSnapshot | None
    changed_parameters: dict[str, tuple[str, str]] = Field(default_factory=dict)  # name -> (last good, failed)
    code_changed: bool = False
    audit_row_found: bool = True  # False: the failed run's side comes from the Jobs API


class CodeChange(BaseModel):
    """A file that changed in git between the last successful run and the failed one."""

    path: str
    status: str  # A / M / D / R
    diff: str  # unified diff, truncated


class EvidenceBundle(BaseModel):
    incident_id: str
    failed_task: str | None
    error: str | None
    error_trace_chars: int
    distilled_log: str
    parameters: dict[str, str]
    comparison: RunComparison | None = None
    code_changes: list[CodeChange] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    problems: list[str] = Field(default_factory=list)  # evidence that could not be collected, and why
