# Databricks notebook source
"""A `PanelData` built from nothing, so the tool layer can be verified without the gold tables.

The gold layer is the data engineer's deliverable and does not exist yet
(`docs/architecture/agent_data_contract.md`). Every `*_body` in `tools/` is pure and takes its
frame as an argument, so the logic can be proven against this instead -- and the one check that
genuinely needs real tables, `tools/panel_data.check`, stays red as the visible marker of the
dependency.

**It is generated, not recorded.** A committed CSV drifts from the contract silently; a generator
that builds frames column by column fails to run the moment the contract changes under it.

**It is deliberately awkward data.** Every branch a tool can take is represented: an overbought
symbol and an oversold one, a golden cross and a death cross, an elevated volatility regime and a
subdued one, fundamentals with real holes in them, crypto and FX rows absent from the fundamentals
frame entirely, a `percent`-unit macro series and an index-unit one. Clean data would let a tool
that mishandles a null pass its own check.

Layering: pure. No langchain, no mlflow, no Spark, no Databricks.
"""

# COMMAND ----------

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from tools.panel_data import PanelData, normalize_dates

# COMMAND ----------

AS_OF = date(2026, 9, 21)
TRADING_DAYS = 420

INSTRUMENTS = [
    # symbol, name, asset_class, currency, sector, industry, exchange
    ("AAPL", "Apple Inc.", "equity", "USD", "Technology", "Consumer Electronics", "NASDAQ"),
    ("MSFT", "Microsoft Corporation", "equity", "USD", "Technology", "Software", "NASDAQ"),
    ("NVDA", "NVIDIA Corporation", "equity", "USD", "Technology", "Semiconductors", "NASDAQ"),
    ("XOM", "Exxon Mobil Corporation", "equity", "USD", "Energy", "Oil & Gas", "NYSE"),
    ("SPY", "SPDR S&P 500 ETF Trust", "etf", "USD", None, None, "NYSE"),
    ("BTC-USD", "Bitcoin USD", "crypto", "USD", None, None, "CCC"),
    ("EURUSD=X", "EUR/USD", "fx", "USD", None, None, "CCY"),
    ("^GSPC", "S&P 500 Index", "index", "USD", None, None, "SNP"),
]

# Each symbol is shaped to exercise a different branch of get_trend_signals.
PROFILES = {
    "AAPL": {"start": 180.0, "drift": 0.0006, "vol": 0.014, "rsi": 72.4, "regime": "elevated"},
    "MSFT": {"start": 400.0, "drift": 0.0004, "vol": 0.011, "rsi": 55.1, "regime": "normal"},
    "NVDA": {"start": 120.0, "drift": 0.0012, "vol": 0.032, "rsi": 28.3, "regime": "elevated"},
    "XOM": {"start": 110.0, "drift": -0.0002, "vol": 0.009, "rsi": 44.8, "regime": "subdued"},
    "SPY": {"start": 560.0, "drift": 0.0004, "vol": 0.008, "rsi": 61.2, "regime": "subdued"},
    "BTC-USD": {"start": 64000.0, "drift": 0.0009, "vol": 0.038, "rsi": 68.0, "regime": "elevated"},
    "EURUSD=X": {"start": 1.09, "drift": 0.0000, "vol": 0.004, "rsi": 49.5, "regime": "normal"},
    "^GSPC": {"start": 5600.0, "drift": 0.0004, "vol": 0.008, "rsi": 60.4, "regime": "subdued"},
}

