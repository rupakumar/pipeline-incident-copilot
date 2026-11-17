# Databricks notebook source
# MAGIC %md
# MAGIC # Gold: hourly event counts
# MAGIC
# MAGIC Aggregates silver into hourly counts per level and component for the operations
# MAGIC dashboards. Runs only after the quality gate passed.

# COMMAND ----------

# MAGIC %run ./_config

# COMMAND ----------

# MAGIC %md ## Aggregate

# COMMAND ----------

from pyspark.sql import functions as F

hourly = (
    spark.table(SILVER_TABLE)
    .groupBy(F.date_trunc("hour", "event_time").alias("hour"), "level", "component")
    .agg(F.count("*").alias("events"))
)

if debug_sample_rows > 0:
    debug_sample = spark.range(0, debug_sample_rows).withColumn("payload", F.repeat(F.lit("x"), 100)).collect()
    print(f"debug sample collected: {len(debug_sample)} rows")

# COMMAND ----------

# MAGIC %md ## Publish

# COMMAND ----------

hourly.write.mode("overwrite").saveAsTable(GOLD_TABLE)

gold_rows = spark.table(GOLD_TABLE).count()
dbutils.jobs.taskValues.set("gold_rows", gold_rows)
print(f"gold rows: {gold_rows} -> {GOLD_TABLE}")
