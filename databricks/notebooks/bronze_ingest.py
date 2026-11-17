# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze: ingest the day's log drop
# MAGIC
# MAGIC Appends the raw lines of one delivery to the bronze table, unchanged, with lineage
# MAGIC columns (input file, run date, job run). Bronze is append-only history.

# COMMAND ----------

# MAGIC %run ./_config

# COMMAND ----------

# MAGIC %md ## Read the landing file

# COMMAND ----------

from pyspark.sql import functions as F

raw = spark.read.text(input_path).select(
    "value",
    F.col("_metadata.file_path").alias("input_file"),
    F.lit(run_date).alias("run_date"),
    F.lit(job_run_id).alias("job_run_id"),
    F.current_timestamp().alias("ingested_at"),
)

# COMMAND ----------

# MAGIC %md ## Append to bronze

# COMMAND ----------

# Lineage columns were added after the first loads; bronze may evolve, silver may not.
raw.write.mode("append").option("mergeSchema", "true").saveAsTable(BRONZE_TABLE)

bronze_rows = spark.table(BRONZE_TABLE).where(F.col("job_run_id") == job_run_id).count()
dbutils.jobs.taskValues.set("bronze_rows", bronze_rows)
dbutils.jobs.taskValues.set("input_path", input_path)
print(f"bronze rows for this run: {bronze_rows} -> {BRONZE_TABLE}")
