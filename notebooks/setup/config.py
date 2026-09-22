# Databricks notebook source
"""Names, paths and settings every other folder reads. No function with logic lives here.

`agent-design.md` section 3.1 draws the line: table names, model service names, the role table,
experiment paths and secret *key names* belong here; prompts, legal text and measured algorithm
thresholds (`0.62`, `0.5`, `0.35`) do not -- those stay next to the code that owns them, beside
the note saying how they were measured.

This module reads environment variables at import time and calls nothing else. Every value is
overridable by environment variable so that a workspace whose names differ needs no code edit.

Layering: standard library only (`.github/scripts/check_agent_layering.py`).
"""

# COMMAND ----------

from __future__ import annotations

import os

# COMMAND ----------
# The environment. There is exactly one (`ARCHITECTURE_V2.md` section 7), but the concept stays
# so that nothing special-cases "the only environment" as if it were unconfigured. Stage
# notebooks read a `profile` widget and fail loudly when it disagrees with this value.

PROFILE = os.getenv("FINHIVE_PROFILE", "free")

# COMMAND ----------
# Unity Catalog.
#
# OPEN: `ARCHITECTURE_V2.md` section 9 names the catalog per environment (`finhive_free`), while
# the ingestion notebooks on `main` hardcode `finhive`. Defaulting to what exists today; the
# question is open with the data engineer (agent_data_contract.md section 7, question 1) and the
# answer is an environment variable, not an edit.

CATALOG = os.getenv("FINHIVE_CATALOG", "finhive")
GOLD_SCHEMA = os.getenv("FINHIVE_GOLD_SCHEMA", "analytics")
UC_FULL_SCHEMA = f"{CATALOG}.{GOLD_SCHEMA}"

# COMMAND ----------
# The gold tables `tools/panel_data.py` loads. Their columns, grain and null discipline are
# specified in docs/architecture/agent_data_contract.md -- this is only where their names live.

TABLE_INSTRUMENTS = os.getenv("FINHIVE_TABLE_INSTRUMENTS", f"{UC_FULL_SCHEMA}.gold_instruments")
TABLE_TECHNICAL = os.getenv("FINHIVE_TABLE_TECHNICAL", f"{UC_FULL_SCHEMA}.gold_technical_indicators")
TABLE_RISK = os.getenv("FINHIVE_TABLE_RISK", f"{UC_FULL_SCHEMA}.gold_risk_metrics")
TABLE_CORRELATIONS = os.getenv("FINHIVE_TABLE_CORRELATIONS", f"{UC_FULL_SCHEMA}.gold_correlations")
TABLE_FUNDAMENTALS = os.getenv("FINHIVE_TABLE_FUNDAMENTALS", f"{UC_FULL_SCHEMA}.gold_fundamentals")
TABLE_MACRO = os.getenv("FINHIVE_TABLE_MACRO", f"{UC_FULL_SCHEMA}.gold_macro")
TABLE_MACRO_SERIES = os.getenv("FINHIVE_TABLE_MACRO_SERIES", f"{UC_FULL_SCHEMA}.gold_macro_series")

GOLD_TABLES = (
    TABLE_INSTRUMENTS,
    TABLE_TECHNICAL,
    TABLE_RISK,
    TABLE_CORRELATIONS,
    TABLE_FUNDAMENTALS,
    TABLE_MACRO,
    TABLE_MACRO_SERIES,
)

# How much daily history `PanelData` loads. The table holds the full series; the agent keeps a
# window so the frames stay the few megabytes section 5.3 assumes.
PRICE_HISTORY_YEARS = int(os.getenv("FINHIVE_PRICE_HISTORY_YEARS", "3"))

# A table older than this turns its row red in `verify_foundations`. These are the thresholds
# proposed in agent_data_contract.md section 4, still awaiting the data engineer's confirmation.
FRESHNESS_MAX_AGE_DAYS = {
    TABLE_INSTRUMENTS: 7,
    TABLE_TECHNICAL: 4,
    TABLE_RISK: 7,
    TABLE_CORRELATIONS: 7,
    TABLE_FUNDAMENTALS: 30,
    # gold_macro is judged per series, not per table: each has its own release cadence, so the
    # tools state the observation date and let the expert judge. No table-level threshold.
}

