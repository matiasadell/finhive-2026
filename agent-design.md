<style>
/*
databricks red #FF3621
databricks grey #1B3139
databricks dark grey #122025
databricks logo link
https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/databricks.png
*/

body {
  background: #122025;
  background-attachment: fixed;
  background-size: cover;
  color: #FFFFFF;
  padding: 2em;
  position: relative;
}

body::before,
body::after {
  content: "";
  position: fixed;
  width: 70vmin;
  height: 70vmin;
  background-image: url("https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/databricks.png");
  background-size: contain;
  background-repeat: no-repeat;
  opacity: 0.05;
  pointer-events: none;
  z-index: -1;
}

body > * {
  position: relative;
  z-index: 1;
}

body::before {
  top: -35vmin;
  left: -35vmin;
}

body::after {
  bottom: -35vmin;
  right: -35vmin;
}

th {
  color: #FF3621;
}

table,
pre,
code {
  background-color: #252525;
}

table {
  display: block;
  width: fit-content;
  max-width: 100%;
  margin: 0 auto;
  overflow-x: auto;
  white-space: nowrap;
}

pre {
  display: block;
  width: fit-content;
  max-width: 100%;
  margin: 0 auto;
  overflow-x: auto;
}

pre code {
  white-space: pre;
}

.hljs-keyword,
.hljs-built_in {
  color: #FF3621;
}

h1 {
  color: #FF3621;
  font-size: 3em;
}

h2 {
  color: #FF3621;
}

h3 {
  color: #FFFFFF;
}
</style>

# FinHive — Agent Design v2 (AI Engineering)

**Status:** the intelligence layer of the v2 rebuild — every model call, every tool, every
prompt's intent, every measured threshold, and the notebooks and jobs that build, prove and
deploy the agent.
**Companion:** `architecture.md` v2 owns the platform: repo layout, ports and adapters, the
composition root, config and secrets, CI/CD, jobs, AKS, Kafka. This document owns everything
under the agent folders of `notebooks/` (`llm`, `tools`, `agents`, `graph`, `guardrails`,
`evaluation`, `serving`). Where the two
overlap — the tree, the job specs, the layering rule — `architecture.md` is authoritative and
this document only says what the agent adds.
**Audience:** the data scientist of the two-person team, and any agent rebuilding this from
zero.
**Convention:** values in `code` are exact and were measured on a live workspace; keep the
*shape* of each decision and re-measure anything environment-dependent. **Planned** means
designed, not built — do not implement it as if it were verified.

---

## 0. Scope

| Here | In `architecture.md` |
|---|---|
| Model access contract, roles, JSON-without-structured-output | The workspace identity and secrets the gateway reads |
| Tool layer, grounding contract, `safe_tool` | Gold tables, medallion |
| Routing rules and planner, experts, consensus, synthesizer | Job specs, promotion, CI/CD |
| Guardrails, in and out | Secrets, identity, network |
| Hybrid search + MMR inside the news tool (§13) | The `gold_news` table, the index over it (`data_modeling`) and the pipeline that feeds both |
| News analyst: tool, prompt, routing rule (§8.3) | The news adapter, medallion pipeline, chunk table, index sync, job and secret (§8.3 lists them) |
| Semantic cache: nodes, similarity, freshness (§14.1) | The Aiven Valkey service, its secret, network reachability and the capability probe (§14.1 lists them) |
| The golden set, and how it is judged in MLflow (§16) | Why eval is run by hand and not scheduled (§8.4 there) |
| The serving functions and the three release actions that run them (§17) | Model Serving in the serving tier, `apps/api` as its HTTP client |
| Which agent notebooks to create, and the end-to-end job that deploys the agent — one task per tree notebook, no stage layer (§19) | Everything else that is a job |
| That non-sensitive config is a literal in `agents/config.ipynb` and only credentials come from the scope (§3.1) | The scope itself and `notebooks/setup/boostrap_secrets.ipynb`, which is the data engineer's |

---

## 1. The five invariants

Every decision below is one of these applied to a case. When a rule looks arbitrary, it is
one of these with a measurement behind it (§21).

1. **The LLM never computes a number.** Every figure in an answer comes from a deterministic
   Python function in `notebooks/tools/`, exposed as a tool. The model chooses tools and
   narrates their output. Consensus is arithmetic.
2. **The model proposes, code decides.** Where a wrong model output would have consequences —
   a ticker that does not exist, an agreement percentage — code owns the decision and the model
   only supplies natural language into it. **One deliberate exception:** which experts to
   consult is decided by the planner's prompt, not by code (§6).
3. **No silent failure.** Everything that can degrade degrades *visibly*: a failed expert
   produces a card, an unparseable judgement produces a `degraded` card, a news search that
   finds nothing says so, an ungrounded answer carries a warning banner. This
   is `architecture.md` principle 6 applied to the graph.
4. **Measure, do not infer.** `0.62`, `0.5`, `0.35`, `MIN_AMBIGUOUS_COUNT = 3`, the 300-token
   floor: each replaced something that was tried and measured worse.
5. **A guardrail the agent can skip is not a guardrail.** Guardrails are graph nodes on the
   only way in and the only way out, never tools the model may choose to call.

---

## 2. Shape of the system

```
                     START
                       │
             ┌─────────▼──────────┐
             │  input_guardrail   │──refuse──▶ END   (fails OPEN)
             └─────────┬──────────┘
                       │ allow
             ┌─────────▼──────────┐
             │   cache_lookup     │──fresh hit──▶ END        (§14.1, Valkey)
             └─────────┬──────────┘
                       │ miss
             ┌─────────▼──────────┐   the model names instruments and sub-questions;
             │      planner       │   CODE resolves symbols; the routing rules are
             └─────────┬──────────┘   in the prompt (§6), no matrix
          Send ┌───┬───┼───┬───┐
               ▼   ▼   ▼   ▼   ▼      run_expert × N — parallel, one superstep
          technical quant fund macro news
               └───┴───┼───┴───┘
             ┌─────────▼──────────┐   consensus computed in code, then composed
             │    synthesizer     │
             └─────────┬──────────┘
             ┌─────────▼──────────┐   not grounded and drafts < 3 → back to the
             │  output_guardrail  │─↺ synthesizer with the unsupported figures;
             └─────────┬──────────┘   fails SAFE; disclaimer appended in code
                       ▼
                      END
```

**Why a panel and not a router.** The fan-out is the product, not an implementation detail.
Every consulted expert forms its view independently, without seeing the others, so agreement
between them is *evidence* rather than an artefact of who went first. A router is cheaper and
answers a different question. (An adaptive router in front of the panel is §14 — it decides
whether a question deserves the panel, it does not replace it.)

**Why LangGraph.** Three things are needed and a plain agent loop gives none of them: typed
state with a reducer so parallel branches append to one list, `Send` fan-out with a single
join, and node-level tracing so a run can be explained after the fact.

---

## 3. Where the agent lives in the v2 tree

`setup/` holds **only variables and paths** — names, endpoints, model roles, table names,
secret keys — the things other notebooks read later. It contains no function with logic. Every
piece of agent code sits in its own folder directly under `notebooks/`, one folder per
responsibility, so the tree reads the way the graph does.

```
notebooks/
├── setup/                         NOT the agent's - the data engineer's, for notebooks that create
│                                  environment state (the secret scope, catalogs). Nothing here is mine
├── data_ingestion/                the data engineer's (architecture.md §3)
│
└── agents/                        the whole agent stack, where architecture.md §3 reserves it
    ├── config.ipynb               catalog/schema, table names, the two base paths and the four
    │                              model names (§4.1), the role table, the secret scope. VS
    │                              endpoint/index and the MLflow names arrive with their consumer
    │
    ├── llm/                           model access — the ONLY folder that talks to the AI Gateway
    │   ├── gateway.ipynb                 get_chat_model(role), token cache, extra_body, 300-token floor
    │   └── parsing.ipynb                 message_text, ask_structured, check_schema_supported (§4.2)
    │
    ├── tools/                         deterministic tools — what the workers may call
    │   ├── symbols.ipynb                 resolve_symbol, alias table, UnknownSymbolError
    │   ├── safe_tool.ipynb               the wrapper that turns exceptions into instructions
    │   ├── panel_data.ipynb              PanelData, loaded once from the gold tables (§5.3)
    │   ├── technical_tools.ipynb         6 tools
    │   ├── risk_tools.ipynb              4 tools
    │   ├── fundamental_tools.ipynb       4 tools
    │   ├── macro_tools.ipynb             5 tools
    │   └── news_tools.ipynb              search_news: hybrid query on the index + MMR, inside the tool (§13)
    │
    ├── experts/                       the WORKERS — one ReAct expert per file
    │   ├── base.ipynb                    OPINION_CONTRACT + build_expert_agent (§8.1)
    │   ├── technical_analyst.ipynb       domain prompt + tools → agent
    │   ├── quant_risk_analyst.ipynb
    │   ├── fundamental_analyst.ipynb
    │   ├── macro_analyst.ipynb
    │   └── news_analyst.ipynb            reads the news gold table (§8.3)
    │
    ├── graph/                         the SUPERVISOR: state, routing, contracts between nodes
    │   ├── state.ipynb                   FinHiveState (+ the cards reducer) and ExpertTask
    │   ├── planner.ipynb                 supervisor node: who to ask, what to ask (§7)
    │   ├── opinion.ipynb                 OpinionCard, evidence extraction, consensus (§9)
    │   ├── synthesizer.ipynb             composition node (§10)
    │   ├── cache_node.ipynb              cache_lookup / cache_store nodes (§14.1)
    │   └── build.ipynb                   assembly: nodes, Send fan-out, join, compile (§12)
    │
    ├── guardrails/                    graph nodes on the only way in and the only way out
    │   ├── input_guardrail.ipynb         fails OPEN (§11.1)
    │   └── output_guardrail.ipynb        fails SAFE, DISCLAIMER in code (§11.2)
    │
    ├── cache/                         semantic cache over Valkey (Aiven) — not part of the news search (§14.1)
    │   ├── valkey.ipynb                  connection, keys, TTL, sanitised secret read; every call fails open
    │   └── semantic_cache.ipynb          lookup / store, similarity, entity guard, freshness
    │
    ├── evaluation/                    the golden set — the only thing here (§16)
    │   └── build_golden_set.ipynb        notebook, run by hand: builds the curated cases and registers them
    │                                  as an MLflow evaluation dataset. No harness, no judges, no scorers
    │
    └── serving/                       what makes the graph callable (§17)
        ├── model_entry.ipynb          the ResponsesAgent that MLflow logs — the only file that would
        │                              run inside the serving container
        └── endpoint.ipynb             set_served_version, champion_version, mark_champion, plus the
                                       package|deploy|rollback actions behind one `action` widget (§17)
```

**Every notebook defines; nothing acts on its own.** Notebooks are `.ipynb` and compose with `%run`
(§3.3), so a `%run` of any of them must be free: it assigns names and nothing else. **No notebook calls
its own `check`.** `llm/gateway`'s costs nine model calls, so auto-running it would bill every caller;
the caller runs `check()` in a cell when it wants it. **There is no `pipeline/` folder and no runner
module.**

A `check` exists only where it touches the environment, which is the only thing you cannot learn by
reading the code:

| Has a `check` | Why | No `check` |
|---|---|---|
| `llm/gateway` | do the models answer, and does `response_format` hold on both routed models (§4.2) | `agents/config`, `graph/state` — variables and types |
| `tools/panel_data` | do the gold tables exist and are they fresh | `llm/parsing` — pure; gateway's check exercises it |
| `tools/news_tools` | does the ticker filter reach the index | `tools/safe_tool`, `experts/base` — a wrapper and a contract, exercised by their users |
| `cache/valkey` | is Valkey reachable at all (§14.1) | `graph/opinion`, `tools/symbols` — pure arithmetic and pure resolution |

Everything else is proven by the golden set in MLflow (§16), which is a quality question, not a smoke
test. `evaluation/build_golden_set` is the one notebook that acts unconditionally.

**Direction of dependencies** — each folder may use the ones to its left, never to its right:

```
config ← llm ← tools ← experts ← graph ← serving
              guardrails ─────↗
              cache     ← graph/cache_node   (uses llm/ for the query embedding)
              evaluation  (imports nothing of ours; nothing uses it)
```

`experts/` never imports `graph/` (a worker does not know it is on a panel); `graph/` is the
only place that knows both. `llm/` imports nothing of ours except `setup/`.

### 3.1 What `config` may and may not contain

**Non-sensitive configuration is a literal in `agents/config.ipynb`; only real credentials live in the
secret scope, read with `dbutils.secrets.get`.** That is `architecture.md` §2 principle 4 — *secret
names in config, secret values only in the scope* — and it is what lets
`docs/development/contracts/contract1.md` and `config` be confronted by reading them side by side,
which is the whole point of having written the contract.

**There is no `os.environ` anywhere in the agent tree.** An environment variable was only ever needed
to get a secret into the Model Serving container, which has no `dbutils`; how the served copy receives
its configuration is deferred with the rest of the deploy design (§3.3, §17). An `os.getenv` with a
default is also the bug in §21's last row: the default *is* the value whenever the variable is unset,
so a retired model name kept answering while the scope already held the new one.

```python
CATALOG = "finhive-2026"          # one catalog holds the model services and every medallion layer
GOLD = f"`{CATALOG}`.gold"        # the hyphen is an identifier SQL only accepts backtick-quoted
TBL_TECHNICAL = f"{GOLD}.technical_indicators"
SECRET_SCOPE = "finhive"          # a secret scope is not a catalog
MODEL_ROUTER = f"{CATALOG}.default.finhive_router"
```

