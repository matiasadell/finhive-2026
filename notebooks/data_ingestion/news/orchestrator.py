# Databricks notebook source
# DBTITLE 1,News ingestion - orchestrator
# MAGIC %md
# MAGIC # News ingestion - orchestrator
# MAGIC
# MAGIC Iterates over the configured symbols (equities + major ETFs only, per contract1 §9), calling the worker notebook once per symbol via `dbutils.notebook.run` with a 3-day lookback window, then appends every result to `` `finhive-2026`.logs.ingestionLog `` and prunes articles older than the retention window (contract1 §5.2).

# COMMAND ----------

# DBTITLE 1,Widgets and config path
import os

dbutils.widgets.text("job_name", "")
dbutils.widgets.text("config_path", "")

job_name = dbutils.widgets.get("job_name")
config_path = dbutils.widgets.get("config_path")

if not config_path:
    notebook_path = (
        dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        .notebookPath().get()
    )
    repo_root = "/Workspace" + notebook_path.rsplit("/notebooks/", 1)[0]
    config_path = f"{repo_root}/config/data_ingestion/news.json"

if not job_name:
    job_name = "manual_run"

# COMMAND ----------

# DBTITLE 1,Worker path
# Resolve worker path relative to this notebook's location
_notebook_path = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext()
    .notebookPath().get()
)
worker_notebook_path = _notebook_path.rsplit("/", 1)[0] + "/worker"

# COMMAND ----------

# DBTITLE 1,Load config
import json

with open(config_path) as f:
    symbols_config = json.load(f)

if not symbols_config:
    raise ValueError(f"no symbols configured in {config_path}")

# COMMAND ----------

# DBTITLE 1,Date range and retention
from datetime import datetime, timezone, timedelta

# 3-day lookback catches weekends + market holidays; MERGE deduplicates overlaps
today = datetime.now(timezone.utc).date()
from_date = (today - timedelta(days=3)).isoformat()
to_date = today.isoformat()

# contract1 §5.2: prune articles older than retention window
RETENTION_DAYS = 30

# COMMAND ----------

# DBTITLE 1,News orchestrator - parallel workers
from concurrent.futures import ThreadPoolExecutor, as_completed

def run_worker(entry):
    symbol = entry["series"]

    raw_result = dbutils.notebook.run(
        worker_notebook_path,
        timeout_seconds=600,
        arguments={
            "series": symbol,
            "from_date": from_date,
            "to_date": to_date,
            "job_name": job_name,
        },
    )
    return json.loads(raw_result)

results = []

max_workers = min(len(symbols_config), 3)
with ThreadPoolExecutor(max_workers=max_workers) as executor:
    future_to_entry = {
        executor.submit(run_worker, entry): entry for entry in symbols_config
    }
    for future in as_completed(future_to_entry):
        result = future.result()
        results.append(result)

# COMMAND ----------

# DBTITLE 1,News orchestrator - write log
from datetime import date, datetime

log_rows = [
    (
        r["series"],
        r["source"],
        r["status"],
        datetime.fromisoformat(r["updateAt"]),
        date.fromisoformat(r["lastObservationDate"]) if r["lastObservationDate"] else None,
        r["item_count"],
        r["error"],
        r["job_name"],
    )
    for r in results
]
log_schema = "series string, source string, status boolean, updateAt timestamp, lastObservationDate date, item_count int, error string, job_name string"
log_df = spark.createDataFrame(log_rows, schema=log_schema)
log_df.write.mode("append").saveAsTable("`finhive-2026`.logs.ingestionLog")

# COMMAND ----------

# DBTITLE 1,News orchestrator - prune old articles
# contract1 §5.2: DELETE rows older than retention window
# Delta Sync propagates deletes, keeping the index inside its size ceiling
spark.sql(f"""
    DELETE FROM `finhive-2026`.gold.news
    WHERE published_at < DATE_SUB(CURRENT_TIMESTAMP(), {RETENTION_DAYS})
""")

print(f"Pruned articles older than {RETENTION_DAYS} days")

# COMMAND ----------

# DBTITLE 1,News orchestrator - check failures
failures = [r for r in results if not r["status"]]
if failures:
    failed_symbols = [r["series"] for r in failures]
    raise RuntimeError(f"{len(failures)} symbols failed news ingestion: {failed_symbols}")

print(f"ingested news for {len(results)} symbols successfully")

# COMMAND ----------

