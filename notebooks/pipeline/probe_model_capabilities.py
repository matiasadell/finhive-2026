# Databricks notebook source
# MAGIC %md
# MAGIC # probe_model_capabilities
# MAGIC
# MAGIC Run by hand, not part of `finhive_deploy_agent`. It answers open item 11 of
# MAGIC `agent-design.md` section 22, which every node that needs typed output rests on:
# MAGIC
# MAGIC > the docs consulted document `response_format` for the GPT-OSS models and function calling
# MAGIC > for all four, but not `response_format` for Llama 3.3 70B or Llama 3.1 8B.
# MAGIC
# MAGIC That matters because `finhive_router` sends roughly 70% of requests to Llama 3.3 70B and
# MAGIC 30% to GPT-OSS 120B, **chosen per request**. If one of them ignores `response_format`, a
# MAGIC prompt-only schema would drift on a share of calls and the failure would look random. So
# MAGIC each model is addressed **directly** here, never through a role.
# MAGIC
# MAGIC The decision this produces, per node: use `response_format`, or fall back to **forced
# MAGIC function calling** -- a single tool whose arguments are the same schema, with the call
# MAGIC forced. Both models document the latter, and nothing about the schemas or the callers
# MAGIC changes either way.
# MAGIC
# MAGIC Paste the markdown table it prints into `docs/runbooks/model-capability-probe.md`.

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
dbutils.widgets.text("attempts", "5")

profile = dbutils.widgets.get("profile").strip()
ATTEMPTS = int(dbutils.widgets.get("attempts"))

# COMMAND ----------

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from llm import gateway
from llm.parsing import check_schema_supported, message_text
from setup import config

# COMMAND ----------
# MAGIC %md
# MAGIC ## The schemas
# MAGIC
# MAGIC These mirror the three schemas of `agent-design.md` section 4.2. They are defined here
# MAGIC rather than imported because `graph/planner.py` and `guardrails/` do not exist yet, and the
# MAGIC decision they depend on has to be made before they are written.
# MAGIC
# MAGIC **When steps 3 and 4 land, import the real ones and delete these** -- a schema measured
# MAGIC here and a schema shipped there that have drifted apart would make this probe a lie.

# COMMAND ----------


class PlannerOutput(BaseModel):
    """The largest of the three: seven fields, two of them lists."""

    mentions: list[str] = Field(description="Instruments as the user wrote them, never expanded.")
    technical_analyst: str = Field(description="Sub-question for the technical analyst; '' to skip.")
    quant_risk_analyst: str = Field(description="Sub-question for the quant/risk analyst; '' to skip.")
    fundamental_analyst: str = Field(description="Sub-question for the fundamental analyst; '' to skip.")
    macro_analyst: str = Field(description="Sub-question for the macro analyst; '' to skip.")
    news_analyst: str = Field(description="Sub-question for the news analyst; '' to skip.")
    reasoning: str = Field(description="One sentence on why these experts and not the others.")


class InputVerdict(BaseModel):
    """The smallest, and the one that runs on the weakest model."""

    verdict: Literal["allow", "refuse"] = Field(description="Whether to answer the question.")
    category: Literal["on_topic", "off_topic", "prompt_injection", "advice_request"] = Field(
        description="Why. 'advice_request' means answering needs the user's own circumstances."
    )
    reason: str = Field(description="One sentence.")


class GroundednessVerdict(BaseModel):
    """The one that runs over a long evidence base, on a reasoning model."""

    grounded: bool = Field(description="True unless a specific figure appears nowhere in the evidence.")
    reason: str = Field(description="One sentence.")
    unsupported: list[str] = Field(description="The figures the draft states that the evidence lacks.")


CASES = [
    (
        PlannerOutput,
        "You route a question to a panel of financial analysts. Never convene the fundamental "
        "analyst for crypto: there is no issuer and no financial statements.",
        "What do you think of Bitcoin right now?",
    ),
    (
        InputVerdict,
        "You classify questions. Research about an instrument is allowed however evaluative it "
        "sounds. A question about the user's own money is an advice_request.",
        "Should I put half my savings into NVDA?",
    ),
    (
        GroundednessVerdict,
        "You check every figure in a draft against the evidence. Rounding and rephrasing are fine.",
        "EVIDENCE:\nAAPL closed at 231.40 as of 2026-09-19. RSI(14) 58.2.\n\n"
        "DRAFT:\nAAPL closed at 231.40 with an RSI of 58 and a price/earnings ratio of 42.",
    ),
]

MODELS = [
    ("router/llama-3.3-70b", config.ROUTER_UNDERLYING_MODELS[0]),
    ("router/gpt-oss-120b", config.ROUTER_UNDERLYING_MODELS[1]),
    ("guard_in", config.GUARDRAIL_INPUT_MODEL),
    ("guard_out", config.GUARDRAIL_OUTPUT_MODEL),
]