| Belongs in `agents/config.ipynb` | Does **not** |
|---|---|
| table names, catalog and schema, model service names, the two base URLs (§4.1), VS endpoint/index names — as literals | any function with logic, and no `check`: it is variables only, the same category §18 already grants `graph/state.ipynb` |
| the role table: role → temperature, max_tokens (§4.1) | algorithm thresholds (`0.62`, `0.5`, `0.35`, `MIN_AMBIGUOUS_COUNT`) and the 300-token floor — measured properties that stay in the file that owns them, next to the note saying how they were measured |
| the secret scope name, and the *key names* the agent reads — **kebab-case**, matching `fred-api-key` in the data engineer's `boostrap_secrets` | secret values, anywhere, ever — including as a fallback default |
| `DEFAULT_MAX_CONCURRENCY`, `MODEL_TIMEOUT_SECONDS`, `MAX_SYNTHESIS_ATTEMPTS`, `GRAPH_RECURSION_LIMIT`, the cache TTL per question type (§14.1), the registered-model name and its `champion` alias (§17) | prompts, and the `DISCLAIMER` text (legal text, owned by `guardrails/`) |

`config` calls nothing and reads nothing at run time; a `%run` of it costs assigning some variables.
A name arrives in it together with the notebook that consumes it — the VS endpoint and index names
wait for `tools/news_tools`, the MLflow experiment and registered-model name for `serving/`, because
inventing an identifier earlier means inventing one nobody has confirmed. The agent has no credential
of its own yet: `fred_api_key`, already in the scope, is `data_ingestion`'s.

### 3.2 Prompts live with the node that uses them

There is no `prompts/` folder. Each system prompt is a module-level constant in the file of the
node that owns it, with a **subject-prefixed name** (`PLANNER_SYSTEM_PROMPT`,
`GRADE_SYSTEM_PROMPT`, `SYNTHESIZER_SYSTEM_PROMPT`). The reason is `%run`: every module shares
one namespace, and two `_SYSTEM_PROMPT` constants overwrite each other silently. The one
exception is `OPINION_CONTRACT`, which exists exactly once, in `experts/base.ipynb`, because the
moment two experts disagree about what "bullish" means the consensus arithmetic stops meaning
anything.

### 3.3 Composition: `%run`, and the one place it cannot reach

**Notebooks are `.ipynb` and compose with `%run`**, the same mechanism `architecture.md` §6.2 uses for
`data_ingestion` and `data_modeling`. One format across the repo, and a notebook stays a notebook.

`%run` executes the target **inline, in the caller's namespace**, which settles three things at once:

1. **`if __name__ == "__main__"` does not work here.** `%run` is not an import — `__name__` stays
   `"__main__"` in the target too, so a guard fires on every caller. That is why nothing auto-runs
   (§3): a notebook defines, and the caller decides when to spend money. `%run ./llm/gateway` costs
   assigning names; `check()` costs nine model calls and is called by hand.
2. **Unique global names** (§3.2). One namespace per run, which is exactly the constraint that makes
   subject-prefixed prompt constants mandatory rather than tidy.
3. **Dependencies arrive as arguments.** Every function that needs a model, a table or an index takes
   it as a parameter. `get_chat_model` is passed into the graph; no node reaches for a global.

**The place `%run` cannot reach is the Model Serving container** — the magic does not exist there, and
an `.ipynb` is not importable, so neither `%run` nor `import` gets the tree into a served artifact.
**The deploy design is therefore open** (§17, §22 item 8). The options, none of them chosen:

| Option | Cost |
|---|---|
| the served subset in `.py` (importable), the rest `.ipynb` | two formats in one tree |
| `.ipynb` throughout, `nbconvert` to `.py` at package time | what a PR reviews is not byte-identical to what the container runs |
| `.ipynb` throughout, no Model Serving — the agent runs as a job | rewrites `architecture.md` §11; `apps/api` cannot answer in seconds |

Nothing in §4–§16 depends on which one wins: every notebook takes its dependencies as arguments and
holds no ambient state, which is what keeps all three reachable. What *does* depend on it is §17 and
§19, and both say so.

Alternatives rejected: an importable agent package (contradicts `architecture.md` §0, no second home
for agent code); dual-mode files that `%run` and re-import on `NameError` (unreadable); a `pipeline/`
folder of stage notebooks that only call other notebooks' checks (indirection with no consumer).

**Consequence for CI.** `check_notebook_layering.py`'s `%run` pass sees almost nothing inside
these folders, so its checks become import-based: `agents/config` may import
nothing but the standard library, and `llm/parsing.ipynb` nothing but the standard library and `pydantic`; `graph/opinion.ipynb` and
`tools/*` (except the file that wraps a LangChain tool) may not import `langchain`, `mlflow`, `pyspark` or `databricks`; and
the direction above is enforced as a folder-level import contract. The pure logic that used to
sit in `setup/core/` is now protected by that list rather than by living in a folder.

**Departure from `architecture.md`, to reconcile there:** that document puts `contracts/`,
`core/`, `ports/` and `adapters/` in `setup/` and describes the `TableStore` / `VectorIndex` /
`ChatModelProvider` ports. This layout empties `setup/` of all of that for the agent half. The
agent code receives its table reader and its index as **arguments**; where they come from (a
container, a widget, a job parameter) is `architecture.md`'s decision, and its §3, §3.1 and §4
need updating to match.

**A second departure, to reconcile the same way:** `architecture.md` §9 names the catalog
`finhive_free.analytics` and the tables `gold_*`. There is one catalog, **`finhive-2026`**, holding the
model services and every medallion layer, and the agent reads `finhive-2026.gold.*` — the shapes are
specified in `docs/development/contracts/contract1.md`, which is the authority on those tables. The
hyphen makes the catalog an identifier SQL only accepts backtick-quoted, so `config` stores the quoted
form.

---

## 4. Model access (`llm/`)

One module (`notebooks/agents/llm/gateway.ipynb`) is the only code in the repo that constructs a model
client. Its surface is `get_chat_model(role, temperature, max_tokens)` and
`get_embedding_model()`; every other folder receives that function as an argument and never
learns what a gateway is.

### 4.1 The model names, and roles

There are four model names and **two paths off one host** — one client shape, because the two
`finhive` services and Databricks' own pay-per-token models answer in different places. The paths and
the names are literals in `agents/config.ipynb` (§3.1); the host is never written down, it comes from
the notebook context together with the token (§5.3 there: no PAT anywhere):

```python
AI_GATEWAY_PATH = "/ai-gateway/mlflow/v1"   # the two finhive services
SERVING_PATH    = "/serving-endpoints"      # Databricks pay-per-token models

MODEL_ROUTER     = f"{CATALOG}.default.finhive_router"     # CATALOG = "finhive-2026"
MODEL_EMBEDDINGS = f"{CATALOG}.default.finhive_embeddings"
MODEL_GUARD_IN   = "databricks-meta-llama-3-1-8b-instruct"
MODEL_GUARD_OUT  = "databricks-gpt-oss-20b"

# the Model Serving ENDPOINT names of the two routed models - not the system.ai.* model names the
# router is configured with, so this ships empty rather than guessed (§22 item 10)
MODELS_ROUTED = ()
```

Both paths are OpenAI-compatible, so one `ChatOpenAI` construction serves both and only `base_url` and
`model` differ. `gateway` makes one cached call to the notebook context and gets the host and the token
from it, so no workspace URL is a literal and the code runs unedited in any workspace.

| Name | Purpose | Consumed by |
|---|---|---|
| `finhive_router` | a UC model service you created; it routes each request to an underlying LLM itself — **70% `system.ai.llama_v3_3_70b_instruct` and 30% `system.ai.gpt-oss-20b`**, both pay-per-token | the roles `router`, `worker`, `synthesizer` — the planner, every expert, the synthesizer |
| `finhive_embeddings` | embeddings, only where the code itself must embed — the semantic cache's query vectors (§14.1) | the `embedding` role |
| `MODEL_GUARD_IN` | a small, non-reasoning, pay-per-token endpoint | the `guard_in` role |
| `MODEL_GUARD_OUT` | the cheapest pay-per-token endpoint that can compare figures against a long evidence base | the `guard_out` role |

`gateway.ipynb` is the only code that maps a role to one of these names, and it holds the map as
`ROLE_MODELS: role -> (base_url, model)`; `config` holds the names, nothing else. This relaxes v1's
"one path to a model" rule twice over: the *names* are four and the *paths* are two. The endpoint the
news index embeds with is chosen where the index is built (`data_modeling`, `architecture.md`); agent
code never names it. There is no fallback path if a model is down; the failure is visible (§1,
invariant 3), and each guardrail already has a defined posture for it (§11).

**`MODEL_GUARD_OUT` is the same model as 30% of the router's own mix.** Not a problem — §11.3 picks it
for being the cheapest per input token, which still holds — but worth knowing before reading a trace
and concluding the router leaked into the guardrail.

`KNOWN_MODEL_ROLES = {"router", "worker", "synthesizer", "guard_in", "guard_out", "embedding"}`. There
is no `critic` role (its only caller was the output guardrail, now `guard_out`) and no `judge` role:
the judges that score answers are configured in MLflow (§16.2). An unknown role raises.

A role is a **label with three jobs**: it sets the model, temperature and token cap; it tags the MLflow
trace so a run shows which part of the graph spent tokens; and it documents intent. The router service
routes per request and was observed answering as two different models on consecutive calls; the
guardrail endpoints are single fixed models.

| Role | Used by | Model | Temperature | max_tokens |
|---|---|---|---|---|
| `router` | planner | `finhive_router` | `0.0` (through `ask_structured`) | `600` |
| `worker` | every expert (ReAct) | `finhive_router` | `0.1` | `1600` |
| `synthesizer` | final composition | `finhive_router` | `0.2` | `2000` |
| `guard_in` | input guardrail | `GUARDRAIL_INPUT_MODEL` | `0.0` (through `ask_structured`) | `300` |
| `guard_out` | output guardrail | `GUARDRAIL_OUTPUT_MODEL` | `0.0` (through `ask_structured`) | `800` |
| `embedding` | semantic cache (§14.1) | `finhive_embeddings` | — | — |

**Because routing happens per request, the response *shape* changes between calls** — `str`
vs a list of content blocks, with `reasoning`/`thinking` blocks on some models.
`message_text(content) -> str` (in `llm/parsing.ipynb`) normalizes all of it: accepts a
message object, a `{"role","content"}` dict, a string, a list of blocks or `None`; drops
reasoning blocks; never returns `None`, never raises. **Call it on every model output in the
codebase**; there is no exception where the raw content is safe to read.

### 4.2 Structured output with Pydantic

Every node that needs data from a model — the planner and both guardrails — declares a **Pydantic
model** for the answer and asks for it through one function, `ask_structured`. The schema is sent to
the gateway as `response_format` (a JSON schema, `strict`), so the server constrains the output to that
shape, and the reply is then validated by Pydantic. There is no fence stripping and no hunting for a
`{` in prose.

```python
class InputVerdict(BaseModel):                       # lives with the node that owns it
    verdict: Literal["allow", "refuse"]
    category: Literal["on_topic", "off_topic", "prompt_injection", "advice_request"]
    reason: str

def ask_structured(chat, role, system_prompt, user_content, schema, default):
    bound = chat.bind(response_format={"type": "json_schema", "json_schema": {
        "name": schema.__name__, "schema": schema.model_json_schema(), "strict": True}})
    messages = [system(system_prompt), user(user_content)]
    for _ in range(2):                               # first attempt + one correction
        try:
            text = message_text(bound.invoke(messages))
            return schema.model_validate_json(text)
        except ValidationError as err:
            messages += [assistant(text[:500]), user(f"That did not validate: {compact(err)}. "
                                                     "Answer again with an object that satisfies the schema.")]
        except Exception:                            # network, timeout, refusal: no second call
            break
    return default                                   # an instance of `schema` — never raises
```

- **`message_text` still runs first.** The router service changes the model per request, and a reasoning
  model can return a list of content blocks; the text is normalized before it is validated.
- **The retry carries Pydantic's error**, which names the exact field that was wrong, instead of a generic
  "answer again". One correction, then `default`.
- **The caller's `default` is an instance of the schema and remains a risk decision**, the most
  important line in each caller. Input guardrail → `verdict="allow"` (an unavailable classifier must not
  break the product). Output guardrail → `grounded=True` (the answer still ships, with its disclaimer).
  Planner → no mentions and every sub-question empty, which the code turns into the full panel (or the
  macro analyst alone if no instrument resolved).

**The schemas have to fit what the gateway accepts.** Databricks structured outputs support only a
subset of JSON Schema: no `pattern`; no `anyOf`, `oneOf`, `allOf`, `prefixItems` or `$ref`; a limited
number of keys (64 for structured outputs, 16 if function calling is used instead); and heavy nesting
degrades generation. Pydantic emits exactly the unsupported constructs by default — `Optional[X]`
becomes `anyOf`, a nested model becomes `$ref`, `dict[str, str]` becomes `additionalProperties` — so the
rules for every schema are:

- **Flat.** No nested models, no `dict`, no `Optional` or `Union`. Use `Literal`, `bool`, `str`,
  `float`, `list[str]`. `additionalProperties` is the one key that has to be judged by its *value*
  rather than its name: `false` is what strict mode wants and pydantic emits it for
  `extra="forbid"`, while a `dict` field emits a schema there instead. `check_schema_supported`
  distinguishes the two.
- **Every field required**, with the *absence* encoded as a value (an empty string, an empty list),
  because strict mode requires all keys.
- **Field descriptions carry the meaning**, because the model reads them as part of the schema.
- `check_schema_supported(schema)` scans `schema.model_json_schema()` for the forbidden keys and the key
  limit. The `check` of each node that owns a schema calls it for that schema (§19.3), so a schema that drifts
  fails in the job, not on the first question.

**The three schemas**

