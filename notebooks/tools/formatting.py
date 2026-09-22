# Databricks notebook source
"""How a tool renders a number, a date and its own as-of line.

This file exists because two of the rules in `agent-design.md` section 5.1 are contracts rather
than style, and duplicating either across four domain files is how they drift:

**"as of YYYY-MM-DD" is parsed, not decorative.** `graph/opinion.py` regex-scans tool output for
exactly `as of (\\d{4}-\\d{2}-\\d{2})` to date the evidence on an OpinionCard. A tool that writes
"as of Sept 19" produces a card with no date and evidence nobody can age.

**A missing value renders `n/a`, never `0`.** A zero is a number a model will reason about, and
"trading at 0x earnings" is the shape that mistake takes in an answer.

Output is labelled prose, never JSON: the consumer is a language model, and text with units
survives truncation better than nested structure.

Layering: pure. No langchain, no mlflow, no Spark, no Databricks.
"""

# COMMAND ----------

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

import pandas as pd

# COMMAND ----------

NA = "n/a"


def is_missing(value: Any) -> bool:
    """True for None, NaN, NaT and the empty string."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def num(value: Any, digits: int = 2, suffix: str = "") -> str:
    """A number, or `n/a`. Never zero-fills a missing value."""
    if is_missing(value):
        return NA
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return NA


def pct(value: Any, digits: int = 1) -> str:
    """A decimal fraction rendered as a percentage: `0.184` -> `18.4%`."""
    if is_missing(value):
        return NA
    try:
        return f"{float(value) * 100:,.{digits}f}%"
    except (TypeError, ValueError):
        return NA


def points(value: Any, digits: int = 2) -> str:
    """An absolute change in points, signed. What a `percent`-unit series reports."""
    if is_missing(value):
        return NA
    try:
        return f"{float(value):+,.{digits}f} pts"
    except (TypeError, ValueError):
        return NA


def signed_pct(value: Any, digits: int = 1) -> str:
    if is_missing(value):
        return NA
    try:
        return f"{float(value) * 100:+,.{digits}f}%"
    except (TypeError, ValueError):
        return NA


def money(value: Any) -> str:
    """A large figure with a magnitude suffix: `3_420_000_000_000` -> `3.42T`."""
    if is_missing(value):
        return NA
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return NA
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(amount) >= threshold:
            return f"{amount / threshold:,.2f}{suffix}"
    return f"{amount:,.2f}"


# COMMAND ----------


def iso(value: Any) -> str:
    """A date as `YYYY-MM-DD`, or `n/a`."""
    if is_missing(value):
        return NA
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return NA


def as_of(value: Any) -> str:
    """The dating contract. `graph/opinion.py` parses exactly this.

    A missing date is stated rather than omitted: an expert that cannot see how old a figure is
    should say so, and a silently undated figure reads as current.
    """
    rendered = iso(value)
    if rendered == NA:
        return "as of an unknown date (the source carried no timestamp)"
    return f"as of {rendered}"


# COMMAND ----------


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """A fixed-width labelled table. Readable to a model, and to a person reading a trace."""
    if not rows:
        return "(no rows)"
    widths = [
        max(len(str(headers[i])), *(len(str(row[i])) for row in rows))
        for i in range(len(headers))
    ]
    lines = ["  ".join(str(headers[i]).ljust(widths[i]) for i in range(len(headers)))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def fields(pairs: Sequence[tuple[str, str]]) -> str:
    """A label/value block, one per line, aligned."""
    if not pairs:
        return "(nothing to report)"
    width = max(len(label) for label, _ in pairs)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in pairs)
