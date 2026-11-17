"""EvidenceCollector (Databricks): the standard evidence every incident gets, before any agent looks at it.

Always: the failed task's error and trace (cleaned), the run's parameters, a comparison with the
last successful run, code/job-definition changes since then, and whether the input arrived.
Anything that cannot be collected is recorded in `problems`, never raised.
"""

import re

from databricks.sdk import WorkspaceClient

from pipeline_incident_copilot.integrations.databricks_job_runs import task_failure
from pipeline_incident_copilot.rag.retrieval.query.log_distiller import distill
from pipeline_incident_copilot.schemas.evidence import CodeChange, EvidenceBundle, RunComparison
from pipeline_incident_copilot.schemas.incident import Incident
from pipeline_incident_copilot.tools.registry import ToolRegistry

VOLUME_PATH = re.compile(r"(/Volumes/[^\s'\"`]+?)\.?(?=[\s'\"`]|$)")


def volume_path_in(error: str | None) -> str | None:
    """A /Volumes/... path named in the error message, e.g. by PATH_NOT_FOUND."""
    m = VOLUME_PATH.search(error or "")
    return m.group(1) if m else None


def deployed_code_version(run) -> str | None:
    """The git commit stamped into the job at deploy time (a task's `code_version` base parameter)."""
    for task in run.tasks or []:
        params = (task.notebook_task.base_parameters or {}) if task.notebook_task else {}
        if params.get("code_version"):
            return params["code_version"]
    return None


class EvidenceCollector:
    def __init__(self, w: WorkspaceClient, tools: ToolRegistry):
        self.w = w
        self.tools = tools

    def collect(self, incident: Incident) -> EvidenceBundle:
        problems: list[str] = []
        run = self.w.jobs.get_run(incident.run.run_id)
        failure = task_failure(self.w, run)
        error = failure.error if failure else None
        trace = failure.error_trace if failure else None
        if failure is None:
            problems.append("no task reported a failure (run-level failure, e.g. timeout or cancellation)")

        results = []
        comparison = None
        cmp_result = self.tools.run(
            "compare_with_last_success",
            job_run_id=str(incident.run.run_id),
            source=incident.source,
            parameters=incident.run.parameters,
            started_at=incident.run.started_at.isoformat() if incident.run.started_at else None,
            code_version=deployed_code_version(run),
            failed_task=incident.run.failed_task,
        )
        results.append(cmp_result)
        if cmp_result.ok:
            comparison = RunComparison.model_validate(cmp_result.data["comparison"])
        else:
            problems.append(cmp_result.summary)

        code_changes: list[CodeChange] = []
        good = comparison.last_success.code_version if comparison and comparison.last_success else None
        bad = comparison.failed.code_version if comparison and comparison.failed else None
        if good and bad:
            diff_result = self.tools.run("git_diff_since_last_success", from_commit=good, to_commit=bad)
            results.append(diff_result)
            if diff_result.ok:
                code_changes = [CodeChange.model_validate(c) for c in diff_result.data["changes"]]
            else:
                problems.append(diff_result.summary)
        else:
            problems.append("code versions unknown: no earlier successful run, or the job has no code_version")

        input_path = (
            (comparison.failed.input_path if comparison and comparison.failed else None)
            or incident.run.parameters.get("input_path")
            or volume_path_in(error)
        )
        if input_path:
            results.append(self.tools.run("check_path_exists", path=input_path))

        return EvidenceBundle(
            incident_id=incident.incident_id,
            failed_task=failure.task_key if failure else incident.run.failed_task,
            error=error,
            error_trace_chars=len(trace or ""),
            distilled_log=distill(error or "", trace or ""),
            parameters=incident.run.parameters,
            comparison=comparison,
            code_changes=code_changes,
            tool_results=results,
            problems=problems,
        )
