# Databricks notebook source
# MAGIC %md
# MAGIC # build_golden_set
# MAGIC
# MAGIC Writes the curated cases from `evaluation/golden_set.py` and registers them as an MLflow
# MAGIC evaluation dataset. Idempotent: cases are keyed by `case_id` and replaced, never duplicated.
# MAGIC
# MAGIC It sits **beside** the release path in `finhive_deploy_agent` -- nothing depends on it, and
# MAGIC it depends on nothing. Evaluation informs a person; it does not gate a deploy
# MAGIC (`agent-design.md` section 16.2).

# COMMAND ----------

import sys

_notebook_path = (
    dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
)
NOTEBOOKS_ROOT = "/Workspace" + _notebook_path.rsplit("/notebooks/", 1)[0] + "/notebooks"
if NOTEBOOKS_ROOT not in sys.path:
    sys.path.insert(0, NOTEBOOKS_ROOT)

# COMMAND ----------

dbutils.widgets.text("profile", "free")
profile = dbutils.widgets.get("profile").strip()

# COMMAND ----------

from evaluation import golden_set
from setup import config

summary = golden_set.validate()
print(f"{summary['total']} cases: {summary['by_category']}")
print(f"  guardrail: {summary['must_allow']} must allow, {summary['must_refuse']} must refuse")

frame = golden_set.to_frame()
display(frame)

# COMMAND ----------

# The dataset is written as a Delta table first, because that is the durable artifact: it can be
# queried, diffed between runs and read by a judge regardless of what the MLflow dataset API
# looks like on this workspace.
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config.UC_FULL_SCHEMA}")

(
    spark.createDataFrame(frame)
    .write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(config.GOLDEN_SET_NAME)
)
print(f"wrote {len(frame)} cases to {config.GOLDEN_SET_NAME}")

# COMMAND ----------

# UNVERIFIED on this workspace: the exact MLflow evaluation-dataset API moves between versions,
# and Free Edition may not expose all of it. The Delta table above is the source of truth either
# way, so a failure here is reported and does not lose the cases.
try:
    import mlflow

    mlflow.set_experiment(config.MLFLOW_EXPERIMENT)
    from mlflow.genai import datasets as genai_datasets

    try:
        dataset = genai_datasets.get_dataset(config.GOLDEN_SET_NAME)
        print(f"evaluation dataset {config.GOLDEN_SET_NAME} already exists")
    except Exception:
        dataset = genai_datasets.create_dataset(config.GOLDEN_SET_NAME)
        print(f"created evaluation dataset {config.GOLDEN_SET_NAME}")

    dataset.merge_records(frame.to_dict(orient="records"))
    print(f"merged {len(frame)} records")
except Exception as exc:
    print(
        f"WARNING could not register the MLflow evaluation dataset "
        f"({type(exc).__name__}: {exc}).\n"
        f"        The cases are in {config.GOLDEN_SET_NAME} and can be read from there. "
        f"Fix this against the MLflow version actually installed, then re-run.\n"
        f"        See docs/runbooks/agent-evaluation.md."
    )

# COMMAND ----------

print("done. Judges are defined in the MLflow UI and written down in the runbook, not in git.")
