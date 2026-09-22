# Databricks notebook source
"""Running one expert on one canned question, and holding it to the opinion contract.

Every expert's `check` is the same shape: ask it something it should be able to answer, then
require what `OPINION_CONTRACT` requires -- at least one tool call, and a judgement block the
parser in `graph/opinion.py` can actually read.

**It is checked with the real parser, not a second copy of the rules.** A judgement this file
accepted but `graph/opinion.py` could not read would pass a stage and then produce degraded cards
on every question.

The parser arrives as an argument rather than an import, because the dependency direction runs
agents -> graph and never the reverse (`agent-design.md` section 3). Worth naming the tension that
creates: `OPINION_CONTRACT` lives in `agents/base.py` and the parser that reads it lives in
`graph/opinion.py`, on opposite sides of that edge. Nothing at import time stops the two from
drifting apart -- only this check does, and only because a stage hands it the real parser.
"""

# COMMAND ----------

from __future__ import annotations

# COMMAND ----------

RECURSION_LIMIT = 12


def exercise_expert(agent, name: str, question: str, build_card) -> dict:
    """One question through one expert. Raises with the reason, not just a failure.

    `build_card` is `graph.opinion.build_card`, supplied by the stage.
    """
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": RECURSION_LIMIT},
    )
    messages = result.get("messages") or []
    card = build_card(name, messages)

    if card["tool_calls"] == 0:
        raise AssertionError(
            f"{name} answered without calling a tool. Every figure must come from a tool call in "
            "the conversation, so an answer with none is either empty or invented."
        )

    if card["degraded"]:
        raise AssertionError(
            f"{name} produced a judgement block graph/opinion.py could not parse, so every one of "
            f"its cards would be degraded. Last message ended: ...{_tail(messages)!r}"
        )

    undated = [item["ref"] for item in card["evidence"] if not item["as_of"]]
    if undated:
        raise AssertionError(
            f"{name} called tool(s) {undated} whose output carried no as-of date, so that evidence "
            "cannot be aged on a card"
        )

    return {
        "detail": (
            f"{card['tool_calls']} tool call(s) "
            f"({', '.join(sorted({e['ref'] for e in card['evidence']}))}); "
            f"stance={card['stance']} confidence={card['confidence']} horizon={card['horizon']}; "
            f"{len(card['key_findings'])} finding(s)"
        )
    }


def _tail(messages: list) -> str:
    for message in reversed(messages):
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content[-200:]
    return ""
