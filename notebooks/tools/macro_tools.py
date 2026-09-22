# Databricks notebook source
"""Rates, inflation, labour, activity and risk appetite. Five tools for the macro analyst.

Two rules from `agent-design.md` section 5.2, both mechanical and both easy to get wrong:

**A `percent`-unit series changes in points, everything else in percent.** Unemployment going from
4.0 to 4.4 rose 0.4 points, not 10%. The `unit` column decides, per series, and getting it backwards
produces "inflation rose 43%" out of a move from 2.3 to 3.3.

**Every level comes with its five-year percentile.** "The VIX is 18" is not an answer; "the VIX is
18, the 34th percentile of its five-year range" is. The percentile is computed here from the series
history rather than precomputed in the gold layer, because invariant 1 puts every figure in an
answer behind a deterministic function the trace can show.

Layering: pure. No langchain here; the `@tool` wrapping goes through `tools/safe_tool.py`.
"""

# COMMAND ----------

from __future__ import annotations

import pandas as pd

from tools import formatting as f
from tools.panel_data import PanelData
from tools.safe_tool import safe_tool

# COMMAND ----------

PERCENTILE_YEARS = 5

CATEGORIES = ("rates", "inflation", "labour", "activity", "risk_appetite")

INFLATION_SERIES = ("CPIAUCSL", "CPILFESL", "PCEPI", "T10YIE")

DEFAULT_MONTHS = 24


def _metadata(data: PanelData, series_id: str) -> pd.Series:
    hits = data.macro_series[data.macro_series["series_id"] == series_id]
    if hits.empty:
        raise ValueError(
            f"{series_id!r} is not a series this system holds. Call list_macro_series to see "
            f"what is available"
        )
    return hits.iloc[0]


def _observations(data: PanelData, series_id: str) -> pd.DataFrame:
    hits = data.macro[data.macro["series_id"] == series_id].sort_values("observation_date")
    if hits.empty:
        raise ValueError(f"no observations for {series_id}")
    return hits


def five_year_percentile(observations: pd.DataFrame) -> float | None:
    """Where the latest value sits in its own five-year range, as a fraction in [0, 1]."""
    if observations.empty:
        return None
    newest = observations["observation_date"].max()
    window = observations[
        observations["observation_date"] >= newest - pd.Timedelta(days=365 * PERCENTILE_YEARS)
    ]
    if len(window) < 2:
        return None
    latest = float(window.iloc[-1]["value"])
    return float((window["value"] <= latest).mean())


def change_over(observations: pd.DataFrame, unit: str, periods: int) -> str:
    """The change across `periods` observations, in the convention the unit demands."""
    if len(observations) <= periods:
        return f.NA
    latest = float(observations.iloc[-1]["value"])
    earlier = float(observations.iloc[-1 - periods]["value"])
    if str(unit).lower() == "percent":
        # An interest rate or an unemployment rate moves in points, never in percent of itself.
        return f.points(latest - earlier)
    if earlier == 0:
        return f.NA
    return f.signed_pct(latest / earlier - 1)


def _ordinal(percentile: float | None) -> str:
    if percentile is None:
        return f.NA
    return f"{percentile * 100:.0f}th percentile of its {PERCENTILE_YEARS}-year range"


def _unit_suffix(unit: str) -> str:
    return "%" if str(unit).lower() == "percent" else ""


# COMMAND ----------


def list_macro_series_body(data: PanelData) -> str:
    """Every macro series available, by category."""
    if data.macro_series.empty:
        raise ValueError("no macro series metadata loaded")

    rows = [
        [
            str(r["series_id"]),
            str(r["title"]),
            str(r["category"]),
            str(r["unit"]),
            str(r["frequency"]),
        ]
        for _, r in data.macro_series.sort_values(["category", "series_id"]).iterrows()
    ]
    return (
        f"{len(rows)} macroeconomic series available:\n\n"
        + f.table(["series_id", "title", "category", "unit", "freq"], rows)
        + "\n\n"
        + f.as_of(data.macro["observation_date"].max())
    )


