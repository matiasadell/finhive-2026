# Databricks notebook source
"""The wrapper that turns an exception into an instruction.

An expert that calls a tool and gets a stack trace, or nothing, invents a number. So every tool
failure returns a sentence that says what happened **and what to do next**:

    ERROR in get_fundamentals: no row for BTC-USD. This data is not available right now. Try a
    different tool or different arguments. If there is no alternative, tell the user this specific
    figure could not be retrieved -- do not estimate it.

The last clause is the one that matters. An error that stops at "not available" leaves the model
to fill the gap, and it will.

This is **the one file under `tools/` allowed to import LangChain**
(`agent-design.md` section 3.3). Every domain tool goes through `safe_tool`, so no other file
there needs to know what a `BaseTool` is -- which is also what keeps the bodies pure and testable
without a framework.

There is no `check` here: every domain tools notebook exercises this file.
"""

# COMMAND ----------

from __future__ import annotations

import functools
from typing import Callable

from langchain_core.tools import BaseTool, tool

# COMMAND ----------

ERROR_TEMPLATE = (
    "ERROR in {name}: {error}. This data is not available right now. Try a different tool or "
    "different arguments. If there is no alternative, tell the user this specific figure could "
    "not be retrieved -- do not estimate it."
)

# An empty string reaching a model is indistinguishable from "there is nothing to say", which is
# a different statement from "the tool returned nothing". Say which.
EMPTY_TEMPLATE = (
    "{name} returned no content. Treat this as missing data, not as an empty result: report that "
    "the figure could not be retrieved rather than inferring one."
)


def safe_tool(fn: Callable[..., str]) -> BaseTool:
    """Wrap a body as a LangChain tool whose failures are instructions.

    **The docstring is what the model reads.** `tool()` takes the wrapped function's docstring as
    the tool description and its annotations as the argument schema, so a body with a vague
    docstring is a tool the model will misuse. Write it for the model.
    """
    if not (fn.__doc__ or "").strip():
        # Caught here rather than at the first question, where it would present as the model
        # choosing tools badly for no visible reason.
        raise ValueError(
            f"{fn.__name__} has no docstring. The docstring is the tool description the model "
            "reads; a tool without one cannot be chosen correctly."
        )

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            # Everything, deliberately. A tool that raises takes its expert's turn with it, and
            # one expert failing must never take the panel with it.
            message = (str(exc).strip() or type(exc).__name__).rstrip(".")
            return ERROR_TEMPLATE.format(name=fn.__name__, error=message)

        text = "" if result is None else str(result)
        if not text.strip():
            return EMPTY_TEMPLATE.format(name=fn.__name__)
        return text

    return tool(wrapper)
