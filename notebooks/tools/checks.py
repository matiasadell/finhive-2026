# Databricks notebook source
"""Calling every tool in a family once, and holding it to its contract.

The four domain tool notebooks have the same `check`: call each tool on a fixed symbol, and
require an as-of date and no error. Written once here so that the contract is enforced identically
for all nineteen -- a per-file copy is how one of them quietly stops dating its output.

**The regex is the one `graph/opinion.py` will use.** If a tool passes here it can be dated on an
OpinionCard; if it does not, the evidence on that card carries no date and nothing downstream can
tell how old the figure is.

Layering: pure. Tools are invoked through their runnable interface, so nothing here imports
LangChain either.
"""

# COMMAND ----------

from __future__ import annotations

import re

# Exactly what evidence extraction will look for. Keep the two in step.
AS_OF_PATTERN = re.compile(r"as of (\d{4}-\d{2}-\d{2})")

ERROR_PREFIX = "ERROR in "

# Output long enough to be a table but short enough not to blow a worker's context.
MIN_REASONABLE_LENGTH = 40


def exercise_tools(tools: list, arguments: dict[str, dict]) -> dict:
    """Invoke each tool once and assert the shared contract. Raises on the first real problem."""
    by_name = {tool.name: tool for tool in tools}

    unexpected = sorted(set(by_name) - set(arguments))
    if unexpected:
        raise AssertionError(f"no probe arguments defined for {unexpected}")
    missing = sorted(set(arguments) - set(by_name))
    if missing:
        raise AssertionError(f"probe arguments given for tools that do not exist: {missing}")

    dated: list[str] = []
    for name, args in arguments.items():
        output = by_name[name].invoke(args)

        if not isinstance(output, str):
            raise AssertionError(f"{name} returned {type(output).__name__}, expected a string")
        if output.startswith(ERROR_PREFIX):
            # safe_tool turned an exception into an instruction, which is correct behaviour and
            # a failed check: the tool was called the way an expert would call it.
            raise AssertionError(f"{name} failed on its own probe arguments -- {output[:220]}")
        if len(output) < MIN_REASONABLE_LENGTH:
            raise AssertionError(f"{name} returned {len(output)} characters: {output!r}")

        match = AS_OF_PATTERN.search(output)
        if not match:
            raise AssertionError(
                f"{name} output carries no 'as of YYYY-MM-DD' date. graph/opinion.py parses that "
                f"exact pattern to date evidence, so this output cannot be aged. Got: "
                f"...{output[-160:]!r}"
            )
        dated.append(match.group(1))

    return {
        "detail": (
            f"{len(arguments)} tools called, all dated, none errored "
            f"(newest as-of {max(dated)}, oldest {min(dated)})"
        )
    }
