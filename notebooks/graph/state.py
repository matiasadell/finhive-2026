# Databricks notebook source
"""The state every node reads and writes.

**`cards` carries a reducer, and that reducer is what makes the fan-out work.** Five experts run
in one superstep; without `operator.add` each would overwrite the others' write to the same key
and four opinions would vanish silently. It is the single most load-bearing line in this file.

**The state carries the route, not just the answer** (`agent-design.md` section 12): who was
consulted and why, how much they agreed, whether the answer verified. A trace has to explain an
answer, not merely contain it -- so anything that cannot be reconstructed from the trace belongs
here.

The node names and `FINAL_ANSWER_ID` live in `setup/config.py` instead: `guardrails/` routes by
them too, and the dependency direction runs guardrails -> graph, never the reverse.

There is no `check` in this file: it is types only, exercised by every node that uses them.
"""

# COMMAND ----------

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import MessagesState

# COMMAND ----------

class ExpertTask(TypedDict):
    """The payload of one `Send`. An expert never sees the other experts' work."""

    expert: str
    sub_question: str
    question: str


class FinHiveState(MessagesState):
    """Everything the graph knows about one question."""

    # Input guardrail
    blocked: bool
    block_reason: str

    # Planner
    plan: dict[str, Any]

    # The panel. `operator.add` is what lets parallel branches append to one list instead of
    # racing to overwrite it.
    cards: Annotated[list[dict[str, Any]], operator.add]

    # Computed in code, never by a model.
    consensus: dict[str, Any]

    # Output guardrail
    grounded: bool
    groundedness_reason: str
    unsupported: list[str]
    synthesis_attempts: int

    # Semantic cache (step 9; the key exists so no node has to special-case its absence)
    cache_hit: bool
