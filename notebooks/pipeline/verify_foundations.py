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


ctx = {
    "profile": profile,
    "enable_cache": enable_cache,
    "chat": gateway.get_chat_model,
    "list_secret_keys": list_secret_keys,
}

# COMMAND ----------

run_stage(
    "verify_foundations",
    [
        Check("setup/config", config.check),
        Check("llm/gateway", gateway.check, needs=("setup/config",)),
        Check("llm/parsing", parsing.check, needs=("llm/gateway",)),
    ],
    ctx,
)
