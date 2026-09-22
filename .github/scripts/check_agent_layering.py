"""Enforce the agent tree's layering rules by reading real `import` statements.

`ARCHITECTURE_V2.md` section 3.1 enforces layering on the Databricks side by parsing `%run`
targets. The agent tree does not use `%run` at all (ADR 0001): its modules are plain Python
imported by name, because `%run` does not exist inside a Model Serving container. So the same
rule needs a different reader, and `agent-design.md` section 3.3 says what it has to check.

Four passes, all over the AST:

1. Direction   -- a folder may only import the folders below it.
2. Third party -- `setup/` is standard library only, `llm/parsing.py` adds pydantic; `tools/`
                  and `graph/opinion.py` may not reach for langchain, mlflow, pyspark or
                  databricks; nothing outside `pipeline/` may import pyspark.
3. Platform    -- `dbutils` and `spark` are Databricks notebook globals that do not exist in the
                  serving container, so nothing outside `pipeline/` may reference them.
4. Side effect -- no `print` at module level outside `pipeline/`: importing a module must do
                  nothing.

Exit code 1 on any violation, with `path:line: message` for each.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

NOTEBOOKS = Path("notebooks")

# Our own top-level import names, once `notebooks/` is on sys.path. Folders under `notebooks/`
# that are not listed here (`data_ingestion/`) are not part of the agent tree and are skipped.
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

# agent-design.md section 3:
#     setup <- llm <- tools <- agents <- graph <- serving
#                     guardrails --------^
#                     cache      <- graph/cache_node
#                     evaluation  (imports nothing of ours; nothing uses it)
# `pipeline/` is the one folder that acts, so it imports whatever it runs.
ALLOWED_INTERNAL: dict[str, frozenset[str]] = {
    "setup": frozenset(),
    "llm": frozenset({"setup"}),
    "tools": frozenset({"setup", "llm"}),
    "agents": frozenset({"setup", "llm", "tools"}),
    "guardrails": frozenset({"setup", "llm"}),
    "cache": frozenset({"setup", "llm"}),
    "graph": frozenset({"setup", "llm", "tools", "agents", "guardrails", "cache"}),
    "serving": frozenset({"setup", "llm", "tools", "agents", "guardrails", "cache", "graph"}),
    "evaluation": frozenset({"setup"}),
    "pipeline": frozenset(AGENT_FOLDERS),
}

# Third-party roots that may not be imported. agent-design.md section 3.3 names exactly two
# targets -- `tools/*` and `graph/opinion.py` -- so the ban is scoped to those rather than spread
# across every folder. `llm/gateway.py` is the one file whose job is to reach the platform, and
# ADR 0001 replaced the ports it would otherwise hide behind with argument passing, so widening
# this list would only force that access somewhere less honest.
PURE_LAYER_BAN = frozenset(
    {"langchain", "langchain_core", "langgraph", "mlflow", "pyspark", "databricks"}
)

FORBIDDEN_THIRD_PARTY: dict[str, frozenset[str]] = {
    "tools": PURE_LAYER_BAN,
}

FORBIDDEN_PER_FILE: dict[str, frozenset[str]] = {
    # The consensus arithmetic and the card shape. Pure by the same rule as tools/.
    "graph/opinion.py": PURE_LAYER_BAN,
}

# Spark exists in a job and not in the serving container, and no tool may issue a query at
# request time (agent-design.md section 5.3). Only pipeline/ runs in a job and nowhere else.
NO_SPARK_OUTSIDE_ACTS = frozenset({"pyspark"})

EXCEPTIONS: dict[str, frozenset[str]] = {
    # safe_tool is *the* file that wraps a plain function as a LangChain tool -- the single
    # exception agent-design.md section 3.3 names. Every domain tool file goes through it, so no
    # other file under tools/ imports langchain.
    "tools/safe_tool.py": frozenset({"langchain", "langchain_core"}),
    # search_news queries the Vector Search index directly and caches the client for 600 s
    # (agent-design.md section 13.2). It is the one remote read a tool is allowed to make.
    "tools/news_tools.py": frozenset({"databricks"}),
    # An operational notebook, run by hand once per environment. Not part of the agent tree;
    # it exists under setup/ only because that is where ARCHITECTURE_V2.md section 5.2 puts it.
    "setup/bootstrap_secrets.py": frozenset({"databricks"}),
}

STDLIB_ONLY = frozenset({"setup"})
STDLIB_PLUS: dict[str, frozenset[str]] = {
    # ask_structured builds and validates the schemas; pydantic is the whole point of the file.
    "llm/parsing.py": frozenset({"pydantic"}),
}

PLATFORM_GLOBALS = frozenset({"dbutils", "spark"})
ACTS = "pipeline"


def rel(path: Path) -> str:
    """The module's path relative to `notebooks/`, with forward slashes."""
    return path.relative_to(NOTEBOOKS).as_posix()


