---
title: LogHub ETL pipeline — architecture
document_type: architecture
owner: data-platform-team
last_reviewed: 2026-10-04
---

# LogHub ETL pipeline — architecture

The LogHub ETL pipeline ingests application logs from production systems (Spark and HDFS),
parses them into structured events, enforces a data-quality gate, and publishes hourly
event counts for operations dashboards. It runs as the multi-task Databricks job `loghub_etl`,
defined in a Databricks Asset Bundle (`databricks/databricks.yml`, `resources/loghub_etl.job.yml`).

## Data flow

Each stage is its own job task; a task starts only when the previous one succeeded.

1. **Landing** — upstream exporters deliver one log file per source per day into a Unity
   Catalog volume.
2. **`bronze_ingest`** — appends the day's raw lines unchanged to the bronze table, with
   lineage columns (input file, run date, job run id, ingestion time).
3. **`silver_parse`** — parses this run's bronze lines with a source-specific regular
   expression into `event_time`, `level`, `component`, `message` and `source`. Optional
   enrichment columns can be added.
4. **`quality_checks`** — evaluates the data-quality rules on this run's silver batch,
   records every result, and stops the run if any rule is breached.
5. **`gold_aggregate`** — aggregates silver events into hourly counts per level and component.
6. **`record_run`** — always runs, also after a failure; writes one audit row per run to
   `pipeline_runs` and then reports the job as failed if any stage failed.

Each notebook loads shared parameters and table names from `_config` (`%run ./_config`).

## Sources and landing

| Source | Landing file | Native timestamp format |
|---|---|---|
| Spark | `Spark_2k.log` | `yy/MM/dd HH:mm:ss` (e.g. `17/06/09 20:10:40`) |
| HDFS | `HDFS_2k.log` | `yyMMdd HHmmss` (e.g. `081109 203615`) |

Landing volume: `/Volumes/workspace/pipeline_demo/raw_logs/`. Each day's delivery lands in a
date folder, `raw_logs/<source>/<YYYY-MM-DD>/<file>`; a run processes the folder for its
`run_date`. Upstream re-sends overwrite the file in the same date folder. Files are plain
text with Windows line endings (CRLF); the parser strips the trailing carriage return.
`databricks/provision.py` creates the volume and loads the deliveries.

## Tables

All tables are Delta tables in `workspace.pipeline_demo`, owned by the pipeline.

| Table | Grain | Columns | Write mode |
|---|---|---|---|
| `<source>_logs_bronze` | one row per raw line | `value` string, `input_file` string, `run_date` string, `job_run_id` string, `ingested_at` timestamp | append-only history (schema may evolve) |
| `<source>_logs_silver` | one row per parsed event | `event_time` timestamp, `level` string, `component` string, `message` string, `source` string, `run_date` string, `job_run_id` string | overwrite (full refresh) or append (incremental), set by `silver_write_mode` |
| `<source>_logs_gold_hourly` | hour × level × component | `hour` timestamp, `level` string, `component` string, `events` long | full overwrite each run |
| `pipeline_runs` | one row per job run | status, failed task, first error line, bronze/silver/gold row counts, input path, code version (git commit), parameters, start time, duration | append (written by `record_run`) |
| `dq_results` | one row per rule per run | rule, observed value, threshold, passed | append (written by `quality_checks`) |

The silver schema is fixed by the pipeline code. A full refresh may replace the schema;
incremental appends require the incoming columns to match the existing table schema
exactly, because schema evolution (`mergeSchema`) is not enabled on the incremental path.

## Job configuration

| Parameter | Default | Meaning |
|---|---|---|
| `source` | `spark` | Which log source to process (`spark` or `hdfs`) |
| `run_date` | the trigger date (`{{job.trigger.time.iso_date}}`) | Delivery date to process, `YYYY-MM-DD`; set explicitly for reruns and backfills |
| `input_path` | the drop for `run_date` | Override to read a specific file |
| `timestamp_format` | the source's native format | Override for sources whose format differs |
| `silver_write_mode` | `overwrite` | `overwrite` = full refresh, `append` = incremental load |
| `enrich_columns` | none | Comma-separated enrichments added to silver: `host_id` (IP extracted from the message), `region`, `cluster` |
| `level_encoding` | `text` | `text` keeps log levels as strings; `numeric` maps them to integer codes |
| `gold_table` | `workspace.pipeline_demo.<source>_logs_gold_hourly` | Fully qualified output table for the hourly aggregate |
| `debug_sample_rows` | `0` | When above 0, collects a sample of that many rows to the driver for inspection |

## Data-quality gate

The `quality_checks` task evaluates these rules on this run's silver batch, before gold is
written, so dashboards never show partially parsed data:

| Rule | Passes when |
|---|---|
| `min_row_count` | the batch has at least 100 rows |
| `null_event_time_rate` | under 5% of rows have a null `event_time` |
| `event_date_matches_run_date` | under 5% of parsed events fall on a date other than `run_date` |

Every result (observed value, threshold, passed) is appended to `dq_results`. If any rule
fails, the task fails with `DATA_QUALITY_FAILED` and lists each breached rule. A null
`event_time` means a log line's timestamp could not be parsed with the active timestamp
format.

## Compute

The job runs on Databricks serverless compute (Free Edition: serverless only, no
configurable clusters). Driver memory is fixed by the platform and cannot be raised per job.
Serverless auto-optimization (automatic retries) is disabled in the dev target so that every
failure is seen once; the prod target enables it and allows two retries per task.
Work that must stay distributed (aggregations, writes) runs on executors; anything collected
to the driver (`collect()`, `toPandas()`) must fit in driver memory.

## Access and ownership

- The job runs as its owner, who has full privileges on `workspace.pipeline_demo` and
  read access to the shared `samples` catalog.
- The `samples` catalog is read-only for everyone; tables cannot be created there.
- Writing to any schema outside `workspace.pipeline_demo` needs `USE CATALOG`, `USE SCHEMA`
  and `CREATE TABLE` granted on the target.

## Operations

- **Owner:** data-platform-team (on-call rotation).
- **Schedule and SLA:** daily at 05:00 UTC (paused in dev), gold available by 06:00 UTC for
  operations dashboards. Runs longer than 15 minutes raise a duration warning.
- **Reruns:** silver (full refresh) and gold overwrite, and every stage reads only its own
  job run's rows, so a failed run can be rerun safely once the cause is fixed.
- **Deploys:** the job definition lives in the bundle; `python databricks/bundle.py deploy`
  syncs the notebooks and updates the job. Each run records the git commit it was deployed
  from (`code_version`), so code changes since the last successful run can be traced.
- **Monitoring:** failures email the job owner and raise an incident. Run history is in the
  Jobs UI/API and in `pipeline_runs`; data-quality history is in `dq_results`.