```python
class PlannerOutput(BaseModel):          # graph/planner.ipynb
    mentions: list[str]                  # instruments as the user wrote them
    technical_analyst: str               # the sub-question for that expert; "" = do not consult
    quant_risk_analyst: str
    fundamental_analyst: str
    macro_analyst: str
    news_analyst: str
    reasoning: str

class GroundednessVerdict(BaseModel):    # guardrails/output_guardrail.ipynb
    grounded: bool
    reason: str
    unsupported: list[str]               # the figures the draft states that the evidence lacks
```

`InputVerdict` is the one shown above (`guardrails/input_guardrail.ipynb`). The planner's sub-questions are
five flat fields, not a dictionary: a dictionary is `additionalProperties`, which the gateway may
reject, and an empty string is an unambiguous way to say "do not consult".

**What the routing mix means for validation.** `finhive_router` sends about 70% of requests to Llama
3.3 70B and 30% to GPT-OSS 20B, chosen per request, and the two behave differently:

| | Llama 3.3 70B | GPT-OSS 20B |
|---|---|---|
| Reasoning | no — the answer is the content | yes — thinking tokens come first and count against the cap |
| Content shape | plain string | may be a list of blocks with reasoning parts |
| Function calling | supported | supported |
| Structured outputs (`response_format`) | **not confirmed by the docs consulted** | documented for the GPT-OSS models |

The consequences: the schema must be **enforced by the server** (a prompt alone would let 30% of calls
drift), Pydantic validates **regardless**, the planner's cap is `600` (not `400`) because of the
reasoning share, and a reply cut off by the cap fails validation and lands on the retry — so a run of
`default`s on the planner is the symptom to look for.

**Probing the router is not enough, and this is the trap.** The router picks per request, so one
structured round trip through it exercises one of the two models with 70/30 odds. If Llama ignores
`response_format` and GPT-OSS honours it, that probe passes seven times in ten while the graph fails
intermittently in production — a green check and a broken system. So `llm/gateway`'s `check` (§18)
probes structured output **three** times: through the router service, and against each model in
`MODELS_ROUTED` **directly**, on `SERVING_PATH`. Only the direct pair is conclusive — and it needs the
two *endpoint* names, which are not the `system.ai.*` *model* names the router is configured with, so
`MODELS_ROUTED` ships empty and those two probes stay off until someone reads the names off the
workspace. Until then a green `structured/router` row proves one of the two, not both. If a model does
not honour `response_format`, that node switches to **function calling** — a single tool whose arguments are
the same schema, with the call forced — which both models document; nothing about the schemas or the
callers changes.

**Where this stops.** The experts' judgement block (§8.1, §9.1) is still parsed by `opinion.ipynb` as
before; the same mechanism could take it over later, but that is a separate decision.

---

## 5. The tool layer

Tools are where the system's credibility lives. `resolve_symbol` and every
`*_body(frame, ...) -> str` are pure; each domain file in `tools/` holds its bodies and the
LangChain wrapping around them, and `symbols.ipynb` and `safe_tool.ipynb` are shared.

### 5.1 Contract

```
tools/<domain>_tools.ipynb   pure *_body(frame, ...) -> str      deterministic, no I/O
                          safe_tool(fn) → @tool(...)          docstring IS what the model reads
                          build_*_tools(frame) -> list[BaseTool]
tools/symbols.ipynb          resolve_symbol, UnknownSymbolError  shared by every domain
tools/safe_tool.ipynb        the exception-to-instruction wrapper
```

- **Every output states its "as of YYYY-MM-DD" date.** `opinion.ipynb` regex-scans tool output
  for exactly that pattern to date the evidence, and the experts are told to repeat it. It is
  a contract, not a nicety.
- **Missing values render `n/a`, never `0`.** A zero is a number a model will reason about.
- **Tools return labelled prose tables, not JSON.** The consumer is a language model; text
  with units survives truncation better than nested JSON.
- `safe_tool` catches everything and returns
  `"ERROR in {name} ({exc_type}). This data is not available right now. Try a different tool or
  different arguments. If there is no alternative, tell the user this specific figure could
  not be retrieved — do not estimate it."` The *instruction* matters more than the message:
  an error that does not say what to do next produces an invented number. **Only the exception
  type crosses into the model's context, never the message** — an HTTP client puts the request URL
  in its exception text and a URL can carry a key in its query string, which `architecture.md` §5.2
  forbids forwarding and §21 records as a real incident. The type is all the model can act on
  anyway: a `TimeoutError` is worth retrying, a `KeyError` is not.
- `resolve_symbol(frame, symbol)`: uppercase → alias table (`BITCOIN/BTC→BTC-USD`,
  `EUR/USD→EURUSD=X`, `APPLE→AAPL`, `NASDAQ→^IXIC`, …) → `{s}-USD` → `{s}=X` → a **unique**
  name-prefix match; otherwise raise `UnknownSymbolError` telling the model to call
  `list_instruments`. **Never near-match** — no edit distance, no closest guess. Silently answering
  about the wrong instrument is worse than failing. Two consequences of that order look like
  near-matching and are not: a fragment resolves when it prefixes exactly one name, so `APPL` and
  `Apple Inc` both reach `AAPL` and no minimum fragment length is invented; and an alias is an
  explicit decision, so it wins even where the names alone would be ambiguous. Every candidate is
  checked against the frame, so a resolved symbol always has data — an alias pointing at an
  instrument nobody ingested raises like any other miss. **An alias names the same instrument,
  never a proxy for it**: `SPX→SPY` is forbidden because `SPX` is the index and `SPY` is an ETF
  tracking it, and so is `S&P 500→SPY`, which is the same substitution under a friendlier name. A
  reader asking about an index the universe lacks gets a miss, sees the ETF described as an ETF in
  `list_instruments`, and substitutes knowingly — the resolver does not decide that quietly.
- One tool should answer the whole question: every domain ships a `compare_*` tool so a
  four-instrument question costs one call, not four.

### 5.2 The tool set (20)

| Expert | Tools |
|---|---|
| Technical | `list_instruments`, `get_price_snapshot`, `get_technical_indicators`, `get_trend_signals`, `get_price_history`, `compare_instruments` |
| Quant/Risk | `get_risk_profile`, `compare_risk`, `get_correlations`, `get_diversification` |
| Fundamental | `get_fundamentals`, `compare_valuation`, `get_sector_peers`, `get_fundamentals_coverage` |
| Macro | `get_macro_snapshot`, `get_macro_series`, `get_yield_curve`, `get_inflation_picture`, `list_macro_series` |
| News | `search_news` (over the vector index, §8.3) |

Rule-based interpretation lives in tools, not prompts: `get_trend_signals` labels RSI ≥ 70
overbought / ≤ 30 oversold, golden and death crosses, vol regimes (20d/60d > 1.2 elevated,
< 0.8 subdued), and says in its own output that it is "not a recommendation". Macro tools
report **absolute** changes in points for `unit == "percent"` series and percent changes
otherwise, and always give the five-year percentile.

### 5.3 Data residency

`tools/panel_data.ipynb` reads the gold tables **once** (it receives the table reader as an argument) into
`PanelData` — a frozen dataclass of five pandas frames (technical, risk, correlations,
fundamentals, macro), a few megabytes in total — and the tool closures capture it. That is
what makes a tool call cost microseconds instead of a Spark job: the difference between a
panel that answers in seconds and one that does not. **No tool may issue a Spark query at
request time.** The one remote read a tool may make is a Vector Search query (`search_news`, §8.3);
it is a read-only call to a Databricks service, not a table scan. Inside the serving container this load happens once at model load, not per
request (§17).

---

### 5.4 Deferred — tools as Unity Catalog functions behind the managed MCP server

**Not part of this build.** Tools stay as Python `*_body` functions over `PanelData` (§5.3). This
section records what was found, so the idea can be picked up later without redoing the research, and
which seams of the current design keep it cheap to adopt.

**What exists.** Databricks hosts a managed MCP server for Unity Catalog functions (**Public Preview**
at the time of writing): `https://<workspace>/api/2.0/mcp/functions/{catalog}/{schema}[/{function}]`.
No server to run; permissions are UC `EXECUTE` grants. A Python agent connects with
`databricks-mcp`'s `DatabricksMCPClient` (or `DatabricksMultiServerMCPClient` in `databricks-langchain`
for LangGraph); on Model Serving the functions are declared as resources at log time
(`DatabricksFunction`, `get_databricks_resources()`) with automatic auth passthrough. Sources:
docs.databricks.com/aws/en/generative-ai/mcp/managed-mcp and
docs.databricks.com/aws/en/agents/mcp-tools/use-mcp-in-agents.

**How it would be done.** Each tool becomes a `CREATE FUNCTION` whose `COMMENT` is the description the
model reads; one **schema per expert** (technical, risk, fundamental, macro, news), because the URL
takes a schema, so each expert sees only its own tools; each expert's ReAct agent gets its tools from an
MCP client instead of `build_*_tools(frame)`; the functions are deployed by an idempotent notebook
(`CREATE OR REPLACE FUNCTION`); `safe_tool` becomes a client-side wrapper so an error still says "do not
estimate".

**What makes it a real project, not a swap.**

- **Python UDFs run in a sandbox with no access to files or internal services**, so they cannot read the
  gold tables. Data-reading tools would have to be **SQL functions**, and the prose formatting ("as of",
  comparison tables) rewritten in SQL — 19 tools. (SQL returning JSON plus a Python formatter is the
  alternative.) A query may call at most five UDFs.
- **Unverified:** whether the MCP server exposes SQL *table* functions, and its timeouts and result-size
  limits; whether it is available on Free Edition at all (serverless-only, with quotas).
- **Latency and quota.** Every call becomes an execution on serverless compute instead of a lookup in
  memory, and a five-expert panel makes about 15 calls a question. It reverses the rule of §5.3, and
  the "microseconds" argument that motivates `PanelData`. A per-process cache of `(tool, arguments)`
  would soften it.
- `search_news` would stay a local tool: it needs MMR and delimiters, which the managed AI Search MCP
  server does not do.

**Spike before committing.** Two tools — `get_price_snapshot` and `get_sector_peers` — as UC functions,
called through the MCP client from a Model Serving endpoint. Measure latency per call, confirm that table
functions are exposed, and confirm it runs on the Free Edition workspace. If a call takes more than a
couple of seconds, or Free Edition refuses it, keep `PanelData`.

**Seams already kept open, so nothing here has to be undone:**

- Every expert's tools are assembled in **one place** (`build_*_tools(frame)` in each `tools/` file),
  so the replacement is a change of source, not of the experts.
- The behaviour contract of §5.1 — "as of" dates, `n/a`, the error instruction — is stated per tool, not
  per implementation; a SQL function must honour the same.
- `opinion.ipynb` uses a tool's name only as an opaque `ref` (§9.1). MCP names look like
  `catalog__schema__function`, which needs no change.
- Bodies take their data as an argument, never as a global (§3.3).
- The agent environment would gain `databricks-mcp` and `databricks-langchain`, pinned exactly.

---

## 6. Which experts apply — rules in the planner prompt

There is **no capability matrix and no `capability.py`**. The rules for which expert to consult
live in the planner's system prompt (§7), as plain sentences the model reads on every question.
This is a deliberate simplification: a table of asset classes and a function to intersect it
with the model's choice was more machinery than the rules deserve.

The experts are `technical_analyst`, `quant_risk_analyst`, `fundamental_analyst`,
`macro_analyst` and `news_analyst` (§8.3). The rules, as the prompt states them:

| Rule | Reason given to the model |
|---|---|
| **Never call `fundamental_analyst` for crypto or FX** | no issuer, no financial statements, no earnings |
| **Never call `news_analyst` when no instrument is named** | the news feed is indexed per instrument; a question about rates or inflation is the macro analyst's |
| **For a pure macro question (no instrument), call only `macro_analyst`** | the instrument experts have nothing to read |
| **ETFs: call `fundamental_analyst` only if the question is about valuation or holdings** | fundamentals data for ETFs is partial |
| **A broad question about an instrument ("what do you think of X") calls every expert not excluded above** | a panel is the product |
| **Include an expert only if it genuinely contributes** | a panel of five on a question that needs one is waste |

**What this trades away, stated plainly.** The routing is now a property of a prompt, and the
model behind the gateway changes per request (§4.1). A prompt can be argued with, and it can
drift when the underlying model does — the same drift that made the input guardrail refuse and
allow the same question on consecutive runs. Nothing in code stops the planner from calling the
fundamental analyst for Bitcoin any more. What remains is the cheap visible failure: that expert's
tools have no data for the instrument, so it returns `insufficient_data` (§9.1), is excluded
from the consensus, and the answer says so — a wasted call, not a wrong answer. The safeguard is
a **gate**, not a function: the golden set's panel cases (§16.1) assert that a crypto question never
convenes the fundamental analyst and that a pure macro question convenes only the macro one, and
they are run in MLflow before each deploy (§16.2).

There are also no `not_applicable` cards: an expert that is not consulted simply does not appear.

---

## 7. Planner (`graph/planner.ipynb`)

The planner is the supervisor. It is one model call plus a few lines of code.

**The model does the routing** — it reads the question and returns a `PlannerOutput` (§4.2) through
`ask_structured(chat, "router", …)`: the `mentions`, one sub-question per expert, and a `reasoning`. An
expert whose field is **non-empty is consulted**; an empty string means it is not. Prompt content: the list of experts and what
each covers; the routing rules of §6, written as instructions; list only instruments the user
named, as they wrote them, never expanding a sector or an index into its members; write each
sub-question in that expert's own terms.

**Code does what still needs a guarantee:**

```python
EXPERTS = (TECHNICAL, QUANT_RISK, FUNDAMENTAL, MACRO, NEWS)        # fixed order
symbols, unresolved = resolve_mentions(frame, mentions)            # hallucinated tickers die here
sub_questions = {e: getattr(plan_out, e) for e in EXPERTS if getattr(plan_out, e).strip()}
experts = list(sub_questions)                                      # non-empty fields, fixed order
if not experts:                                                    # planner failed or returned nothing
    experts = list(EXPERTS) if symbols else [MACRO]
    sub_questions = {e: DEFAULT_SUB_QUESTION.format(question=question) for e in experts}
```

