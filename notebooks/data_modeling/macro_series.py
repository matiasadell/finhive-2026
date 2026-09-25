# Databricks notebook source
# DBTITLE 1,Macro Series (gold)
# MAGIC %md
# MAGIC # Macro Series (gold)
# MAGIC
# MAGIC Builds `finhive-2026.gold.macro_series` — long format union of all FRED series. Reads FRED bronze tables. Per contract1 §3.3.

# COMMAND ----------

# DBTITLE 1,Imports and config
import json
from datetime import datetime, timezone
from pyspark.sql import functions as F
from pyspark.sql.window import Window

CATALOG = "finhive-2026"
GOLD = f"`{CATALOG}`.gold"

notebook_path = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext()
    .notebookPath().get()
)
repo_root = "/Workspace" + notebook_path.rsplit("/notebooks/", 1)[0]

with open(f"{repo_root}/config/data_ingestion/fred.json") as f:
    fred_config = json.load(f)

series_list = [entry["series"] for entry in fred_config]
print(f"FRED series from config: {series_list}")

# COMMAND ----------

# DBTITLE 1,Read and union FRED bronze, deduplicate
# Each FRED bronze table has: date (timestamp), value (double), ingested_at (timestamp), pipeline_name
# Gold needs: series_id, observation_date (date), value (double), computed_at (timestamp)

parts = []
for series_id in series_list:
    bronze_table = f"`{CATALOG}`.fred.`{series_id}`"
    try:
        df = (
            spark.table(bronze_table)
            .withColumn("series_id", F.lit(series_id))
            .withColumn("observation_date", F.col("date").cast("date"))
            .select("series_id", "observation_date", "value", "ingested_at")
        )
        parts.append(df)
    except Exception as e:
        print(f"WARNING: could not read {bronze_table}: {e}")

if not parts:
    raise RuntimeError("No FRED bronze tables found — run the FRED ingestion job first")

raw_df = parts[0]
for df in parts[1:]:
    raw_df = raw_df.unionByName(df)

# Deduplicate: keep the row with the newest ingested_at per (series_id, observation_date)
w = Window.partitionBy("series_id", "observation_date").orderBy(F.col("ingested_at").desc())
deduped_df = (
    raw_df
    .withColumn("rn", F.row_number().over(w))
    .filter("rn = 1")
    .drop("rn", "ingested_at")
    .withColumn("computed_at", F.lit(datetime.now(timezone.utc)))
)

print(f"Total rows after dedup: {deduped_df.count()}")
display(deduped_df.limit(20))

# COMMAND ----------

# DBTITLE 1,Write to gold.macro_series
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")

# Full rebuild — overwrite is idempotent since we recompute from bronze each run
deduped_df.write.mode("overwrite").saveAsTable(f"{GOLD}.macro_series")

print(f"Wrote macro_series into {GOLD}.macro_series")