def get_macro_snapshot_body(data: PanelData) -> str:
    """The latest level of every series, grouped by category, each with its percentile."""
    if data.macro_series.empty:
        raise ValueError("no macro series metadata loaded")

    blocks = []
    newest = None
    for category in CATEGORIES:
        members = data.macro_series[data.macro_series["category"] == category]
        if members.empty:
            continue
        rows = []
        for _, meta in members.sort_values("series_id").iterrows():
            try:
                observations = _observations(data, str(meta["series_id"]))
            except ValueError:
                continue
            latest_date = observations["observation_date"].max()
            newest = latest_date if newest is None else max(newest, latest_date)
            unit = str(meta["unit"])
            rows.append(
                [
                    str(meta["series_id"]),
                    f"{f.num(observations.iloc[-1]['value'])}{_unit_suffix(unit)}",
                    change_over(observations, unit, 1),
                    change_over(observations, unit, 12),
                    _ordinal(five_year_percentile(observations)),
                    f.iso(latest_date),
                ]
            )
        if rows:
            blocks.append(
                category.replace("_", " ").upper()
                + "\n"
                + f.table(["series", "latest", "1 period", "12 periods", "percentile", "observed"], rows)
            )

    if not blocks:
        raise ValueError("no macro observations available for any series")

    return (
        "Macroeconomic snapshot\n\n"
        + "\n\n".join(blocks)
        + "\n\nPercent-unit series (rates, unemployment) report changes in points; index and "
        "level series report percent changes.\n\n"
        + f.as_of(newest)
    )