# COMMAND ----------
# Model services (`agent-design.md` section 4.1).
#
# Everything goes through the same AI Gateway base URL and the same client; only `model=` differs.
# `finhive_router` routes each request itself, today 70% llama-3.3-70b / 30% gpt-oss-120b. The
# guardrails deliberately do NOT use it: they call the cheapest pay-per-token endpoints directly
# (section 11.3), because they are the most frequent and most mechanical calls in the graph.
#
# No model name is written anywhere except this file.

MODEL_SCHEMA = os.getenv("FINHIVE_MODEL_SCHEMA", UC_FULL_SCHEMA)

AI_GATEWAY_ROUTER_MODEL = os.getenv(
    "FINHIVE_GATEWAY_ROUTER_MODEL", f"{MODEL_SCHEMA}.finhive_router"
)
AI_GATEWAY_EMBEDDINGS_MODEL = os.getenv(
    "FINHIVE_GATEWAY_EMBEDDINGS_MODEL", f"{MODEL_SCHEMA}.finhive_embeddings"
)
GUARDRAIL_INPUT_MODEL = os.getenv(
    "FINHIVE_GUARDRAIL_INPUT_MODEL", "databricks-meta-llama-3-1-8b-instruct"
)
GUARDRAIL_OUTPUT_MODEL = os.getenv(
    "FINHIVE_GUARDRAIL_OUTPUT_MODEL", "databricks-gpt-oss-20b"
)

# The two models `finhive_router` routes between, addressed directly. Used only by
# `pipeline/probe_model_capabilities.py`, which has to measure each one's behaviour separately --
# the router picks per request, so a probe through it cannot attribute a failure.
ROUTER_UNDERLYING_MODELS = (
    os.getenv("FINHIVE_ROUTER_MODEL_A", "databricks-meta-llama-3-3-70b-instruct"),
    os.getenv("FINHIVE_ROUTER_MODEL_B", "databricks-gpt-oss-120b"),
)

# The AI Gateway base URL. Empty means "derive it from the workspace host", which is what
# `llm/gateway.py` does when it resolves credentials.
AI_GATEWAY_BASE_URL = os.getenv("FINHIVE_GATEWAY_BASE_URL", "")

# COMMAND ----------
# The role table. A role is a label with three jobs: it sets the model, temperature and token cap;
# it tags the MLflow trace so a run shows which part of the graph spent tokens; and it documents
# intent (`agent-design.md` section 4.1).

ROUTER = "router"
WORKER = "worker"
SYNTHESIZER = "synthesizer"
GUARD_IN = "guard_in"
GUARD_OUT = "guard_out"
EMBEDDING = "embedding"

ROLES = {
    # The planner. `600`, not `400`: about 30% of requests land on a reasoning model whose
    # thinking tokens count against the cap, and a reply cut off by the cap fails validation.
    ROUTER: {"model": AI_GATEWAY_ROUTER_MODEL, "temperature": 0.0, "max_tokens": 600},
    # Every expert's ReAct loop.
    WORKER: {"model": AI_GATEWAY_ROUTER_MODEL, "temperature": 0.1, "max_tokens": 1600},
    # Final composition.
    SYNTHESIZER: {"model": AI_GATEWAY_ROUTER_MODEL, "temperature": 0.2, "max_tokens": 2000},
    # Input guardrail: a short classification, on a small non-reasoning model.
    GUARD_IN: {"model": GUARDRAIL_INPUT_MODEL, "temperature": 0.0, "max_tokens": 300},
    # Output guardrail: compares every figure in a draft against a long evidence base. `800`,
    # not `400`, because the model reasons first and the JSON carries an `unsupported` list.
    GUARD_OUT: {"model": GUARDRAIL_OUTPUT_MODEL, "temperature": 0.0, "max_tokens": 800},
    # The semantic cache's query vectors -- its only consumer today.
    EMBEDDING: {"model": AI_GATEWAY_EMBEDDINGS_MODEL},
}

KNOWN_MODEL_ROLES = frozenset(ROLES)
CHAT_ROLES = (ROUTER, WORKER, SYNTHESIZER, GUARD_IN, GUARD_OUT)

# There is no `critic` role (its only caller was the output guardrail, now `guard_out`) and no
# `judge` role: the judges that score answers are configured in MLflow (section 16.2).

# A first guess, to adjust after the first full run. Nothing in the design measured it.
MODEL_TIMEOUT_SECONDS = int(os.getenv("FINHIVE_MODEL_TIMEOUT_SECONDS", "120"))
MODEL_MAX_RETRIES = int(os.getenv("FINHIVE_MODEL_MAX_RETRIES", "2"))

# COMMAND ----------
# The experts, and the graph's fixed limits.

