# Databricks notebook source
"""Price, indicators and trend. Six tools for the technical analyst.

**Interpretation lives here, not in the prompt.** `get_trend_signals` labels an RSI, names a
crossover and calls a volatility regime, because a threshold in a prompt is a threshold a model
can argue itself out of, and because `agent-design.md` invariant 1 says the LLM never computes a
number. The model narrates what these return; it does not decide what 72 means.

The thresholds sit in this file rather than in `setup/config.py` -- they are measured properties
of the indicators, not configuration, and they belong next to the code that applies them
(section 3.1).

Layering: pure. No langchain here; the `@tool` wrapping goes through `tools/safe_tool.py`.
"""

# COMMAND ----------

from __future__ import annotations

import pandas as pd

from tools import formatting as f
from tools.panel_data import PanelData, latest_row
from tools.safe_tool import safe_tool
from tools.symbols import resolve_symbol

# COMMAND ----------

# Conventional RSI bands. Reported as a state, never as an instruction.
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# The ratio of 20-day to 60-day realized volatility. Above the first the near term is noisier
# than the medium term; below the second it is unusually quiet.
VOL_REGIME_ELEVATED = 1.2
VOL_REGIME_SUBDUED = 0.8

MAX_HISTORY_ROWS = 40

NOT_A_RECOMMENDATION = (
    "These are descriptions of the price series, not a recommendation. They say what the "
    "indicators show, not what anyone should do."
)


def _row(data: PanelData, symbol: str) -> pd.Series:
    row = latest_row(data.technical, symbol, "trade_date")
    if row is None:
        raise ValueError(f"no price history for {symbol}")
    return row


# COMMAND ----------


def list_instruments_body(data: PanelData) -> str:
    """Every instrument the panel holds, with its asset class."""
    if data.instruments.empty:
        raise ValueError("the instrument universe is empty")

    rows = [
        [
            str(r["symbol"]),
            str(r["name"]),
            str(r["asset_class"]),
            f.NA if f.is_missing(r.get("sector")) else str(r["sector"]),
        ]
        for _, r in data.instruments.sort_values(["asset_class", "symbol"]).iterrows()
    ]
    newest = data.technical["trade_date"].max() if not data.technical.empty else None
    return (
        f"{len(rows)} instruments with data in this system:\n\n"
        + f.table(["symbol", "name", "asset class", "sector"], rows)
        + "\n\nUse these symbols exactly as written. "
        + f.as_of(newest)
    )


def get_price_snapshot_body(data: PanelData, symbol: str) -> str:
    """Latest close and the changes around it."""
    resolved = resolve_symbol(data, symbol)
    row = _row(data, resolved)
    return (
        f"{resolved} price snapshot\n\n"
        + f.fields(
            [
                ("close", f.num(row.get("close"))),
                ("open / high / low", f"{f.num(row.get('open'))} / {f.num(row.get('high'))} / {f.num(row.get('low'))}"),
                ("volume", f.num(row.get("volume"), digits=0)),
                ("1-day change", f.signed_pct(row.get("pct_change_1d"))),
                ("5-day change", f.signed_pct(row.get("pct_change_5d"))),
                ("21-day change", f.signed_pct(row.get("pct_change_21d"))),
                ("252-day change", f.signed_pct(row.get("pct_change_252d"))),
                ("drawdown from peak", f.signed_pct(row.get("drawdown_from_peak"))),
            ]
        )
        + "\n\n"
        + f.as_of(row.get("trade_date"))
    )


def get_technical_indicators_body(data: PanelData, symbol: str) -> str:
    """The raw indicator values, unlabelled. `get_trend_signals` is what interprets them."""
    resolved = resolve_symbol(data, symbol)
    row = _row(data, resolved)
    return (
        f"{resolved} technical indicators\n\n"
        + f.fields(
            [
                ("close", f.num(row.get("close"))),
                ("SMA 20 / 50 / 200", f"{f.num(row.get('sma_20'))} / {f.num(row.get('sma_50'))} / {f.num(row.get('sma_200'))}"),
                ("EMA 12 / 26", f"{f.num(row.get('ema_12'))} / {f.num(row.get('ema_26'))}"),
                ("RSI (14)", f.num(row.get("rsi_14"), digits=1)),
                ("MACD / signal / hist", f"{f.num(row.get('macd'), 3)} / {f.num(row.get('macd_signal'), 3)} / {f.num(row.get('macd_hist'), 3)}"),
                ("Bollinger upper / mid / lower", f"{f.num(row.get('bb_upper'))} / {f.num(row.get('bb_mid'))} / {f.num(row.get('bb_lower'))}"),
                ("ATR (14)", f.num(row.get("atr_14"))),
                ("realized vol 20d / 60d", f"{f.pct(row.get('realized_vol_20d'))} / {f.pct(row.get('realized_vol_60d'))}"),
            ]
        )
        + "\n\n"
        + f.as_of(row.get("trade_date"))
    )


