# Databricks notebook source
# DBTITLE 1,Risk Metrics (gold)
# MAGIC %md
# MAGIC # Risk Metrics (gold)
# MAGIC
# MAGIC Builds `finhive-2026.gold.risk_metrics` — as-of snapshot with Sharpe, Sortino, VaR, expected shortfall, max drawdown, beta. Reads `technical_indicators` and `macro_series` (DGS3MO for risk-free rate). Per contract1 §3.5.

# COMMAND ----------

# DBTITLE 1,Read from gold tables
import pandas as pd
import numpy as np
from datetime import datetime, timezone

CATALOG = "finhive-2026"
GOLD = f"`{CATALOG}`.gold"

# Read technical_indicators (return_1d, adj_close per symbol/date)
tech_pdf = spark.sql(f"""
    SELECT symbol, trade_date, adj_close, return_1d
    FROM {GOLD}.technical_indicators
""").toPandas()
tech_pdf["trade_date"] = pd.to_datetime(tech_pdf["trade_date"])

# Read DGS3MO risk-free rate from macro_series (percent → decimal)
rf_pdf = spark.sql(f"""
    SELECT observation_date, value
    FROM {GOLD}.macro_series
    WHERE series_id = 'DGS3MO'
    ORDER BY observation_date DESC LIMIT 1
""").toPandas()
rf_annualized = rf_pdf["value"].iloc[0] / 100 if len(rf_pdf) > 0 else 0.0
print(f"Risk-free rate (DGS3MO, annualized decimal): {rf_annualized}")
print(f"Symbols: {sorted(tech_pdf['symbol'].unique())}")

# COMMAND ----------

# DBTITLE 1,Compute risk metrics per symbol/window
# Windows in trading days (contract1 §3.5: 1y required, others welcome)
WINDOWS = {"3m": 63, "1y": 252, "3y": 756}
BENCHMARK = "SPY"

benchmark_df = (
    tech_pdf[tech_pdf["symbol"] == BENCHMARK]
    [["trade_date", "return_1d"]]
    .rename(columns={"return_1d": "bench_ret"})
    .dropna()
)

results = []
for symbol in sorted(tech_pdf["symbol"].unique()):
    sym_df = (
        tech_pdf[tech_pdf["symbol"] == symbol]
        .sort_values("trade_date")
        .reset_index(drop=True)
    )

    for window_name, window_days in WINDOWS.items():
        window_df = sym_df.iloc[-window_days:] if len(sym_df) >= window_days else sym_df
        returns = window_df["return_1d"].dropna()
        n = len(returns)
        if n < 2:
            continue

        # Returns
        total_return = window_df["adj_close"].iloc[-1] / window_df["adj_close"].iloc[0] - 1
        years = n / 252
        ann_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else None
        vol_ann = returns.std() * np.sqrt(252)

        # Sharpe (excess returns over daily rf)
        rf_daily = rf_annualized / 252
        excess = returns - rf_daily
        sharpe = excess.mean() / returns.std() * np.sqrt(252) if returns.std() > 0 else None

        # Sortino (downside deviation only)
        downside = returns[returns < 0]
        downside_dev_ann = downside.std() * np.sqrt(252) if len(downside) > 1 else None
        sortino = excess.mean() / downside.std() * np.sqrt(252) if downside_dev_ann and downside_dev_ann > 0 else None

        # VaR (historical / empirical quantile, negative)
        var_95 = returns.quantile(0.05)
        var_99 = returns.quantile(0.01)

        # Expected shortfall (mean of returns at or below VaR 95)
        tail = returns[returns <= var_95]
        es_95 = tail.mean() if len(tail) > 0 else None

        # Max drawdown (<= 0, within window)
        adj = window_df["adj_close"]
        max_dd = (adj / adj.cummax() - 1).min()

        # Beta and R^2 vs benchmark
        if symbol != BENCHMARK:
            sym_rets = window_df[["trade_date", "return_1d"]].dropna().rename(columns={"return_1d": "sym_ret"})
            aligned = sym_rets.merge(benchmark_df, on="trade_date", how="inner")
            if len(aligned) >= 2 and aligned["bench_ret"].std() > 0:
                beta, alpha = np.polyfit(aligned["bench_ret"], aligned["sym_ret"], 1)
                pred = beta * aligned["bench_ret"] + alpha
                ss_res = ((aligned["sym_ret"] - pred) ** 2).sum()
                ss_tot = ((aligned["sym_ret"] - aligned["sym_ret"].mean()) ** 2).sum()
                r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else None
            else:
                beta, r_squared = None, None
        else:
            beta, r_squared = 1.0, 1.0

        results.append({
            "symbol": symbol,
            "window": window_name,
            "as_of_date": window_df["trade_date"].iloc[-1].date(),
            "observations": n,
            "total_return": total_return,
            "annualized_return": ann_return,
            "volatility_annualized": vol_ann,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "downside_deviation": downside_dev_ann,
            "risk_free_rate_annualized": rf_annualized,
            "risk_free_source": "DGS3MO",
            "var_95_1d": var_95,
            "var_99_1d": var_99,
            "expected_shortfall_95_1d": es_95,
            "max_drawdown": max_dd,
            "beta": beta,
            "r_squared": r_squared,
            "benchmark_symbol": BENCHMARK if symbol != BENCHMARK else None,
            "computed_at": datetime.now(timezone.utc),
        })

result_pdf = pd.DataFrame(results)
print(f"Rows: {len(result_pdf)}")
display(result_pdf)

# COMMAND ----------

# DBTITLE 1,Write to gold.risk_metrics
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")

# As-of snapshot — overwrite is idempotent (contract1 §3.5)
result_df = spark.createDataFrame(result_pdf)
result_df.write.mode("overwrite").saveAsTable(f"{GOLD}.risk_metrics")

print(f"Wrote {result_df.count()} rows to {GOLD}.risk_metrics")