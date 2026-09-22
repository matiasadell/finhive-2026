# Databricks notebook source
"""Risk-adjusted figures and how instruments move together. Four tools for the quant/risk analyst.

Two things this family reports that the others do not, both because `agent-design.md` section 8.2
makes them the reason this expert is on the panel at all:

**The Sharpe/Sortino gap is information, not noise.** When they diverge, the volatility is
asymmetric, and averaging them away hides exactly what the reader needs. Both are always shown.

**The sample size is part of the figure.** A Sharpe on 40 observations is a different statement
from one on 252, and a correlation on 15 overlapping days is noise. Every output states its count
so the expert can qualify the number rather than quote it flat.

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

# Below this, a statistic computed from daily returns is describing the sample and not the
# instrument. Reported as a caveat rather than suppressed -- the expert decides what to do with it.
THIN_SAMPLE = 120

# Conventional reading of a pairwise correlation, so the tool labels rather than the prompt.
CORR_HIGH = 0.7
CORR_MODERATE = 0.3
CORR_NEGATIVE = -0.2

DEFAULT_TOP_N = 6


def _risk_row(data: PanelData, symbol: str) -> pd.Series:
    row = latest_row(data.risk, symbol, "as_of_date")
    if row is None:
        raise ValueError(f"no risk metrics for {symbol}")
    return row


def _sample_note(observations) -> str:
    if f.is_missing(observations):
        return ""
    if int(observations) < THIN_SAMPLE:
        return (
            f"\n\nCaveat: these are computed from only {int(observations)} daily observations. "
            "Say so when you quote them -- at this sample size they describe the window more "
            "than the instrument."
        )
    return ""


def _label_correlation(value) -> str:
    if f.is_missing(value):
        return f.NA
    if value >= CORR_HIGH:
        return "strongly positive"
    if value >= CORR_MODERATE:
        return "moderately positive"
    if value <= CORR_NEGATIVE:
        return "negative"
    return "weak"


# COMMAND ----------


def get_risk_profile_body(data: PanelData, symbol: str) -> str:
    """Return, volatility, Sharpe and Sortino, tail risk, drawdown and beta for one instrument."""
    resolved = resolve_symbol(data, symbol)
    row = _risk_row(data, resolved)

    sharpe, sortino = row.get("sharpe"), row.get("sortino")
    gap = ""
    if not f.is_missing(sharpe) and not f.is_missing(sortino):
        if sortino > sharpe * 1.25:
            gap = " -- Sortino well above Sharpe: the volatility is mostly upside"
        elif sortino < sharpe * 0.8:
            gap = " -- Sortino below Sharpe: the volatility is concentrated in the downside"

    beta = row.get("beta")
    benchmark = row.get("benchmark_symbol")
    beta_text = (
        f.NA
        if f.is_missing(beta)
        else f"{f.num(beta)} vs {benchmark if not f.is_missing(benchmark) else 'an unstated benchmark'}"
    )

    return (
        f"{resolved} risk profile ({int(row['window_days'])}-day window, "
        f"{int(row['observations'])} observations)\n\n"
        + f.fields(
            [
                ("annualized return", f.signed_pct(row.get("ann_return"))),
                ("annualized volatility", f.pct(row.get("ann_volatility"))),
                ("Sharpe / Sortino", f"{f.num(sharpe)} / {f.num(sortino)}{gap}"),
                ("VaR 95% / 99% (daily)", f"{f.signed_pct(row.get('var_95'))} / {f.signed_pct(row.get('var_99'))}"),
                ("expected shortfall 95%", f.signed_pct(row.get("expected_shortfall_95"))),
                ("max drawdown", f.signed_pct(row.get("max_drawdown"))),
                ("beta", beta_text),
                ("skewness / kurtosis", f"{f.num(row.get('skewness'))} / {f.num(row.get('kurtosis'))}"),
                ("risk-free rate used", f.pct(row.get("risk_free_rate"))),
            ]
        )
        + _sample_note(row.get("observations"))
        + "\n\n"
        + f.as_of(row.get("as_of_date"))
    )


def compare_risk_body(data: PanelData, symbols: str) -> str:
    """Several instruments' risk figures side by side, in one call."""
    wanted = [s.strip() for s in str(symbols).replace(";", ",").split(",") if s.strip()]
    if not wanted:
        raise ValueError("no symbols given; pass them comma-separated, e.g. 'AAPL, BTC-USD'")

    rows = []
    newest = None
    for raw in wanted:
        resolved = resolve_symbol(data, raw)
        row = _risk_row(data, resolved)
        newest = row["as_of_date"] if newest is None else max(newest, row["as_of_date"])
        rows.append(
            [
                resolved,
                f.signed_pct(row.get("ann_return")),
                f.pct(row.get("ann_volatility")),
                f.num(row.get("sharpe")),
                f.num(row.get("sortino")),
                f.signed_pct(row.get("var_95")),
                f.signed_pct(row.get("max_drawdown")),
                f.num(row.get("beta")),
            ]
        )

    return (
        f"Risk comparison of {len(rows)} instruments\n\n"
        + f.table(
            ["symbol", "ann return", "ann vol", "Sharpe", "Sortino", "VaR 95%", "max DD", "beta"],
            rows,
        )
        + "\n\nA higher return at a higher volatility is not a better instrument; read the "
        "Sharpe and the drawdown together.\n\n"
        + f.as_of(newest)
    )