def get_trend_signals_body(data: PanelData, symbol: str) -> str:
    """The interpreted view: momentum band, crossover state, position against the bands."""
    resolved = resolve_symbol(data, symbol)
    row = _row(data, resolved)
    signals: list[tuple[str, str]] = []

    rsi = row.get("rsi_14")
    if f.is_missing(rsi):
        signals.append(("momentum (RSI 14)", f.NA))
    else:
        band = (
            f"overbought (>= {RSI_OVERBOUGHT})"
            if rsi >= RSI_OVERBOUGHT
            else f"oversold (<= {RSI_OVERSOLD})"
            if rsi <= RSI_OVERSOLD
            else "neutral"
        )
        signals.append(("momentum (RSI 14)", f"{f.num(rsi, 1)} -- {band}"))

    sma_50, sma_200 = row.get("sma_50"), row.get("sma_200")
    if f.is_missing(sma_50) or f.is_missing(sma_200):
        signals.append(("50/200 crossover", f"{f.NA} (not enough history)"))
    else:
        cross = "golden cross (50 above 200)" if sma_50 > sma_200 else "death cross (50 below 200)"
        signals.append(("50/200 crossover", cross))

    close, sma_20 = row.get("close"), row.get("sma_20")
    if not f.is_missing(close) and not f.is_missing(sma_20):
        side = "above" if close > sma_20 else "below"
        signals.append(("price vs SMA 20", f"{side} by {f.pct(abs(close / sma_20 - 1))}"))

    vol_20, vol_60 = row.get("realized_vol_20d"), row.get("realized_vol_60d")
    if f.is_missing(vol_20) or f.is_missing(vol_60) or not vol_60:
        signals.append(("volatility regime", f.NA))
    else:
        ratio = vol_20 / vol_60
        regime = (
            f"elevated (20d/60d {ratio:.2f} > {VOL_REGIME_ELEVATED})"
            if ratio > VOL_REGIME_ELEVATED
            else f"subdued (20d/60d {ratio:.2f} < {VOL_REGIME_SUBDUED})"
            if ratio < VOL_REGIME_SUBDUED
            else f"normal (20d/60d {ratio:.2f})"
        )
        signals.append(("volatility regime", regime))

    bb_upper, bb_lower = row.get("bb_upper"), row.get("bb_lower")
    if not f.is_missing(close) and not f.is_missing(bb_upper) and not f.is_missing(bb_lower):
        where = (
            "above the upper band"
            if close > bb_upper
            else "below the lower band"
            if close < bb_lower
            else "inside the bands"
        )
        signals.append(("Bollinger position", where))

    return (
        f"{resolved} trend signals\n\n"
        + f.fields(signals)
        + f"\n\n{NOT_A_RECOMMENDATION}\n\n"
        + f.as_of(row.get("trade_date"))
    )