def get_macro_series_body(data: PanelData, series_id: str, months: int = DEFAULT_MONTHS) -> str:
    """One series over time, with its changes and percentile."""
    key = str(series_id).strip().upper()
    meta = _metadata(data, key)
    observations = _observations(data, key)
    unit = str(meta["unit"])

    newest = observations["observation_date"].max()
    window = observations[
        observations["observation_date"] >= newest - pd.Timedelta(days=int(months) * 31)
    ]
    step = max(1, len(window) // 24)
    rows = [
        [f.iso(r["observation_date"]), f"{f.num(r['value'])}{_unit_suffix(unit)}"]
        for _, r in window.iloc[::step].iterrows()
    ]

    return (
        f"{key} -- {meta['title']} ({unit}, {meta['frequency']})\n\n"
        + f.fields(
            [
                ("latest", f"{f.num(observations.iloc[-1]['value'])}{_unit_suffix(unit)}"),
                ("change, 1 period", change_over(observations, unit, 1)),
                ("change, 12 periods", change_over(observations, unit, 12)),
                ("percentile", _ordinal(five_year_percentile(observations))),
            ]
        )
        + "\n\n"
        + f.table(["date", "value"], rows)
        + "\n\n"
        + f.as_of(newest)
    )


def get_yield_curve_body(data: PanelData) -> str:
    """The Treasury curve by tenor, and whether any part of it is inverted."""
    curve = data.macro_series[data.macro_series["curve_tenor_months"].notna()]
    if curve.empty:
        raise ValueError(
            "no constant-maturity Treasury series are configured, so there is no curve to build. "
            "A single tenor is not a curve"
        )

    points_on_curve = []
    newest = None
    for _, meta in curve.sort_values("curve_tenor_months").iterrows():
        try:
            observations = _observations(data, str(meta["series_id"]))
        except ValueError:
            continue
        latest_date = observations["observation_date"].max()
        newest = latest_date if newest is None else max(newest, latest_date)
        points_on_curve.append(
            {
                "series": str(meta["series_id"]),
                "tenor_months": int(meta["curve_tenor_months"]),
                "value": float(observations.iloc[-1]["value"]),
                "change_12": change_over(observations, str(meta["unit"]), 12),
                "percentile": five_year_percentile(observations),
            }
        )

    if len(points_on_curve) < 2:
        raise ValueError("fewer than two tenors have observations; a curve needs at least two")

    rows = [
        [
            p["series"],
            f"{p['tenor_months'] / 12:.2f}y" if p["tenor_months"] >= 12 else f"{p['tenor_months']}m",
            f"{f.num(p['value'])}%",
            p["change_12"],
            _ordinal(p["percentile"]),
        ]
        for p in points_on_curve
    ]

    spreads = []
    by_tenor = {p["tenor_months"]: p["value"] for p in points_on_curve}
    for short, long, label in ((3, 120, "10y minus 3m"), (24, 120, "10y minus 2y")):
        if short in by_tenor and long in by_tenor:
            spread = by_tenor[long] - by_tenor[short]
            state = "INVERTED" if spread < 0 else "positive"
            spreads.append((label, f"{spread:+.2f} pts -- {state}"))

    return (
        "US Treasury yield curve\n\n"
        + f.table(["series", "tenor", "yield", "12-period change", "percentile"], rows)
        + ("\n\n" + f.fields(spreads) if spreads else "")
        + "\n\nYields are percent-unit series, so changes are in points.\n\n"
        + f.as_of(newest)
    )


def get_inflation_picture_body(data: PanelData) -> str:
    """Headline, core, PCE and breakevens together, since no one of them is the answer."""
    available = [s for s in INFLATION_SERIES if not _series_missing(data, s)]
    if not available:
        raise ValueError(
            f"none of the inflation series {list(INFLATION_SERIES)} is configured, so there is no "
            "inflation picture to give"
        )

    rows = []
    newest = None
    for series_id in available:
        meta = _metadata(data, series_id)
        observations = _observations(data, series_id)
        latest_date = observations["observation_date"].max()
        newest = latest_date if newest is None else max(newest, latest_date)
        unit = str(meta["unit"])
        rows.append(
            [
                series_id,
                str(meta["title"])[:42],
                f"{f.num(observations.iloc[-1]['value'])}{_unit_suffix(unit)}",
                change_over(observations, unit, 12),
                _ordinal(five_year_percentile(observations)),
            ]
        )

    absent = [s for s in INFLATION_SERIES if s not in available]
    note = "" if not absent else f"\n\nNot available: {', '.join(absent)}."
    return (
        "Inflation picture\n\n"
        + f.table(["series", "title", "latest", "12-period change", "percentile"], rows)
        + note
        + "\n\nAn index series changes in percent; a breakeven rate is percent-unit and changes "
        "in points. Read the core measure alongside the headline rather than instead of it.\n\n"
        + f.as_of(newest)
    )


def _series_missing(data: PanelData, series_id: str) -> bool:
    return data.macro_series[data.macro_series["series_id"] == series_id].empty


# COMMAND ----------


def build_macro_tools(data: PanelData) -> list:
    """The five macro tools, each closing over the already-loaded panel."""

    @safe_tool
    def list_macro_series() -> str:
        """List every macroeconomic series available, with its category, unit and frequency.

        Call this when you need to know what macro data exists. It takes no arguments.
        """
        return list_macro_series_body(data)

    @safe_tool
    def get_macro_snapshot() -> str:
        """The current macro picture across rates, inflation, labour, activity and risk appetite.

        Each series comes with its recent change and its five-year percentile, which is what
        makes a level meaningful. This is the one to call for a broad macro question. It takes
        no arguments.
        """
        return get_macro_snapshot_body(data)

    @safe_tool
    def get_macro_series(series_id: str, months: int = DEFAULT_MONTHS) -> str:
        """One macroeconomic series over time, with its changes and five-year percentile.

        Args:
            series_id: The FRED series id, e.g. 'UNRATE', 'DGS10', 'VIXCLS'. Use
                list_macro_series if you are unsure.
            months: How far back to show. Defaults to 24.
        """
        return get_macro_series_body(data, series_id, months)

    @safe_tool
    def get_yield_curve() -> str:
        """The US Treasury yield curve by tenor, with the 10y-3m and 10y-2y spreads.

        Use it for questions about rates, the shape of the curve, or inversion. It takes no
        arguments.
        """
        return get_yield_curve_body(data)

    @safe_tool
    def get_inflation_picture() -> str:
        """Headline CPI, core CPI, PCE and market breakevens together, with percentiles.

        Use it for any inflation question: no single one of these series is the answer on its
        own. It takes no arguments.
        """
        return get_inflation_picture_body(data)

    return [
        list_macro_series,
        get_macro_snapshot,
        get_macro_series,
        get_yield_curve,
        get_inflation_picture,
    ]


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Every tool called once, plus the points-versus-percent rule that is easy to invert.

    `ctx` keys used:
      panel  PanelData, real or fixture
    """
    from tools.checks import exercise_tools

    data = ctx["panel"]
    result = exercise_tools(
        build_macro_tools(data),
        {
            "list_macro_series": {},
            "get_macro_snapshot": {},
            "get_macro_series": {"series_id": "UNRATE", "months": 24},
            "get_yield_curve": {},
            "get_inflation_picture": {},
        },
    )

    # The one rule in this family that produces a plausible wrong number rather than an error.
    frame = pd.DataFrame({"value": [4.0, 4.4], "observation_date": pd.to_datetime(["2026-08-01", "2026-09-01"])})
    in_points = change_over(frame, "percent", 1)
    in_percent = change_over(frame, "index", 1)
    if "pts" not in in_points or in_points.strip() != "+0.40 pts":
        raise AssertionError(f"a percent-unit series must change in points; got {in_points!r}")
    if "%" not in in_percent:
        raise AssertionError(f"a non-percent series must change in percent; got {in_percent!r}")

    return {"detail": result["detail"] + f"; unit rule: percent->{in_points}, index->{in_percent}"}
