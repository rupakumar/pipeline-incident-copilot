"""What every pipeline platform must offer the copilot, split by who uses it.

- FailureSource  (FailureWatcher):      test the credentials, list new failed runs, report pipeline health
- EvidenceSource (IncidentIntake):      the standard evidence for one failed run
- RerunTarget    (ActionExecutor, RunVerifier): job defaults, start an approved rerun, wait for its result

A connector implements all three (PipelineConnector); callers depend only on the part they need. Adding a
platform = one class + one entry in catalog.PLATFORMS; the watcher, graph and console do not change.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pipeline_incident_copilot.schemas.connection import ConnectionTest, PipelineHealth, RunId
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle
from pipeline_incident_copilot.schemas.incident import FailedRun, Incident
from pipeline_incident_copilot.schemas.remediation import RemediationResult


@dataclass
class PollResult:
    failed_runs: list[FailedRun] = field(default_factory=list)  # every failed run since `since` (deduped later)
    health: list[PipelineHealth] = field(default_factory=list)  # one row per watched pipeline


class FailureSource(Protocol):
    platform: str

    def test(self) -> ConnectionTest:
        """Reach the platform with the saved credentials; never raises."""
        ...

    def poll(self, since: datetime, lookback: int) -> PollResult:
        """Failed runs of the watched pipelines that started at or after `since`, plus their health."""
        ...

    def complete(self, run: FailedRun) -> FailedRun:
        """Details too costly to fetch for every run in a poll (e.g. the failed step); only for new failures."""
        ...


class EvidenceSource(Protocol):
    def collect_evidence(self, incident: Incident) -> EvidenceBundle:
        """Error + trace, parameters, comparison with the last success, changes; problems recorded, never raised."""
        ...


class RerunTarget(Protocol):
    def job_defaults(self, job_id: RunId) -> dict[str, str]: ...

    def start_rerun(self, incident: Incident, parameters: dict[str, str]) -> RemediationResult: ...

    def wait_for_rerun(self, result: RemediationResult) -> RemediationResult:
        """Block until the rerun ends; succeeded=True, or the failed step and error."""
        ...


class PipelineConnector(FailureSource, EvidenceSource, RerunTarget, Protocol):
    pass


def summarize_health(
    connection_id: str,
    pipeline: str,
    runs: Iterable[tuple[str, str, datetime | None, str | None, bool]],
    now: datetime | None = None,
) -> PipelineHealth:
    """Health from finished runs, newest first: (run id, state, started, url, failed)."""
    now = now or datetime.now(UTC)
    runs = list(runs)
    day = [r for r in runs if r[2] and r[2] >= now - timedelta(hours=24)]
    last = runs[0] if runs else None
    return PipelineHealth(
        connection_id=connection_id,
        pipeline=pipeline,
        checked_at=now,
        last_run_id=last[0] if last else None,
        last_state=last[1] if last else None,
        last_run_at=last[2] if last else None,
        last_run_url=last[3] if last else None,
        runs_24h=len(day),
        failures_24h=sum(1 for r in day if r[4]),
    )
