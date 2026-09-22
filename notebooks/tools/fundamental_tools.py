# Databricks notebook source
"""Valuation and quality. Four tools for the fundamental analyst.

Two rules shape this family, both from `agent-design.md` section 8.2:

**A multiple means nothing alone.** "A P/E of 35 is a different statement in software than in
utilities", so `get_sector_peers` exists and the expert is forbidden from calling anything cheap or
expensive without it. `get_fundamentals` says so in its own output rather than relying on the
prompt to remember.

**Equities only, and say so directly.** Crypto and FX have no issuer and no financial statements.
Asked about one, these tools explain that plainly instead of improvising an analogue or returning
an empty table -- which is what turns a routing mistake into a visible, cheap failure rather than a
wrong answer (section 6).

Coverage from a free fundamentals source is genuinely patchy, which is why
`get_fundamentals_coverage` is a tool of its own: an expert that can state what it could not see
is more useful than one that pads.

Layering: pure. No langchain here; the `@tool` wrapping goes through `tools/safe_tool.py`.
"""

# COMMAND ----------

from __future__ import annotations

import pandas as pd

from tools import formatting as f
from tools.panel_data import PanelData, latest_row
from tools.safe_tool import safe_tool
from tools.symbols import asset_class, resolve_symbol

# COMMAND ----------

NO_FUNDAMENTALS_CLASSES = {"crypto", "fx", "index"}

PEER_MULTIPLES = ("pe_trailing", "pe_forward", "pb", "ps", "ev_ebitda")

CONTEXT_REQUIRED = (
    "A multiple is only meaningful against its sector. Call get_sector_peers before describing "
    "any of these as cheap or expensive."
)


class NoFundamentalsForAssetClass(Exception):
    """Not an error: this asset class has no financial statements and never will.

    Kept distinct from a failure because `safe_tool`'s error text tells the model to try
    different arguments, and for crypto or FX there are none that would work. A structural
    absence is a finding the expert should report, so it travels as output rather than as an
    error (see `_no_fundamentals_statement`).
    """


def _no_fundamentals_statement(data: PanelData, symbol: str, cls: str) -> str:
    """What to say about an instrument that structurally has no fundamentals."""
    return (
        f"{symbol} is classified as {cls}. It has no issuer, no financial statements and no "
        f"earnings, so there are no fundamentals to report -- not now and not in any future run. "
        f"State that directly rather than substituting an analogue; the technical, risk and macro "
        f"experts on this panel cover what can be said about {symbol}.\n\n"
        + f.as_of(data.instruments["as_of"].max() if not data.instruments.empty else None)
    )


def _fundamentals_row(data: PanelData, symbol: str) -> pd.Series:
    cls = asset_class(data, symbol)
    if cls in NO_FUNDAMENTALS_CLASSES:
        raise NoFundamentalsForAssetClass(cls)
    row = latest_row(data.fundamentals, symbol, "as_of_date")
    if row is None:
        raise ValueError(f"no fundamentals row for {symbol}")
    return row


# COMMAND ----------


def get_fundamentals_body(data: PanelData, symbol: str) -> str:
    """Valuation multiples, margins, returns and growth for one issuer."""
    resolved = resolve_symbol(data, symbol)
    try:
        row = _fundamentals_row(data, resolved)
    except NoFundamentalsForAssetClass as absent:
        return _no_fundamentals_statement(data, resolved, str(absent))

    sector = f.NA if f.is_missing(row.get("sector")) else str(row["sector"])
    return (
        f"{resolved} fundamentals -- {sector}"
        + (f" / {row['industry']}" if not f.is_missing(row.get("industry")) else "")
        + "\n\n"
        + f.fields(
            [
                ("market cap", f.money(row.get("market_cap"))),
                ("enterprise value", f.money(row.get("enterprise_value"))),
                ("P/E trailing / forward", f"{f.num(row.get('pe_trailing'))} / {f.num(row.get('pe_forward'))}"),
                ("P/B", f.num(row.get("pb"))),
                ("P/S", f.num(row.get("ps"))),
                ("EV/EBITDA", f.num(row.get("ev_ebitda"))),
                ("gross / operating / net margin", f"{f.pct(row.get('gross_margin'))} / {f.pct(row.get('operating_margin'))} / {f.pct(row.get('net_margin'))}"),
                ("ROE / ROA", f"{f.pct(row.get('roe'))} / {f.pct(row.get('roa'))}"),
                ("revenue growth YoY", f.signed_pct(row.get("revenue_growth_yoy"))),
                ("EPS growth YoY", f.signed_pct(row.get("eps_growth_yoy"))),
                ("debt/equity, current ratio", f"{f.num(row.get('debt_to_equity'))}, {f.num(row.get('current_ratio'))}"),
                ("dividend yield", f.pct(row.get("dividend_yield"))),
                ("statements through", f.iso(row.get("fiscal_period_end"))),
            ]
        )
        + f"\n\n{CONTEXT_REQUIRED}\n"
        + "Any field showing n/a was not available from the source; report it as unavailable "
        "rather than treating it as zero.\n\n"
        + f.as_of(row.get("as_of_date"))
    )


