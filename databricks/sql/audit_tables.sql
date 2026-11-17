-- Audit tables for the LogHub ETL pipeline. Created by databricks/provision.py; the
-- quality_checks and record_run tasks append to them. Read by the incident copilot's
-- evidence collectors ("compare with the last successful run", data-quality history).

CREATE TABLE IF NOT EXISTS workspace.pipeline_demo.pipeline_runs (
  job_run_id STRING COMMENT 'Databricks job run id',
  run_date STRING COMMENT 'Delivery date processed (YYYY-MM-DD)',
  source STRING COMMENT 'spark | hdfs',
  status STRING COMMENT 'SUCCEEDED | FAILED',
  failed_task STRING COMMENT 'First task that failed, if any',
  error_summary STRING COMMENT 'First line of the failed task error',
  bronze_rows BIGINT,
  silver_rows BIGINT,
  gold_rows BIGINT,
  input_path STRING,
  code_version STRING COMMENT 'git commit the job was deployed from',
  parameters STRING COMMENT 'Job parameters as JSON',
  started_at TIMESTAMP,
  recorded_at TIMESTAMP,
  duration_seconds DOUBLE
) COMMENT 'One row per loghub_etl job run (written by the record_run task)';

CREATE TABLE IF NOT EXISTS workspace.pipeline_demo.dq_results (
  job_run_id STRING,
  run_date STRING,
  source STRING,
  rule STRING COMMENT 'min_row_count | null_event_time_rate | event_date_matches_run_date',
  observed DOUBLE,
  threshold DOUBLE,
  passed BOOLEAN,
  checked_at TIMESTAMP
) COMMENT 'One row per quality rule per run (written by the quality_checks task)';
