# Databricks notebook source
# MAGIC %md
# MAGIC # Quality gate: check this run's silver batch
# MAGIC
# MAGIC Evaluates every rule on the batch, writes each result to `dq_results`, and fails the
# MAGIC run before gold if any rule is breached, so dashboards never show bad data.

# COMMAND ----------

# MAGIC %run ./_config

# COMMAND ----------

# MAGIC %md ## Rules

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql import types as T

MIN_ROWS = 100
MAX_NULL_EVENT_TIME_RATE = 0.05
MAX_OFF_DATE_RATE = 0.05

batch = spark.table(SILVER_TABLE).where(F.col("job_run_id") == job_run_id)
stats = batch.agg(
    F.count("*").alias("rows"),
    F.sum(F.col("event_time").isNull().cast("int")).alias("null_ts"),
    F.sum((F.date_format("event_time", "yyyy-MM-dd") != F.lit(run_date)).cast("int")).alias("off_date"),
).first()
rows, null_ts, off_date = stats.rows, stats.null_ts or 0, stats.off_date or 0
parsed = rows - null_ts

# (rule, observed, threshold, passed, message if breached)
results = [
    ("min_row_count", rows, MIN_ROWS, rows >= MIN_ROWS, f"only {rows} rows in this run's batch (minimum {MIN_ROWS})"),
    (
        "null_event_time_rate",
        null_ts / rows if rows else 1.0,
        MAX_NULL_EVENT_TIME_RATE,
        rows > 0 and null_ts / rows < MAX_NULL_EVENT_TIME_RATE,
        f"{null_ts / rows if rows else 1.0:.2%} of rows in {SILVER_TABLE} have null event_time "
        f"(threshold {MAX_NULL_EVENT_TIME_RATE:.0%}). Check timestamp format / upstream log format",
    ),
    (
        "event_date_matches_run_date",
        off_date / parsed if parsed else 0.0,
        MAX_OFF_DATE_RATE,
        not run_date or not parsed or off_date / parsed < MAX_OFF_DATE_RATE,
        f"{off_date / parsed if parsed else 0.0:.2%} of parsed events are not dated {run_date} "
        f"(threshold {MAX_OFF_DATE_RATE:.0%}). Check that the delivery belongs to this date",
    ),
]

# COMMAND ----------

# MAGIC %md ## Record results and enforce

# COMMAND ----------

schema = T.StructType(
    [
        T.StructField("job_run_id", T.StringType()),
        T.StructField("run_date", T.StringType()),
        T.StructField("source", T.StringType()),
        T.StructField("rule", T.StringType()),
        T.StructField("observed", T.DoubleType()),
        T.StructField("threshold", T.DoubleType()),
        T.StructField("passed", T.BooleanType()),
    ]
)
rows_out = [(job_run_id, run_date, source, rule, float(obs), float(thr), bool(ok)) for rule, obs, thr, ok, _ in results]
(
    spark.createDataFrame(rows_out, schema)
    .withColumn("checked_at", F.current_timestamp())
    .write.mode("append")
    .saveAsTable(DQ_RESULTS_TABLE)
)

for rule, observed, threshold, ok, _ in results:
    print(f"{'PASS' if ok else 'FAIL'}  {rule}: observed={observed:.4g} threshold={threshold:.4g}")

breaches = [message for _, _, _, ok, message in results if not ok]
assert not breaches, "DATA_QUALITY_FAILED: " + "; ".join(breaches) + "."