def compare_valuation_body(data: PanelData, symbols: str) -> str:
    """Several issuers' multiples side by side, in one call."""
    wanted = [s.strip() for s in str(symbols).replace(";", ",").split(",") if s.strip()]
    if not wanted:
        raise ValueError("no symbols given; pass them comma-separated, e.g. 'AAPL, MSFT'")

    rows = []
    skipped = []
    newest = None
    for raw in wanted:
        resolved = resolve_symbol(data, raw)
        try:
            row = _fundamentals_row(data, resolved)
        except (ValueError, NoFundamentalsForAssetClass):
            skipped.append(f"{resolved} ({asset_class(data, resolved) or 'no data'})")
            continue
        newest = row["as_of_date"] if newest is None else max(newest, row["as_of_date"])
        rows.append(
            [
                resolved,
                f.NA if f.is_missing(row.get("sector")) else str(row["sector"]),
                f.num(row.get("pe_trailing")),
                f.num(row.get("pe_forward")),
                f.num(row.get("pb")),
                f.num(row.get("ev_ebitda")),
                f.pct(row.get("net_margin")),
                f.pct(row.get("roe")),
            ]
        )

    if not rows:
        raise ValueError(
            f"none of {', '.join(wanted)} has fundamentals: {', '.join(skipped)}. "
            "Say so rather than comparing something else"
        )

    note = "" if not skipped else f"\n\nNo fundamentals for {', '.join(skipped)}; left out."
    return (
        f"Valuation comparison of {len(rows)} issuers\n\n"
        + f.table(["symbol", "sector", "P/E", "fwd P/E", "P/B", "EV/EBITDA", "net margin", "ROE"], rows)
        + note
        + f"\n\n{CONTEXT_REQUIRED}\n\n"
        + f.as_of(newest)
    )


def get_sector_peers_body(data: PanelData, symbol: str) -> str:
    """The instrument's sector, its peers, and where it sits against their median multiples."""
    resolved = resolve_symbol(data, symbol)
    try:
        row = _fundamentals_row(data, resolved)
    except NoFundamentalsForAssetClass as absent:
        return _no_fundamentals_statement(data, resolved, str(absent))

    sector = row.get("sector")
    if f.is_missing(sector):
        raise ValueError(
            f"{resolved} has no sector recorded, so there is no peer group to compare it against. "
            "Report the multiples without a cheap/expensive judgement"
        )

    peers = data.fundamentals[
        (data.fundamentals["sector"] == sector) & (data.fundamentals["symbol"] != resolved)
    ]
    if peers.empty:
        raise ValueError(
            f"{resolved} is the only {sector} issuer in this universe, so there is no peer median "
            "to compare against. Say that rather than calling it cheap or expensive"
        )

    rows = []
    for multiple in PEER_MULTIPLES:
        if multiple not in data.fundamentals.columns:
            continue
        own = row.get(multiple)
        median = peers[multiple].median(skipna=True)
        if f.is_missing(own) or f.is_missing(median):
            rows.append([multiple, f.num(own), f.num(median), f.NA])
            continue
        gap = own / median - 1
        reading = "above peers" if gap > 0.05 else "below peers" if gap < -0.05 else "in line"
        rows.append([multiple, f.num(own), f.num(median), f"{f.signed_pct(gap)} {reading}"])

    peer_list = ", ".join(sorted(peers["symbol"].astype(str)))
    return (
        f"{resolved} against its {sector} peers ({len(peers)}: {peer_list})\n\n"
        + f.table(["multiple", resolved, "peer median", "gap"], rows)
        + "\n\nA gap against the peer median is context, not a verdict: a premium can be earned "
        "by growth or margins, both of which are in get_fundamentals.\n\n"
        + f.as_of(row.get("as_of_date"))
    )


