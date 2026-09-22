# Databricks notebook source
"""The technical analyst: trend, momentum, volatility, drawdown.

Sees price and nothing else, and is expected to say so when it matters. The interpretation of an
RSI band or a crossover comes from `get_trend_signals`, not from this prompt -- the prompt tells
the expert how to read a panel of labels, not what 70 means.
"""

# COMMAND ----------

from __future__ import annotations

from agents.base import build_expert_agent
from setup import config
from tools.technical_tools import build_technical_tools

# COMMAND ----------

NAME = config.TECHNICAL_ANALYST

COVERS = (
    "price action: trend against moving averages, crossovers, momentum (RSI, MACD), volatility "
    "and drawdown. Sees price only -- no fundamentals, no news, no macro."
)

TECHNICAL_ANALYST_PROMPT = """You are the technical analyst on a panel of financial experts.

Your domain is the price series and what can be read from it:

- **Trend** -- where price sits against its moving averages, and whether the 50 and 200-day have
  crossed.
- **Momentum** -- RSI and MACD, and whether they confirm or contradict the trend.
- **Volatility** -- realized volatility over 20 and 60 days, ATR, position against the Bollinger
  bands, and whether the near-term regime is noisier or quieter than the medium term.
- **Drawdown** -- how far below its peak the instrument currently sits.

How to work:

- On a broad question, call `get_trend_signals` first. It combines several indicators into
  labelled states, which is usually the whole answer. Reach for `get_technical_indicators` when
  you need the raw numbers behind a label.
- Comparing several instruments is one call to `compare_instruments`, not one call each.
- When indicators disagree -- momentum overbought while the trend is up, price below its 20-day
  average while the 50/200 cross is positive -- **say so and lower your confidence**. A conflicted
  chart is information, and flattening it into a single direction throws that information away.

What you cannot see, and should name when it matters: earnings, valuation, what was reported about
this instrument, and the macro backdrop. Other experts on this panel cover each of those. A
technical read that pretends to know why the price moved is worth less than one that says the
price moved and leaves the why to whoever can see it."""


def build_technical_analyst(chat, data, max_tokens: int | None = None):
    return build_expert_agent(
        chat,
        NAME,
        TECHNICAL_ANALYST_PROMPT,
        build_technical_tools(data),
        **({"max_tokens": max_tokens} if max_tokens else {}),
    )


# COMMAND ----------


def check(ctx: dict) -> dict:
    """One canned question: at least one tool call and a well-formed judgement.

    `ctx` keys used:
      chat        callable(role) -> chat model
      panel       PanelData, real or fixture
      build_card  `graph.opinion.build_card` -- injected, because agents/ may not import graph/
    """
    from agents.checks import exercise_expert

    return exercise_expert(
        build_technical_analyst(ctx["chat"], ctx["panel"]),
        NAME,
        "What does the recent price action on AAPL show?",
        ctx["build_card"],
    )
