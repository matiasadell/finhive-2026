# Databricks notebook source
"""Assembly: the nodes, the fan-out, the join, and one compiled graph.

Every node factory takes what it needs as an argument. That is what makes the same graph buildable
from a notebook, from a verification stage and from inside the serving container with no ambient
state -- and it is the whole reason `build_graph` has a signature instead of reading globals.

Three things in here are load-bearing and easy to get subtly wrong:

- **`run_expert -> synthesizer` is the only static edge.** It fires once per superstep, not once
  per `Send`, which is what joins five parallel experts into one synthesis.
- **Everything else routes by `Command(goto=...)`**, so a node decides its own successor and the
  graph has no conditional-edge tables to keep in step with the nodes.
- **`run_expert` never raises.** One expert failing becomes a `failed_card` and the panel goes on.
  An exception here would take the other four with it.
"""

# COMMAND ----------

from __future__ import annotations

from langgraph.graph import START, StateGraph

from agents.panel import COVERS, build_panel
from graph.cache_node import make_cache_lookup, make_cache_store
from graph.opinion import build_card, failed_card
from graph.planner import make_planner_node
from graph.state import FinHiveState
from graph.synthesizer import make_synthesizer_node
from guardrails.input_guardrail import make_input_guardrail
from guardrails.output_guardrail import make_output_guardrail
from setup import config

# COMMAND ----------

# What the expert is actually asked. It gets the reader's original question for context but never
# the other experts' work -- independence is what makes agreement between them mean anything.
EXPERT_PROMPT = "{sub_question}\n\n(The reader's original question was: {question})"

# High enough for three drafts and a full fan-out, low enough that a routing loop stops.
EXPERT_RECURSION_LIMIT = 12


def make_expert_node(panel: dict):
    """Run one expert on one sub-question and return exactly one card."""

    def run_expert(task: dict) -> dict:
        expert = task.get("expert", "")
        agent = panel.get(expert)
        if agent is None:
            return {"cards": [failed_card(expert, f"no such expert in this deployment: {expert!r}")]}

        prompt = EXPERT_PROMPT.format(
            sub_question=task.get("sub_question", ""), question=task.get("question", "")
        )
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": EXPERT_RECURSION_LIMIT},
            )
            card = build_card(expert, result.get("messages") or [])
        except Exception as exc:
            # One expert failing must never take the panel with it.
            card = failed_card(expert, f"{type(exc).__name__}: {exc}")

        return {"cards": [card]}

    return run_expert


# COMMAND ----------


def build_graph(data, chat, cache=None, checkpointer=None):
    """Compile the graph. `data` is a `PanelData`; `chat` is `llm.gateway.get_chat_model`."""
    panel = build_panel(chat, data)
    covers = {name: COVERS[name] for name in panel}

    builder = StateGraph(FinHiveState)
    builder.add_node(config.NODE_INPUT_GUARDRAIL, make_input_guardrail(chat))
    builder.add_node(config.NODE_CACHE_LOOKUP, make_cache_lookup(cache, chat))
    builder.add_node(config.NODE_PLANNER, make_planner_node(chat, data, covers))
    builder.add_node(config.NODE_RUN_EXPERT, make_expert_node(panel))
    builder.add_node(config.NODE_SYNTHESIZER, make_synthesizer_node(chat))
    builder.add_node(config.NODE_OUTPUT_GUARDRAIL, make_output_guardrail(chat))
    builder.add_node(config.NODE_CACHE_STORE, make_cache_store(cache, chat))

    builder.add_edge(START, config.NODE_INPUT_GUARDRAIL)
    # The join. Once per superstep, not once per Send.
    builder.add_edge(config.NODE_RUN_EXPERT, config.NODE_SYNTHESIZER)

    return builder.compile(checkpointer=checkpointer)


def ask(graph, question: str, max_concurrency: int | None = None) -> dict:
    """One question through a compiled graph, with the limits section 12 fixes."""
    return graph.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={
            "max_concurrency": max_concurrency or config.DEFAULT_MAX_CONCURRENCY,
            "recursion_limit": config.RECURSION_LIMIT,
        },
    )


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Three questions end to end: an ordinary one, an advice request, and a crypto one.

    `ctx` keys used:
      chat   callable(role) -> chat model
      panel  PanelData, real or fixture
    """
    from guardrails.output_guardrail import DISCLAIMER
    from llm.parsing import message_text

    graph = build_graph(ctx["panel"], ctx["chat"])

    ordinary = ask(graph, "What do you think of Apple at the moment?")
    if ordinary.get("blocked"):
        raise AssertionError("an ordinary research question was blocked")
    if not ordinary.get("cards"):
        raise AssertionError("no cards came back; the fan-out or the reducer is wrong")
    if len(ordinary["cards"]) != len(ordinary["plan"]["experts"]):
        raise AssertionError(
            f"the planner convened {ordinary['plan']['experts']} but "
            f"{len(ordinary['cards'])} card(s) arrived. Without the operator.add reducer on "
            "`cards`, parallel experts overwrite each other"
        )
    if not ordinary.get("consensus", {}).get("label"):
        raise AssertionError("no consensus was computed")

    answer = message_text(ordinary["messages"][-1])
    if DISCLAIMER not in answer:
        raise AssertionError("the final message must carry the disclaimer, appended in code")
    if answer.count(DISCLAIMER) != 1:
        raise AssertionError("the disclaimer is stacked; it belongs on the final message only")
    if ordinary["messages"][-1].id != config.FINAL_ANSWER_ID:
        raise AssertionError(
            "the answer must be messages[-1] under FINAL_ANSWER_ID, so a consumer reading only "
            "the last message never sees an earlier draft"
        )
    drafts = int(ordinary.get("synthesis_attempts") or 0)

    advice = ask(graph, "Should I put half my savings into NVDA?")
    if not advice.get("blocked"):
        raise AssertionError("a personal advice request reached the panel")
    if advice.get("cards"):
        raise AssertionError("a blocked question must not convene anyone")

    crypto = ask(graph, "What do you think of Bitcoin right now?")
    if config.FUNDAMENTAL_ANALYST in (crypto.get("plan") or {}).get("experts", []):
        raise AssertionError("a crypto question convened the fundamental analyst")

    return {
        "detail": (
            f"ordinary: {len(ordinary['cards'])} cards, consensus "
            f"{ordinary['consensus']['label']} at {ordinary['consensus']['agreement_pct']}%, "
            f"{drafts} draft(s), disclaimer once; advice blocked "
            f"({advice.get('block_reason', '')[:40]}); crypto -> {crypto['plan']['experts']}"
        )
    }
