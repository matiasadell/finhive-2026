# Databricks notebook source
"""The gold tables, read once into pandas, and the shape every tool closes over.

`agent-design.md` section 5.3 is the rule this file implements: the panel loads the gold tables
**once** -- at model load inside the serving container, at stage start in a job -- into a few
megabytes of pandas frames, and the tool closures capture it. That is the difference between a
tool call costing microseconds and costing a Spark job, and between a panel that answers in
seconds and one that does not.

**No tool may issue a query at request time.** The one exception in the whole design is
`search_news`, which reads a Vector Search index (section 13.2).

The reader arrives as an argument. This file never learns what a Spark session is, which is what
lets it load identically in a job and in a serving container (ADR 0001).

Seven frames, not the five of section 5.3: `instruments` and `macro_series` were added when the
data contract was written, because `list_instruments` needs an asset class it cannot infer -- the
planner's routing rules key off it -- and the macro tools need the `unit` that decides whether a
change is reported in points or in percent. See `docs/architecture/agent_data_contract.md`.

Layering: pure. No langchain, no mlflow, no Spark, no Databricks.
"""

# COMMAND ----------

from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime, timedelta, timezone

import pandas as pd

from setup import config
from tools import formatting

# COMMAND ----------

# Which column dates each table, for the freshness check. This is a property of the frame's
# shape, so it lives here rather than in `setup/config.py`, which holds names and values only.
FRESHNESS_COLUMN = {
    config.TABLE_INSTRUMENTS: "as_of",
    config.TABLE_TECHNICAL: "trade_date",
    config.TABLE_RISK: "as_of_date",
    config.TABLE_CORRELATIONS: "as_of_date",
    config.TABLE_FUNDAMENTALS: "as_of_date",
}

DATE_COLUMNS = ("trade_date", "as_of_date", "observation_date", "fiscal_period_end", "as_of")


@dataclass(frozen=True, eq=False)
class PanelData:
    """Every figure the panel can state, in memory. A few megabytes, loaded once."""

    instruments: pd.DataFrame
    technical: pd.DataFrame
    risk: pd.DataFrame
    correlations: pd.DataFrame
    fundamentals: pd.DataFrame
    macro: pd.DataFrame
    macro_series: pd.DataFrame

    @property
    def frames(self) -> dict[str, pd.DataFrame]:
        return {f.name: getattr(self, f.name) for f in dataclass_fields(self)}


# COMMAND ----------


def normalize_dates(frame: pd.DataFrame) -> pd.DataFrame:
    """Parse every known date column, so a tool never compares a string to a timestamp."""
    if frame.empty:
        return frame
    out = frame.copy()
    for column in DATE_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], errors="coerce", utc=False)
            # Tz-aware and naive values cannot be compared; the panel works in naive local dates.
            if getattr(out[column].dtype, "tz", None) is not None:
                out[column] = out[column].dt.tz_localize(None)
    return out


def load_panel_data(read_gold_table, history_years: int | None = None) -> PanelData:
    """Read the seven gold tables through `read_gold_table(name) -> pandas.DataFrame`.

    `history_years` bounds the daily price series so the frames stay small; the table itself
    holds the full history. Filtering happens here rather than in the reader because the reader's
    contract is deliberately one argument (`agent_data_contract.md` section 1) -- if the table
    ever outgrows that, push a predicate into the reader rather than widening this signature.
    """
    years = config.PRICE_HISTORY_YEARS if history_years is None else history_years

    technical = normalize_dates(read_gold_table(config.TABLE_TECHNICAL))
    if not technical.empty and "trade_date" in technical.columns and years:
        cutoff = technical["trade_date"].max() - pd.Timedelta(days=int(years * 365.25))
        technical = technical[technical["trade_date"] >= cutoff]

    return PanelData(
        instruments=normalize_dates(read_gold_table(config.TABLE_INSTRUMENTS)),
        technical=technical.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        if not technical.empty
        else technical,
        risk=normalize_dates(read_gold_table(config.TABLE_RISK)),
        correlations=normalize_dates(read_gold_table(config.TABLE_CORRELATIONS)),
        fundamentals=normalize_dates(read_gold_table(config.TABLE_FUNDAMENTALS)),
        macro=normalize_dates(read_gold_table(config.TABLE_MACRO)),
        macro_series=normalize_dates(read_gold_table(config.TABLE_MACRO_SERIES)),
    )


