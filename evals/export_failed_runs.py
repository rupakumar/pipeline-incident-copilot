"""Export the failed scenario runs (error + trace) recorded in the run manifest.

Takes the latest run per failure scenario that failed as expected, and writes
evals/datasets/failed_runs.json — the evidence side of the eval set (ground truth stays
in failure_scenarios.yaml, keyed by scenario_id).

Usage: python -m evals.export_failed_runs
"""

import json

from evals.dataset_loader import FAILED_RUNS_FILE, RUN_MANIFEST, scenario_params, scenarios_by_id
from pipeline_incident_copilot.integrations.databricks_client import get_client
from pipeline_incident_copilot.integrations.databricks_job_runs import task_failure


def latest_failed_runs() -> dict[str, dict]:
    latest = {}
    for line in RUN_MANIFEST.read_text().splitlines():
        record = json.loads(line)
        if record["result"] == "FAILED" and record["as_expected"]:
            latest[record["scenario_id"]] = record  # manifest is append-only, so later wins
    return latest


def main() -> None:
    w = get_client()
    catalog = scenarios_by_id()
    rows = []
    for scenario_id, record in sorted(latest_failed_runs().items()):
        scenario = catalog[scenario_id]
        run = w.jobs.get_run(record["run_id"])
        failure = task_failure(w, run)
        rows.append(
            {
                "run_id": run.run_id,
                "scenario_id": scenario_id,
                "failure_class": scenario["failure_class"],
                "split": scenario["split"],
                "source": scenario_params(scenario)["source"],
                "failed_task": failure.task_key,
                "error": failure.error,
                "error_trace": failure.error_trace,
                "run_page_url": run.run_page_url,
            }
        )
    FAILED_RUNS_FILE.write_text(json.dumps(rows, indent=2))
    by_split = {s: sum(r["split"] == s for r in rows) for s in ("train", "test")}
    print(f"wrote {len(rows)} failed runs -> {FAILED_RUNS_FILE} {by_split}")


if __name__ == "__main__":
    main()
