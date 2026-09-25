# Databricks notebook source
# DBTITLE 1,Technical Indicators (gold)
# MAGIC %md
# MAGIC # Technical Indicators (gold)
# MAGIC
# MAGIC Builds `finhive-2026.gold.technical_indicators` — full history per symbol with all 15+ indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, realized vol, drawdown). Reads Yahoo bronze, deduplicates, computes in pandas. Per contract1 §3.2.

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

with open(f"{repo_root}/config/data_ingestion/yahoo.json") as f:
    yahoo_config = json.load(f)

symbols = [entry["series"] for entry in yahoo_config]
print(f"Symbols from config: {symbols}")

# COMMAND ----------

# DBTITLE 1,Read and union bronze, normalize, deduplicate
# --- Read and union all Yahoo bronze tables ---
# Bronze columns: Date, Open, High, Low, Close, adj_close, Volume, ingested_at, pipeline_name
# Normalize to lowercase per contract1 §7.1

parts = []
for symbol in symbols:
    bronze_table = f"`{CATALOG}`.yahoo.`{symbol}`"
    try:
        df = (
            spark.table(bronze_table)
            .withColumn("symbol", F.lit(symbol))
            .withColumn("trade_date", F.col("Date").cast("date"))
            .withColumnRenamed("Open", "open")
            .withColumnRenamed("High", "high")
            .withColumnRenamed("Low", "low")
            .withColumnRenamed("Close", "close")
            .withColumnRenamed("Volume", "volume")
            .select("symbol", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "ingested_at")
        )
        parts.append(df)
    except Exception as e:
        print(f"WARNING: could not read {bronze_table}: {e}")

if not parts:
    raise RuntimeError("No Yahoo bronze tables found — run the Yahoo ingestion job first")

raw_df = parts[0]
for df in parts[1:]:
    raw_df = raw_df.unionByName(df)

# Deduplicate: keep newest ingested_at per (symbol, trade_date) — contract1 §7.2
w = Window.partitionBy("symbol", "trade_date").orderBy(F.col("ingested_at").desc())
deduped_df = (
    raw_df
    .withColumn("rn", F.row_number().over(w))
    .filter("rn = 1")
    .drop("rn", "ingested_at")
)

print(f"Rows after dedup: {deduped_df.count()}")
display(deduped_df.limit(10))

# COMMAND ----------

# DBTITLE 1,Compute all technical indicators in pandas
import pandas as pd
import numpy as np

pdf = deduped_df.toPandas()
pdf = pdf.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

result_parts = []
for symbol, g in pdf.groupby("symbol"):
    g = g.sort_values("trade_date").reset_index(drop=True)
    adj = g["adj_close"]

    # 1. Daily return (decimal, NULL on first row)
    g["return_1d"] = adj.pct_change()

    # 2. Simple moving averages (NULL during warm-up)
    g["sma_20"] = adj.rolling(20).mean()
    g["sma_50"] = adj.rolling(50).mean()
    g["sma_200"] = adj.rolling(200).mean()

    # 3. Exponential moving averages (MACD inputs)
    g["ema_12"] = adj.ewm(span=12, adjust=False).mean()
    g["ema_26"] = adj.ewm(span=26, adjust=False).mean()

    # 4. RSI 14 (Wilder's, 0-100 scale)
    delta = adj.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    g["rsi_14"] = 100 - (100 / (1 + rs))
    g.loc[g.index[0], "rsi_14"] = np.nan  # no prior data on first row

    # 5. MACD
    g["macd"] = g["ema_12"] - g["ema_26"]
    g["macd_signal"] = g["macd"].ewm(span=9, adjust=False).mean()
    g["macd_hist"] = g["macd"] - g["macd_signal"]

    # 6. Bollinger Bands (20-period, 2 std dev)
    g["bb_mid_20"] = g["sma_20"]
    rolling_std = adj.rolling(20).std()
    g["bb_upper_20"] = g["sma_20"] + 2 * rolling_std
    g["bb_lower_20"] = g["sma_20"] - 2 * rolling_std

    # 7. ATR 14 (Wilder's, price units)
    high, low = g["high"], g["low"]
    prev_close = g["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    g["atr_14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    g.loc[g.index[0], "atr_14"] = np.nan

    # 8. Realized volatility (annualized decimal, x sqrt(252))
    g["realized_vol_20d"] = g["return_1d"].rolling(20).std() * np.sqrt(252)
    g["realized_vol_60d"] = g["return_1d"].rolling(60).std() * np.sqrt(252)

    # 9. Drawdown (decimal, <= 0)
    g["drawdown"] = adj / adj.cummax() - 1

    result_parts.append(g)

result_pdf = pd.concat(result_parts, ignore_index=True)
result_pdf["computed_at"] = datetime.now(timezone.utc)

# Ensure volume is nullable integer for proper Spark NULL handling
result_pdf["volume"] = result_pdf["volume"].astype("Int64")

print(f"Total rows: {len(result_pdf)}")
print(f"Columns: {list(result_pdf.columns)}")
display(result_pdf.head(10))

# COMMAND ----------

# DBTITLE 1,Write to gold.technical_indicators
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")

# Full rebuild — overwrite is idempotent since we recompute from bronze each run (contract1 §2.6)
result_df = spark.createDataFrame(result_pdf)
result_df.write.mode("overwrite").saveAsTable(f"{GOLD}.technical_indicators")

print(f"Wrote {result_df.count()} rows to {GOLD}.technical_indicators")