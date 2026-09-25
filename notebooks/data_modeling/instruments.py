# Databricks notebook source
# DBTITLE 1,Instruments (gold)
# MAGIC %md
# MAGIC # Instruments (gold)
# MAGIC
# MAGIC Builds `finhive-2026.gold.instruments` — one row per symbol with asset_class, currency, and date coverage. Reads Yahoo bronze tables.

# COMMAND ----------

# DBTITLE 1,Imports and config
import json
from datetime import datetime, timezone
from pyspark.sql import functions as F

CATALOG = "finhive-2026"
GOLD = f"`{CATALOG}`.gold"

# resolve repo root from notebook path
notebook_path = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext()
    .notebookPath().get()
)
repo_root = "/Workspace" + notebook_path.rsplit("/notebooks/", 1)[0]

with open(f"{repo_root}/config/data_ingestion/yahoo.json") as f:
    yahoo_config = json.load(f)

symbols = [entry["series"] for entry in yahoo_config]
print(f"Symbols from config: {symbols}")

# COMMAND ----------

# DBTITLE 1,Build instruments DataFrame from bronze
# Static metadata not available from yfinance (contract1 §3.1)
# asset_class is a closed vocabulary: equity, etf, index, crypto, fx
INSTRUMENT_META = {
    "SPY":   {"name": "SPDR S&P 500 ETF Trust", "asset_class": "etf",    "currency": "USD", "exchange": "AMEX"},
    "^DJI":  {"name": "Dow Jones Industrial Average", "asset_class": "index",  "currency": "USD", "exchange": None},
    "^IXIC": {"name": "NASDAQ Composite Index",     "asset_class": "index",  "currency": "USD", "exchange": None},
    "^GSPC": {"name": "S&P 500 Index",              "asset_class": "index",  "currency": "USD", "exchange": None},
    "AAPL":  {"name": "Apple Inc.",                "asset_class": "equity", "currency": "USD", "exchange": "NASDAQ"},
    "MSFT":  {"name": "Microsoft Corporation",     "asset_class": "equity", "currency": "USD", "exchange": "NASDAQ"},
    "GOOG":  {"name": "Alphabet Inc.",             "asset_class": "equity", "currency": "USD", "exchange": "NASDAQ"},
}

rows = []
for symbol in symbols:
    meta = INSTRUMENT_META.get(symbol, {"name": symbol, "asset_class": "equity", "currency": "USD", "exchange": None})
    bronze_table = f"`{CATALOG}`.yahoo.`{symbol}`"
    try:
        result = spark.sql(f"SELECT MIN(CAST(Date AS DATE)) AS first_date, MAX(CAST(Date AS DATE)) AS last_date FROM {bronze_table}").collect()[0]
        first_date = result["first_date"]
        last_date = result["last_date"]
    except Exception:
        first_date = None
        last_date = None

    rows.append((
        symbol,
        meta["name"],
        meta["asset_class"],
        meta["currency"],
        meta["exchange"],
        None,   # sector — deferred to contract 2
        True,   # is_active
        first_date,
        last_date,
        datetime.now(timezone.utc),
    ))

schema = "symbol string, name string, asset_class string, currency string, exchange string, sector string, is_active boolean, first_date date, last_date date, computed_at timestamp"
instruments_df = spark.createDataFrame(rows, schema=schema)
display(instruments_df)

# COMMAND ----------

# DBTITLE 1,Write to gold.instruments
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")

# Full rebuild — overwrite is idempotent since we recompute from bronze each run
instruments_df.write.mode("overwrite").saveAsTable(f"{GOLD}.instruments")

print(f"Wrote {len(rows)} instruments into {GOLD}.instruments")