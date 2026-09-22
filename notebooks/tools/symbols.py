# Databricks notebook source
"""Turning what a user wrote into a ticker the data actually has.

**The rule that matters: never near-match.** Answering confidently about the wrong instrument is
worse than failing, because nothing downstream can detect it -- the figures are real, the tools
are honest, and the answer is about a different company. So the resolution chain is exact at every
step, a name-prefix match has to be *unique*, and anything else raises.

The exception is written for the model, not for a log: it names the tool that lists what exists,
because an error that does not say what to do next is how an invented ticker gets answered anyway.

Layering: pure. No langchain, no mlflow, no Spark, no Databricks.
"""

# COMMAND ----------

from __future__ import annotations

# COMMAND ----------

# Written the way people write them, resolved to what the data holds. Keys are uppercase; the
# lookup uppercases first. Extend freely -- an alias is cheaper than a near-match.
ALIASES = {
    # Crypto
    "BITCOIN": "BTC-USD",
    "BTC": "BTC-USD",
    "ETHEREUM": "ETH-USD",
    "ETH": "ETH-USD",
    "SOLANA": "SOL-USD",
    "SOL": "SOL-USD",
    # FX, in both the shapes people type
    "EUR/USD": "EURUSD=X",
    "EURUSD": "EURUSD=X",
    "EUR": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "USDJPY": "USDJPY=X",
    # Indices, including the ETF people usually mean when they name one
    "S&P 500": "^GSPC",
    "S&P500": "^GSPC",
    "SP500": "^GSPC",
    "SPX": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW": "^DJI",
    "DOW JONES": "^DJI",
    "VIX": "^VIX",
    # Companies people name rather than ticker
    "APPLE": "AAPL",
    "MICROSOFT": "MSFT",
    "GOOGLE": "GOOG",
    "ALPHABET": "GOOG",
    "AMAZON": "AMZN",
    "NVIDIA": "NVDA",
    "TESLA": "TSLA",
    "META": "META",
    "FACEBOOK": "META",
}


class UnknownSymbolError(ValueError):
    """The symbol could not be resolved exactly. Never raised for an ambiguous near-match."""


# COMMAND ----------


def known_symbols(data) -> list[str]:
    """Every symbol in the universe, in the order the instruments table holds them."""
    if data.instruments.empty:
        return []
    return [str(s) for s in data.instruments["symbol"].tolist()]


def _unique_name_prefix(data, text: str) -> str | None:
    """The one instrument whose name starts with `text`, or None if zero or several do."""
    if data.instruments.empty or "name" not in data.instruments.columns:
        return None
    names = data.instruments["name"].astype(str).str.upper()
    hits = data.instruments[names.str.startswith(text)]
    if len(hits) == 1:
        return str(hits.iloc[0]["symbol"])
    return None


def resolve_symbol(data, symbol: str) -> str:
    """Resolve a user's wording to a symbol the panel holds, or raise.

    The chain, each step exact:

    1. the uppercased text, if the universe already has it;
    2. the alias table;
    3. `{s}-USD`, which is how crypto is held;
    4. `{s}=X`, which is how FX is held;
    5. a **unique** prefix match on the instrument's full name.

    Two instruments whose names share a prefix resolve to neither. That is deliberate: a coin
    flip between them is the failure this function exists to prevent.
    """
    if not symbol or not str(symbol).strip():
        raise UnknownSymbolError(
            "No symbol was given. Call list_instruments to see what is available, then ask again "
            "with one of those symbols."
        )

    text = str(symbol).strip().upper()
    universe = set(known_symbols(data))

    if text in universe:
        return text

    aliased = ALIASES.get(text)
    if aliased and aliased in universe:
        return aliased

    for candidate in (f"{text}-USD", f"{text}=X"):
        if candidate in universe:
            return candidate

    by_name = _unique_name_prefix(data, text)
    if by_name:
        return by_name

    # Deliberately no "did you mean". A suggestion is a near-match wearing a question mark, and
    # a model reading one under pressure will take it.
    raise UnknownSymbolError(
        f"{symbol!r} is not an instrument this system holds data for. Do not substitute a "
        f"similar one. Call list_instruments to see exactly what is available, and if the "
        f"instrument the user asked about is not there, say so plainly."
    )


def asset_class(data, symbol: str) -> str:
    """The asset class of an already-resolved symbol, or an empty string.

    Tools use it to refuse honestly -- a fundamentals lookup on a currency pair should say why
    there is nothing rather than return an empty table.
    """
    if data.instruments.empty or "asset_class" not in data.instruments.columns:
        return ""
    hits = data.instruments[data.instruments["symbol"] == symbol]
    if hits.empty:
        return ""
    value = hits.iloc[0]["asset_class"]
    return "" if value is None else str(value)


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Aliases and prefixes resolve, and an unknown symbol raises instead of near-matching.

    `ctx` keys used:
      panel  PanelData, real or fixture
    """
    data = ctx["panel"]
    universe = set(known_symbols(data))
    if not universe:
        raise AssertionError("the instruments frame is empty; nothing can resolve")

    resolved: dict[str, str] = {}
    for candidate in sorted(universe)[:3]:
        resolved[candidate] = resolve_symbol(data, candidate.lower())
        if resolved[candidate] != candidate:
            raise AssertionError(f"{candidate} did not resolve to itself")

    alias_hits = 0
    for alias, target in ALIASES.items():
        if target in universe:
            got = resolve_symbol(data, alias.lower())
            if got != target:
                raise AssertionError(f"alias {alias!r} resolved to {got!r}, expected {target!r}")
            alias_hits += 1

    for nonsense in ("ZZZZ", "NOT A TICKER", ""):
        try:
            got = resolve_symbol(data, nonsense)
        except UnknownSymbolError:
            continue
        raise AssertionError(f"{nonsense!r} near-matched to {got!r} instead of raising")

    return {
        "detail": (
            f"{len(universe)} instruments; {alias_hits} aliases resolved; "
            f"3 unknown inputs raised instead of near-matching"
        )
    }