MACRO_SERIES = [
    # series_id, title, unit, frequency, category, curve_tenor_months, level
    ("FEDFUNDS", "Federal Funds Effective Rate", "percent", "M", "rates", None, 4.33),
    ("DGS3MO", "3-Month Treasury Constant Maturity", "percent", "D", "rates", 3, 4.41),
    ("DGS2", "2-Year Treasury Constant Maturity", "percent", "D", "rates", 24, 3.92),
    ("DGS5", "5-Year Treasury Constant Maturity", "percent", "D", "rates", 60, 3.78),
    ("DGS10", "10-Year Treasury Constant Maturity", "percent", "D", "rates", 120, 4.06),
    ("DGS30", "30-Year Treasury Constant Maturity", "percent", "D", "rates", 360, 4.38),
    ("CPIAUCSL", "Consumer Price Index for All Urban Consumers", "index", "M", "inflation", None, 315.2),
    ("CPILFESL", "CPI Less Food and Energy", "index", "M", "inflation", None, 322.8),
    ("PCEPI", "Personal Consumption Expenditures Price Index", "index", "M", "inflation", None, 126.4),
    ("T10YIE", "10-Year Breakeven Inflation Rate", "percent", "D", "inflation", None, 2.31),
    ("UNRATE", "Unemployment Rate", "percent", "M", "labour", None, 4.2),
    ("PAYEMS", "All Employees, Total Nonfarm", "thousands", "M", "labour", None, 159_400.0),
    ("GDPC1", "Real Gross Domestic Product", "billions", "Q", "activity", None, 23_400.0),
    ("INDPRO", "Industrial Production Index", "index", "M", "activity", None, 103.6),
    ("VIXCLS", "CBOE Volatility Index", "index", "D", "risk_appetite", None, 17.9),
]

# COMMAND ----------


def _prices(rng: np.random.Generator, profile: dict, days: int) -> pd.DataFrame:
    """A daily bar series with the moving averages a trend tool reads."""
    steps = rng.normal(profile["drift"], profile["vol"], days)
    close = profile["start"] * np.exp(np.cumsum(steps))
    frame = pd.DataFrame({"close": close})
    frame["open"] = frame["close"].shift(1).fillna(profile["start"])
    frame["high"] = frame[["open", "close"]].max(axis=1) * (1 + rng.uniform(0, 0.006, days))
    frame["low"] = frame[["open", "close"]].min(axis=1) * (1 - rng.uniform(0, 0.006, days))
    frame["volume"] = rng.integers(1_000_000, 90_000_000, days)
    return frame


def _fixture_technical(rng: np.random.Generator) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp(AS_OF), periods=TRADING_DAYS)
    blocks = []

    for symbol, profile in PROFILES.items():
        bars = _prices(rng, profile, len(dates))
        close = bars["close"]

        sma_20 = close.rolling(20).mean()
        sma_50 = close.rolling(50).mean()
        sma_200 = close.rolling(200).mean()

        # Force the crossover state each profile is meant to demonstrate, by nudging the long
        # average rather than the price -- the price series stays a real random walk.
        if profile["rsi"] >= 70:
            sma_200 = sma_200 * 0.93  # golden cross: 50 above 200
        elif profile["rsi"] <= 30:
            sma_200 = sma_200 * 1.07  # death cross: 50 below 200

        returns = close.pct_change()
        vol_20 = returns.rolling(20).std() * np.sqrt(252)
        vol_60 = returns.rolling(60).std() * np.sqrt(252)
        ratio = {"elevated": 1.45, "subdued": 0.62, "normal": 1.0}[profile["regime"]]
        vol_20 = vol_60 * ratio

        block = pd.DataFrame(
            {
                "symbol": symbol,
                "trade_date": dates,
                "open": bars["open"].to_numpy(),
                "high": bars["high"].to_numpy(),
                "low": bars["low"].to_numpy(),
                "close": close.to_numpy(),
                # FX has no meaningful volume. A null, not a zero.
                "volume": None if symbol.endswith("=X") else bars["volume"].to_numpy(),
                "sma_20": sma_20.to_numpy(),
                "sma_50": sma_50.to_numpy(),
                "sma_200": sma_200.to_numpy(),
                "ema_12": close.ewm(span=12).mean().to_numpy(),
                "ema_26": close.ewm(span=26).mean().to_numpy(),
                "rsi_14": np.clip(
                    profile["rsi"] + rng.normal(0, 1.5, len(dates)).cumsum() * 0.02, 1, 99
                ),
                "macd": (close.ewm(span=12).mean() - close.ewm(span=26).mean()).to_numpy(),
                "macd_signal": (close.ewm(span=12).mean() - close.ewm(span=26).mean())
                .ewm(span=9)
                .mean()
                .to_numpy(),
                "bb_mid": sma_20.to_numpy(),
                "bb_upper": (sma_20 + 2 * close.rolling(20).std()).to_numpy(),
                "bb_lower": (sma_20 - 2 * close.rolling(20).std()).to_numpy(),
                "atr_14": (bars["high"] - bars["low"]).rolling(14).mean().to_numpy(),
                "realized_vol_20d": vol_20.to_numpy(),
                "realized_vol_60d": vol_60.to_numpy(),
                "pct_change_1d": close.pct_change(1).to_numpy(),
                "pct_change_5d": close.pct_change(5).to_numpy(),
                "pct_change_21d": close.pct_change(21).to_numpy(),
                "pct_change_252d": close.pct_change(252).to_numpy(),
                "drawdown_from_peak": (close / close.cummax() - 1).to_numpy(),
                "as_of": pd.Timestamp(AS_OF),
            }
        )
        block["macd_hist"] = block["macd"] - block["macd_signal"]
        blocks.append(block)

    return pd.concat(blocks, ignore_index=True)