TECHNICAL_ANALYST = "technical_analyst"
QUANT_RISK_ANALYST = "quant_risk_analyst"
FUNDAMENTAL_ANALYST = "fundamental_analyst"
MACRO_ANALYST = "macro_analyst"
NEWS_ANALYST = "news_analyst"

# Fixed order, so a plan and a results table read the same way every run.
EXPERTS = (
    TECHNICAL_ANALYST,
    QUANT_RISK_ANALYST,
    FUNDAMENTAL_ANALYST,
    MACRO_ANALYST,
    NEWS_ANALYST,
)

# The whole panel today. Pay-per-token endpoints rate-limit if pushed harder.
DEFAULT_MAX_CONCURRENCY = int(os.getenv("FINHIVE_MAX_CONCURRENCY", "4"))

# Total drafts, not extra retries: the first synthesis plus at most two revisions.
MAX_SYNTHESIS_ATTEMPTS = int(os.getenv("FINHIVE_MAX_SYNTHESIS_ATTEMPTS", "3"))

# Leaves ample room for three drafts.
RECURSION_LIMIT = int(os.getenv("FINHIVE_RECURSION_LIMIT", "40"))

# COMMAND ----------
# Graph node names, and the id the final answer always occupies.
#
# They live here, with every other name, because `guardrails/` and `graph/` both route by these
# strings and the direction of dependencies runs guardrails -> graph, never the reverse
# (`agent-design.md` section 3). A typo in a `Command(goto=...)` is otherwise a runtime dead end
# rather than an import error.

NODE_INPUT_GUARDRAIL = "input_guardrail"
NODE_CACHE_LOOKUP = "cache_lookup"
NODE_PLANNER = "planner"
NODE_RUN_EXPERT = "run_expert"
NODE_SYNTHESIZER = "synthesizer"
NODE_OUTPUT_GUARDRAIL = "output_guardrail"
NODE_CACHE_STORE = "cache_store"

# The final answer always occupies one message with this id. The synthesizer writes it and the
# output guardrail rewrites it on a retry; because the id matches, `add_messages` *replaces*
# rather than appends. That is what guarantees the answer is `messages[-1]`, so a consumer that
# reads only the last message never sees an earlier draft (section 11.2).
FINAL_ANSWER_ID = "finhive-final-answer"

# COMMAND ----------
# Secrets. Key *names* only -- a value exists solely in the Databricks secret scope.

SECRET_SCOPE = os.getenv("FINHIVE_SECRET_SCOPE", "finhive")

SECRET_KEY_NEWS_API = "tavily-api-key"
SECRET_KEY_VALKEY_URI = "valkey-uri"

# What the agent itself needs today: nothing. `llm/gateway.py` authenticates with the ambient
# identity -- the notebook's in a job, the served model's in the serving container -- so there is
# no gateway token to store. The two names below become required as their features are built.
REQUIRED_SECRET_KEYS: tuple[str, ...] = ()

DEFERRED_SECRET_KEYS = {
    "news_analyst": SECRET_KEY_NEWS_API,
    "semantic_cache": SECRET_KEY_VALKEY_URI,
}

# COMMAND ----------
# Vector Search -- the news index. Not built yet (agent_data_contract.md section 6); the names
# live here so nothing else has to invent them.

VECTOR_SEARCH_ENDPOINT = os.getenv("FINHIVE_VS_ENDPOINT", "finhive_vs")
NEWS_INDEX = os.getenv("FINHIVE_NEWS_INDEX", f"{UC_FULL_SCHEMA}.gold_news_index")

# COMMAND ----------
# MLflow, the registry and the golden set.

MLFLOW_EXPERIMENT = os.getenv("FINHIVE_MLFLOW_EXPERIMENT", f"/Shared/finhive/{PROFILE}")
REGISTERED_MODEL_NAME = os.getenv("FINHIVE_REGISTERED_MODEL", f"{MODEL_SCHEMA}.finhive_agent")

# The durable record of the last known good version. `deploy_agent` moves it only after the
# served endpoint answered a smoke question; `rollback_agent` restores it.
CHAMPION_ALIAS = "champion"

SERVING_ENDPOINT_NAME = os.getenv("FINHIVE_SERVING_ENDPOINT", "finhive-agent")
SERVING_WORKLOAD_SIZE = os.getenv("FINHIVE_SERVING_WORKLOAD_SIZE", "Small")
SERVING_SCALE_TO_ZERO = os.getenv("FINHIVE_SERVING_SCALE_TO_ZERO", "true").lower() == "true"

