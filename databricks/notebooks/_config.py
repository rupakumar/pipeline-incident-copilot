# Databricks notebook source
# MAGIC %md
# MAGIC # LogHub ETL — shared configuration
# MAGIC
# MAGIC Job parameters, paths and table names used by every task (`%run ./_config`).
# MAGIC Defaults match the job definition in `resources/loghub_etl.job.yml`.

# COMMAND ----------

dbutils.widgets.text("source", "spark")  # spark | hdfs
dbutils.widgets.text("run_date", "")  # delivery date YYYY-MM-DD
dbutils.widgets.text("input_path", "")  # default: the drop for run_date in the landing volume
dbutils.widgets.text("timestamp_format", "")  # default: the source's native log timestamp format
dbutils.widgets.text("silver_write_mode", "overwrite")  # overwrite (full refresh) | append (incremental)
dbutils.widgets.text("enrich_columns", "")  # comma-separated enrichments: host_id, region, cluster
dbutils.widgets.text("level_encoding", "text")  # text | numeric
dbutils.widgets.text("gold_table", "")  # default: <catalog>.<schema>.<source>_logs_gold_hourly
dbutils.widgets.text("debug_sample_rows", "0")  # >0 collects a sample to the driver for inspection
dbutils.widgets.text("job_run_id", "")  # set by the job; empty when run interactively

CATALOG = "workspace"
SCHEMA = "pipeline_demo"
VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/raw_logs"
LANDING_FILES = {"spark": "Spark_2k.log", "hdfs": "HDFS_2k.log"}

source = dbutils.widgets.get("source")
run_date = dbutils.widgets.get("run_date")
input_path = dbutils.widgets.get("input_path") or f"{VOLUME_ROOT}/{source}/{run_date}/{LANDING_FILES[source]}"
timestamp_format = dbutils.widgets.get("timestamp_format")
silver_write_mode = dbutils.widgets.get("silver_write_mode")
enrich_columns = [c.strip() for c in dbutils.widgets.get("enrich_columns").split(",") if c.strip()]
level_encoding = dbutils.widgets.get("level_encoding")
debug_sample_rows = int(dbutils.widgets.get("debug_sample_rows") or 0)
job_run_id = dbutils.widgets.get("job_run_id") or "interactive"

BRONZE_TABLE = f"{CATALOG}.{SCHEMA}.{source}_logs_bronze"
SILVER_TABLE = f"{CATALOG}.{SCHEMA}.{source}_logs_silver"
GOLD_TABLE = dbutils.widgets.get("gold_table") or f"{CATALOG}.{SCHEMA}.{source}_logs_gold_hourly"
PIPELINE_RUNS_TABLE = f"{CATALOG}.{SCHEMA}.pipeline_runs"
DQ_RESULTS_TABLE = f"{CATALOG}.{SCHEMA}.dq_results"

print(f"source={source} run_date={run_date} input={input_path} job_run_id={job_run_id}")
