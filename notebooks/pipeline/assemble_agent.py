# Databricks notebook source
# MAGIC %md
# MAGIC # assemble_agent
# MAGIC
# MAGIC Third stage of `finhive_deploy_agent`, and the last one before anything is published. It
# MAGIC compiles the graph and runs real questions through it end to end.
# MAGIC
# MAGIC This is the first stage that **cannot pass on fixture data**. Its gate is three questions
# MAGIC answered, and an answer built on a fixture is not an answer. If the gold tables are not
# MAGIC there, `verify_foundations` already said so on the `tools/panel_data` row; this stage
# MAGIC stops the run rather than publishing a model that would load and then fail on its first
# MAGIC question.
# MAGIC
# MAGIC `serving/model_entry` joins this stage at step 6.

# COMMAND ----------

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

for name, where in sorted(check_import_names(NOTEBOOKS_ROOT)["resolved"].items()):
    print(f"  {name:<12} {where}")

# COMMAND ----------

from graph import build, cache_node
from llm import gateway
from setup import config
from tools import panel_data

# COMMAND ----------

import mlflow

try:
    mlflow.set_experiment(config.MLFLOW_EXPERIMENT)
    mlflow.langchain.autolog()
    print(f"tracing to {config.MLFLOW_EXPERIMENT}")
except Exception as exc:
    print(f"WARNING tracing unavailable ({type(exc).__name__}: {exc})")

# COMMAND ----------


def read_table(name: str):
    return spark.table(name).toPandas()


# No fixture fallback here, unlike the two verifying stages. A graph that answers from generated
# data proves nothing about a graph that has to answer from the real thing, and this is the last
# gate before a model is logged.
panel = panel_data.load_panel_data(read_table)
print(panel_data.describe(panel))

ctx = {
    "profile": profile,
    "enable_cache": enable_cache,
    "chat": gateway.get_chat_model,
    "read_table": read_table,
    "panel": panel,
}

# COMMAND ----------

run_stage(
    "assemble_agent",
    [
        Check("graph/cache_node", cache_node.check),
        Check("graph/build", build.check),
    ],
    ctx,
)