# COMMAND ----------


def latest_row(frame: pd.DataFrame, symbol: str, date_column: str) -> pd.Series | None:
    """The most recent row for one symbol, or None. The shape most tools start from."""
    if frame.empty or "symbol" not in frame.columns:
        return None
    hits = frame[frame["symbol"] == symbol]
    if hits.empty:
        return None
    if date_column in hits.columns:
        hits = hits.sort_values(date_column)
    return hits.iloc[-1]


def describe(data: PanelData) -> str:
    """One block saying what loaded, how much of it, and how old it is."""
    rows = []
    for name, frame in data.frames.items():
        newest = formatting.NA
        for column in ("trade_date", "as_of_date", "observation_date", "as_of"):
            if column in frame.columns and not frame.empty:
                newest = formatting.iso(frame[column].max())
                break
        symbols = (
            str(frame["symbol"].nunique())
            if "symbol" in frame.columns and not frame.empty
            else formatting.NA
        )
        rows.append([name, f"{len(frame):,}", symbols, newest])
    return formatting.table(["frame", "rows", "symbols", "newest"], rows)


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Every gold table exists, is fresh, and loads into `PanelData`.

    `ctx` keys used:
      read_table  callable(fully qualified name) -> pandas.DataFrame

    **This is the check that carries the dependency on the data engineer.** Until the gold layer
    exists it is red, and that red row is the marker -- every other `tools/` check runs against a
    fixture and stays green, which is what lets the agent be built in parallel with the data.
    """
    read_table = ctx.get("read_table")
    if read_table is None:
        raise AssertionError("no read_table in ctx; the stage must supply one")

    missing: list[str] = []
    for name in config.GOLD_TABLES:
        try:
            read_table(name)
        except Exception as exc:
            missing.append(f"{name} ({type(exc).__name__}: {str(exc)[:80]})")
    if missing:
        raise AssertionError(
            "gold table(s) not readable: "
            + "; ".join(missing)
            + ". See docs/architecture/agent_data_contract.md -- this is the data engineer's "
            "deliverable, and the agent cannot answer a question without it."
        )

    data = load_panel_data(read_table)

    empty = [name for name, frame in data.frames.items() if frame.empty]
    if empty:
        raise AssertionError(f"gold table(s) readable but empty: {empty}")

    today = datetime.now(timezone.utc).replace(tzinfo=None)
    stale: list[str] = []
    for table_name, max_age in config.FRESHNESS_MAX_AGE_DAYS.items():
        column = FRESHNESS_COLUMN.get(table_name)
        frame = {
            config.TABLE_INSTRUMENTS: data.instruments,
            config.TABLE_TECHNICAL: data.technical,
            config.TABLE_RISK: data.risk,
            config.TABLE_CORRELATIONS: data.correlations,
            config.TABLE_FUNDAMENTALS: data.fundamentals,
        }.get(table_name)
        if column is None or frame is None or frame.empty or column not in frame.columns:
            continue
        newest = pd.Timestamp(frame[column].max())
        if newest < pd.Timestamp(today - timedelta(days=max_age)):
            stale.append(f"{table_name.rsplit('.', 1)[-1]} newest {formatting.iso(newest)}")
    if stale:
        raise AssertionError(
            f"gold table(s) stale beyond the agreed threshold: {'; '.join(stale)}. "
            "An answer built on a stale table is worse than no answer, because nothing in it "
            "looks wrong."
        )

    print(describe(data))
    return {
        "detail": (
            f"{len(config.GOLD_TABLES)} tables loaded, "
            f"{data.instruments['symbol'].nunique()} instruments, "
            f"{len(data.technical):,} daily bars, fresh"
        )
    }