def get_price_history_body(data: PanelData, symbol: str, days: int = 30) -> str:
    """A recent slice of the daily series, sampled if it is long."""
    resolved = resolve_symbol(data, symbol)
    hits = data.technical[data.technical["symbol"] == resolved].sort_values("trade_date")
    if hits.empty:
        raise ValueError(f"no price history for {resolved}")

    window = hits.tail(max(int(days), 2))
    step = max(1, len(window) // MAX_HISTORY_ROWS)
    sampled = window.iloc[::step]

    rows = [
        [f.iso(r["trade_date"]), f.num(r["close"]), f.signed_pct(r.get("pct_change_1d"))]
        for _, r in sampled.iterrows()
    ]
    note = "" if step == 1 else f" (every {step}th session, to keep this readable)"
    return (
        f"{resolved} daily closes, last {len(window)} sessions{note}\n\n"
        + f.table(["date", "close", "1d change"], rows)
        + "\n\n"
        + f.as_of(window["trade_date"].max())
    )


def compare_instruments_body(data: PanelData, symbols: str) -> str:
    """Several instruments side by side, so a four-instrument question costs one call."""
    wanted = [s.strip() for s in str(symbols).replace(";", ",").split(",") if s.strip()]
    if not wanted:
        raise ValueError("no symbols given; pass them comma-separated, e.g. 'AAPL, MSFT, BTC'")

    rows = []
    newest = None
    for raw in wanted:
        resolved = resolve_symbol(data, raw)
        row = _row(data, resolved)
        newest = row["trade_date"] if newest is None else max(newest, row["trade_date"])
        rows.append(
            [
                resolved,
                f.num(row.get("close")),
                f.signed_pct(row.get("pct_change_21d")),
                f.signed_pct(row.get("pct_change_252d")),
                f.num(row.get("rsi_14"), 1),
                f.pct(row.get("realized_vol_20d")),
                f.signed_pct(row.get("drawdown_from_peak")),
            ]
        )

    return (
        f"Comparison of {len(rows)} instruments\n\n"
        + f.table(
            ["symbol", "close", "21d", "252d", "RSI", "vol 20d", "drawdown"],
            rows,
        )
        + "\n\n"
        + f.as_of(newest)
    )


# COMMAND ----------


def build_technical_tools(data: PanelData) -> list:
    """The six technical tools, each closing over the already-loaded panel."""

    @safe_tool
    def list_instruments() -> str:
        """List every instrument this system has data for, with its asset class and sector.

        Call this first when a symbol did not resolve, or when you need to know what the
        universe contains. It takes no arguments.
        """
        return list_instruments_body(data)

    @safe_tool
    def get_price_snapshot(symbol: str) -> str:
        """Latest close, volume and the 1-day, 5-day, 21-day and 252-day changes.

        Args:
            symbol: The instrument, e.g. 'AAPL', 'BTC-USD', 'EURUSD=X'. Names such as 'Apple'
                or 'Bitcoin' also resolve.
        """
        return get_price_snapshot_body(data, symbol)

    @safe_tool
    def get_technical_indicators(symbol: str) -> str:
        """Raw indicator values: moving averages, RSI, MACD, Bollinger bands, ATR, realized vol.

        Use get_trend_signals instead if you want these interpreted rather than listed.

        Args:
            symbol: The instrument.
        """
        return get_technical_indicators_body(data, symbol)

    @safe_tool
    def get_trend_signals(symbol: str) -> str:
        """Interpreted trend and momentum: RSI band, 50/200 crossover, volatility regime.

        This is the one to call for a broad 'how does it look' question; it combines several
        indicators into labelled states rather than raw numbers.

        Args:
            symbol: The instrument.
        """
        return get_trend_signals_body(data, symbol)

    @safe_tool
    def get_price_history(symbol: str, days: int = 30) -> str:
        """Recent daily closes, for describing a path rather than a point.

        Args:
            symbol: The instrument.
            days: How many trading sessions back to go. Defaults to 30.
        """
        return get_price_history_body(data, symbol, days)

    @safe_tool
    def compare_instruments(symbols: str) -> str:
        """Compare several instruments in one call: price, returns, RSI, volatility, drawdown.

        Prefer this over calling get_price_snapshot once per instrument.

        Args:
            symbols: Comma-separated symbols, e.g. 'AAPL, MSFT, NVDA'.
        """
        return compare_instruments_body(data, symbols)

    return [
        list_instruments,
        get_price_snapshot,
        get_technical_indicators,
        get_trend_signals,
        get_price_history,
        compare_instruments,
    ]


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Every tool called once: an as-of date on each, and no ERROR.

    `ctx` keys used:
      panel  PanelData, real or fixture
    """
    from tools.checks import exercise_tools

    return exercise_tools(
        build_technical_tools(ctx["panel"]),
        {
            "list_instruments": {},
            "get_price_snapshot": {"symbol": "AAPL"},
            "get_technical_indicators": {"symbol": "AAPL"},
            "get_trend_signals": {"symbol": "AAPL"},
            "get_price_history": {"symbol": "AAPL", "days": 30},
            "compare_instruments": {"symbols": "AAPL, MSFT, BTC"},
        },
    )