`Plan = {question, symbols, experts, sub_questions, unresolved_mentions, reasoning}`.
Two things stay in code on purpose: ticker resolution, because a hallucinated instrument must not
reach an expert, and the fallback, so a failed planner call still produces a panel. Everything
else about *who* to ask is the model's, within the rules of §6.

---

## 8. Experts (`experts/`)

```python
build_expert_agent(chat, name, domain_prompt, tools, max_tokens=1600) =
    create_react_agent(model=chat("worker", temperature=0.1, max_tokens=max_tokens),
                       tools=tools, prompt=domain_prompt + OPINION_CONTRACT, name=name)
```

ReAct from `langgraph.prebuilt`, not a hand-rolled loop: the loop is not where the value is,
and the prebuilt one keeps traces legible.

### 8.1 `OPINION_CONTRACT` — defined once, appended to every expert

One constant, because the moment two experts disagree about what "bullish" means, the
consensus arithmetic stops meaning anything. Four blocks:

1. **Grounding.** Every figure must come from a tool call *in this conversation*; never
   compute, estimate, recall or interpolate; always report the tool's "as of" date; if a tool
   errors, say so plainly ("I could not retrieve that" is a correct answer, an invented
   number never is); prefer the tool that answers in one call.
2. **The judgement block.** Prose first (direct answer, then evidence), then end the message
   with exactly this and nothing after it:
   ```json
   {"stance": "bullish|bearish|neutral|insufficient_data", "confidence": 0.0,
    "horizon": "intraday|short|medium|long",
    "key_findings": ["one short sentence per finding, at most four"],
    "caveats": ["what would change your read, or what you could not see"]}
   ```
3. **Field semantics.** `stance` is what the evidence **in your domain** shows, not advice.
   `insufficient_data` is to be used honestly — a panel is more useful when one member admits
   it has nothing than when it pads. `confidence` reflects how strong and consistent the
   evidence is, not conviction: conflicting indicators mean lower confidence whatever the
   stance.
4. **Positioning.** You are one expert on a panel; others cover what you cannot see; do not
   hedge into vagueness to sound balanced, or stretch beyond your domain to sound complete.
   Never buy/sell/hold, never a position size, never a price target.

### 8.2 Domain prompts (intent — keep the substance, rewrite the wording freely)

- **Technical** — trend (price vs moving averages, crossovers), momentum (RSI, MACD),
  volatility (ATR, realized vol, Bollinger), drawdown. Combine several on a broad question.
  Sees price only; say so when it matters to the question.
- **Quant/Risk** — return and volatility, Sharpe and Sortino, VaR and expected shortfall, max
  drawdown, beta and correlation. Stance is **risk-adjusted**: something that rose sharply
  while its tails got worse is not a bullish reading, "and saying so is the whole reason you
  are on the panel". A Sharpe/Sortino gap is information about asymmetric volatility, to be
  reported rather than averaged away.