def folder_of(path: Path) -> str:
    return path.relative_to(NOTEBOOKS).parts[0]


def import_roots(tree: ast.AST, folder: str) -> list[tuple[int, str]]:
    """Every imported top-level module name, with the line it was imported on.

    A relative import (`from . import x`) resolves to the importing file's own folder, which is
    always allowed, so it is reported as that folder rather than skipped.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append((node.lineno, folder))
            elif node.module:
                found.append((node.lineno, node.module.split(".")[0]))
    return found


def bound_names(tree: ast.AST) -> set[str]:
    """Every name the module binds somewhere -- assignment, parameter, import, loop or `with`.

    Used to tell a genuine reference to the Databricks `dbutils` global from a parameter that
    merely happens to be called `spark`.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def check_module(path: Path) -> list[str]:
    folder = folder_of(path)
    name = rel(path)
    exempt = EXCEPTIONS.get(name, frozenset())
    loc = path.as_posix()
    problems: list[str] = []

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as err:
        return [f"{loc}:{err.lineno}: does not parse as Python ({err.msg})"]

    allowed_internal = ALLOWED_INTERNAL[folder]
    forbidden = FORBIDDEN_THIRD_PARTY.get(folder, frozenset())
    forbidden |= FORBIDDEN_PER_FILE.get(name, frozenset())
    if folder != ACTS:
        forbidden |= NO_SPARK_OUTSIDE_ACTS
    forbidden -= exempt
    stdlib_extra = STDLIB_PLUS.get(name, frozenset())

    for lineno, root in import_roots(tree, folder):
        if root in AGENT_FOLDERS:
            if root != folder and root not in allowed_internal:
                problems.append(
                    f"{loc}:{lineno}: {folder}/ may not import {root}/ "
                    f"(allowed: {', '.join(sorted(allowed_internal)) or 'nothing of ours'})"
                )
            continue

        if root in forbidden:
            problems.append(f"{loc}:{lineno}: {folder}/ may not import {root!r}")

        if folder in STDLIB_ONLY and root not in sys.stdlib_module_names and root not in exempt:
            problems.append(
                f"{loc}:{lineno}: {folder}/ is standard library only, got {root!r} "
                "(it holds names and values, never logic -- agent-design.md section 3.1)"
            )
        elif (
            stdlib_extra
            and root not in sys.stdlib_module_names
            and root not in stdlib_extra
            and root not in exempt
        ):
            problems.append(
                f"{loc}:{lineno}: {name} may import only the standard library and "
                f"{', '.join(sorted(stdlib_extra))}, got {root!r}"
            )

    if folder != ACTS:
        bound = bound_names(tree)
        seen: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in PLATFORM_GLOBALS
                and node.id not in bound
                and node.id not in seen
            ):
                seen.add(node.id)
                problems.append(
                    f"{loc}:{node.lineno}: references the Databricks global {node.id!r}, "
                    "which does not exist in the serving container -- take it as an argument, "
                    "or move this to pipeline/ (ADR 0001)"
                )

        for node in tree.body:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "print"
            ):
                problems.append(
                    f"{loc}:{node.lineno}: print at module level -- importing a module must do "
                    "nothing; only pipeline/ acts (ADR 0001)"
                )

    return problems


def main() -> int:
    if not NOTEBOOKS.is_dir():
        print(f"no {NOTEBOOKS}/ directory, nothing to check")
        return 0

    modules = sorted(
        path
        for folder in AGENT_FOLDERS
        for path in (NOTEBOOKS / folder).rglob("*.py")
        if path.name != "__init__.py"
    )

    if not modules:
        print("no agent modules yet -- the tree is built from step 1 onward")
        return 0

    problems: list[str] = []
    for path in modules:
        found = check_module(path)
        problems.extend(found)
        print(f"{'FAIL' if found else 'OK  '} {rel(path)}")

    if problems:
        print(f"\n{len(problems)} layering violation(s):\n")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"\n{len(modules)} module(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