GOLDEN_SET_NAME = os.getenv("FINHIVE_GOLDEN_SET", f"{UC_FULL_SCHEMA}.finhive_golden_set")

# COMMAND ----------
# Semantic cache. Off until the three checks of `agent-design.md` section 14.1 pass.

ENABLE_CACHE_DEFAULT = os.getenv("FINHIVE_ENABLE_CACHE", "false").lower() == "true"

# Freshness is set at store time from the plan: an answer is only as fresh as its stalest input,
# so the TTL is the *shortest* among the experts that ran.
CACHE_TTL_SECONDS_BY_EXPERT = {
    TECHNICAL_ANALYST: 900,
    QUANT_RISK_ANALYST: 900,
    NEWS_ANALYST: 3600,
    FUNDAMENTAL_ANALYST: 86400,
    MACRO_ANALYST: 86400,
}

CACHE_TIMEOUT_SECONDS = float(os.getenv("FINHIVE_CACHE_TIMEOUT_SECONDS", "0.5"))

# COMMAND ----------


def check(ctx: dict) -> dict:
    """Every name resolves, and every required secret name exists in the scope.

    `ctx` keys used:
      profile          str, the widget the stage was started with
      list_secret_keys callable(scope) -> set[str], or absent to skip the secret pass

    Never reads a secret *value*. The list of names the system needs is not itself a secret.
    """
    if ctx.get("profile") and ctx["profile"] != PROFILE:
        raise ValueError(
            f"profile widget is {ctx['profile']!r} but config resolved {PROFILE!r} -- "
            "set FINHIVE_PROFILE, or start the stage with the matching profile"
        )

    blank = [
        name
        for name, value in sorted(globals().items())
        if name.isupper() and isinstance(value, str) and not value.strip()
        # AI_GATEWAY_BASE_URL is deliberately empty by default: it is derived from the host.
        and name != "AI_GATEWAY_BASE_URL"
    ]
    if blank:
        raise ValueError(f"empty configuration value(s): {blank}")

    unqualified = [t for t in GOLD_TABLES if t.count(".") != 2]
    if unqualified:
        raise ValueError(f"gold table name(s) not catalog.schema.table: {unqualified}")

    missing_roles = sorted(set(CHAT_ROLES) - KNOWN_MODEL_ROLES)
    if missing_roles:
        raise ValueError(f"chat role(s) absent from the role table: {missing_roles}")

    for role in KNOWN_MODEL_ROLES:
        if not ROLES[role].get("model"):
            raise ValueError(f"role {role!r} has no model")

    ttl_unknown = sorted(set(CACHE_TTL_SECONDS_BY_EXPERT) - set(EXPERTS))
    if ttl_unknown:
        raise ValueError(f"cache TTL names an expert that does not exist: {ttl_unknown}")

    secrets_detail = "no secret required by the agent yet"
    list_secret_keys = ctx.get("list_secret_keys")
    if list_secret_keys is not None:
        try:
            present = set(list_secret_keys(SECRET_SCOPE))
        except Exception as exc:
            # The agent needs no secret today, so an unreadable scope is information, not a
            # failure. It becomes one the moment REQUIRED_SECRET_KEYS is non-empty.
            if REQUIRED_SECRET_KEYS:
                raise ValueError(
                    f"secret scope {SECRET_SCOPE!r} is unreadable but "
                    f"{sorted(REQUIRED_SECRET_KEYS)} are required: {exc}"
                ) from exc
            present = None
            secrets_detail = f"scope {SECRET_SCOPE!r} unreadable ({type(exc).__name__}), none needed"

        if present is not None:
            missing = sorted(set(REQUIRED_SECRET_KEYS) - present)
            if missing:
                raise ValueError(f"secret scope {SECRET_SCOPE!r} is missing key(s): {missing}")
            deferred = {
                feature: (key in present)
                for feature, key in sorted(DEFERRED_SECRET_KEYS.items())
            }
            secrets_detail = (
                f"scope {SECRET_SCOPE!r}: {len(present)} key(s); deferred present={deferred}"
            )

    return {
        "detail": (
            f"profile={PROFILE} catalog={CATALOG} schema={GOLD_SCHEMA} "
            f"| {len(GOLD_TABLES)} gold tables | {len(KNOWN_MODEL_ROLES)} roles "
            f"| {secrets_detail}"
        ),
        "profile": PROFILE,
        "router_model": AI_GATEWAY_ROUTER_MODEL,
    }