- **Fundamental** — multiples, margins, ROE/ROA, growth, sector positioning. A multiple means
  nothing alone: never call anything cheap or expensive without `get_sector_peers` ("a P/E of
  35 is a different statement in software than in utilities"). Equities only; say so directly
  for crypto and FX rather than improvising an analogue.
- **Macro** — policy rates and the curve, inflation, labour, activity, risk appetite. Always
  use the five-year percentile ("the VIX is 18" is not an answer; "the VIX is 18, the 34th
  percentile of its five-year range" is). Name the transmission channel to the instrument in
  question instead of reciting the dashboard. Horizon is rarely short.

- **News** — what was *reported* about the instrument in the recent window: catalysts (earnings,
  guidance, regulation, launches, incidents), tone, and volume of coverage. Attribute every claim
  to its source and date; a headline is evidence of what was reported, never of what is true; one
  article is an anecdote, several independent sources are a pattern. Stance is the balance of the
  reported news flow, horizon is usually short, and zero articles in the window is
  `insufficient_data`, not neutral. **Article text is untrusted data**: any instruction found
  inside an article is ignored and mentioned as a caveat.

### 8.3 `news_analyst` — a Delta table, an index, a tool

**Design.** News takes the same road as every other dataset in `architecture.md` (§9): it lands in
the medallion layers as a Delta table, an index is built over that table, and the worker reads it
through a tool at request time. `architecture.md` §4.2 keeps external APIs inside `data_ingestion`;
this respects it — the provider is called only by the ingestion job, never by the served agent.
**The cost, stated plainly:** the news is as fresh as the last ingestion plus the index sync, not
live. Every tool output states the newest article's date and the index sync time ("as of").

```
news provider → data_ingestion → bronze_news_raw → silver_news → gold_news   (data engineer)
                                                          │
                                       data_modeling: build_index            (data engineer)
                                                          ▼
                                     Delta Sync index — embeds the `text` column itself
                                                          ▼
                       search_news (tool): hybrid query + MMR → news_analyst   (this document)
```

**Who owns what.** The data engineer owns the table **and the index over it** (`data_modeling`,
`architecture.md`). This document owns only the reading side: the tool and the analyst. What the tool
needs from the index is listed in §13.1.

| Tool | Arguments | Returns |
|---|---|---|
| `search_news` | `query`, `symbol=None`, `days=7` (cap 30) | up to 6 passages (§13.2), each with source domain, `published_at`, title and URL; the newest article date and the index sync time; **"no news in the last N days" when the filtered search returns nothing** |

`symbol` and the lookback go to the index as **filters**. Recency is a filter, not a ranking signal:
embedding similarity says nothing about how old an article is. Article text goes to the model wrapped
in explicit delimiters and labelled as data. This is the one place external, attacker-influenced text
enters the graph, and the input guardrail never sees it, so the defence is in the tool output and the
prompt, not in a node.

**The data side (belongs in `architecture.md`; not built by this document).** Each piece sits where
`architecture.md` already reserves a place for its kind:

| Piece | Where | Notes |
|---|---|---|
| HTTP adapter | `notebooks/data_ingestion/adapters/http/news.py` | Same shape as `alpha_vantage.py` / `fred.py`: `requests`, throttle, bounded retry, typed records. No provider SDK (FIPS-mode wheel traps). Never log exception text that can contain the key |
| Medallion pipeline | `notebooks/data_ingestion/pipelines/news.py` | `bronze_news_raw` (append-only, `source`, `ingested_at`) → `silver_news` (deduplicated by `row_number()` over `(symbol, url)`) → `gold_news`, the table the index is built on. Expectations on write: non-null `url` and `published_at`, `published_at` not in the future, row-count bounds; failures land in `quality_violations` and fail the task |
| Index build | `notebooks/data_modeling/build_index.py` | The existing job of `architecture.md`, pointed at `gold_news`; its lifecycle rules (two-sided convergence, progress-based waits, `force_rebuild`) are its own. **Not built by this document** |
| Job notebook / job | `data_ingestion/ingest_news.py`, `deploy/databricks/jobs/ingest_news.yaml` | Same prologue as the other ingest notebooks; serverless, exact pins, `requests` only. In `finhive_daily_pipeline`, followed by the `build_index` sync task |
| Secret | provider key (`tavily-api-key`) added to the `secrets` list in `notebooks/setup/boostrap_secrets.ipynb`, kebab-case | read with `dbutils.secrets.get`, sanitised, redacted in logs |
| Config | key name, lookback, max articles per symbol, symbol subset, retention window | Data, not code (`architecture.md` §5.1) |
| Probe | `capability_probe` adds outbound reach to the news provider | Fails the environment check early |

**Quota warning.** A daily call per symbol over the 35-instrument universe is roughly 1,000 calls a
month, at or above the free plan of most search APIs (Tavily's was about 1,000 credits a month when
this was written — verify the current figure). Restrict ingestion to equities, crypto and the major
ETFs, or run it every other day. The same restriction bounds the index size (§13.1).

---

## 9. OpinionCard and consensus (`graph/opinion.ipynb`)

```python
OpinionCard = {expert, stance, confidence, horizon, narrative, key_findings,
               caveats, evidence: [{source_type: "tool", ref, detail, as_of}],
               tool_calls, degraded, error}
```

### 9.1 The model declares judgement; code records evidence

`evidence` is rebuilt from the agent's own messages where `type == "tool"`:
`ref = message.name`, `detail = content[:1200]`, `as_of` = first `as of (\d{4}-\d{2}-\d{2})`
match, `tool_calls = len(evidence)`. **The model cannot fabricate its own evidence list**, and
a card claiming tool-backed figures with `tool_calls == 0` is visible immediately.

Parsing is defensive: `parse_judgement` takes the **last** fenced JSON block containing
`"stance"`, falling back to the last `{…}` span; the narrative is the text with that block
removed. No parseable judgement → a **degraded card** (`stance="neutral"`, `confidence=0.25`,
`horizon="medium"`, `degraded=True`, plus a caveat saying so) rather than an exception.
Coercion: invalid stance → `neutral`; invalid horizon → `medium`; unparseable confidence →
`0.3`; a value `> 2.0` is read as a percentage and divided by 100; clamp to `[0,1]`; lists
truncated to 8.

One synthetic card completes the set: `failed_card(expert, error)`
(`stance="insufficient_data", confidence=0, error=…`), produced when an expert raises.

### 9.2 Consensus is arithmetic, never a model

```python
_STANCE_SCORE = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}   # insufficient_data excluded
_DIRECTIONAL_THRESHOLD   = 0.5
_DISAGREEMENT_DISPERSION = 0.35

participating = [c for c in cards if c.stance in _STANCE_SCORE and not c.error]
w   = max(confidence, 0.05)
net = Σ s·w / Σ w
dispersion = min(sqrt(Σ w·(s − net)² / Σ w), 1.0)
label      = "bullish" if net > 0.5 else "bearish" if net < -0.5 else
             ("mixed" if dispersion >= 0.35 else "neutral")
agreement  = weight on the majority side / Σ w · 100
conflicts  = pairs of experts with opposite signs
```

`Consensus = {participating_experts, excluded_experts, net_stance_score (3dp), label,
agreement_pct (1dp), dispersion (3dp), conflicts, mean_confidence}`.

Calibration record: a directional threshold of `0.34` labelled *one bearish + two neutral* as
"bearish" with 36% agreement; not separating neutral from mixed produced "mixed, 100%
agreement". Excluding `insufficient_data` from the arithmetic is what stops an expert with
nothing to say from dragging the panel toward neutral.

---

## 10. Synthesizer (`graph/synthesizer.ipynb`)

`compute_consensus(cards)` → `render_cards_for_synthesis(cards, consensus)` → one
`chat("synthesizer", 0.2, 2000)` call. The rendered context ends with a
`COMPUTED CONSENSUS (… do not recompute)` block.

Prompt rules: quote the consensus figures, never recompute or contradict them; use only
figures present in the cards (the synthesizer has no tools and no data of its own);
**disagreement is the most valuable thing on the page** — name which expert holds which view
and what drives the difference, never split it into a vague middle; report experts that were
unavailable or had no data as stated limits of the answer; never advise. Structure: direct answer in two
or three sentences → evidence by domain with as-of dates and attribution → where the panel
agrees and where it does not → what would change the picture, drawn from the caveats. Prose,
no JSON.

Empty model output → `fallback_answer` renders every card verbatim with the consensus line.
The panel already did the work; a silent model must not throw it away.

**On a retry** (§11.2) the synthesizer reads `unsupported` and `groundedness_reason` from the
state and appends a block to its input: *"REVISION REQUIRED. Your previous draft stated these
figures, which are not in the evidence: […]. Rewrite it using only the figures in the cards."*
It increments `synthesis_attempts` on every run. The experts are **not** re-run: their evidence is
fixed and grounded by construction, and a fabricated figure almost always appears when the
synthesizer paraphrases.

---

## 11. Guardrails (`guardrails/`)

### 11.1 Input — fails open

`ask_structured(chat, "guard_in", PROMPT, last_user_text, InputVerdict,
default=InputVerdict(verdict="allow", category="on_topic", reason="guardrail unavailable"))`
(§4.2) — a `verdict` of `allow` or `refuse`, a `category` of `on_topic`, `off_topic`,
`prompt_injection` or `advice_request`, and a one-sentence `reason`.

**The line is personalization, not topic.** A question about an instrument is research however
evaluative it sounds ("Is Bitcoin attractive?", "Is NVDA overvalued?", "Is this a good entry
point for AMD?"); a question about the user's own money is advice ("Should I buy NVDA?", "How
much of my portfolio should be in crypto?"). The test is whether answering requires knowing
circumstances the system does not have. **When torn, allow** — every answer already carries a
disclaimer and can only report what the tools computed, and a guardrail that fires on
legitimate questions teaches users to route around it.

This replaced a vaguer rule ("asks for a recommendation") that refused "Is Bitcoin attractive
at the moment?" on one run and allowed it on the next. The behaviour drifts whenever the model
behind the gateway changes, so the boundary gets its own cases in the golden set (§16.1) rather than
being rediscovered by accident.

Refuse → `Command(goto=END, update={"blocked": True, "block_reason": f"{category}: {reason}",
"messages": [AIMessage(template, name="input_guardrail")]})`; the `advice_request` template
offers research rephrasings of the same question. Allow → `goto="cache_lookup"`, which goes to `planner` on a miss (§14.1).

### 11.2 Output — fails safe, retries up to three drafts

The evidence base, capped at `24_000` chars, has **three** sources: every card's tool evidence
(`[expert -> tool]`), every card's declared judgement, and the computed consensus. *Omitting
the consensus flagged 3 of 6 correct answers as ungrounded*, because the synthesizer was
quoting agreement figures FinHive itself had computed but had never shown the verifier.

`ask_structured(chat, "guard_out", PROMPT, f"TOOL OUTPUT:\n{evidence}\n\nDRAFT ANSWER:\n{answer}",
GroundednessVerdict, default=GroundednessVerdict(grounded=True, reason="verifier unavailable",
unsupported=[]))` (§4.2) — `grounded`, a `reason`, and `unsupported`, e.g. `["P/E of 42", "an 18% drop"]`. Ungrounded
**only** when a specific figure appears nowhere in the evidence and cannot be read from it;
rounding, rephrasing and restating a consensus figure are explicitly fine. `unsupported` lists the
offending figures, because the retry needs to say *what* to fix.

**The loop.** The node is a conditional router, not a terminal step:

```python
MAX_SYNTHESIS_ATTEMPTS = 3                       # in agents/config.ipynb — total drafts, not extra retries

verdict = ask_structured(...)                    # a GroundednessVerdict
if verdict.grounded or not evidence:
    return finish(answer)                        # answer + DISCLAIMER
if state["synthesis_attempts"] < MAX_SYNTHESIS_ATTEMPTS:
    return Command(goto="synthesizer",
                   update={"grounded": False, "groundedness_reason": verdict.reason,
                           "unsupported": verdict.unsupported})
return finish(warning + answer)                  # drafts exhausted → ship it, visibly flagged
```

- **Three drafts in total**: the first synthesis plus at most two revisions. Each attempt costs two
  model calls (synthesizer + verifier), so the worst case adds four calls and their latency.
- **Only the synthesizer is retried, never the panel** (§10).
- **The retry carries the verifier's findings.** Without them, a second draft at temperature `0.2`
  can reproduce the same mistake.
- **Exhausting the drafts does not withhold the answer.** The last draft ships with a warning
  **prepended to the same message**, then the `DISCLAIMER`. The reader always gets an answer; the
  retry only reduces how often it carries a warning.
- **One message, replaced not appended.** The synthesizer and this guardrail write the answer with
  a fixed message id (`FINAL_ANSWER_ID`), so `add_messages` *replaces* the previous draft. The
  answer is always `messages[-1]`, and a consumer that reads only the last message — most clients,
  every evaluator, and `apps/api` — never sees an earlier draft.
- **No evidence at all** → skip the check. **Verifier unavailable** → treated as grounded, no retry.
- A final message goes to `cache_store` (§14.1), never straight to `END`; the store writes only a
  clean, grounded answer.
- `DISCLAIMER` is appended **in code, once, on the final message only**. Something legally
  load-bearing must not depend on a model remembering it, and must not be stacked once per draft.

**The cost to watch.** The verifier has produced false positives (3 of 6 correct answers before the
consensus was added to the evidence). Under a retry loop a false positive is no longer free: the
synthesizer cannot "fix" a figure that was not wrong, and each wasted attempt is two calls.
the trace carries `synthesis_attempts`, so the evaluation table in MLflow shows how often a retry
happens (§16.2); if retries are common, the verifier is the problem, not the synthesizer.

---

### 11.3 Which models the guardrails use, and why

The guardrails run on **Databricks-hosted pay-per-token endpoints**, not on `finhive_router`, and
therefore on the second path — `SERVING_PATH`, not `AI_GATEWAY_PATH` (§4.1). They are the most
frequent and most mechanical model calls in the graph — one classification per question, and up to
three groundedness checks over a long evidence base — so they are where the cheapest adequate model
pays off most.

| | `guard_in` | `guard_out` |
|---|---|---|
| Job | classify a short question: allow / refuse, plus a category | compare every figure in a draft against up to `24_000` chars of evidence |
| Typical size | ~700 tokens in, ~40 out | ~6,500 tokens in, up to ~300 out (per attempt, up to 3 attempts) |
| Default | `databricks-meta-llama-3-1-8b-instruct` — small and **not** a reasoning model, so a short JSON answer needs no thinking budget | `databricks-gpt-oss-20b` — the cheapest per **input** token, and a reasoning model, which helps at checking numbers |
| List price, DBU per 1M tokens (in / out) | 2.143 / 6.429 | 1.000 / 4.286 |
| Cost per call, DBU (estimate) | ≈ 0.0018 | ≈ 0.0078 (the 8B model would be ≈ 0.014) |

The per-call figures are arithmetic on the list prices and the token counts above, which are
assumptions; the DBU-to-dollar rate is not on the pricing page. On the short input classification the
two candidates cost about the same, so the non-reasoning one wins on predictability; on the long
evidence check the 20B model is roughly 45% cheaper.

**What this trades away, stated plainly.**

- **The boundary has to be recalibrated.** The personalization line (§11.1) was tuned on a larger
  model. A small model may draw it differently, so the guardrail cases of the golden set (§16.1) are
  the acceptance test for this change. If `guard_in` fails them, move up one size — for example
  `databricks-gpt-oss-120b` (2.143 / 8.571) — rather than loosening the prompt.
- **A reasoning model spends the budget thinking.** `guard_out` gets `800` tokens, not `400`, because
  its JSON now carries the `unsupported` list on top of the thinking (low caps produced empty answers
  in v1).
- **The catalogue churns.** Models are retired on published dates, which is why the names sit in one
  place (`config`) and changing one is a one-line edit.
- **Availability on Free Edition is not guaranteed.** The Free Edition limits list "certain models not
  available" for Model Serving. `llm/gateway`'s `check` (§18) calls both endpoints and reports which
  answer; pick the cheapest one that does. A 404 there is a name problem, not a capability problem.
- **Rate limits are per token, per minute** on pay-per-token endpoints. The output guardrail sends the
  most tokens, and a retry loop (§11.2) sends them up to three times per question.
- **The postures do not change.** `guard_in` fails open, `guard_out` fails safe, whatever the model.
  The cheaper model is never a reason to relax either.

Databricks also offers safety and PII filters as AI Gateway features on an endpoint. They do not
replace these guardrails: whether a question is *personal advice* is a product rule of this system,
not a generic safety category.

---

## 12. Graph assembly (`graph/build.ipynb`, `state.ipynb`)

```python
def build_graph(data, chat, cache=None, checkpointer=None):
    panel = build_panel(chat, data)
    b = StateGraph(FinHiveState)
    b.add_node("input_guardrail", make_input_guardrail(chat))
    b.add_node("cache_lookup",   make_cache_lookup(cache, chat))   # cache=None → pass-through
    b.add_node("planner",        make_planner_node(chat, data))
    b.add_node("run_expert",     make_expert_node(panel))
    b.add_node("synthesizer",    make_synthesizer_node(chat))
    b.add_node("output_guardrail", make_output_guardrail(chat))
    b.add_node("cache_store",    make_cache_store(cache, chat))    # cache=None → pass-through
    b.add_edge(START, "input_guardrail")
    b.add_edge("run_expert", "synthesizer")   # fires once per superstep, not once per Send
    # synthesizer → output_guardrail and output_guardrail → synthesizer | END are Command(goto=…)
    return b.compile(checkpointer=checkpointer)

graph.invoke({"messages": [{"role": "user", "content": q}]},
             config={"max_concurrency": 4, "recursion_limit": 40})   # 40 leaves ample room for 3 drafts
```

Every node factory takes what it needs as arguments (`chat`, `data`) — that is what makes the graph
buildable from a notebook, from the eval harness and from inside the serving container with no
ambient state.

- Routing between nodes uses `Command(goto=…, update=…)` return values; only the join edge is
  static.
- `FinHiveState(MessagesState)` adds `blocked`, `block_reason`, `plan`,
  `cards: Annotated[list[OpinionCard], operator.add]`, `consensus`, `grounded`,
  `groundedness_reason`, `unsupported: list[str]`, `synthesis_attempts: int`, `cache_hit: bool`. **The `operator.add` reducer is what makes the fan-out work** —
  without it, parallel experts overwrite each other.
- `Send` payload is `{"expert", "sub_question", "question"}`; the expert receives
  `f"{sub_question}\n\n(The reader's original question was: {question})"`, so it knows what
  the reader wanted without being handed the other experts' work.
- `run_expert` wraps `agent.invoke` in `try/except` and returns
  `failed_card(expert, f"{type(exc).__name__}: {exc}")`. **One expert failing must never take
  the panel with it.**
- `DEFAULT_MAX_CONCURRENCY = 4`: the whole panel today, and pay-per-token endpoints rate-limit
  if pushed harder.
- The state carries the route, not just the answer: who was consulted and
  why, how much they agreed. A trace must explain an answer, not merely contain it.

---

## 13. News search — inside `tools/news_tools.ipynb`

There is **no `retrieval/` folder**. The index is built by the data engineer in `data_modeling`
(`architecture.md`); this side only queries it, from the tool, with a hybrid search and MMR. No HyDE,
no CRAG, no dedupe stage, no result or config classes — those were built for SEC filings and are
removed until a news golden set shows they are needed (§13.3).

### 13.1 What the tool expects from the index

These are requests to the data engineer, to be reconciled into `architecture.md`:

| Expectation | Why the tool needs it |
|---|---|
| A Delta Sync index over `gold_news`, `index_subtype: HYBRID`, with **managed embeddings** on a `text` column | the tool sends `query_text` and the index embeds it; Free Edition has no Direct Vector Access, so vectors are never uploaded or returned |
| Columns `chunk_id` (primary key), `text`, `ticker`, `published_at`, `title`, `source_domain`, `source_uri`, `ingested_at` | the tool returns them and cites them |
| `ticker` and `published_at` usable as filters | recency is a filter, not a ranking signal |
| A **stable, content-derived `chunk_id`** (a hash of `(url, chunk_no)`), never positional | v1 derived ids from position and reshuffled them when the source changed |
| The table is **MERGEd or appended and pruned, never overwritten** | a TRIGGERED index does not reconcile a wholesale replacement of keys; v1 had to force a full rebuild once the drift passed 50 rows |
| Rows older than the retention window are deleted | Delta Sync propagates the deletes; recency filters stay cheap |
| A way to read the index's last sync time | the tool states the "as of" |

**Sizing is a constraint on the data side, not a detail.** v1 measured **723–1,048 ms per row** to
embed, with a build-job timeout of `10,800 s`, so a full rebuild fits only up to about **10,000 rows**
(6,000 ≈ 1.2–1.8 h; 8,000 ≈ 1.6–2.3 h; 16,800 ≈ 3.4–4.9 h, which does not fit).
`rows = symbols × articles per day × retention days`; the retention window and the symbol subset (§8.3)
must be chosen against it. Daily increments are small and not affected.

### 13.2 The search

`search_news` calls the index directly; there is no wrapper module.

1. **Hybrid query.** `query_type="HYBRID"` — dense cosine plus sparse keyword, fused server-side.
   Validate the value locally (the client forwards it unvalidated). `num_results = candidate_k = 20`.
2. **Filters** are exact metadata matches applied before scoring: `{"ticker": symbol}`; pass `None`
   rather than `{}`. For recency, pass a `published_at` lower bound as a range filter **if the Free
   Edition endpoint supports it — verify**; otherwise over-fetch and drop older rows client-side.
3. **Row assembly.** The response is positional (`data_array` + `manifest.columns`) and **the score
   is a trailing column the manifest may not name**:
   ```python
   names = [c["name"] for c in manifest.get("columns", [])] or list(columns)
   row = dict(zip(names, values, strict=False))
   if len(values) > len(names):
       row.setdefault("score", values[len(names)])
   ```
4. **MMR**: pick `k = 6` of the 20, balancing relevance against novelty.
5. **Format** each passage with source domain, date, title and URL, inside data delimiters, plus the
   newest article date and the index sync time.

- Client: `databricks-vectorsearch==0.75` (a deprecated name; it re-exports `databricks.ai_search`),
  built with `VectorSearchClient(disable_notice=True)`, cached for 600 s. Its identity is the served
  agent's, with read access on the index (`architecture.md` §5.3).
- Zero rows after the filters → "no news in the last N days" → the expert answers `insufficient_data`.
  There is **no relevance grading**: whether the passages answer the question is the expert's
  judgement; the tool guarantees only that they are recent, on the right ticker and varied.
- Any exception from the client goes through `safe_tool`, so an unreachable index reads as "news
  unavailable right now", never as an empty result.

### 13.3 MMR — a private function in the same file

Lexical, because the index never returns vectors: 4-word shingles;
`similarity = max(jaccard, containment)`, where containment `|a∩b| / min(|a|,|b|)` catches a passage
that is a slice of another; relevance normalized by the **best score in the set** (`score / best`),
**not min-max** — min-max maps the weakest candidate to zero and makes it unselectable; greedy pick on
`λ·relevance − (1−λ)·max_similarity_to_selected`, `λ = 0.7`; applied only when there are more
candidates than `k`. **Scores are not comparable across query modes** (measured: ANN ≈ 0.55–0.60,
HYBRID ≈ 0.79–1.00, FULL_TEXT ≈ 4.2–4.5), so there is never an absolute score threshold.
MMR penalizes near-duplicates but does not drop them, so a story syndicated by several outlets can
still surface twice; check that on the golden set (§16.1).

**What was removed, and when to bring it back** — one at a time, only when the news golden set shows a
failure the layer fixes (invariant 4); none of the v1 numbers, measured on filings, transfer:

| Removed | What it did in v1 (on SEC filings) | Bring back if |
|---|---|---|
| HyDE | wrote a filing-style passage to search with; one model call, about 2 s | short colloquial questions retrieve poorly on the news set |
| CRAG | graded every candidate and rewrote the query on failure; precision `0.889 → 0.958`, refusal accuracy only `0 → 0.333` | irrelevant passages reach the expert, or "no answer" cases are answered anyway |
| Dedupe stage | dropped copy-forward risk factors at similarity `0.62` | duplicate stories survive MMR |

If any of them returns, it becomes a function next to `search_news`, not a folder.

---

## 14. Semantic cache and planned layers

### 14.1 Semantic cache (Valkey on Aiven) — designed, not built

**Position:** right after `input_guardrail`, before `planner`. A fresh hit returns the cached
answer and ends the run, so it skips the panel, the synthesizer and the output guardrail.

```
input_guardrail ─allow─▶ cache_lookup ─hit─▶ END
                              │ miss
                              ▼
                          planner → … → output_guardrail ─final─▶ cache_store ─▶ END
```

**Where it lives — not in the tools.** A tool finds *evidence for a worker*; the cache
decides whether *the whole run* can be skipped. Different job, different failure mode, different
store. So: a `cache/` folder (`valkey.ipynb`, `semantic_cache.ipynb`) holds the mechanism, and
`graph/cache_node.ipynb` holds the two graph nodes that use it, because a node that can end the run is
routing, and routing lives in `graph/`. `cache/` may use `llm/` (for the query embedding) and
nothing else of ours.

**What is stored.** Key: the embedding of the question (gateway `embedding` role, the
`finhive_embeddings` service — its first consumer). Value: the final answer including its
disclaimer, the cards and consensus (so `apps/api` can still return them), the resolved symbols and
experts of the plan, `cached_at` and `expires_at`.

**`cache_store` writes only a clean answer:** not blocked, `grounded` true, no failed or degraded
card, drafts not exhausted. A refusal is cheap to recompute and a warned answer must not be
replayed to the next reader.

**A hit must not cross instruments.** "What do you think of NVDA?" and "What do you think of AMD?"
are almost identical to an embedding. Similarity alone would answer one with the other, the classic
failure of a semantic cache. So a hit needs **both** cosine similarity above a threshold calibrated
by measurement **and** the same set of instruments: the question's mentions are matched against the
stored symbols by the same alias resolution the tools use, and any mismatch is a miss.

**Freshness is set at store time, from the plan.** At lookup there is no plan yet, but at store
there is, so the TTL is the **shortest** among the experts that ran: an answer is only as fresh as
its stalest input. Price-driven experts (technical, risk): minutes. News: hours. Macro and
fundamentals only: longer. The mapping is a variable in `agents/config.ipynb`. A hit prints
"Cached answer from <time>" — added in code, from `cached_at`.

**Failure mode.** Valkey unreachable, slow (short timeout) or returning garbage → **skip the cache
and answer normally**, in both directions, never an error. Same fail-open posture as the input
guardrail.

**What has to be verified before this is built** — none of it is established:

| Question | Why it matters | Fallback |
|---|---|---|
| Does Aiven's Valkey plan (including the free one) include vector search? Valkey's vector search is a separate module | the natural design stores embeddings in Valkey and runs KNN there | keep the cache small (a bounded set of a few hundred entries, LRU) and store the embeddings in Valkey as plain values; compute cosine in the app over the entries. Same `lookup(embedding, entities)` interface either way |
| Can the **Model Serving endpoint** and **serverless jobs** reach Aiven? The free plan has no private link, only IP allowlisting, and Databricks serverless egress addresses may not be stable | the cache nodes run inside the served agent | add the reachability check to `capability_probe`; if it fails, move the cache to `apps/api` on AKS (stable egress, already talks to Aiven), at the price of sitting *before* the input guardrail |
| Does the Python client work in the job environment under FIPS-mode OpenSSL? | a wheel that bundles its own OpenSSL aborts the process | use the pure-Python `valkey` client with the runtime's TLS, and prove it in the probe, in a subprocess, as v1 did for other libraries |

**Departures from `architecture.md`, to reconcile there.** §4.2 there says Aiven access happens from
`apps/*` on AKS and never from notebooks. The cache puts an Aiven connection inside the served
agent, which needs: a Valkey service on Aiven; its connection URI in the secret scope
(`valkey-uri` in the `secrets` list of `boostrap_secrets`, sanitised on every read, redacted in logs); a config entry; the
probe checks above; and an exact-pinned `valkey` in the agent environment.

**Acceptance.** Ship only if a fixed question set shows fewer tokens and lower latency at equal
answer quality, and these cache cases of the golden set (§16.1) pass: an identical question hits; the same question
about a different instrument misses; an expired entry misses; a Valkey outage produces a normal
answer.

### 14.2 Planned layers

| Layer | Position | Contract |
|---|---|---|
| **Adaptive router** | replaces the unconditional fan-out | Classify into `LOOKUP` (one tool, no panel), `SINGLE_EXPERT`, `PANEL`. The panel is the expensive path and most questions do not need it. Default to `PANEL` whenever classification is uncertain. |
| **Memory** | `checkpointer=` on `compile` | A `psycopg2` LangGraph checkpointer over Lakebase, thread-scoped. The state is already designed for it — no node reads anything outside `FinHiveState`. |

---

## 15. Observability

`mlflow.set_experiment(...)` and `mlflow.langchain.autolog()` run once, in the notebook that is making
the calls or in `model_entry.ipynb`, before any graph call — never inside a notebook that only defines,
since a `%run` of it would then reconfigure its caller's tracing. Every invocation is then traced: nodes, tool calls
with inputs and outputs, model calls, token counts. The role label passed to `chat(...)` is
what lets a trace answer "which part of the graph spent this".

The `Tracer` port (`architecture.md` §4.1) wraps this so the graph does not import `mlflow`
directly, and so the `trace_id` minted at the API edge (there, §14) can be attached as a run
tag — that is what makes "this slow answer" followable from HTTP through the graph to the
vector search call.

A trace must be enough to answer, without rerunning: which experts were consulted and which
were not consulted; which tools each called and what they returned; what each declared; what the
consensus computed; whether the answer verified as grounded. **If something cannot be
reconstructed from the trace, it belongs in the state.**

---

## 16. Evaluation — a golden set, judged in MLflow

`architecture.md` §8.4 is explicit: no evaluation job, no schedule, no results table. This document
goes one step further: **the repo contains no evaluation harness at all.** It contains one notebook
that builds the golden set. The judging is done in MLflow — judges are created and evaluations are run
and compared in its UI — so no metric code, ladder or scorer is written here.

### 16.1 The golden set (`evaluation/build_golden_set.ipynb`)

One notebook, run by hand, that writes the curated cases and registers them as an **MLflow evaluation
dataset** (its name is a variable in `agents/config.ipynb`). The cases are a list in the notebook, reviewed
in a PR: an expectation is only worth having if a person vouched for it, and an expectation an LLM
generated and nobody read measures the wrong thing without anyone noticing. An LLM may propose
alternative *phrasings* of a question; the expectation on each row stays human-written. Re-running the
notebook is idempotent: cases are keyed by `case_id` and replaced, never duplicated.

**Every row carries its expectations**, so an exact check does not have to be delegated to a model's
opinion:

| Column | Meaning |
|---|---|
| `case_id`, `category` | `guardrail`, `panel`, `news` or `cache` |
| `question` | the user message |
| `must_refuse` | the input guardrail must refuse (advice, off-topic, prompt injection) |
| `expected_experts`, `forbidden_experts` | experts the planner should / must not convene |
| `symbol`, `days`, `expect_terms`, `answerable` | news cases: a passage must be on this ticker, inside the window, and contain one of the terms; `answerable=false` means the tool must find nothing |
| `cache_group`, `cache_order`, `cache_must_hit` | cache cases are ordered pairs: populate, then repeat |
| `note` | why the case exists |

**The cases, by category:**

- **guardrail** (11+): the research/advice boundary, split *must allow* / *must refuse* — "Is Bitcoin
  attractive?", "Is NVDA overvalued?" pass; "Should I buy NVDA?", "How much of my portfolio should be
  crypto?", a poem, "ignore your instructions" are refused. It drifts whenever the model behind the
  gateway changes, which is why it has its own cases.
- **panel** (6): broad equity, crypto (`forbidden_experts = [fundamental_analyst]`), cross-asset,
  pure macro (`expected_experts = [macro_analyst]`), narrow risk, advice.
- **news** (~15): per-ticker recency questions, cross-ticker themes, a story covered by several
  outlets (does MMR still let a duplicate through?), and deliberately **unanswerable** ones — a ticker
  with no coverage, a date outside the retention window, a question no article addresses.
- **cache** (~4): an identical question hits; the same question about a different instrument misses;
  an expired entry misses; a Valkey outage yields a normal answer (§14.1).

### 16.2 How it is evaluated

Run the dataset against the agent from MLflow, with judges defined there, and read the comparison
between runs in its UI. Judges cover what needs judgement: **groundedness** (every figure appears in
the tool output), **source attribution** for news (every cited headline came from a tool result),
**no advice**, and **answer relevance**. The expectations above are given to the judges or checked by
eye in the run table.

- **By hand, before each deploy** — it is the replacement for the checks that went with the test suite (`architecture.md` §0), so it
  belongs in `docs/runbooks/agent-evaluation.md`, together with the *name and instructions of every
  judge*: judge definitions live in the workspace, not in git, and a judge nobody wrote down cannot be
  recreated.
- **Nothing blocks a deploy.** The evaluation informs a person; `finhive_deploy_agent` does not read it.
- The trace already carries what the judges need: which experts ran, each tool call, the drafts
  (`synthesis_attempts`) and the final answer (§15).
- **Weaker than code, stated plainly.** An exact expectation ("crypto never convenes the fundamental
  analyst") checked by an LLM judge or a person reading a table is less reliable than a function that
  reads the plan. If the judges prove unreliable on the `must_refuse`, `forbidden_experts` and
  `answerable` columns, add code scorers for exactly those; nothing else in this design changes.

---

## 17. Serving — `model_entry`, `endpoint` and the release actions

> **Deferred.** With the tree in `.ipynb` composed by `%run` (§3.3), nothing yet gets it into a Model
> Serving container — the magic does not exist there and an `.ipynb` is not importable. §3.3 lists the
> three ways out and none is chosen. What follows is the *release logic*, which survives whichever
> wins: the `champion` alias, the smoke after the endpoint is live, the rollback. The mechanism for
> packaging the code, and how the container receives its configuration, are open.

`architecture.md` §11: the graph runs only on Databricks, behind one Model Serving endpoint, and
`apps/api` is a thin HTTP client of it. Two files in `serving/` make that real, and only two: `model_entry`
defines what is served, `endpoint` holds everything about releasing it — including the three release
*actions* (`package` / `deploy` / `rollback`, §3.3, §19), each one a value of the same widget rather
than a notebook of its own.

**`notebooks/serving/model_entry.ipynb`** — what MLflow logs (models-from-code):

```python
import mlflow
from mlflow.pyfunc import ResponsesAgent
from graph.build import build_graph          # plain imports; no %run here (§3.3)

class FinHiveAgent(ResponsesAgent):
    def load_context(self, context):          # ONCE per replica, never per request
        self.data  = load_panel_data(read_gold_table)        # PanelData, a few MB (§5.3)
        self.graph = build_graph(self.data, get_chat_model)  # get_chat_model from llm/gateway

    def predict(self, request):        ...    # invoke; return messages + cards + consensus
    def predict_stream(self, request): ...    # stream node updates; final answer last

mlflow.models.set_model(FinHiveAgent())

def check(ctx):                               # its smoke test, run directly as a job task (§18, §19)
    ...                                       # instantiate, load_context, one predict, check it serializes

```

`check` is not called from inside the notebook: the caller runs it (§3.3).

Requirements the graph already satisfies and must keep satisfying:

- **The final answer is always `messages[-1]`** — this is why the ungrounded warning is
  prepended rather than appended as a new message (§11.2).
- The state is JSON-serializable; no node holds a Spark session, a `dbutils` handle or a
  notebook global.
- `PanelData` loads in `load_context`, not per request.
- The response carries the **cards, the consensus and the evidence refs**, not just prose —
  that is what makes `apps/api`'s auditable response shape possible, and it is a better demo
  than a chat box.

**Where the deployment logic lives.** `model_entry` has to be a separate file because it is the one that
runs inside the serving container. Every other release step runs only in a Databricks job and never
inside the container, so all of it lives in `serving/endpoint.ipynb`, behind one `action` widget
(§3.3) — not three near-empty notebooks whose only content would have been "import this and call it":

```python
# notebooks/serving/endpoint.ipynb
def champion_version(ctx): ...              # read the alias
def mark_champion(ctx, version): ...        # move the alias
def set_served_version(ctx, version): ...   # create/update the endpoint via the SDK — name, workload
                                             # size, scale-to-zero as plain arguments, no separate YAML;
                                             # waits until ready; environment_vars set from the secret
                                             # scope (§3.1) so the container needs no dbutils to see them

def package_agent(ctx):
    # log_model(python_model="notebooks/serving/model_entry.ipynb", code_paths=["notebooks"], pip_requirements=…)
    # register in Unity Catalog under the name in agents/config.ipynb
    return model_uri, model_version

def deploy_agent(ctx):
    prior = champion_version(ctx)                       # read BEFORE touching the endpoint
    new = ctx.model_version                              # from package_agent, via dbutils.jobs.taskValues
    set_served_version(ctx, new)
    smoke(ctx)                                            # one ordinary question, one advice request (must refuse)
    mark_champion(ctx, new)                               # only after the smoke passes

def rollback_agent(ctx):
    prior = champion_version(ctx)
    if prior is not None:
        set_served_version(ctx, prior)                    # no-op if the endpoint is already on it

# COMMAND ----------
# no guard: this notebook is only ever run directly, never %run by another (§3.3)
dbutils.widgets.text("profile", "free")
dbutils.widgets.dropdown("action", "deploy", ["package", "deploy", "rollback"])
ctx = build_ctx(dbutils.widgets.get("profile"))
{"package": package_agent, "deploy": deploy_agent, "rollback": rollback_agent}[dbutils.widgets.get("action")](ctx)
```

`champion_version` / `mark_champion` / `set_served_version` have no `check` of their own — they are
exercised by whichever action calls them (§18) — but `package_agent`, `deploy_agent` and
`rollback_agent` are each a real job task (§19), one per value of `action`, all pointed at this same
notebook.

**How the served copy sees its configuration — open.** The container has no `dbutils`, so it cannot
read the secret scope the way a notebook does. It does not need to for the non-sensitive names, which
are literals in `config` and travel with the code (§3.1); the open question is the credentials, once
there are any. Model Serving's documented path is an endpoint **environment variable whose value is a
secret reference** (`{{secrets/scope/key}}`), resolved at deploy time — which works, and is the reason
§3.1's "no `os.environ`" rule is scoped to the agent tree rather than claimed as universal. Settle it
with the packaging question, not before.

Values pass between the three job tasks in `dbutils.jobs.taskValues` (`model_uri`, `model_version` out
of `package`, read by `deploy`); the last known good version lives in Unity Catalog, not in a task.

**The `champion` alias is the durable record of the last known good version.** `deploy_agent` reads it
*before* updating the endpoint, updates the endpoint, smokes it, and moves the alias to the new version
**only after the smoke passed**. If the update, the wait or the smoke fails — or the task times out or
dies — the alias still names the version that worked, and `rollback_agent` restores it. Nothing depends
on a value held by a failed task. On the very first deploy there is no alias, so a failure has nothing to
restore to and stays visible.

The smoke is a step after `set_served_version` because an endpoint that loads a model is not one that answers:
`load_context` failures (a missing `code_path`, a `PanelData` read without the right grant, an
`environment_vars` secret reference that does not resolve) only appear when the new version is live.
**The pins of `package_agent`'s `log_model` and those of the job environment must match**
(§22 item 14): the serving container is another machine and the job environment does not follow the
model into it.

---

## 18. The agent notebooks to create

Every notebook defines its functions, and four of them also define a `check` — only the ones that touch
the environment (§3.3). **Nothing calls its own `check`**: a `%run` has to stay free, so the caller
invokes it in a cell. The `check` column below says what each one does where it exists, and what
exercises the notebook where it does not. All at zero model calls unless stated.

Built so far: `agents/config`, `llm/parsing`, `llm/gateway`, `tools/symbols`, `tools/safe_tool`.

| Notebook | Definitions | `check` |
|---|---|---|
| `agents/config.ipynb` | every name, path and role setting others read (§3.1) | *no check: variables only* |
| `llm/gateway.ipynb` | `get_chat_model`, `get_embedding_model`: `ROLE_MODELS`, token cache, `extra_body`, 300-token floor | **the one live check**: five chat roles answer, embeddings return a vector, and structured output is probed through the router *and against each routed model directly* (§4.2) — 9 calls |
| `llm/parsing.ipynb` | `message_text`, `ask_structured`, `check_schema_supported` (§4.2) | *no check: pure; gateway's check exercises all three* |
| `tools/symbols.ipynb` | `resolve_symbol`, `ALIASES`, `UnknownSymbolError` | *no check: pure resolution over a literal alias table* |
| `tools/safe_tool.ipynb` | `safe_tool`, `TOOL_ERROR` — the wrapper that turns an exception into an instruction | *no check: exercised by every tools notebook* |
| `tools/panel_data.ipynb` | `PanelData`, `describe(data)` | every gold table exists and is fresh; the frames load |
| `tools/{technical,risk,fundamental,macro}_tools.ipynb` | the bodies and their `@tool` wrapping | *no check: pure over `PanelData`; `panel_data`'s check covers the data* |
| `tools/news_tools.ipynb` | `search_news`: hybrid query and MMR (§13) | a filtered query returns only that ticker; an unanswerable one returns nothing |
| `cache/valkey.ipynb` | connection, keys, TTL | a probe key round-trips within the timeout |
| `cache/semantic_cache.ipynb` | lookup / store, similarity, entity guard (§14.1) | identical hits; another instrument misses; expired misses (embedding calls) |
| `guardrails/input_guardrail.ipynb` | the node, `InputVerdict` | *no check: the golden set's 11 guardrail cases are the acceptance test (§16.1)* |
| `guardrails/output_guardrail.ipynb` | the node, `GroundednessVerdict`, `DISCLAIMER` | *no check: groundedness is an MLflow judge, not a smoke test (§16.2)* |
| `graph/state.ipynb` | `FinHiveState`, the `cards` reducer, `ExpertTask` | *no check: types only* |
| `graph/opinion.ipynb` | card shape, evidence extraction, coercion, consensus | *no check: pure arithmetic; the golden set's panel cases are the real test* |
| `experts/base.ipynb` | `OPINION_CONTRACT`, `build_expert_agent` | *no check: exercised by every expert* |
| `experts/<expert>.ipynb` (×5) | one domain prompt + its tools → one expert | *no check: exercised end to end by the golden set* |
| `graph/planner.ipynb` | the supervisor node, `PlannerOutput` | *no check: the golden set's panel cases assert the routing rules of §6* |
| `graph/synthesizer.ipynb` | composition node, `fallback_answer` | *no check* |
| `graph/cache_node.ipynb` | `cache_lookup`, `cache_store` | a miss continues, a hit ends, an outage is a miss |
| `graph/build.ipynb` | assembly and compile | *no check: this is what the walkthrough notebook and the golden set run* |
| `serving/model_entry.ipynb` | the `ResponsesAgent` MLflow logs | *no check*; and what gets it into a container is open (§17) |
| `serving/endpoint.ipynb` | `set_served_version`, `champion_version`, `mark_champion`, and the `package_agent` / `deploy_agent` / `rollback_agent` actions behind the `action` widget (§17) | *no check on the helpers: exercised by whichever action runs*; the three actions are themselves the three release tasks of §19.3, not smoke tests |
| **`evaluation/build_golden_set.ipynb`** | the curated cases (§16.1) | acts too: registers the MLflow evaluation dataset |
| `docs/notebooks/agent_walkthrough.ipynb` | exploratory: one question, printing plan → cards → consensus → answer, with the trace link | never scheduled, never deployed (`architecture.md` §3) |

These checks are **smoke tests that a notebook works, not a quality evaluation.** Quality stays in MLflow
(§16): the golden set, judged by hand before a release. v1's `02_demo_technical.py` is dropped
outright — it called `build_graph(frame)` with an M1-era DataFrame while the graph now expects
`PanelData`, and `tools/technical_tools` covers what it was for.

---

## 19. Orchestration of the agent

> **Rests on the deploy decision (§3.3, §17), and on a mechanism that no longer holds.** The
> task-per-notebook DAG below was designed when every tree notebook carried an
> `if __name__ == "__main__"` block that made it directly job-runnable. With `.ipynb` and `%run` that
> guard fires on every caller, so it is gone (§3.3) and nothing auto-runs. A job task pointing at a
> tree notebook therefore does nothing today — the notebook only defines.
>
> What survives unchanged: the **dependency order** in §19.3's graph (it is a property of the tree, not
> of the runner), the **release sequence** in §17 (champion, smoke, rollback), and §19.5's runtime path
> (that is the graph, not the job). What has to be redesigned once packaging is settled: how a task
> invokes a notebook's `check`, and how many tasks there are. §19.2's inputs are unaffected.

This section orchestrates **the notebooks of the tree in §3**: how one job runs them — a task per
notebook — to verify, package and deploy the agent, and how one question then travels through them.
It says nothing about how data is produced; what the agent needs from the data platform is a contract
(§19.2).

### 19.1 The idea: one level, not two

There used to be a `pipeline/` folder of stage notebooks standing between the job and the tree, each
one importing a handful of tree notebooks and calling their `check`. It added a second copy of the
dependency graph (the stage's internal order) on top of the one the tree already has (§3), and a second
file for every notebook that already knew how to run itself. Both are gone:

- **The job connects the notebooks of the tree directly.** Every task's `notebook_path` is a real file
  under `notebooks/` — `agents/config`, `llm/gateway`, `tools/panel_data`, …, with the three release
  tasks all pointing at `serving/endpoint` under a different `action` (§17). There is no notebook in the
  job whose only job is to call another notebook.
- **The dependency graph is the job's own DAG.** The order the tree notebooks must run in — `config`
  before `llm/gateway`, `panel_data` and `symbols` before a domain tools file, every tools file before
  its expert, and so on — is exactly what `depends_on` between tasks expresses. It is drawn once, in
  §19.3, and it is the same graph that used to be redrawn *inside* each stage in the old design.
- **Databricks reports per task, natively.** A task's `notebook_path` points straight at the failing
  notebook, so the Jobs UI graph already is the "one row per notebook, status, red row" table the old
  runner used to print by hand. `run_if: ALL_SUCCESS` (the default) is the skip rule: a task whose
  upstream failed simply never runs, and *Repair run* reruns only what is downstream of a failure.
- **Tasks share confidence, not code.** What travels between them is only *success* (via `depends_on`)
  and a few small values in `dbutils.jobs.taskValues` (`model_uri`, `model_version` out of `package`,
  read by `deploy`); the last known good model version lives in Unity Catalog (§17), not in a task.
- **The tail is the release**: `build` and `model_entry` prove the assembled graph and the served class,
  then `package` logs and registers it, `deploy` ships and smokes it, and `rollback` undoes it if the
  deploy failed.

**What this costs, stated plainly.** Roughly two dozen tasks instead of six means roughly two dozen
serverless environment startups per run instead of six, each paying its own cold start and re-importing
the tree, where a stage used to amortise that over all the checks it ran in one process. The run gets
slower and slightly more expensive in exchange for a red task that *is* the red notebook, a
`Repair run` scoped to one notebook, and one fewer file per notebook to keep in sync. Independent
tasks do run in parallel — the four domain tools files, the five experts and the two guardrails have no
edges between them — so wall-clock cost is far below the sum. If the startup overhead turns out to
dominate (measure it on the first full run, §22), the cheapest fix is to collapse only the leaf
fan-outs that share a dependency set, never to reintroduce a stage layer.

### 19.2 What the agent requires before it is built

Inputs, not tasks. Each is checked by the notebook named, and a missing one turns that notebook's task
red:

| Input | Used by | Checked by |
|---|---|---|
| The gold tables `PanelData` loads: technical indicators, risk, correlations, fundamentals, macro (their names are secrets, §3.1) | `tools/panel_data` | its `check` — they exist and are fresh |
| The news index (§13.1) | `tools/news_tools` | its `check` — a filtered query answers |
| The four model names (§4.1) | `llm/gateway` | `llm/gateway` and `llm/parsing` — each role answers, each schema validates |
| The scope readable, and every credential the agent declares present in it — today none; `fred_api_key` is `data_ingestion`'s (§3.1) | `cache/`, `tools/news_tools`, once those exist | the notebook that reads the key |
| The Valkey service, only if the cache is on | `cache/` | `cache/valkey` |


### 19.3 Dependency order — a property of the tree, not of a runner

This is the order the notebooks have to run in, and it holds whatever the job turns out to look like:
`config` before anything, `panel_data` and `symbols` before a domain tools file, every tools file
before its expert, `parsing` before anything that asks a model for a schema, and everything before
`graph/build`.

```mermaid
flowchart TD
    CFG["config"] --> GW["llm/gateway"] --> PA["llm/parsing"]
    CFG --> PD["panel_data"]
    CFG --> SY["symbols"]
    CFG --> TN["news_tools"]
    CFG --> VK["cache/valkey"] --> SC["semantic_cache"]
    GW --> SC
    PD --> TT["technical_tools"]
    PD --> TR["risk_tools"]
    PD --> TF["fundamental_tools"]
    PD --> TM["macro_tools"]
    SY --> TT
    SY --> TR
    SY --> TF
    SY --> TM
    OP["opinion"] --> SN["synthesizer"]
    PA --> IG["input_guardrail"]
    PA --> OG["output_guardrail"]
    PA --> PL["planner"]
    PD --> PL
    SY --> PL
    GW --> SN
    GW --> AT["technical_analyst"]
    GW --> AR["quant_risk_analyst"]
    GW --> AF["fundamental_analyst"]
    GW --> AM["macro_analyst"]
    GW --> AN["news_analyst"]
    TT --> AT
    TR --> AR
    TF --> AF
    TM --> AM
    TN --> AN
    SC --> CN["cache_node"]
    OP --> GB["graph/build"]
    IG --> GB
    OG --> GB
    PL --> GB
    AT --> GB
    AR --> GB
    AF --> GB
    AM --> GB
    AN --> GB
    CN --> GB
    SN --> GB
    GB --> ME["serving/model_entry"]
    ME --> GP{"publish?"}
    GP -->|true| PK["package"]
    PK -->|"model_uri, model_version"| DP["deploy"]
    DP -.->|"failed"| RB["rollback"]
    GS["evaluation/build_golden_set"]
    classDef side fill:#1B3139,stroke:#FF3621,color:#ffffff;
    classDef undo fill:#5a1f1f,stroke:#FF3621,color:#ffffff;
    class GS side;
    class RB undo;
```

Every box is a task whose `notebook_path` is that tree notebook — *how a task invokes that notebook's

**There is no job specification here any more.** The previous version of this section listed about two
dozen tasks, one per notebook, each with a timeout and a "what it checks" column. That only worked
because every notebook carried an `if __name__ == "__main__"` block making it directly runnable — a
mechanism `%run` makes impossible (§3.3) — and because every notebook had a `check`, where now only
four do. A task pointing at a tree notebook today would run its definitions and report success without
verifying anything, which is worse than no job at all.

What a job needs before it can be written:

1. **the packaging decision** (§22 item 8) — it determines whether tasks run notebooks or a packaged
   entry point;
2. **a way for a task to invoke a `check`** — the checks exist and are callable; nothing calls them
   from a job yet;
3. **the release sequence**, which is already specified and survives unchanged: §17's `champion` alias
   read before the endpoint is touched, the smoke against the *live* endpoint, `champion` moved only
   after it passes, and `rollback` on failure.

Until then the four checks are run by hand, which is what they were built for: `%run ./llm/gateway`
then `check()`.

### 19.4 A release from the outside, and what a failure leaves

The shape of the release, independent of how the job is eventually written (§19.3):

```mermaid
flowchart LR
    subgraph P["Person, by hand, before merging"]
        EV["Evaluate in MLflow<br/>judges + expectations"]
    end
    EV --> M["Merge to main"]
    subgraph CI["CI: cd.yml"]
        U["upsert the job"] --> RN["trigger it"]
        RN --> SY["sync the shared Repos checkout"]
    end
    M --> U
    RN -.->|"fire-and-forget"| J["package -> deploy -> smoke"]
    J --> EP[("Model Serving endpoint")]
    EP --> API["apps/api on AKS"]
    classDef manual fill:#1B3139,stroke:#FF3621,color:#ffffff;
    class EV manual;
```

- **What a failure leaves.** Before `deploy` nothing is registered or served. A failure inside
  `deploy` — the update, the wait, the smoke, or a timeout — leaves the `champion` alias on the
  version that worked, and `rollback` repoints the endpoint at it, so the previous version serves
  again and the run ends red (§17).
- Every run creates a new model version, so a rerun is harmless.
- **Repair, do not restart**, whatever the task granularity turns out to be.

### 19.5 One question, through the tree

The runtime orchestration is the graph of §2 and §12; this is the same path with the file that does
each step, what it calls, and what happens when it fails. Every model call goes through `llm/`.

```mermaid
flowchart TD
    Q(["Question"]) --> IG["input_guardrail<br/>role guard_in"]
    IG -->|refuse| E1(["END: refusal message"])
    IG -->|allow| CL["cache_lookup<br/>embedding + Valkey"]
    CL -->|hit| E2(["END: cached answer"])
    CL -->|miss| PL["planner<br/>role router, resolve_symbol"]
    PL --> FAN{{"Send: only the experts the planner chose,<br/>at most 4 in parallel"}}
    FAN --> T["technical_analyst"]
    FAN --> R["quant_risk_analyst"]
    FAN --> FU["fundamental_analyst"]
    FAN --> MA["macro_analyst"]
    FAN --> NW["news_analyst"]
    T --> SY["synthesizer<br/>consensus in code, then role synthesizer"]
    R --> SY
    FU --> SY
    MA --> SY
    NW --> SY
    SY --> OG["output_guardrail<br/>role guard_out"]
    OG -->|"not grounded, drafts left"| SY
    OG -->|"grounded, or no drafts left"| CS["cache_store"]
    CS --> E3(["END: answer + disclaimer"])
    classDef stop fill:#1B3139,stroke:#FF3621,color:#ffffff;
    class E1,E2,E3 stop;
```

| # | Step | File | Model role | If it fails |
|---|---|---|---|---|
| 1 | receive the question, return `messages[-1]` plus cards and consensus | `serving/model_entry` | — | the endpoint returns an error; `apps/api` reports it |
| 2 | classify: allow or refuse | `guardrails/input_guardrail` | `guard_in` | **fails open**: the question goes on |
| 3 | look for a fresh cached answer; a hit ends the run | `graph/cache_node` → `cache/` | `embedding` | **fails open**: treated as a miss |
| 4 | choose the experts and write their sub-questions; resolve the tickers | `graph/planner`, `tools/symbols` | `router` | the code falls back to the full panel (or the macro analyst alone) |
| 5 | consult the experts **in parallel** (`max_concurrency 4`); each is a ReAct loop over its own tools | `experts/<expert>`, `tools/*_tools` | `worker` | one expert raising becomes a `failed_card`; the panel goes on |
| 6 | turn each expert's answer into a card; evidence is recorded by code | `graph/build` → `graph/opinion` | — | no parseable judgement becomes a degraded card |
| 7 | compute the consensus, then compose the answer | `graph/synthesizer` (+ `graph/opinion`) | `synthesizer` | an empty answer becomes `fallback_answer`, the cards verbatim |
| 8 | verify every figure against the evidence; not grounded → back to 7 with the unsupported figures, **at most 3 drafts** | `guardrails/output_guardrail` | `guard_out` | **fails safe**: the answer ships with its disclaimer |
| 9 | store a clean, grounded answer | `graph/cache_node` → `cache/` | — | **fails open**: nothing is stored |

Steps 2 and 3 can end the run early, step 8 can send it back to 7, and steps 2, 3 and 9 never make the
run fail. The state that carries all of this is `graph/state` (§12); the fixed limits are
`recursion_limit=40` and `MAX_SYNTHESIS_ATTEMPTS = 3`, both variables in `agents/config.ipynb`.

---

## 20. Build order

Each step ends with something runnable and a gate. Do not start the next until it passes.

| Step | Build | Gate |
|---|---|---|
| **A1** ✅ built | `agents/config.ipynb`, `llm/parsing.ipynb`, `llm/gateway.ipynb` | `check()` green: every role callable, embeddings return a vector, and the probe schema validates on **each routed model directly** and on both guardrail endpoints (§4.2) |
| **A2** | `tools/`: symbols and safe_tool are built; panel_data and the five domain files need slice A of `contract1.md` | all 20 tools return; every output carries an as-of date; an unknown symbol raises instead of near-matching |
| **A3** | `guardrails/` + `evaluation/build_golden_set.ipynb` (guardrail cases) | the guardrail cases pass in MLflow, 100%; disclaimer appended in code |
| **A4** | `experts/`, `graph/` (all of it), the panel cases of the golden set | in MLflow: the right experts convened, no degraded card, every answer grounded, none needs all 3 drafts |
| **A5** | `tools/news_tools.ipynb` (search + MMR), the news cases of the golden set (needs the `gold_news` index built in `data_modeling`) | the ticker filter returns only that ticker; unanswerable news cases return nothing |
| **A6** | `experts/news_analyst.ipynb`, the news routing rule in the planner prompt (requires the index of §13.1 to exist first) | a news question answers with dated, attributed headlines; a symbol with no articles in the window returns `insufficient_data`; an instruction planted in an article is ignored |
| **A7** | **first settle how the tree is packaged** (§3.3, §22 item 8), then `serving/*` (both files, including the three `action` branches) and `deploy/databricks/jobs/deploy_agent.yaml` | a green `finhive_deploy_agent` run; the served answer matches the in-process one; `predict_stream` yields increments |
| **A8** | the golden set grows to ~40 questions; judges created in MLflow and written down in the runbook | first full run recorded; thresholds fixed from it |
| **A9** | `cache/`, `graph/cache_node.ipynb` (after the three checks of §14.1), then the adaptive router | each ships only if measured to help on a fixed question set |
| **A10** | Lakebase memory (checkpointer) | a follow-up question resolves a pronoun to the previous instrument |

---

## 21. Failure catalogue — the decisions that were paid for

| Symptom | Cause | Rule it produced |
|---|---|---|
| Empty model responses, ~40% of calls | reasoning models spend the output budget thinking | floor every chat call at 300 output tokens |
| `400 unknown field "max_completion_tokens"` | langchain-openai rewrites `max_tokens` | pass caps through `extra_body` |
| Responses sometimes a list of blocks, sometimes a string | the gateway routes per request | normalize every model output through `message_text` |
| "Is Bitcoin attractive?" refused on one run, answered on the next | the guardrail rule was "asks for a recommendation" | the line is personalization, not topic; when torn, allow; calibrate it with dedicated golden-set cases |
| 3 of 6 correct answers flagged ungrounded | the verifier never saw the consensus the synthesizer was quoting | the evidence base has three sources, including the computed consensus |
| "Mixed, 100% agreement" | neutral and mixed shared one label | separate them by dispersion (`0.35`) |
| One bearish + two neutral labelled "bearish" | directional threshold at `0.34` | raise it to `0.5` |
| A baseline that cannot refuse scored 1.0 on refusal | `!=` instead of `==` in the metric | when an expectation is "finds nothing", read the comparison twice |
| The weakest candidate could never be selected by MMR | min-max normalization maps it to zero | normalize by best-in-set |
| A whole panel lost to one expert's exception | no per-expert isolation | `failed_card`, never a raised exception |
| `ResolutionTooDeep` before any code ran | `>=` specifiers in a job environment | pin every dependency with `==`, in the job *and* in `log_model` |
| An API key leaked into a job log | raw `requests` errors echo the URL | never log exception text that can contain a query-string secret |
| A retired model name kept answering from a stale `os.getenv` default while the scope held the new one | the default *was* the value whenever the variable was unset | no `os.environ` in the agent tree at all; a model name is a literal in `config`, changed in one place and reviewed in a diff (§3.1) |

---

## 22. Open items

1. **The news data side does not exist yet and `architecture.md` does not mention it** (§8.3): adapter,
   medallion pipeline, the `gold_news` table, its index in `data_modeling`, job, secret. The news
   analyst cannot be built before it, and §13.1 lists what the tool needs from it.
2. **The Valkey cache rests on three unverified facts** (§14.1): vector search on Aiven's plan,
   reachability from Databricks serverless and Model Serving, and the client under FIPS-mode OpenSSL.
3. **Retention and symbol subset are unset**, and they decide whether a full index rebuild fits the
   `10,800 s` job timeout (§13.1) — a request to the data engineer. Roughly 10,000 rows is the ceiling
   at the measured embedding rate.
4. Whether range filters on `published_at` work on the Free Edition endpoint is unverified (§13.2).
5. **Evaluation is a habit, not a gate** (§16.2): nothing stops a deploy that skipped it. Judges live in the workspace, not in git, and the
   exact expectations (`must_refuse`, `forbidden_experts`, `answerable`) rest on LLM judges or a person
   reading a table; code scorers are the fallback if that proves unreliable.
6. No semantic cache (designed, §14.1), no memory, no adaptive router — every question pays full panel price.
7. `databricks-vectorsearch` is deprecated in favour of `databricks-ai-search`.
8. **How the agent gets served is open, and it is the biggest one** (§3.3, §17, §19). The tree is
   `.ipynb` composed by `%run`, which is one format across the repo and matches `architecture.md` §6.2 —
   but a Model Serving container has neither `%run` nor a way to import an `.ipynb`, so nothing yet
   packages the tree into a served artifact. §3.3 lists three ways out (the served subset in `.py`;
   `nbconvert` at package time; no Model Serving at all, the agent as a job) and none is chosen.
   Everything in §4–§16 is independent of the outcome because every notebook takes its dependencies as
   arguments; §17 and §19 are not, and both say so at the top. Worth an ADR.
9. The removal of the test suite (`architecture.md` §0) leaves three invariants with no cheap guard: the
    hybrid query mode actually reaching the index (`query_type` validated locally), the MMR
    scale-by-best normalization, and the direction of any "nothing found" check (`!=` versus `==`). Each was a
    real bug caught by a test in v1; if pytest is ever reintroduced, start with those three.
10. **The guardrail endpoints are unverified** (§11.3): that `databricks-meta-llama-3-1-8b-instruct`
    and `databricks-gpt-oss-20b` exist on this Free Edition workspace and answer on `SERVING_PATH`, and
    that the small model holds the research/advice line on the golden set. `llm/gateway`'s `check`
    answers the first two; a 404 there is a name problem, not a capability one. `MODELS_ROUTED` is
    **empty on purpose**: probing needs Model Serving *endpoint* names and the router is configured
    with Unity Catalog *model* names (`system.ai.llama_v3_3_70b_instruct`, `system.ai.gpt-oss-20b`),
    which are different strings. Read the two endpoint names off the workspace's Serving page and the
    70/30 probe turns itself on; shipping a guess there would have gated a health check on a name
    nobody confirmed.
11. **Structured outputs are unverified on the routed pair and on the guardrail endpoints** (§4.2): the
    docs consulted document `response_format` for the GPT-OSS models and function calling for all four,
    but not `response_format` for Llama 3.3 70B or Llama 3.1 8B. `llm/gateway`'s `check` now probes each
    routed model **directly** rather than only through the router, which is what makes the answer
    conclusive instead of 70/30. A model that does not honour it moves that node to forced function
    calling; nothing else changes.
12. **The job of §19 has to be redesigned, not just verified** (§3.3, §19). Its task-per-notebook
    shape assumed an `if __name__ == "__main__"` guard, which `%run` makes impossible — the guard fires
    on every caller. How a task invokes a notebook's `check` is open, and follows from item 8. Still
    unverified whichever way that goes: that `run_if: AT_LEAST_ONE_FAILED` runs `rollback` when `deploy`
    fails or times out, that `dbutils.jobs.taskValues` carries `model_version` from `package` to
    `deploy`, that Unity Catalog model aliases behave as §17 assumes, and every timeout in §19.3.
13. **Tools as UC functions behind the managed MCP server is deferred** (§5.4): the spike (two tools, latency,
    table-function support, Free Edition) has not been run.
14. **The agent's dependencies are pinned in `notebooks/agents/requirements.txt`**
    (`langchain-openai==1.6.2`, `langchain-core==1.6.3`, `openai==3.13.0`), and installing them is a
    manual step: `%pip install -r ../requirements.txt` then `dbutils.library.restartPython()`, once
    per session. No notebook installs anything itself, because `restartPython` wipes the namespace
    and would reset every caller that `%run`s `llm/gateway`. `pydantic` is deliberately unpinned —
    `langchain-core` already requires v2, which is what the schemas need, and a second pin on it is
    how a resolution conflict starts. What is still open is how the pins reach a *job* environment,
    which is part of item 8.
15. **The one catalog is `finhive-2026`, and the ingestion does not write there yet.** Both workers
    create a `finhive` catalog without the year (`contract1.md` §7.4), so the gold tables this design
    reads would sit in a different catalog from their own sources until that is repointed. The agent
    side needs one line of `config` either way.
