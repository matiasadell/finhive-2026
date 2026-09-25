# Databricks notebook source
# DBTITLE 1,Correlations (gold)
# MAGIC %md
# MAGIC # Correlations (gold)
# MAGIC
# MAGIC Builds `finhive-2026.gold.correlations` — pairwise Pearson correlations per window, unordered pairs only. Reads `technical_indicators`. Per contract1 §3.6.

# COMMAND ----------

# DBTITLE 1,Read from gold.technical_indicators
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from itertools import combinations

CATALOG = "finhive-2026"
GOLD = f"`{CATALOG}`.gold"

# Read technical_indicators (return_1d per symbol/date)
tech_pdf = spark.sql(f"""
    SELECT symbol, trade_date, return_1d
    FROM {GOLD}.technical_indicators
""").toPandas()
tech_pdf["trade_date"] = pd.to_datetime(tech_pdf["trade_date"])

symbols = sorted(tech_pdf["symbol"].unique())
print(f"Symbols: {symbols}")

# COMMAND ----------

# DBTITLE 1,Compute pairwise correlations
# Windows in trading days (contract1 §3.6)
WINDOWS = {"90d": 90, "1y": 252}

# Pivot: one column per symbol, indexed by trade_date
returns_wide = tech_pdf.pivot(index="trade_date", columns="symbol", values="return_1d")

results = []
for sym_a, sym_b in combinations(symbols, 2):
    # contract1 §3.6: symbol_a < symbol_b lexicographically (combinations guarantees this)
    for window_name, window_days in WINDOWS.items():
        # Take last N rows where both symbols have data
        pair = returns_wide[[sym_a, sym_b]].dropna().iloc[-window_days:]
        n = len(pair)
        if n < 2:
            continue

        corr = pair[sym_a].corr(pair[sym_b])
        as_of = pair.index[-1].date()

        results.append({
            "symbol_a": sym_a,
            "symbol_b": sym_b,
            "window": window_name,
            "as_of_date": as_of,
            "correlation": corr,
            "observations": n,
            "computed_at": datetime.now(timezone.utc),
        })

result_pdf = pd.DataFrame(results)
print(f"Rows: {len(result_pdf)} (unordered pairs only)")
display(result_pdf)

# COMMAND ----------

# DBTITLE 1,Write to gold.correlations
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")

# As-of snapshot — overwrite is idempotent (contract1 §3.6)
result_df = spark.createDataFrame(result_pdf)
result_df.write.mode("overwrite").saveAsTable(f"{GOLD}.correlations")

print(f"Wrote {result_df.count()} rows to {GOLD}.correlations")