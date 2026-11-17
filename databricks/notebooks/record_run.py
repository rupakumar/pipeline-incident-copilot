# Databricks notebook source
# MAGIC %md
# MAGIC # Record the run in `pipeline_runs`
# MAGIC
# MAGIC Runs after every other task, also when one failed. Writes one audit row per job run:
# MAGIC outcome, failed task and its error, row counts per layer, input and code version.
# MAGIC "Compare with the last successful run" reads this table.

# COMMAND ----------

# MAGIC %run ./_config

# COMMAND ----------

dbutils.widgets.text("code_version", "")
code_version = dbutils.widgets.get("code_version")

# COMMAND ----------

# MAGIC %md ## Collect the outcome

# COMMAND ----------

import json
from datetime import UTC, datetime

from databricks.sdk import WorkspaceClient

PIPELINE_TASKS = ["bronze_ingest", "silver_parse", "quality_checks", "gold_aggregate"]
TASK_VALUES = {
    "bronze_ingest": ["bronze_rows", "input_path"],
    "silver_parse": ["silver_rows"],
    "gold_aggregate": ["gold_rows"],
}

MISSING = "<missing>"  # taskValues.get rejects default=None; a task that failed early set nothing


def task_value(task: str, key: str):
    value = dbutils.jobs.taskValues.get(taskKey=task, key=key, default=MISSING, debugValue=MISSING)
    return None if value == MISSING else value


values = {key: task_value(task, key) for task, keys in TASK_VALUES.items() for key in keys}

started_at, failed_task, error_summary = None, None, None
if job_run_id.isdigit():
    w = WorkspaceClient()
    run = w.jobs.get_run(int(job_run_id))
    started_at = datetime.fromtimestamp(run.start_time / 1000, tz=UTC)
    tasks = sorted((t for t in run.tasks if t.task_key in PIPELINE_TASKS), key=lambda t: t.start_time or 0)
    failed = [t for t in tasks if t.state.result_state and t.state.result_state.value in ("FAILED", "TIMEDOUT")]
    if failed:
        failed_task = failed[0].task_key
        error = w.jobs.get_run_output(run_id=failed[0].run_id).error or ""
        error_summary = error.strip().splitlines()[0][:500] if error.strip() else None

recorded_at = datetime.now(UTC)
parameters = {
    name: dbutils.widgets.get(name)
    for name in [
        "source",
        "run_date",
        "input_path",
        "timestamp_format",
        "silver_write_mode",
        "enrich_columns",
        "level_encoding",
        "gold_table",
        "debug_sample_rows",
    ]
}

# COMMAND ----------

# MAGIC %md ## Write the audit row

# COMMAND ----------

from pyspark.sql import types as T

schema = T.StructType(
    [
        T.StructField("job_run_id", T.StringType()),
        T.StructField("run_date", T.StringType()),
        T.StructField("source", T.StringType()),
        T.StructField("status", T.StringType()),
        T.StructField("failed_task", T.StringType()),
        T.StructField("error_summary", T.StringType()),
        T.StructField("bronze_rows", T.LongType()),
        T.StructField("silver_rows", T.LongType()),
        T.StructField("gold_rows", T.LongType()),
        T.StructField("input_path", T.StringType()),
        T.StructField("code_version", T.StringType()),
        T.StructField("parameters", T.StringType()),
        T.StructField("started_at", T.TimestampType()),
        T.StructField("recorded_at", T.TimestampType()),
        T.StructField("duration_seconds", T.DoubleType()),
    ]
)
row = (
    job_run_id,
    run_date,
    source,
    "FAILED" if failed_task else "SUCCEEDED",
    failed_task,
    error_summary,
    values["bronze_rows"],
    values["silver_rows"],
    values["gold_rows"],
    values["input_path"] or input_path,
    code_version,
    json.dumps(parameters),
    started_at,
    recorded_at,
    (recorded_at - started_at).total_seconds() if started_at else None,
)
spark.createDataFrame([row], schema).write.mode("append").saveAsTable(PIPELINE_RUNS_TABLE)
print(f"recorded run {job_run_id}: {row[3]} failed_task={failed_task}")

# COMMAND ----------

# MAGIC %md ## Keep the job status honest

# COMMAND ----------

# This task always runs, so a success here would mark a broken pipeline SUCCESS_WITH_FAILURES
# (no failure alert, green-looking run). Fail once the audit row is safely written.
if failed_task:
    raise RuntimeError(f"Pipeline task {failed_task} failed; run recorded in {PIPELINE_RUNS_TABLE}.")