def get_correlations_body(data: PanelData, symbol: str, top_n: int = DEFAULT_TOP_N) -> str:
    """What one instrument moves with, most correlated first."""
    resolved = resolve_symbol(data, symbol)
    frame = data.correlations
    if frame.empty:
        raise ValueError("no correlation data loaded")

    # Stored once per unordered pair; mirror it here rather than in the gold layer.
    hits = frame[(frame["symbol_a"] == resolved) | (frame["symbol_b"] == resolved)].copy()
    if hits.empty:
        raise ValueError(f"no correlations computed for {resolved}")
    hits["other"] = hits.apply(
        lambda r: r["symbol_b"] if r["symbol_a"] == resolved else r["symbol_a"], axis=1
    )
    hits = hits.sort_values("correlation", ascending=False)

    rows = [
        [
            str(r["other"]),
            f.num(r["correlation"], 3),
            _label_correlation(r["correlation"]),
            f.num(r.get("observations"), 0),
        ]
        for _, r in hits.head(max(int(top_n), 1)).iterrows()
    ]

    absent = sorted(
        set(data.instruments["symbol"]) - set(hits["other"]) - {resolved}
    )
    missing_note = (
        ""
        if not absent
        else f"\n\nNo correlation is available against {', '.join(absent)} -- too few overlapping "
        "observations to compute one. That is missing data, not a correlation of zero."
    )

    return (
        f"{resolved} correlations ({int(hits.iloc[0]['window_days'])}-day window)\n\n"
        + f.table(["against", "correlation", "reading", "observations"], rows)
        + missing_note
        + "\n\n"
        + f.as_of(hits["as_of_date"].max())
    )


def get_diversification_body(data: PanelData, symbols: str) -> str:
    """How much a set of instruments actually diversifies each other."""
    wanted = [s.strip() for s in str(symbols).replace(";", ",").split(",") if s.strip()]
    if len(wanted) < 2:
        raise ValueError("give at least two symbols, comma-separated, e.g. 'AAPL, XOM, BTC-USD'")

    resolved = [resolve_symbol(data, raw) for raw in wanted]
    frame = data.correlations
    pairs = []
    unavailable = []

    for i, a in enumerate(resolved):
        for b in resolved[i + 1 :]:
            hit = frame[
                ((frame["symbol_a"] == a) & (frame["symbol_b"] == b))
                | ((frame["symbol_a"] == b) & (frame["symbol_b"] == a))
            ]
            if hit.empty:
                unavailable.append(f"{a}/{b}")
                continue
            pairs.append((a, b, float(hit.iloc[0]["correlation"]), hit.iloc[0]["as_of_date"]))

    if not pairs:
        raise ValueError(f"no correlations available between any of {', '.join(resolved)}")

    average = sum(p[2] for p in pairs) / len(pairs)
    most = max(pairs, key=lambda p: p[2])
    least = min(pairs, key=lambda p: p[2])

    reading = (
        "these move closely together; holding all of them is close to holding one"
        if average >= CORR_HIGH
        else "there is real diversification between these"
        if average <= CORR_MODERATE
        else "partial diversification: they share a good deal of common movement"
    )

    lines = [
        ("instruments", ", ".join(resolved)),
        ("pairs measured", f"{len(pairs)} of {len(resolved) * (len(resolved) - 1) // 2}"),
        ("average pairwise correlation", f"{f.num(average, 3)} -- {reading}"),
        ("most correlated pair", f"{most[0]}/{most[1]} at {f.num(most[2], 3)}"),
        ("least correlated pair", f"{least[0]}/{least[1]} at {f.num(least[2], 3)}"),
    ]
    if unavailable:
        lines.append(("no data for", ", ".join(unavailable)))

    return (
        "Diversification across the requested set\n\n"
        + f.fields(lines)
        + "\n\nThis describes co-movement over the measured window. It is not portfolio advice "
        "and says nothing about how much of anything anyone should hold.\n\n"
        + f.as_of(max(p[3] for p in pairs))
    )


# COMMAND ----------


def build_risk_tools(data: PanelData) -> list:
    """The four quant/risk tools, each closing over the already-loaded panel."""

    @safe_tool
    def get_risk_profile(symbol: str) -> str:
        """Risk-adjusted figures for one instrument: return, volatility, Sharpe, Sortino, VaR,
        expected shortfall, max drawdown, beta, skew and kurtosis.

        Args:
            symbol: The instrument, e.g. 'NVDA', 'BTC-USD'.
        """
        return get_risk_profile_body(data, symbol)

    @safe_tool
    def compare_risk(symbols: str) -> str:
        """Compare the risk figures of several instruments in one call.

        Prefer this over calling get_risk_profile once per instrument.

        Args:
            symbols: Comma-separated symbols, e.g. 'AAPL, NVDA, BTC-USD'.
        """
        return compare_risk_body(data, symbols)

    @safe_tool
    def get_correlations(symbol: str, top_n: int = DEFAULT_TOP_N) -> str:
        """What one instrument moves with, most correlated first.

        Args:
            symbol: The instrument.
            top_n: How many counterparts to list. Defaults to 6.
        """
        return get_correlations_body(data, symbol, top_n)

    @safe_tool
    def get_diversification(symbols: str) -> str:
        """How much a set of instruments diversifies each other: average pairwise correlation,
        and the most and least correlated pairs.

        Args:
            symbols: Two or more comma-separated symbols, e.g. 'AAPL, XOM, BTC-USD'.
        """
        return get_diversification_body(data, symbols)

    return [get_risk_profile, compare_risk, get_correlations, get_diversification]


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Every tool called once: an as-of date on each, and no ERROR.

    `ctx` keys used:
      panel  PanelData, real or fixture
    """
    from tools.checks import exercise_tools

    return exercise_tools(
        build_risk_tools(ctx["panel"]),
        {
            "get_risk_profile": {"symbol": "AAPL"},
            "compare_risk": {"symbols": "AAPL, MSFT"},
            "get_correlations": {"symbol": "AAPL", "top_n": 4},
            "get_diversification": {"symbols": "AAPL, MSFT, XOM"},
        },
    )
