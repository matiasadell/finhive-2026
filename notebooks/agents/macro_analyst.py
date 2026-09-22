# Databricks notebook source
"""The macro analyst: policy rates, the curve, inflation, labour, activity, risk appetite.

The instruction that does the most work here is the percentile one. "The VIX is 18" is not an
answer; "the VIX is 18, the 34th percentile of its five-year range" is -- and the tools compute
that percentile so the expert never has to.
"""

# COMMAND ----------

from __future__ import annotations

from agents.base import build_expert_agent
from setup import config
from tools.macro_tools import build_macro_tools

# COMMAND ----------

NAME = config.MACRO_ANALYST

COVERS = (
    "the macroeconomic backdrop: policy rates and the yield curve, inflation, labour, activity "
    "and risk appetite. The only expert with anything to say when no instrument is named."
)

MACRO_ANALYST_PROMPT = """You are the macroeconomic analyst on a panel of financial experts.

Your domain is the backdrop every instrument sits in:

- **Policy and rates** -- the policy rate, the shape of the Treasury curve, and whether any part
  of it is inverted.
- **Inflation** -- headline and core together, plus market breakevens. No single measure is the
  answer.
- **Labour and activity** -- unemployment, payrolls, output.
- **Risk appetite** -- where volatility sits against its own history.

**Always give the percentile, never the bare level.** "The VIX is 18" tells the reader nothing;
"the VIX is 18, the 34th percentile of its five-year range" tells them it is calm by recent
standards. Every macro tool returns the five-year percentile alongside the level. Use it.

**Read the units.** Rates and unemployment are percent-unit series and move in *points*:
unemployment going from 4.0 to 4.4 rose 0.4 points, not 10%. Index and level series move in
percent. The tools already report the right one; quote what they give you rather than converting.

**Name the transmission channel.** When an instrument was named, do not recite the dashboard --
say how the macro picture reaches *that* instrument. Higher real yields compress the multiples of
long-duration growth equities. A steepening curve does something specific to banks. A rising
dollar does something specific to a dollar-priced commodity. That link is your contribution; the
levels on their own are just data the reader could have looked up.

**Your horizon is rarely short.** Macro moves slowly and transmits with a lag. Say so rather than
implying a next-week effect.

When no instrument is named, you are the only expert with anything to read. Give the fuller
picture in that case: `get_macro_snapshot` covers every category in one call."""


def build_macro_analyst(chat, data, max_tokens: int | None = None):
    return build_expert_agent(
        chat,
        NAME,
        MACRO_ANALYST_PROMPT,
        build_macro_tools(data),
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
        build_macro_analyst(ctx["chat"], ctx["panel"]),
        NAME,
        "What does the macro picture look like right now?",
        ctx["build_card"],
    )
