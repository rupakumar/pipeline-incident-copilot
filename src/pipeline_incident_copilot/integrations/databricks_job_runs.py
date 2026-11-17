"""Reading the outcome of a multi-task Databricks job run: did it fail, which task, what error."""

import time
from dataclasses import dataclass
from datetime import timedelta

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import Run, RunTask

# A run whose always-run record_run task succeeded after an upstream failure ends as
# SUCCESS_WITH_FAILURES — still a pipeline failure.
FAILED_RESULTS = {"FAILED", "TIMEDOUT", "SUCCESS_WITH_FAILURES"}
FAILED_TASK_RESULTS = {"FAILED", "TIMEDOUT"}


@dataclass(frozen=True)
class TaskFailure:
    task_key: str
    error: str | None
    error_trace: str | None


def wait_for_run(w: WorkspaceClient, run_id: int, timeout: timedelta = timedelta(minutes=20), poll: int = 15) -> Run:
    """Poll until the run has ended. Failed serverless runs end in life-cycle state INTERNAL_ERROR,
    which the SDK's wait_get_run_job_terminated_or_skipped never treats as finished."""
    deadline = time.monotonic() + timeout.total_seconds()
    while time.monotonic() < deadline:
        run = w.jobs.get_run(run_id=run_id)
        if run.end_time:
            return run
        time.sleep(poll)
    raise TimeoutError(f"run {run_id} did not finish within {timeout}")


def run_result(run: Run) -> str:
    """Final result state (SUCCESS, FAILED, …), or the life-cycle state if there is none."""
    return run.state.result_state.value if run.state.result_state else run.state.life_cycle_state.value


def is_failed(run: Run) -> bool:
    return run_result(run) in FAILED_RESULTS or failed_task(run) is not None


def failed_task(run: Run) -> RunTask | None:
    """The first task that failed itself (tasks skipped because of it are UPSTREAM_FAILED)."""
    failed = [
        t
        for t in run.tasks or []
        if t.state and t.state.result_state and t.state.result_state.value in FAILED_TASK_RESULTS
    ]
    return min(failed, key=lambda t: t.start_time or 0, default=None)


def task_failure(w: WorkspaceClient, run: Run) -> TaskFailure | None:
    task = failed_task(run)
    if task is None:
        return None
    output = w.jobs.get_run_output(run_id=task.run_id)
    return TaskFailure(task.task_key, output.error, output.error_trace)
