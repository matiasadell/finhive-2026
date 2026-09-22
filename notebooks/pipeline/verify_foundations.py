# Databricks notebook source
# MAGIC %md
# MAGIC # verify_foundations
# MAGIC
# MAGIC First stage of `finhive_deploy_agent`. Runs the `check` of every notebook the rest of the
# MAGIC tree stands on, in dependency order, and fails the task if any row is red.
# MAGIC
# MAGIC Coverage grows with the tree: `setup/config`, `llm/gateway` and `llm/parsing` today;
# MAGIC `tools/*` joins at step 2 and `cache/*` at step 9.

# COMMAND ----------

# The one bootstrap the tree needs. `%run` is not used anywhere (ADR 0001), so `notebooks/` has to
# be on sys.path before `setup`, `llm` and the rest resolve as top-level names -- which is exactly
# what `code_paths=["notebooks"]` produces inside the serving container. Four lines, repeated at
# the head of every stage notebook, because nothing can import a helper that does it.
import sys

_notebook_path = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
)
NOTEBOOKS_ROOT = "/Workspace" + _notebook_path.rsplit("/notebooks/", 1)[0] + "/notebooks"
if NOTEBOOKS_ROOT not in sys.path:
    sys.path.insert(0, NOTEBOOKS_ROOT)

print(f"notebooks root: {NOTEBOOKS_ROOT}")

# COMMAND ----------

dbutils.widgets.text("profile", "free")
dbutils.widgets.text("enable_cache", "false")

profile = dbutils.widgets.get("profile").strip()
enable_cache = dbutils.widgets.get("enable_cache").strip().lower() == "true"

# COMMAND ----------

from pipeline.runner import Check, check_import_names, run_stage

# Before anything else: prove `setup`, `llm`, `tools`, `graph` and `agents` resolve to our own
# modules. They are generic enough to be shadowed by an installed distribution, and a collision is
# silent -- the import succeeds and the wrong code runs.
for name, where in sorted(check_import_names(NOTEBOOKS_ROOT)["resolved"].items()):
    print(f"  {name:<12} {where}")

# COMMAND ----------

from llm import gateway, parsing
from setup import config
from tools import (
    fixtures,
    fundamental_tools,
    macro_tools,
    panel_data,
    risk_tools,
    symbols,
    technical_tools,
)

# COMMAND ----------

import mlflow

try:
    mlflow.set_experiment(config.MLFLOW_EXPERIMENT)
    mlflow.langchain.autolog()
    print(f"tracing to {config.MLFLOW_EXPERIMENT}")
except Exception as exc:
    # Observability, not correctness. A stage that cannot trace still reports what works.
    print(f"WARNING tracing unavailable ({type(exc).__name__}: {exc})")

# COMMAND ----------


def list_secret_keys(scope: str) -> set[str]:
    """The key *names* in a scope. Never a value -- the names are not themselves secret."""
    return {item.key for item in dbutils.secrets.list(scope)}


def read_table(name: str):
    """The reader `tools/panel_data.py` receives as an argument (agent_data_contract.md)."""
    return spark.table(name).toPandas()


# COMMAND ----------

# The gold layer is the data engineer's deliverable and may not exist yet. Every tool body is
# pure and takes its frame as an argument, so the logic is provable against a fixture in the
# meantime -- while `tools/panel_data` below still runs against the real tables and goes red.
# That red row is the dependency, visible in the results table rather than buried in a plan.
try:
    panel = panel_data.load_panel_data(read_table)
    panel_source = "gold"
except Exception as exc:
    panel = fixtures.fixture_panel_data()
    panel_source = "FIXTURE"
    print(f"WARNING the gold tables did not load ({type(exc).__name__}: {str(exc)[:160]}).")
    print("        Falling back to the checked-in fixture. Every tools/ row below is therefore")
    print("        proving logic, NOT data. tools/panel_data will report the real failure.")

print(f"panel source: {panel_source}")
print(panel_data.describe(panel))

# COMMAND ----------

ctx = {
    "profile": profile,
    "enable_cache": enable_cache,
    "chat": gateway.get_chat_model,
    "list_secret_keys": list_secret_keys,
    "read_table": read_table,
    "panel": panel,
    "panel_source": panel_source,
}

# COMMAND ----------

run_stage(
    "verify_foundations",
    [
        Check("setup/config", config.check),
        Check("llm/gateway", gateway.check, needs=("setup/config",)),
        Check("llm/parsing", parsing.check, needs=("llm/gateway",)),
        # Reads the real gold tables. Red until the data engineer delivers them, by design.
        Check("tools/panel_data", panel_data.check, needs=("setup/config",)),
        Check("tools/symbols", symbols.check, needs=("setup/config",)),
        Check("tools/technical_tools", technical_tools.check, needs=("tools/symbols",)),
        Check("tools/risk_tools", risk_tools.check, needs=("tools/symbols",)),
        Check("tools/fundamental_tools", fundamental_tools.check, needs=("tools/symbols",)),
        Check("tools/macro_tools", macro_tools.check, needs=("setup/config",)),
    ],
    ctx,
)