def _fixture_risk(rng: np.random.Generator, technical: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol in PROFILES:
        series = technical[technical["symbol"] == symbol]["close"].pct_change().dropna()
        downside = series[series < 0]
        ann_return = float(series.mean() * 252)
        ann_vol = float(series.std() * np.sqrt(252))
        risk_free = 0.0433
        sortino_vol = float(downside.std() * np.sqrt(252)) or ann_vol
        rows.append(
            {
                "symbol": symbol,
                "as_of_date": pd.Timestamp(AS_OF),
                "window_days": 252,
                "observations": int(min(len(series), 252)),
                "ann_return": ann_return,
                "ann_volatility": ann_vol,
                "sharpe": (ann_return - risk_free) / ann_vol if ann_vol else None,
                "sortino": (ann_return - risk_free) / sortino_vol if sortino_vol else None,
                "var_95": float(series.quantile(0.05)),
                "var_99": float(series.quantile(0.01)),
                "expected_shortfall_95": float(series[series <= series.quantile(0.05)].mean()),
                "max_drawdown": float(
                    (
                        technical[technical["symbol"] == symbol]["close"]
                        / technical[technical["symbol"] == symbol]["close"].cummax()
                        - 1
                    ).min()
                ),
                # An FX pair has no meaningful equity beta. Null, and the benchmark null with it.
                "beta": None if symbol == "EURUSD=X" else float(rng.uniform(0.6, 1.9)),
                "benchmark_symbol": None if symbol == "EURUSD=X" else "^GSPC",
                "skewness": float(series.skew()),
                "kurtosis": float(series.kurtosis()),
                "risk_free_rate": risk_free,
                "as_of": pd.Timestamp(AS_OF),
            }
        )
    return pd.DataFrame(rows)


def _fixture_correlations(rng: np.random.Generator) -> pd.DataFrame:
    symbols = sorted(PROFILES)
    rows = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            # Deliberately omitted: a pair with too few overlapping observations is absent, not
            # written with a placeholder (agent_data_contract.md section 3.4).
            if {a, b} == {"EURUSD=X", "BTC-USD"}:
                continue
            rows.append(
                {
                    "symbol_a": a,
                    "symbol_b": b,
                    "window_days": 252,
                    "as_of_date": pd.Timestamp(AS_OF),
                    "correlation": round(float(rng.uniform(-0.45, 0.95)), 4),
                    "observations": 252,
                    "as_of": pd.Timestamp(AS_OF),
                }
            )
    return pd.DataFrame(rows)


def _fixture_fundamentals(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for symbol, name, asset_class, currency, sector, industry, _ in INSTRUMENTS:
        # Crypto, FX and raw indices are absent from this frame entirely -- not present and
        # empty. An expert asking for them should find nothing, and say so.
        if asset_class in {"crypto", "fx", "index"}:
            continue
        etf = asset_class == "etf"
        rows.append(
            {
                "symbol": symbol,
                "as_of_date": pd.Timestamp(AS_OF),
                "sector": sector,
                "industry": industry,
                "market_cap": float(rng.uniform(2e11, 3.6e12)),
                "enterprise_value": None if etf else float(rng.uniform(2e11, 3.5e12)),
                "currency": currency,
                "pe_trailing": float(rng.uniform(11, 58)),
                # Coverage is genuinely patchy on a free source. These holes are the point.
                "pe_forward": None if symbol in {"XOM", "SPY"} else float(rng.uniform(10, 42)),
                "pb": float(rng.uniform(1.1, 48)),
                "ps": None if etf else float(rng.uniform(1.4, 32)),
                "ev_ebitda": None if etf else float(rng.uniform(6, 44)),
                "gross_margin": None if etf else float(rng.uniform(0.18, 0.78)),
                "operating_margin": None if etf else float(rng.uniform(0.05, 0.62)),
                "net_margin": None if etf else float(rng.uniform(0.03, 0.55)),
                "roe": None if etf else float(rng.uniform(0.04, 1.4)),
                "roa": None if etf else float(rng.uniform(0.01, 0.42)),
                "revenue_growth_yoy": None if etf else float(rng.uniform(-0.12, 0.94)),
                "eps_growth_yoy": None if symbol == "XOM" else float(rng.uniform(-0.3, 1.6)),
                "debt_to_equity": None if etf else float(rng.uniform(0.1, 2.4)),
                "current_ratio": None if etf else float(rng.uniform(0.7, 3.1)),
                "dividend_yield": float(rng.uniform(0.0, 0.041)),
                "fiscal_period_end": pd.Timestamp(AS_OF) - pd.Timedelta(days=62),
                "as_of": pd.Timestamp(AS_OF),
            }
        )
    return pd.DataFrame(rows)


def _fixture_macro(rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    observations = []
    metadata = []
    for series_id, title, unit, frequency, category, tenor, level in MACRO_SERIES:
        periods, step = {"D": (500, 1), "M": (72, 30), "Q": (24, 91)}[frequency]
        dates = [pd.Timestamp(AS_OF) - timedelta(days=step * i) for i in range(periods)][::-1]
        walk = level * np.exp(np.cumsum(rng.normal(0, 0.006, periods)))
        # End exactly on the stated level, so the snapshot reads the intended number.
        values = walk * (level / walk[-1])
        observations.append(
            pd.DataFrame(
                {
                    "series_id": series_id,
                    "observation_date": dates,
                    "value": values,
                    "as_of": pd.Timestamp(AS_OF),
                }
            )
        )
        metadata.append(
            {
                "series_id": series_id,
                "title": title,
                "unit": unit,
                "frequency": frequency,
                "category": category,
                "curve_tenor_months": tenor,
                "as_of": pd.Timestamp(AS_OF),
            }
        )
    return pd.concat(observations, ignore_index=True), pd.DataFrame(metadata)


# COMMAND ----------


def fixture_panel_data(seed: int = 20260922) -> PanelData:
    """A complete `PanelData`. Deterministic for a given seed."""
    rng = np.random.default_rng(seed)

    instruments = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "name": name,
                "asset_class": asset_class,
                "currency": currency,
                "sector": sector,
                "industry": industry,
                "exchange": exchange,
                "is_active": True,
                "as_of": pd.Timestamp(AS_OF),
            }
            for symbol, name, asset_class, currency, sector, industry, exchange in INSTRUMENTS
        ]
    )

    technical = _fixture_technical(rng)
    macro, macro_series = _fixture_macro(rng)

    return PanelData(
        instruments=normalize_dates(instruments),
        technical=normalize_dates(technical),
        risk=normalize_dates(_fixture_risk(rng, technical)),
        correlations=normalize_dates(_fixture_correlations(rng)),
        fundamentals=normalize_dates(_fixture_fundamentals(rng)),
        macro=normalize_dates(macro),
        macro_series=normalize_dates(macro_series),
    )
