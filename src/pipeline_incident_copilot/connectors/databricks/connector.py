"""DatabricksConnector: Lakeflow Jobs runs of the watched jobs (Jobs API), evidence, rerun with parameters."""

from datetime import UTC, datetime, timedelta
from itertools import islice

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import Run

from pipeline_incident_copilot.connectors.base import PollResult, summarize_health
from pipeline_incident_copilot.connectors.databricks.evidence import EvidenceCollector
from pipeline_incident_copilot.integrations.databricks_job_runs import (
    failed_task,
    is_failed,
    run_result,
    task_failure,
    wait_for_run,
)
from pipeline_incident_copilot.schemas.connection import Connection, ConnectionTest
from pipeline_incident_copilot.schemas.evidence import EvidenceBundle
from pipeline_incident_copilot.schemas.incident import FailedRun, Incident
from pipeline_incident_copilot.schemas.remediation import RemediationResult
from pipeline_incident_copilot.tools.registry import ToolRegistry

PAGE_SIZE = 25  # Jobs API maximum is 26


def _ts(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None


def to_failed_run(job_id: int, job_name: str, run: Run, connection_id: str | None = None) -> FailedRun:
    task = failed_task(run)
    return FailedRun(
        **({"connection_id": connection_id} if connection_id else {}),
        platform="databricks",
        job_id=job_id,
        job_name=job_name,
        run_id=run.run_id,
        run_page_url=run.run_page_url,
        started_at=_ts(run.start_time),
        ended_at=_ts(run.end_time),
        result_state=run_result(run),
        failed_task=task.task_key if task else None,
        # unset parameters come back as None: the job default applied
        parameters={p.name: p.value if p.value is not None else (p.default or "") for p in run.job_parameters or []},
    )


class DatabricksConnector:
    platform = "databricks"

    def __init__(self, connection_id: str, w: WorkspaceClient, tools: ToolRegistry, pipelines: list[str]):
        self.connection_id = connection_id
        self.w = w
        self.tools = tools
        self.pipelines = pipelines
        self._job_ids: dict[str, int] = {}

    @classmethod
    def from_connection(cls, connection: Connection, secrets: dict[str, str]) -> "DatabricksConnector":
        from pipeline_incident_copilot.integrations.databricks_client import get_client
        from pipeline_incident_copilot.integrations.databricks_sql import SqlWarehouse
        from pipeline_incident_copilot.integrations.git_client import GitClient

        if connection.settings.get("host") and secrets.get("token"):  # entered in the console (Keychain)
            w = get_client(connection.settings["host"], secrets["token"])
        else:  # the original setup: DATABRICKS_HOST / DATABRICKS_TOKEN from .env
            w = get_client()
        return cls(
            connection.connection_id, w, ToolRegistry.default(w, SqlWarehouse(w), GitClient()), connection.pipelines
        )

    # --- FailureSource ---------------------------------------------------------------------------
    def test(self) -> ConnectionTest:
        try:
            me = self.w.current_user.me()
            names = [j.settings.name for j in islice(self.w.jobs.list(), 100) if j.settings and j.settings.name]
        except Exception as e:  # noqa: BLE001 — any failure is the answer to "does it connect?"
            return ConnectionTest(ok=False, message=f"cannot reach the workspace: {e.__class__.__name__}: {e}"[:300])
        missing = [p for p in self.pipelines if p not in names]
        found = len(self.pipelines) - len(missing)
        return ConnectionTest(
            ok=not missing,
            message=f"signed in as {me.user_name}; {found} of {len(self.pipelines)} job(s) found"
            + (f"; not found: {', '.join(missing)}" if missing else ""),
            available=names[:20],
            missing=missing,
        )

    def job_id(self, name: str) -> int:
        if name not in self._job_ids:
            job = next(iter(self.w.jobs.list(name=name)), None)
            if job is None:
                raise RuntimeError(f"job {name} not found")
            self._job_ids[name] = job.job_id
        return self._job_ids[name]

    def poll(self, since: datetime, lookback: int) -> PollResult:
        result = PollResult()
        for name in self.pipelines:
            job_id = self.job_id(name)
            runs = list(
                islice(
                    self.w.jobs.list_runs(job_id=job_id, completed_only=True, expand_tasks=True, limit=PAGE_SIZE),
                    lookback,
                )
            )
            result.health.append(
                summarize_health(
                    self.connection_id,
                    name,
                    ((str(r.run_id), run_result(r), _ts(r.start_time), r.run_page_url, is_failed(r)) for r in runs),
                )
            )
            start_ms = since.timestamp() * 1000
            result.failed_runs += [
                to_failed_run(job_id, name, r, self.connection_id)
                for r in runs
                if (r.start_time or 0) >= start_ms and is_failed(r)
            ]
        return result

    def complete(self, run: FailedRun) -> FailedRun:
        return run  # the Jobs API listing already names the failed task

    # --- EvidenceSource --------------------------------------------------------------------------
    def collect_evidence(self, incident: Incident) -> EvidenceBundle:
        return EvidenceCollector(self.w, self.tools).collect(incident)

    # --- RerunTarget -----------------------------------------------------------------------------
    def job_defaults(self, job_id) -> dict[str, str]:
        return {p.name: p.default or "" for p in self.w.jobs.get(int(job_id)).settings.parameters or []}

    def start_rerun(self, incident: Incident, parameters: dict[str, str]) -> RemediationResult:
        waiter = self.w.jobs.run_now(job_id=int(incident.run.job_id), job_parameters=parameters)
        run = self.w.jobs.get_run(waiter.run_id)
        return RemediationResult(
            connection_id=self.connection_id,
            job_id=incident.run.job_id,
            rerun_run_id=waiter.run_id,
            run_page_url=run.run_page_url,
            parameters=parameters,
            started=True,
        )

    def wait_for_rerun(
        self, result: RemediationResult, timeout: timedelta = timedelta(minutes=20), poll: int = 15
    ) -> RemediationResult:
        run = wait_for_run(self.w, int(result.rerun_run_id), timeout=timeout, poll=poll)
        if not is_failed(run):
            return result.model_copy(update={"succeeded": True, "result_state": run_result(run)})
        failure = task_failure(self.w, run)
        return result.model_copy(
            update={
                "succeeded": False,
                "result_state": "FAILED",
                "failed_task": failure.task_key if failure else None,
                "error": (failure.error or "")[:2000] if failure else None,
            }
        )
