"""Create one fresh failing run for a demo, and open its incident.

Runs a known failure scenario on Databricks (default pe-04: gold table pointed at a schema the job may not
write to), waits for it to fail, then lets the watcher pick it up so the incident opens with its evidence
(and the "investigating" email goes out). The run is NOT added to the eval run manifest.

Then: /diagnose-incident INC-x in Claude Code (Task 1), or `copilot diagnose INC-x` (LangGraph).

Usage: python -m scripts.demo_failure [--scenario pe-04]
"""

import argparse
import sys

from evals.dataset_loader import scenarios_by_id
from evals.run_scenarios import run_scenario
from pipeline_incident_copilot.bootstrap import App
from pipeline_incident_copilot.observability.langfuse_tracer import get_langfuse
from pipeline_incident_copilot.schemas.connection import DEFAULT_CONNECTION_ID


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="pe-04")
    args = parser.parse_args()
    catalog = scenarios_by_id()
    if args.scenario not in catalog or not catalog[args.scenario].get("expect_failure", True):
        sys.exit(f"unknown or non-failing scenario {args.scenario}; known: {sorted(catalog)}")

    app = App()
    app.watcher.watch_from(DEFAULT_CONNECTION_ID)  # make sure the watcher has a start point before the run begins
    job = next(iter(app.workspace.jobs.list(name=app.job_name)), None)
    if job is None:
        sys.exit(f"job {app.job_name} not found — deploy it with python databricks/bundle.py deploy")
    record = run_scenario(app.workspace, job.job_id, catalog[args.scenario])
    if record["result"] != "FAILED":
        sys.exit(f"run {record['run_id']} did not fail ({record['result']}); nothing to diagnose")

    try:
        for run in app.watcher.poll(lookback=10):
            opened = app.intake.process(run)
            mark = "  ← demo" if run.run_id == record["run_id"] else ""
            if opened is None:  # the running watcher opened it first
                print(f"run {run.run_id}  {run.failed_task}{mark}: already opened by the watcher")
                continue
            incident, evidence = opened
            print(f"{incident.incident_id}  run {run.run_id}  {run.failed_task}{mark}")
    finally:
        get_langfuse().flush()
    row = app.repo.conn.execute(
        "SELECT incident_id FROM incidents WHERE run_id = ?", (str(record["run_id"]),)
    ).fetchone()
    if row:
        print(f"demo run {record['run_id']} is {row['incident_id']}")
    print("next: the watcher (if running) diagnoses it within a minute: auto-fix or approval request;")
    print("      or drive it yourself with /diagnose-incident <INC id above> in Claude Code")


if __name__ == "__main__":
    main()
