# Databricks notebook source
"""The panel: every expert that exists, built once, and what each of them covers.

One place that knows the full roster, so `graph/build.py` wires a panel it is handed and
`graph/planner.py` is told which experts exist rather than hardcoding a list. Adding the news
analyst at step 7 is an entry here and nothing else.

`COVERS` feeds the planner's prompt. It is written for a model deciding whom to ask, not for a
person reading documentation.
"""

# COMMAND ----------

from __future__ import annotations

from agents import fundamental_analyst, macro_analyst, quant_risk_analyst, technical_analyst

# COMMAND ----------

# Fixed order, matching `setup.config.EXPERTS`, so a plan and a results table read the same way
# every run. `news_analyst` joins at step 7, once `gold_news` and its index exist.
BUILDERS = {
    technical_analyst.NAME: technical_analyst.build_technical_analyst,
    quant_risk_analyst.NAME: quant_risk_analyst.build_quant_risk_analyst,
    fundamental_analyst.NAME: fundamental_analyst.build_fundamental_analyst,
    macro_analyst.NAME: macro_analyst.build_macro_analyst,
}

COVERS = {
    technical_analyst.NAME: technical_analyst.COVERS,
    quant_risk_analyst.NAME: quant_risk_analyst.COVERS,
    fundamental_analyst.NAME: fundamental_analyst.COVERS,
    macro_analyst.NAME: macro_analyst.COVERS,
}


def build_panel(chat, data) -> dict:
    """Every expert, built over the already-loaded panel data.

    Built once per graph, not per question: constructing a ReAct agent rebinds its tools and its
    model, and doing that per request would put that cost on every answer.
    """
    return {name: build(chat, data) for name, build in BUILDERS.items()}


def available_experts() -> tuple[str, ...]:
    return tuple(BUILDERS)
