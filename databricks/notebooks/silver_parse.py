# Databricks notebook source
# MAGIC %md
# MAGIC # Silver: parse raw lines into structured events
# MAGIC
# MAGIC Parses this run's bronze lines with a source-specific pattern, applies optional
# MAGIC enrichments and level encoding, and writes the silver table (full refresh or append).

# COMMAND ----------

# MAGIC %run ./_config

# COMMAND ----------

# MAGIC %md ## Parse

# COMMAND ----------

from pyspark.sql import functions as F

# Spark: "17/06/09 20:10:40 INFO executor.CoarseGrainedExecutorBackend: Registered ..."
# HDFS:  "081109 203615 148 INFO dfs.DataNode$PacketResponder: PacketResponder 1 ..."
patterns = {
    "spark": (r"^(\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) (\w+) ([^:]+): (.*)$", "yy/MM/dd HH:mm:ss"),
    "hdfs": (r"^(\d{6} \d{6}) \d+ (\w+) ([^:]+): (.*)$", "yyMMdd HHmmss"),
}
regex, native_format = patterns[source]
ts_format = timestamp_format or native_format

parsed = (
    spark.table(BRONZE_TABLE)
    .where(F.col("job_run_id") == job_run_id)
    .withColumn("value", F.regexp_replace("value", "\r$", ""))  # LogHub files use CRLF
    .select(
        F.try_to_timestamp(F.regexp_extract("value", regex, 1), F.lit(ts_format)).alias("event_time"),
        F.regexp_extract("value", regex, 2).alias("level"),
        F.regexp_extract("value", regex, 3).alias("component"),
        F.regexp_extract("value", regex, 4).alias("message"),
        F.lit(source).alias("source"),
        "run_date",
        "job_run_id",
    )
)

# COMMAND ----------

# MAGIC %md ## Enrich and encode

# COMMAND ----------

enrichments = {
    "host_id": F.regexp_extract("message", r"(\d+\.\d+\.\d+\.\d+)", 1),
    "region": F.lit("eu-west-1"),
    "cluster": F.lit("prod-etl-01"),
}
for column in enrich_columns:
    parsed = parsed.withColumn(column, enrichments[column])

if level_encoding == "numeric":
    level_codes = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "FATAL": 50}
    code = F.lit(0)
    for name, value in level_codes.items():
        code = F.when(F.col("level") == name, value).otherwise(code)
    parsed = parsed.withColumn("level", code)

# COMMAND ----------

# MAGIC %md ## Write silver

# COMMAND ----------

writer = parsed.write.mode(silver_write_mode)
if silver_write_mode == "overwrite":
    writer = writer.option("overwriteSchema", "true")
writer.saveAsTable(SILVER_TABLE)

silver_rows = spark.table(SILVER_TABLE).where(F.col("job_run_id") == job_run_id).count()
dbutils.jobs.taskValues.set("silver_rows", silver_rows)
print(f"silver rows for this run: {silver_rows} -> {SILVER_TABLE} ({silver_write_mode})")
