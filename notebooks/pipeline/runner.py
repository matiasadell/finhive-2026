# Databricks notebook source
"""Run a stage's checks in dependency order and report one row per notebook.

Every module in the tree defines `check(ctx) -> dict`, its own smoke test, and does nothing until
a stage calls it. This is what calls them (`agent-design.md` section 19.1):

- checks run in dependency order, and **a check whose prerequisite is red is skipped**, with the
  table saying which prerequisite blocked it;
- a raised exception is a red row, not the end of the stage, so **one run reports everything that
  is broken** rather than the first thing;
- the stage prints one table -- status, notebook, seconds, detail -- and then fails if any row is
  red. A red check that produces a green run is worse than no check.

`ctx` is a plain dict, deliberately untyped: a typed carrier would have to live somewhere every
layer could import, and there is no such folder below `setup/`, which holds only names and values.
The keys in use:

    profile           str          the widget the stage was started with
    enable_cache      bool         false until the three checks of section 14.1 pass
    chat              callable     `llm.gateway.get_chat_model`
    list_secret_keys  callable     (scope) -> set of key names; never a value
    read_table        callable     (fully qualified name) -> pandas.DataFrame  (from step 2)

This file has no `check` of its own: every stage exercises it.
"""

# COMMAND ----------

from __future__ import annotations

import importlib.util
import time
import traceback
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

# COMMAND ----------

PASS = "pass"
FAIL = "FAIL"
SKIP = "skip"

# The folders that become top-level import names once `notebooks/` is on sys.path.
AGENT_FOLDERS = (
    "setup",
    "llm",
    "tools",
    "agents",
    "graph",
    "guardrails",
    "cache",
    "evaluation",
    "pipeline",
    "serving",
)


class Check(NamedTuple):
    """One notebook's smoke test, and what has to be green before it is worth running."""

    label: str
    fn: Callable[[dict], dict]
    needs: tuple[str, ...] = ()


class Row(NamedTuple):
    label: str
    status: str
    seconds: float
    detail: str


class StageFailed(RuntimeError):
    """At least one check went red. The red row names the red notebook."""


# COMMAND ----------


def check_import_names(root: str) -> dict:
    """Assert every agent folder we own resolves to our own module, not an installed package.

    `llm`, `tools`, `graph`, `agents` and above all `setup` are generic enough to collide -- a
    `tools` distribution and a stray `setup.py` on the path were both observed while this was
    written -- and a collision is silent: the import succeeds and the wrong code runs.
    `agent-design.md` section 3.3 flags it as the cost of composing by top-level name.

    Only folders that exist under `root` can be shadowed, so only those fail. A name we have not
    created yet is reported as `at_risk` instead, which is what makes the warning arrive one step
    before the collision would.

    Returns `{"resolved": {name: origin}, "at_risk": {name: origin}}`.
    """
    base = Path(root)
    normalized = str(base).replace("\\", "/").rstrip("/")
    resolved: dict[str, str] = {}
    at_risk: dict[str, str] = {}
    shadowed: list[str] = []

    for name in AGENT_FOLDERS:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is None:
            continue

        locations = list(spec.submodule_search_locations or [])
        origin = spec.origin or (locations[0] if locations else "")
        where = str(origin).replace("\\", "/")

        if not (base / name).is_dir():
            # Not ours yet. It will be, so say so now rather than at the import that breaks.
            at_risk[name] = where
            continue

        resolved[name] = where
        if not where.startswith(normalized):
            shadowed.append(f"{name} -> {where}")

    if shadowed:
        raise StageFailed(
            "agent folder name(s) shadowed by an installed package: "
            + "; ".join(shadowed)
            + f". Expected them under {normalized}. Put the notebooks root first on sys.path, "
            "or rename the folder."
        )

    for name, where in sorted(at_risk.items()):
        print(
            f"  AT RISK  {name:<12} resolves to {where} -- nothing of ours yet, but it will "
            "shadow the folder the moment that folder exists"
        )

    return {"resolved": resolved, "at_risk": at_risk}


# COMMAND ----------


def _format_table(rows: Sequence[Row], stage: str) -> str:
    label_width = max([len(r.label) for r in rows] + [8])
    header = f"{'status':<6}  {'notebook':<{label_width}}  {'secs':>6}  detail"
    lines = [f"stage: {stage}", "-" * max(len(header), 72), header, "-" * max(len(header), 72)]
    for row in rows:
        lines.append(
            f"{row.status:<6}  {row.label:<{label_width}}  {row.seconds:>6.1f}  {row.detail}"
        )
    lines.append("-" * max(len(header), 72))

    counts = {status: sum(1 for r in rows if r.status == status) for status in (PASS, FAIL, SKIP)}
    lines.append(f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    return "\n".join(lines)


def run_stage(stage: str, checks: Sequence[Check], ctx: dict) -> list[Row]:
    """Run every check it can, print the table, and raise if any row is red."""
    rows: list[Row] = []
    status_by_label: dict[str, str] = {}

    for check in checks:
        blocked = [need for need in check.needs if status_by_label.get(need) != PASS]
        if blocked:
            row = Row(check.label, SKIP, 0.0, f"needs {', '.join(blocked)}")
            rows.append(row)
            status_by_label[check.label] = SKIP
            continue

        started = time.monotonic()
        try:
            result = check.fn(ctx) or {}
            detail = result.get("detail") or ", ".join(f"{k}={v}" for k, v in result.items())
            row = Row(check.label, PASS, time.monotonic() - started, detail)
        except Exception as exc:
            # The traceback goes to the cell output; the table keeps one readable line.
            traceback.print_exc()
            detail = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
            row = Row(check.label, FAIL, time.monotonic() - started, detail)

        rows.append(row)
        status_by_label[check.label] = row.status

    print(_format_table(rows, stage))

    failed = [row.label for row in rows if row.status == FAIL]
    if failed:
        raise StageFailed(f"{stage}: {len(failed)} check(s) red: {', '.join(failed)}")
    return rows
