# Databricks notebook source
# MAGIC %md
# MAGIC # verify_components
# MAGIC
# MAGIC Second stage of `finhive_deploy_agent`. Everything `verify_foundations` proved is assumed;
# MAGIC this stage exercises the pieces built on top of it -- the card and consensus arithmetic,
# MAGIC both guardrails, the planner, the synthesizer and each expert.
# MAGIC
# MAGIC Coverage grows with the tree: the card arithmetic, both guardrails, the planner, the
# MAGIC synthesizer and four experts today. `agents/news_analyst` joins at step 7 and
# MAGIC `graph/cache_node` at step 9.

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

from agents import (
    fundamental_analyst,
    macro_analyst,
    quant_risk_analyst,
    technical_analyst,
)
from graph import opinion, planner, synthesizer
from guardrails import input_guardrail, output_guardrail
from llm import gateway
from setup import config
from tools import fixtures, panel_data

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


try:
    panel = panel_data.load_panel_data(read_table)
    panel_source = "gold"
except Exception as exc:
    panel = fixtures.fixture_panel_data()
    panel_source = "FIXTURE"
    print(f"WARNING the gold tables did not load ({type(exc).__name__}: {str(exc)[:160]}).")
    print("        Falling back to the fixture. Rows below prove logic, not data.")

print(f"panel source: {panel_source}")

ctx = {
    "profile": profile,
    "enable_cache": enable_cache,
    "chat": gateway.get_chat_model,
    "read_table": read_table,
    "panel": panel,
    "panel_source": panel_source,
    # agents/ may not import graph/, so the expert checks are handed the real card parser here
    # rather than importing it. See agents/checks.py.
    "build_card": opinion.build_card,
}

# COMMAND ----------

run_stage(
    "verify_components",
    [
        Check("graph/opinion", opinion.check),
        Check("guardrails/input_guardrail", input_guardrail.check),
        Check("guardrails/output_guardrail", output_guardrail.check),
        Check("graph/planner", planner.check),
        Check("graph/synthesizer", synthesizer.check, needs=("graph/opinion",)),
        Check("agents/technical_analyst", technical_analyst.check),
        Check("agents/quant_risk_analyst", quant_risk_analyst.check),
        Check("agents/fundamental_analyst", fundamental_analyst.check),
        Check("agents/macro_analyst", macro_analyst.check),
    ],
    ctx,
)