def get_fundamentals_coverage_body(data: PanelData, symbol: str) -> str:
    """Which fields are actually populated for this issuer, and which are not."""
    resolved = resolve_symbol(data, symbol)
    try:
        row = _fundamentals_row(data, resolved)
    except NoFundamentalsForAssetClass as absent:
        return _no_fundamentals_statement(data, resolved, str(absent))

    ignore = {"symbol", "as_of_date", "as_of", "currency", "fiscal_period_end"}
    present, absent = [], []
    for column in data.fundamentals.columns:
        if column in ignore:
            continue
        (absent if f.is_missing(row.get(column)) else present).append(column)

    coverage = len(present) / max(len(present) + len(absent), 1)
    return (
        f"{resolved} fundamentals coverage: {len(present)} of {len(present) + len(absent)} "
        f"fields populated ({f.pct(coverage)})\n\n"
        + f.fields(
            [
                ("available", ", ".join(present) or "none"),
                ("not available", ", ".join(absent) or "none"),
            ]
        )
        + "\n\nState the unavailable fields as limits of your read. An expert that admits what it "
        "could not see is more useful than one that pads.\n\n"
        + f.as_of(row.get("as_of_date"))
    )


# COMMAND ----------


def build_fundamental_tools(data: PanelData) -> list:
    """The four fundamental tools, each closing over the already-loaded panel."""

    @safe_tool
    def get_fundamentals(symbol: str) -> str:
        """Valuation multiples, margins, returns on capital and growth for one issuer.

        Equities and ETFs only. Crypto and FX have no financial statements.

        Args:
            symbol: The instrument, e.g. 'AAPL', 'MSFT'.
        """
        return get_fundamentals_body(data, symbol)

    @safe_tool
    def compare_valuation(symbols: str) -> str:
        """Compare several issuers' multiples and margins in one call.

        Prefer this over calling get_fundamentals once per issuer.

        Args:
            symbols: Comma-separated symbols, e.g. 'AAPL, MSFT, NVDA'.
        """
        return compare_valuation_body(data, symbols)

    @safe_tool
    def get_sector_peers(symbol: str) -> str:
        """The issuer's sector peers and where its multiples sit against their medians.

        Call this before describing any multiple as cheap or expensive: a P/E of 35 means
        something different in software than in utilities.

        Args:
            symbol: The instrument.
        """
        return get_sector_peers_body(data, symbol)

    @safe_tool
    def get_fundamentals_coverage(symbol: str) -> str:
        """Which fundamental fields are populated for this issuer and which are missing.

        Use it to state the limits of your read rather than guessing at a missing figure.

        Args:
            symbol: The instrument.
        """
        return get_fundamentals_coverage_body(data, symbol)

    return [get_fundamentals, compare_valuation, get_sector_peers, get_fundamentals_coverage]


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Every tool called once: an as-of date on each, and no ERROR.

    `ctx` keys used:
      panel  PanelData, real or fixture
    """
    from tools.checks import exercise_tools

    data = ctx["panel"]
    result = exercise_tools(
        build_fundamental_tools(data),
        {
            "get_fundamentals": {"symbol": "AAPL"},
            "compare_valuation": {"symbols": "AAPL, MSFT"},
            "get_sector_peers": {"symbol": "AAPL"},
            "get_fundamentals_coverage": {"symbol": "AAPL"},
        },
    )

    # The refusal path is the one that keeps a routing mistake cheap, so it is checked too.
    tools = {t.name: t for t in build_fundamental_tools(data)}
    crypto = tools["get_fundamentals"].invoke({"symbol": "BTC-USD"})
    if crypto.startswith("ERROR in "):
        raise AssertionError(
            "get_fundamentals on crypto came back as an error. A structural absence is a finding, "
            "not a failure -- the error text tells the model to try different arguments, and there "
            f"are none. Got: {crypto[:200]}"
        )
    if "no issuer" not in crypto or "as of " not in crypto:
        raise AssertionError(f"crypto statement is not the expected shape: {crypto[:200]}")

    return {"detail": result["detail"] + "; crypto answered structurally, not as an error"}
