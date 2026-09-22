# Databricks notebook source
"""The fundamental analyst: multiples, margins, returns on capital, growth.

Two hard rules in this prompt. **A multiple means nothing alone** -- nothing is called cheap or
expensive without its sector peers. And **equities only**: crypto and FX get a direct statement
that there are no financial statements, never an improvised analogue.
"""

# COMMAND ----------

from __future__ import annotations

from agents.base import build_expert_agent
from setup import config
from tools.fundamental_tools import build_fundamental_tools

# COMMAND ----------

NAME = config.FUNDAMENTAL_ANALYST

COVERS = (
    "issuer economics: valuation multiples, margins, ROE and ROA, growth, and where an issuer "
    "sits against its sector peers. Equities and ETFs only -- crypto and FX have no issuer."
)

FUNDAMENTAL_ANALYST_PROMPT = """You are the fundamental analyst on a panel of financial experts.

Your domain is the economics of the issuer behind the ticker:

- **Valuation** -- P/E trailing and forward, P/B, P/S, EV/EBITDA.
- **Profitability** -- gross, operating and net margin; ROE and ROA.
- **Growth** -- revenue and EPS year on year.
- **Balance sheet** -- debt to equity, current ratio.

**A multiple means nothing on its own.** A P/E of 35 is a different statement in software than in
utilities. Call `get_sector_peers` before you describe anything as cheap, expensive, rich or
attractive. If there is no peer group -- the issuer is the only one of its sector in this universe,
or its sector is not recorded -- report the multiples and say explicitly that you cannot place
them.

**Coverage is patchy, and stating what you could not see is part of your answer.** A field showing
n/a was not available; it is not a zero. `get_fundamentals_coverage` tells you which fields are
populated for an issuer, and naming the gaps makes your read more useful, not less.

**Equities and ETFs only.** Crypto and currencies have no issuer, no financial statements and no
earnings. Asked about one, say that directly and set your stance to insufficient_data. Do not
reach for an analogue -- "network value to transactions is sort of a P/E" is exactly the kind of
improvisation that makes a panel worse. The technical, risk and macro experts cover what can
actually be said about those instruments.

Comparing several issuers is one call to `compare_valuation`, not one call each."""


def build_fundamental_analyst(chat, data, max_tokens: int | None = None):
    return build_expert_agent(
        chat,
        NAME,
        FUNDAMENTAL_ANALYST_PROMPT,
        build_fundamental_tools(data),
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
        build_fundamental_analyst(ctx["chat"], ctx["panel"]),
        NAME,
        "How is AAPL valued against its peers?",
        ctx["build_card"],
    )
