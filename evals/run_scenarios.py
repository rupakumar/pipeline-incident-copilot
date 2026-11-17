"""Run loghub_etl failure scenarios on Databricks and record them in the run manifest.

Usage:
  python -m evals.run_scenarios ok-spark mi-01        # specific scenarios / baselines
  python -m evals.run_scenarios --split train         # every scenario in a split (train | test | baseline)
  python -m evals.run_scenarios --reconcile           # record finished runs whose wait timed out locally
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from itertools import islice

from evals.dataset_loader import RUN_MANIFEST, scenario_params, scenarios_by_id
from pipeline_incident_copilot.integrations.databricks_client import JOB_NAME, get_client
from pipeline_incident_copilot.integrations.databricks_job_runs import is_failed, run_result, task_failure
from pipeline_incident_copilot.integrations.databricks_job_runs import wait_for_run as wait_for_job_run

POLL_SECONDS = 15
RUN_TIMEOUT = timedelta(minutes=20)


def wait_for_run(w, run_id: int):
    return wait_for_job_run(w, run_id, timeout=RUN_TIMEOUT, poll=POLL_SECONDS)


def run_scenario(w, job_id: int, scenario: dict) -> dict:
    params = scenario_params(scenario)
    waiter = w.jobs.run_now(job_id=job_id, job_parameters=params)
    print(f"[{scenario['id']}] run {waiter.run_id} started {params}", flush=True)
    run = wait_for_run(w, waiter.run_id)

    failure = task_failure(w, run)
    result = "FAILED" if is_failed(run) else run_result(run)
    as_expected = (result == "FAILED") == scenario["expect_failure"]
    where = f"{failure.task_key}: " if failure else ""
    first_line = failure.error.splitlines()[0][:160] if failure and failure.error else ""
    print(f"[{scenario['id']}] {result}{'' if as_expected else '  <-- UNEXPECTED'} | {where}{first_line}")
    return {
        "run_id": run.run_id,
        "scenario_id": scenario["id"],
        "result": result,
        "failed_task": failure.task_key if failure else None,
        "as_expected": as_expected,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _recorded_run_ids() -> set[int]:
    if not RUN_MANIFEST.exists():
        return set()
    return {json.loads(line)["run_id"] for line in RUN_MANIFEST.read_text().splitlines()}


def reconcile(w, job_id: int, catalog: dict, recent: int = 60) -> None:
    """Match finished runs to scenarios by their non-default parameters; record the missing ones."""
    by_params = {tuple(sorted(scenario_params(s).items())): sid for sid, s in catalog.items()}
    recorded = _recorded_run_ids()
    # the SDK pages through the whole history; 25 per page is the API maximum (26)
    for summary in islice(w.jobs.list_runs(job_id=job_id, completed_only=True, limit=25), recent):
        if summary.run_id in recorded:
            continue
        run = w.jobs.get_run(summary.run_id)
        params = {
            p.name: p.value
            for p in run.job_parameters or []
            # unset parameters come back as None (= the job default)
            if (p.value is not None and p.value != p.default) or p.name == "source"
        }
        sid = by_params.get(tuple(sorted(params.items())))
        if sid is None:
            continue
        failure = task_failure(w, run)
        result = "FAILED" if is_failed(run) else run_result(run)
        record = {
            "run_id": run.run_id,
            "scenario_id": sid,
            "result": result,
            "failed_task": failure.task_key if failure else None,
            "as_expected": (result == "FAILED") == catalog[sid]["expect_failure"],
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        with RUN_MANIFEST.open("a") as f:
            f.write(json.dumps(record) + "\n")
        print(f"[{sid}] recorded run {run.run_id} ({result})")


def main() -> None:
    args = sys.argv[1:]
    catalog = scenarios_by_id()
    w = get_client()
    job = next(iter(w.jobs.list(name=JOB_NAME)), None)
    if job is None:
        sys.exit(f"job {JOB_NAME} not found — deploy it with python databricks/bundle.py deploy")
    if args[:1] == ["--reconcile"]:
        reconcile(w, job.job_id, catalog)
        return
    if args[:1] == ["--split"]:
        ids = [sid for sid, s in catalog.items() if s.get("split") == args[1]]
    else:
        ids = args
    unknown = [i for i in ids if i not in catalog]
    if not ids or unknown:
        sys.exit(f"unknown or missing scenario ids: {unknown or '(none given)'}; known: {sorted(catalog)}")

    failed_to_run = []
    for sid in ids:
        try:
            record = run_scenario(w, job.job_id, catalog[sid])
        except (TimeoutError, OSError) as e:  # transient API/network errors: keep going, report at the end
            print(f"[{sid}] could not complete ({e.__class__.__name__}); rerun it later")
            failed_to_run.append(sid)
            continue
        with RUN_MANIFEST.open("a") as f:
            f.write(json.dumps(record) + "\n")
    if failed_to_run:
        sys.exit(f"not recorded (rerun): {' '.join(failed_to_run)}")


if __name__ == "__main__":
    main()