# COMMAND ----------
# MAGIC %md
# MAGIC ## The two mechanisms

# COMMAND ----------


def try_response_format(model_name, schema, system_prompt, user_content, max_tokens):
    """One call constrained by `response_format`. Returns (ok, note)."""
    client = gateway.chat_model_for_name(model_name, 0.0, max_tokens, role="probe")
    bound = client.bind(
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
                "strict": True,
            },
        }
    )
    try:
        text = message_text(
            bound.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ]
            )
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    if not text.strip():
        # The signature of a cap consumed by reasoning before any answer was emitted.
        return False, "empty content"
    try:
        schema.model_validate_json(text)
        return True, ""
    except ValidationError as err:
        return False, f"invalid: {str(err)[:120]}"


def try_function_calling(model_name, schema, system_prompt, user_content, max_tokens):
    """One call with a single tool, forced. Returns (ok, note)."""
    client = gateway.chat_model_for_name(model_name, 0.0, max_tokens, role="probe")
    try:
        bound = client.bind_tools(
            [schema],
            tool_choice={"type": "function", "function": {"name": schema.__name__}},
        )
        reply = bound.invoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    calls = getattr(reply, "tool_calls", None) or []
    if not calls:
        return False, "no tool call"
    try:
        schema.model_validate(calls[0].get("args") or {})
        return True, ""
    except ValidationError as err:
        return False, f"invalid args: {str(err)[:120]}"


# COMMAND ----------
# MAGIC %md
# MAGIC ## Schemas first
# MAGIC
# MAGIC A schema the gateway would reject makes every number below meaningless, so it is checked
# MAGIC before a single call is spent.

# COMMAND ----------

for schema, _, _ in CASES:
    report = check_schema_supported(schema)
    print(f"{report['schema']:<22} {report['properties']} properties, limit {report['limit']}")
    if report["fields_without_description"]:
        print(f"  note: no description on {report['fields_without_description']}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## The runs
# MAGIC
# MAGIC `attempts` calls per (model, schema) with `response_format`. Function calling is tried only
# MAGIC where `response_format` was not perfect -- it is the fallback, not a competitor.

# COMMAND ----------

results = []

for label, model_name in MODELS:
    for schema, system_prompt, user_content in CASES:
        cap = config.ROLES[config.GUARD_OUT]["max_tokens"] if schema is GroundednessVerdict else 600

        rf_ok, rf_notes = 0, []
        for _ in range(ATTEMPTS):
            ok, note = try_response_format(model_name, schema, system_prompt, user_content, cap)
            rf_ok += int(ok)
            if note:
                rf_notes.append(note)

        fc_ok, fc_notes = None, []
        if rf_ok < ATTEMPTS:
            fc_ok = 0
            for _ in range(ATTEMPTS):
                ok, note = try_function_calling(model_name, schema, system_prompt, user_content, cap)
                fc_ok += int(ok)
                if note:
                    fc_notes.append(note)

        results.append(
            {
                "label": label,
                "model": model_name,
                "schema": schema.__name__,
                "response_format": f"{rf_ok}/{ATTEMPTS}",
                "function_calling": "-" if fc_ok is None else f"{fc_ok}/{ATTEMPTS}",
                "note": (rf_notes + fc_notes)[0] if (rf_notes or fc_notes) else "",
            }
        )
        print(
            f"{label:<22} {schema.__name__:<22} response_format {rf_ok}/{ATTEMPTS}"
            + ("" if fc_ok is None else f"  function_calling {fc_ok}/{ATTEMPTS}")
        )

# COMMAND ----------
# MAGIC %md
# MAGIC ## The verdict

# COMMAND ----------

header = "| role | model | schema | response_format | function calling | note |"
lines = [header, "|---|---|---|---|---|---|"]
for row in results:
    lines.append(
        f"| {row['label']} | `{row['model']}` | `{row['schema']}` | {row['response_format']} "
        f"| {row['function_calling']} | {row['note']} |"
    )

print("\n".join(lines))
print()

perfect = [r for r in results if r["response_format"] == f"{ATTEMPTS}/{ATTEMPTS}"]
if len(perfect) == len(results):
    print(
        "VERDICT: response_format is honoured everywhere. `ask_structured` keeps its current\n"
        "         mechanism and open item 22.11 closes."
    )
else:
    failing = sorted({f"{r['label']} / {r['schema']}" for r in results if r not in perfect})
    print("VERDICT: response_format is NOT reliable for:")
    for item in failing:
        print(f"  - {item}")
    print(
        "\n         Those nodes switch to forced function calling. Nothing about the schemas or\n"
        "         the callers changes; only the binding inside `llm/parsing.ask_structured`.\n"
        "         If a guardrail endpoint is among them, check it exists on Free Edition at all\n"
        "         before assuming the mechanism is the problem (section 11.3)."
    )
