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

# FinHive — Platform Architecture v2

**Status:** the current architecture — repo layout, configuration, deployment and connectivity, plus
the behaviour worth preserving from the original build: every constant, every algorithm, every
measured limit, every failure mode that cost a real run.
**Audience:** the two engineers building this — one data engineer, one data scientist — plus
anyone who arrives from a LinkedIn link with ten minutes and no context.

---

## 0. The one-paragraph version

FinHive today is a single Databricks bundle: eight notebooks, one `src/finhive` package, a secret
scope, seven jobs. It works, and the engineering inside it is genuinely careful — the measured
constants, the two-sided index convergence check, the fail-open/fail-safe guardrail asymmetry are
all things most projects get wrong. What it is *not* is a platform: it cannot run anywhere except
one Databricks workspace, it has no CI, no environment promotion, no way to accept a real-time
event, and no surface anyone can call. v2 keeps every line of the domain logic and re-homes it using
the same **ports-and-adapters discipline**, expressed in whatever medium the code actually runs in.
On Databricks, that discipline is **layered, `%run`-composed notebooks** — pure-maths notebooks,
port notebooks, adapter notebooks, agent/pipeline notebooks, wired together by one composition-root
notebook every job `%run`s first. Off Databricks — the AKS API, the Kafka apps, CI — the same
discipline is expressed as real Python code, because that's what a container and a GitHub Actions
job can actually import. The agent itself never leaves Databricks: it is logged, registered and
served behind a Databricks **Model Serving** endpoint, so `apps/api` on AKS is a thin HTTP client of
that endpoint, not a second runtime for the graph or its tools — there is no agent code, no tool
code and no notebook code of any kind inside an `apps/*` image. `notebooks/setup/contracts/` stays
the one canonical schema, but only for boundaries that live on the Databricks side (Kafka topics
consumed by Structured Streaming, the gold tables, the agent's internal contracts); each `apps/*`
container defines and owns its own small, independent models for whatever wire format it speaks
(the Model Serving request/response, its own Kafka producer/consumer payloads) — duplicated by hand
against the documented contract, never copied in as code. The folder structure is still the
deliverable: it is what makes "add Kafka" a new adapter notebook (or container) plus a new app
rather than a rewrite.

**Scope note — no test suite.** This is a fast-turnaround portfolio build, not a production system
with a maintenance budget, so there is deliberately no pytest suite, no `finhive-testing` package,
and no `tests/` tree. The original build already validated this behaviour with a ~280-test suite that
doesn't need re-proving — the behaviour those tests pinned is carried forward unchanged. What CI enforces instead is cheap and
config-only: linting, type checking, and the `import-linter` layering contract (§3.1), which is what
actually makes the "clean structure" claim real rather than aspirational. If test coverage is ever
worth adding later, the layering here is what makes it cheap to bolt on per-package.

---

## 1. What is wrong with v1, precisely

v1 is a *good* single-target layout. These are the specific properties that stop it from growing.

| # | Property of v1 | Why it blocks the goal | Fix |
|---|---|---|---|
| 1 | `src/finhive/` mixes pure maths (`indicators.py`), Spark pipelines (`pipeline*.py`), SDK calls (`platform/`) and agent orchestration (`graph/`) in one package | You cannot import the risk maths without pulling in `databricks-sdk`. You cannot see, at a glance, what depends on what. | `notebooks/` split into a `setup/` foundation (core, ports, platform-generic adapters) plus three domain folders — `data_ingestion/`, `data_modeling/`, `agents/` — each only reaching down into `setup/`, never sideways, with a one-way dependency rule (§3), enforced in CI by a script that reads notebook source directly (§3.1) — since notebooks aren't real Python packages, `import-linter` itself can't see them |
| 2 | Databricks is imported at the call site everywhere (`dbutils`, `WorkspaceClient`, `VectorSearchClient`, the ambient `spark`) | Every consumer inherits the platform, and it's invisible from the file tree which notebook actually touches Databricks vs which one is pure maths | All platform access goes through **ports** (Protocol-shaped notebooks); concrete classes live in adapter notebooks and are selected once, at the top, by a composition-root notebook every job `%run`s first (§4) |
| 3 | Notebooks mix business logic with orchestration, bootstrapped with `sys.path.insert` because the package isn't actually installed | The maths and the plumbing are copy-pasted between notebooks instead of shared, because there's no clean way to reuse code across notebooks other than duplication | Notebooks stay the job's unit of execution (`notebook_task`, unchanged from v1) and stay modular — via `%run`, not `sys.path.insert` — against a real `notebooks/{setup,data_ingestion,data_modeling,agents}/` tree, so shared logic is `%run` once, not copy-pasted (§6.2) |
| 4 | Dependencies pinned per job in YAML, duplicated across seven files | A version bump is seven edits. An unpinned `>=` already caused a `ResolutionTooDeep` failure before any code ran | One **uv workspace lockfile**; job environments reference the built wheel plus a generated, fully-pinned requirements file (§6.3) |
| 5 | Exactly one environment — whichever workspace you are logged into | No safe place to try a destructive index rebuild, the operation that once deleted 50 minutes of embedding in a retry loop | Still one environment (this is a free-tier build, so there's no paid tier to fan out into, §7) — but now it's a named `free` environment with its own catalog and service principal, deployed by CI, instead of "whichever workspace happens to be open" |
| 6 | Secrets are handled correctly but entered by hand (`databricks secrets put-secret`, typed on Windows — hence the BOM bug) | Manual entry is not reproducible, not rotatable, not auditable | Entry is still manual — there is no Key Vault available to this project — but it moves from ad hoc CLI commands into a single checked-in `bootstrap_secrets.py` notebook that creates the scope and puts every secret the code references by name, run once per environment by whoever has the values. That's a smaller win than a Key Vault would be, and §5 says so plainly. The BOM sanitiser stays — manual entry is exactly the path it guards (§5) |
| 7 | No CI at all | Nothing catches a broken import, an unpinned dependency or a malformed job spec before it merges | `ci.yml` on every PR: lint, types, import-linter, job-spec validation (§8) |
| 8 | Batch only. No ingress, no egress, no callable surface | Nothing can call FinHive and FinHive can react to nothing | FastAPI on AKS (§11) is the callable surface this build ships; Kafka ingress (§10), a Databricks App UI (§12) and ADF as cross-system orchestrator (§13) are architected as capabilities the design accommodates, not built yet |
| 9 | `memory/`, `ports/`, `serving/` are empty directories reserved for future milestones (semantic cache, Lakebase memory, golden-set eval, the serving contract) | A reader cannot tell built from planned | Planned work lives in ADRs, not empty directories. A directory exists only when it contains code |

None of these are correctness bugs. The maths, the measured constants and the failure-mode
discipline are the parts worth keeping verbatim, and v2 keeps them.

---

## 2. Principles (the tie-breakers)

When a structural question comes up, answer it with one of these.

1. **The domain does not know where it runs.** `notebooks/setup/core/*` imports `pandas`, `numpy` and
   the standard library. Nothing else. If a function needs Spark, Databricks or the network, it is
   not domain code — it belongs in an adapter or agent/pipeline notebook.
2. **Dependencies point inwards, always.** `job notebooks → agents/pipelines → adapters → ports →
   core → contracts`. On Databricks this is a folder convention checked by parsing notebook source
   (§3.1); off Databricks, where the same layers exist as real packages, it's a CI-enforced
   `import-linter` contract. Either way it's checked, not just written down.
3. **A directory is a deployable or a package, never a filing cabinet.** No `utils/`, no `common/`,
   no `misc/`. If you cannot say what is built from a directory, it should not exist.
4. **Configuration is data; secrets are references.** YAML per environment, checked in. Secret
   *names* in config, secret *values* only in the Databricks secret scope. The repo never holds a
   value.
5. **Every boundary has a versioned schema — but the schema is shared as a contract, not as code.**
   Kafka topics, the gold tables and the agent's internal state all have a typed contract (pydantic
   models) in `notebooks/setup/contracts/`, canonical on the Databricks side. Nothing under
   `notebooks/` is ever copied or imported into an `apps/*` image: the agent runs only behind the
   Model Serving endpoint, so `apps/api` is a pure HTTP client of it, not a second host for agent or
   tool code. Where an `apps/*` container needs to speak one of these shapes — the Model Serving
   request/response, its own Kafka payloads — it defines its own small model by hand against the
   documented contract (§0's schema docs, the registry for events) and validates at the boundary, so
   a shape change fails loudly at the first message, not silently downstream. The duplication is
   deliberate and cheap; the alternative is a container depending on notebook source it can't `%run`.
6. **Degrade visibly**, extended to infrastructure: a missing or misconfigured
   adapter fails at startup in the composition root with a named error, never silently at first use.
7. **The showcase is the repo, not a demo video.** Structure, CI badges, ADRs and runbooks are part
   of the deliverable. A reviewer should understand the system from the folder tree alone.

---

## 3. Repository layout

This is the core of the proposal: the tree, then the dependency rule, then the rationale.

```
finhive/
├── README.md                          architecture at a glance, badges, environment map
├── pyproject.toml                     uv workspace root for apps/*; ruff, mypy, import-linter config
├── uv.lock                            one resolved lockfile for apps/*
├── .gitignore                          anchors /data/ (a known trap, guarded against) — see §3.2
│
├── config/                            reserved — layout defined separately (§5.1)
│
├── notebooks/                          everything that runs on Databricks — the actual system (§6.2)
│   ├── setup/                          ① foundation — every other folder %runs from here, nothing here %runs anything else
│   │   ├── contracts/                  pydantic schemas — canonical for every Databricks-side boundary; never copied into apps/* (§3.1, §6.3)
│   │   ├── core/                       pure domain: indicators, risk, chunking, diversity, opinion, capability
│   │   ├── ports/                      Protocol-shaped notebooks: SecretProvider, ChatModelProvider, VectorIndex, TableStore, MarketDataSource, Clock, Tracer
│   │   ├── adapters/                   platform-generic only: databricks/{secrets,tables,volumes,tracing}
│   │   ├── container.py                composition root — %run's ports + adapters, exposes build_container(profile)
│   │   ├── capability_probe.py         job notebook — verifies the environment before any domain job runs (§6.2)
│   │   └── bootstrap_secrets.py        run by hand once per environment, never scheduled — creates the scope and puts every secret (§5.2)
│   │
│   ├── data_ingestion/                 ② external data in — the only folder allowed to call a non-Databricks API
│   │   ├── adapters/http/              yahoo.py, fred.py, alpha_vantage.py, edgar.py (v1 data/sources, unchanged logic)
│   │   ├── pipelines/                  prices.py, macro.py, fundamentals.py, filings.py — bronze → silver → gold per domain
│   │   ├── ingest_prices.py            job notebook
│   │   ├── ingest_macro_fundamentals.py
│   │   └── ingest_filings.py
│   │
│   ├── data_modeling/                  ③ derived structure over already-ingested gold data — no external fetch
│   │   ├── adapters/databricks/        vector_search.py
│   │   ├── pipelines/                  risk.py — gold_risk_metrics, gold_correlations
│   │   └── build_index.py              job notebook — rebuilds the Delta Sync index over gold_doc_chunks
│   │
│   └── agents/                         ④ the LangGraph stack and everything that talks to an LLM
│       ├── adapters/databricks/        gateway.py — the only folder allowed to call the AI Gateway
│       ├── graph/ experts/ guardrails/ retrieval/ tools/ evaluation.py   (LangGraph graph, retrieval, tools, eval harness — run by hand, no scheduled job; §8.4)
│       └── serve_agent.py              the only task of finhive_deploy_agent — logs the graph as an MLflow ResponsesAgent, deploys the serving endpoint (§6.2, §11)
│
├── apps/                              deployable units — each has a Dockerfile or an app spec
│   ├── api/                           FastAPI gateway → AKS; calls the Model Serving endpoint, never runs the graph in-process (§11)
│   ├── market-ingestor/               market tick producer → Aiven Kafka → AKS
│   ├── alert-worker/                  Kafka consumer, KEDA-scaled → AKS
│   └── console/                       Databricks App (Streamlit); calls the same Model Serving endpoint as apps/api
│
├── deploy/                            specs read and upserted by the CI/CD pipeline (§6.2, §8.0)
│   ├── databricks/
│   │   ├── jobs/                      one plain job-spec file per job (notebook_task pointing into a finhive-2026 Repos checkout, environment-agnostic);
│   │   │                              includes finhive_deploy_agent, whose task deploys the agent (§6.2)
│   │   └── app.yaml                   the console's Databricks App spec
│   └── helm/finhive/                  umbrella chart: api, alert-worker, market-ingestor
│
├── docs/
│   ├── architecture/                  C4 diagrams, data contracts, serving contract
│   ├── adr/                           0001-ports-and-adapters.md, one file per decision
│   ├── runbooks/                      index-rebuild, secret-rotation, incident-triage, azure-resource-provisioning (§7.1)
│   └── notebooks/                     exploratory / walkthrough notebooks — never scheduled, never deployed;
│                                       distinct from the production `notebooks/` at repo root
│
└── .github/
    ├── workflows/                     ci, cd-databricks, cd-services — thin YAML, no inline logic (no cd-infra, no nightly-eval; §7.1, §8.4)
    ├── actions/                       composite actions: setup-uv, setup-databricks-cli, setup-azure-login
    ├── scripts/                       Python that workflows call, plus the same scripts a developer runs by hand
    │                                  (gen-schemas, check-secrets, bootstrap-env) — the pipeline's actual logic (§8.0)
    └── CODEOWNERS                     scientist owns notebooks/setup/core/, notebooks/agents/;
                                       engineer owns notebooks/setup/{ports,adapters}/, notebooks/setup/container.py,
                                       notebooks/data_ingestion/, notebooks/data_modeling/,
                                       apps/, deploy/, .github/
```

### 3.1 The dependency rule — two enforcement mechanisms for one rule

There are two slices of code with two different enforcement tools, because only one of them is made
of real Python packages.

```
   apps/*  (containers)          deploy/* (compose only, no logic)
      │  own local models, hand-written against the documented contract — no dependency on notebooks/
      ▼
   (nothing under notebooks/ — apps/* never %runs or imports notebook code, including contracts/)

   notebooks/{data_ingestion,data_modeling,agents}/*.py    (job notebooks — orchestration only)
      │  %run
      ▼
   notebooks/{data_ingestion,data_modeling,agents}/pipelines|graph|experts|.../   ④ domain logic: medallion transforms, LangGraph graph, retrieval, tools
      │  %run
      ▼
   notebooks/{data_ingestion,data_modeling,agents}/adapters/                     ③ knows Databricks / Azure / an external API — scoped to its own domain folder
      │  %run
      ▼
   notebooks/setup/ports/                    ② Protocol-shaped notebooks only
      │  %run
      ▼
   notebooks/setup/core/                     ① pure domain
      │  %run
      ▼
   notebooks/setup/contracts/                ⓪ pydantic schemas, zero platform deps
```

A domain folder (`data_ingestion`, `data_modeling`, `agents`) never `%run`s another domain folder — each is a
sibling that only reaches down into `setup/`, never sideways. That is the one addition to the dependency rule
this layout requires: it was implicit when everything lived under one `lib/`, and is now enforced the same way
as everything else in this section (§3.1's script, updated below).

**Off Databricks** (`apps/*`), there's no internal package to layer at all, and nothing to import from
`notebooks/` either — each container is self-contained, with its own small hand-written models for
whatever wire format it speaks, checked against the documented contract rather than sharing code with
it (§2 principle 5). `import-linter` still runs there, but scoped to what actually matters
between the containers themselves: nothing in `apps/*` may import from another app in `apps/*`.

**On Databricks**, `%run` is a notebook magic, not a Python import — a tool like `import-linter`,
which walks Python's import system, cannot see it at all. The fix is a script that reads notebook
*source* directly, which turns out to reconstruct both of import-linter's contracts faithfully:

```python
# .github/scripts/check_notebook_layering.py
DOMAINS = ["data_ingestion", "data_modeling", "agents"]                    # siblings, never %run each other
SETUP_LAYER_ORDER = ["contracts", "core", "ports", "adapters"]             # + domain adapters/pipelines/graph on top
FORBIDDEN_IN_CORE = {"pyspark", "databricks", "azure", "mlflow", "langchain", "confluent_kafka", "requests"}

for notebook in iter_notebooks("notebooks/"):
    tree = ast.parse(notebook.source)                      # regular Python `import` statements
    domain, layer = domain_and_layer_of(notebook.path)      # from its folder: setup/<layer> or <domain>/<layer>
    if domain == "setup" and layer in {"core", "ports", "contracts"}:
        for imp in real_imports(tree):
            assert imp.module not in FORBIDDEN_IN_CORE, f"{notebook.path} imports {imp.module}"
    for target in run_magic_targets(notebook.source):        # regex over `# MAGIC %run "..."` lines
        target_domain, target_layer = domain_and_layer_of(target)
        assert target_domain == "setup" or target_domain == domain, \
            f"{notebook.path} %runs sideways into another domain: {target}"
        if target_domain == "setup":
            assert SETUP_LAYER_ORDER.index(target_layer) <= SETUP_LAYER_ORDER.index(layer, len(SETUP_LAYER_ORDER)-1), \
                f"{notebook.path} %runs a higher setup layer: {target}"
```

Two passes, matching the two v1 contracts exactly: a **forbidden-import** pass (real `import`
statements are still ordinary Python AST inside a notebook cell, so this half of the check is
unchanged) and a **layering** pass over `%run` targets, now checking both "does this `%run` point
downward within `setup/`" and "does this `%run` stay inside its own domain folder" (this half is
bespoke, since nothing off the shelf understands the magic). This script — not `import-linter` — is
the single most valuable check for the Databricks half of the repo, and it's the first thing to point
a reviewer at for that half.

The honest limitation, stated once here rather than glossed over: this only catches what it's told
to check. `import-linter` additionally benefits from mypy resolving real imports across a whole
run; `%run`-injected names are invisible to a type checker across that boundary, so a notebook that
calls a function `%run` brought into scope with the wrong arguments only fails at run time, not at
review time. That's the accepted cost of composing by notebook instead of by package.

### 3.2 Nothing runs locally — what that does and does not change

There is no local execution profile: this system has no `local` platform, no offline adapters, and
no `.env` file. Cloning the repo is for reading and editing code; every run — including a manual
one during development — happens against the real `free` cloud environment (§7), authenticated
with real Databricks/Azure credentials, reading real Databricks-backed secrets. That is what "use
Databricks secrets" means taken literally: **the secret backend is never mocked**, not even for a
one-off script.

This does **not** remove `.gitignore` — that solves an unrelated problem. It keeps things that
should never be committed out of version control regardless of where secrets live: `.venv/`,
`__pycache__/`, `*.pyc`, build output (`dist/`, `*.whl`), the Databricks CLI's
local profile cache (`.databricks/`), and an **anchored** `/data/` entry, because an unanchored `data/`
silently drops `src/finhive/data/` (now `notebooks/setup/core/data/`) from git entirely. Every repo
needs this file whether it has one secret or a hundred. (`.databricksignore` is specifically an
Asset Bundle / `databricks sync` mechanism — with neither in use, there's nothing left for it to do,
so it's dropped rather than kept as dead weight.)

One practical consequence: `docs/notebooks/` demo notebooks and `.github/scripts/bootstrap_env.py` (a
script that runs `databricks auth login` and points the shell at a chosen profile) are the only things a
developer runs before their first cloud deploy — there is no seeded local dataset, no
recorded-response fixture to keep in sync, and no task runner pretending there is.

### 3.3 Branches — `main` plus feature branches, nothing else

There is no `dev`/`stg` branch tier, matching §7's single `free` environment: just `main` and feature
branches cut from it, merged back by PR. What differs per branch is not the code — it's *where the
Databricks Repos checkout of this repository lives in the Workspace*, and that governs which
`notebook_path` a job spec is allowed to reference (§6.2):

- **Feature branch.** A developer checks the repo out as a Databricks Repo under their own user
  folder — `/Workspace/Users/<their-email>/finhive-2026/` — the folder is always named `finhive-2026`,
  but the parent path is whoever's user folder it happens to be. Job specs written while iterating on
  that branch reference notebook paths under that checkout.
- **`main`.** Exactly one fixed checkout, `/Workspace/Shared/finhive-2026/`, always tracked at the tip
  of `main`. Nothing is ever checked out here by hand — CI/CD keeps it in sync (§6.2, §8.2). This is
  the only Repos checkout production job definitions may reference.

Both checkouts share the same folder name — `finhive-2026` — deliberately: that shared name is what
lets promotion be a mechanical prefix swap (§6.2) rather than something that needs to know which
user's branch a job spec came from.

---

## 4. Ports and adapters — the mechanism that makes this adaptive

### 4.1 The ports

`notebooks/setup/ports/` — one notebook per port, `Protocol` classes only, no implementations, no
imports beyond `typing` and the `contracts` wrapper. The port *interfaces* live centrally in `setup/`
even though their concrete adapters are domain-owned (§4.2) — a `Protocol` has no platform dependency
to isolate, so there's no reason to duplicate or split it.

| Port | Method surface (abbreviated) | Replaces in v1 |
|---|---|---|
| `SecretProvider` | `get(key) -> str` | `settings.get_secret` + `dbutils.secrets` |
| `ChatModelProvider` | `chat(role, temperature, max_tokens)`, `embeddings()` | `platform/gateway.py` |
| `VectorIndex` | `search(...)`, `describe()`, `sync()`, `exists()`, `create(force)` | `platform/vector_search.py` |
| `TableStore` | `read(name) -> DataFrame`, `write(name, df, mode)`, `count(name)` | scattered `spark.table(...)` |
| `ObjectStore` | `exists/read_bytes/write_bytes(uri)` | `/Volumes/...` path handling |
| `MarketDataSource` | `daily_bars`, `fundamentals`, `macro_series`, `filings` | `data/sources/*.py` |
| `Clock` | `now()` | `datetime.now()` scattered at call sites |
| `Tracer` | `span(name, **attrs)` context manager | `mlflow.langchain.autolog()` |

`Clock` looks pedantic and is not: a known bug class (`period2 = now` silently storing a partial
current session as a daily bar) is exactly what a single, injected notion of "now"
prevents — every caller reads the same timestamp instead of racing the market clock independently.

A `Protocol` in a `%run`-composed notebook is a discipline, not a compiler-enforced boundary — there
is nothing stopping a notebook from skipping the port and calling `dbutils` directly. That's exactly
what `check_notebook_layering.py` (§3.1) exists to catch: an adapter notebook calling `dbutils` is
fine, a `core`/`ports` notebook doing the same is a CI failure.

### 4.2 The adapters

One notebook per technology, but adapters are **domain-owned**, not pooled in one folder: an adapter
lives next to the job notebooks that are its only callers, and the two platform-generic ones every
domain needs — `secrets`, `tables` — live in `setup/` alongside the ports themselves.

```
notebooks/setup/adapters/databricks/       secrets.py  tables.py  volumes.py  tracing.py
notebooks/data_ingestion/adapters/http/    yahoo.py  fred.py  alpha_vantage.py  edgar.py   (v1 data/sources, unchanged logic)
notebooks/data_modeling/adapters/databricks/  vector_search.py
notebooks/agents/adapters/databricks/         gateway.py
```

This is the direct consequence of the domain split (§3): `data_ingestion` is the only folder that
talks to Yahoo/FRED/Alpha Vantage/EDGAR, `data_modeling` is the only one that talks to Vector Search,
and `agents` is the only one that talks to the AI Gateway — so each adapter sits where its one caller
is, and `check_notebook_layering.py`'s sideways-import check (§3.1) is what keeps e.g. a
`data_ingestion` job notebook from reaching across into `agents/adapters/databricks/gateway.py`.

There's no Azure or Kafka subfolder here — that access happens from `apps/*` on AKS as plain code
inside each container, not from Databricks notebooks (§10, §11), so it never needed a port/adapter
layer of its own here. A notebook adapter never needs an "extras" mechanism the
way a pip package does — the job's `environment` block simply doesn't include libraries it doesn't
use, and a notebook that never `%run`s the vector-search adapter never pays for
`databricks-vectorsearch` at import time either.

### 4.3 The composition root

One notebook — `notebooks/setup/container.py` — is the only place allowed to instantiate the
platform-generic adapters every job needs regardless of domain. Every job notebook `%run`s it first,
before anything else:

```python
# notebooks/setup/container.py
# MAGIC %run ./ports/secret_provider
# MAGIC %run ./adapters/databricks/secrets
# MAGIC %run ./adapters/databricks/tables

def build_container(profile: str) -> Container:
    cfg = load_config(profile)                 # resolves config/ into a typed object — layout TBD (§5.1)
    if cfg.platform != "databricks":
        raise ConfigurationError(f"unexpected platform {cfg.platform!r} inside a Databricks notebook")
    secrets = DatabricksSecretProvider(cfg.secret_scope)
    tables  = UnityCatalogTableStore(cfg.catalog, cfg.schema)
    return Container(secrets=secrets, tables=tables, config=cfg)
```

`Container` deliberately does **not** carry a `VectorIndex` or a `ChatModelProvider` — those adapters
are domain-owned (§4.2), so the job notebook that needs one instantiates it itself, on top of the
shared container, immediately after the `%run`:

```python
# notebooks/data_modeling/build_index.py
# MAGIC %run ../setup/container
# MAGIC %run ./adapters/databricks/vector_search

c = build_container(dbutils.widgets.get("profile"))
index = DatabricksVectorIndex(c.config.vector_search)   # domain-specific, built here, not in Container
```

Every job notebook then does exactly two things before its own logic: `%run` the shared container and
`c = build_container(dbutils.widgets.get("profile"))`. Everything downstream receives `c` and never
asks where it is running — same principle as v1's original composition root, just a notebook instead
of a package, and narrower in scope now that it only ever runs on Databricks. `apps/api` has no
equivalent composition root pulling in notebook code at all: it is a small, independent FastAPI app
whose only external dependency is an HTTP client for the Model Serving endpoint plus whatever
`httpx`/Azure SDK calls it needs for its own direct SQL reads (§11) — there's no cross-platform
`match` anymore, because there's no cross-platform Python module to put it in.

### 4.4 What moves where (migration map)

| v1 path | v2 path | Change |
|---|---|---|
| `src/finhive/config/settings.py` | `notebooks/setup/core/config.py` + `config/` | Constants become typed config loaded from `config/` (layout defined separately, §5.1); the universe, FRED table and thresholds move out of Python. Secret *reads* move to the `SecretProvider` port |
| `src/finhive/platform/gateway.py` | `notebooks/agents/adapters/databricks/gateway.py` | `message_text`, `extract_json`, `ask_json` are **pure** → they move to `notebooks/setup/core/llm.py`, `%run` by the adapter. Only the `ChatOpenAI` construction (token cache, `extra_body`, 300-token floor) stays in the adapter notebook |
| `src/finhive/platform/vector_search.py` | `notebooks/data_modeling/adapters/databricks/vector_search.py` | Unchanged logic. The lifecycle rules (two-sided convergence, progress-based waits, build-in-progress detection) are the adapter's contract |
| `src/finhive/data/indicators.py`, `risk.py`, `chunking.py` | `notebooks/setup/core/indicators.py`, `risk.py`, `chunking.py` | Verbatim. These are the crown jewels — pure pandas, no platform dependency, and the notebooks CI actively forbids them from importing one (§3.1) |
| `src/finhive/data/sources/*.py` | `notebooks/data_ingestion/adapters/http/*.py` | Verbatim, behind the `MarketDataSource` port |
| `src/finhive/data/pipeline.py`, `pipeline_macro.py`, `pipeline_fundamentals.py`, `pipeline_filings.py` | `notebooks/data_ingestion/pipelines/{prices,macro,fundamentals,filings}.py` | One notebook per domain; Spark I/O goes through `TableStore`, transforms call `setup/core` |
| `src/finhive/data/pipeline_risk.py` | `notebooks/data_modeling/pipelines/risk.py` | No external fetch, only reads already-ingested gold tables → modeling, not ingestion |
| `src/finhive/data/loader.py` | `notebooks/agents/loader.py` | Reads via `TableStore`; lives with `agents/` because it feeds tool state at request time, not a batch pipeline |
| `src/finhive/tools/` | `notebooks/setup/core/tools/bodies.py` + `notebooks/agents/tools.py` | The `*_body(frame, ...) -> str` functions are pure → core. The `@tool` / `safe_tool` wrapping is LangChain → agents |
| `src/finhive/retrieval/` | `notebooks/agents/retrieval/` | `diversity.py` (shingles, Jaccard, MMR) is pure → moves to `notebooks/setup/core/text.py` |
| `src/finhive/graph/`, `agents/`, `guardrails/` | `notebooks/agents/{graph,experts,guardrails}/` | `opinion.py` consensus arithmetic and `capability.py` matrix are pure → core. Prompts, nodes and graph wiring stay in agents |
| `src/finhive/evaluation/` | `notebooks/agents/evaluation.py` | Stays a `%run`-able module; the golden set stays a checked-in data file |
| `notebooks/00_capability_probe.py` | `notebooks/setup/capability_probe.py` | Stays a notebook, stays the job's `notebook_task`. Shrinks to orchestration: `%run` the setup container, read widgets, call one function, print the report. The `sys.path.insert` bootstrap disappears because `%run` resolves relative paths natively — no manual `sys.path` manipulation at all |
| `notebooks/01_ingest_prices.py`, `03_ingest_macro_fundamentals.py`, `05_ingest_filings.py` | `notebooks/data_ingestion/ingest_{prices,macro_fundamentals,filings}.py` | Same shrink as above; numeric prefixes dropped — execution order now lives in the job specs' task `depends_on` (§6.2), not the filename |
| `notebooks/06_build_index.py` | `notebooks/data_modeling/build_index.py` | Same shrink |
| `notebooks/07_eval_retrieval.py` | dropped as a job notebook | `evaluation.py`'s eval harness (above) is still run by hand when needed; there's no `finhive_eval_retrieval` job, no schedule, and no dedicated deploy step for it (§8.4) |
| `resources/*.job.yml` | `deploy/databricks/jobs/*.yaml` | Same `notebook_task` shape as v1, `notebook_path` now pointing into a `finhive-2026` Repos checkout (no upload step); promoted to `/Workspace/Shared/finhive-2026/` and upserted by `promote_job_specs.py` / `deploy_databricks_jobs.py` from CI (§6.2), instead of an Asset Bundle |
| `src/finhive/{memory,ports,serving}/` (empty) | deleted | Re-created when those future milestones land |

The mechanical edit needed across the domain codebase: replace direct `spark`/`dbutils` use with an
injected port, and replace `import` with the equivalent `%run`. Everything else is a file move.

---

## 5. Configuration and secrets

### 5.1 Configuration

`config/` exists in the tree as a reserved location — **its internal layout is intentionally left
undefined here**, to be designed separately. What is fixed, because it follows from principle 4
(§2) rather than from any particular file layout, is:

- configuration is checked-in data, not code — no constant lives only inside a Python module;
- it is resolved for the one `free` environment (§7), whatever that resolution mechanism turns out
  to be (a single YAML file, or something else) — the profile concept stays even with one profile,
  so the code never special-cases "the only environment" as if it were unconfigured;
- it holds secret **names**, never secret **values** — a value only ever exists in the Databricks
  secret scope, read through the `SecretProvider` port (§5.2);
- it is loaded into a typed, validated model — `notebooks/setup/core/config.py` on Databricks,
  and each `apps/*` service's own small settings module off the same `config/` YAML, independently
  implemented rather than sharing the notebook file (§2 principle 5) — so a typo in a key is a
  startup failure with a field path, not a `None` surfacing three layers down at runtime;
- `free` stays a fully specified, working configuration, so Free Edition remains a supported
  deployment target rather than a casualty of the redesign.

The composition root (§4.3) depends only on the *result* of resolving a profile — a typed config
object with fields like `platform`, `catalog`, `secret_scope`, `vector_search`, `gateway` — not on
how `config/` is organized internally. That indirection is what makes the internal layout safe to
design later without touching §4 or §6.

### 5.2 Secrets

**Honest framing, stated once here rather than glossed over:** this project has no Key Vault and no
Terraform. A Databricks-managed secret scope (not Key Vault-backed — there is nothing to back it
with) is the only secret store, and it is populated by hand, once per environment, by running
`notebooks/setup/bootstrap_secrets.py`. That is a real regression from the Key Vault design this
document used to describe — manual entry is still manual entry — so the honest claim is narrower
than "reproducible, rotatable, auditable": it is "manual entry happens exactly once per environment,
through one reviewed script, instead of through untracked ad hoc CLI commands typed by whoever is at
the keyboard that day." That retires the specific bug that produced `﻿<key>\r\n` on Windows (§5.2's
sanitiser, below), but not the underlying manual-entry risk.

```python
# notebooks/setup/bootstrap_secrets.py — run by hand, once per environment, never scheduled as a job
dbutils.widgets.text("profile", "dev")
w = WorkspaceClient()                                    # notebook auth context — no stored credential needed here
scope = f"finhive-{dbutils.widgets.get('profile')}"

if scope not in {s.name for s in w.secrets.list_scopes()}:
    w.secrets.create_scope(scope)                         # plain Databricks-backed scope, not Key Vault-backed

REQUIRED_KEYS = ["fred-api-key", "alpha-vantage-key", "gateway-token", "aks-databricks-client-secret", ...]
for key in REQUIRED_KEYS:
    value = dbutils.widgets.get(key) or getpass.getpass(f"{key}: ")   # typed in interactively, never hardcoded
    w.secrets.put_secret(scope=scope, key=key, string_value=value.strip())
```

`REQUIRED_KEYS` is checked in and reviewed like any other code — the list of secret *names* the
system needs is not a secret, and keeping it in the notebook means a new dependency's secret can't be
added silently. The one AKS-specific entry (`aks-databricks-client-secret`) is a chicken-and-egg
exception explained below.

**How `apps/*` reads a secret without Key Vault or Workload Identity to Databricks.** Every `apps/*`
service authenticates to Databricks as an OAuth M2M service principal (§5.3) and calls
`WorkspaceClient(...).secrets.get_secret(scope, key)` over the REST API — the same scope
`bootstrap_secrets.py` populated, read by a second, independent implementation of `SecretProvider`
(§2 principle 5), not a shared import. The one secret that *can't* come from that scope is the
service principal's own OAuth client secret — the credential needed to make the first call — so that
one is provisioned out of band as a Kubernetes Secret (via `kubectl create secret` or a Helm
`--set-string` sourced from a GitHub Actions secret, at deploy time), never fetched at runtime.
Everything downstream of that one bootstrap credential — the FRED key, the AI Gateway token, whatever
else a container needs — comes from the Databricks scope, so there is exactly one manually-managed
credential per environment on the AKS side, not one per secret.

Rules that stay from v1 and are now enforced rather than remembered:

- **Sanitise on every read** (`strip`, BOM removal). A two-line pure function, kept in
  `notebooks/setup/contracts/` for `DatabricksSecretProvider` and independently re-implemented in
  each `apps/*` service's `SecretProvider` — trivial to duplicate, and duplicating it is cheaper than
  giving a container a dependency on notebook source just for two lines (§2 principle 5). This
  sanitiser is the one piece of hardening that matters *more* now that entry is manual again, not
  less.
- **Never log exception text that can contain a query-string secret.** Now enforced by a redaction
  filter — one implementation in `notebooks/setup/contracts/`, one in each `apps/*` service, same
  logic, deliberately not shared as code — applied in every entrypoint, plus a `gitleaks` step in CI
  as a backstop.
- **A lookup failure propagates on-platform** and never silently falls back to an env var.
- **Rotation is `docs/runbooks/secret-rotation.md`** — re-running `bootstrap_secrets.py` with the new
  value for the one key that changed, plus updating the one AKS Kubernetes Secret if it was the
  bootstrap credential that rotated. It is a documented manual procedure, not an automated one; the
  runbook exists so it's the same procedure every time, not tribal knowledge.

### 5.3 Identity — no personal access tokens anywhere

| Caller | Mechanism |
|---|---|
| GitHub Actions → Azure | OIDC federated credential on the `free` environment's app registration. No client secret |
| GitHub Actions → Databricks | Service principal with OAuth M2M (`databricks auth login` + federated token) |
| AKS pods → Aiven Kafka | SASL username/token, stored in the Databricks secret scope and fetched at startup the same way as every other secret (§5.2) — Aiven isn't an Azure AD resource, so Microsoft Entra Workload Identity doesn't apply here; this is one more secret behind the single AKS→Databricks bootstrap credential, not a second mounted secret |
| AKS pods → Databricks (secret scope, SQL, Model Serving) | Service principal OAuth; the client secret is the one credential that *is* mounted, as a Kubernetes Secret provisioned at deploy time (§5.2) — everything it unlocks downstream, including the Aiven credential above, comes from the Databricks secret scope, not from another mounted secret |
| ADF → Databricks | Managed identity, granted `CAN_MANAGE_RUN` on the job |
| Databricks job → ADLS / Aiven Kafka | Unity Catalog storage credential for ADLS; the job's own secret-scope lookup for the Aiven credential, since Aiven has no Databricks-native storage-credential integration |
| `finhive_deploy_agent` job → MLflow / Model Serving API | Runs as a Databricks-native service principal, granted `CAN_MANAGE` on the registered model and the serving endpoint — deliberately separate from CI's own OIDC identity, which only needs `CAN_MANAGE` on jobs and apps (§6.2) |

v1's `get_databricks_token` resolution chain survives intact as the Databricks
adapter's implementation detail — it already handles OAuth M2M as step 3.

---

## 6. Packaging, build and job execution

### 6.1 uv workspace — the off-Databricks slice only

```toml
# pyproject.toml (root)
[tool.uv.workspace]
members = ["apps/*"]
```

One `uv.lock` covers every `apps/*` container — there's no internal package to add as a member,
since each service is fully self-contained and depends on nothing under `notebooks/` (§2 principle 5).
`uv sync --package finhive-api` installs exactly the third-party dependencies the API needs
(`fastapi`, `pydantic`, `azure-identity`, …). Reproducible, fast, and it makes "pin everything
exactly" automatic rather than a discipline.

The Databricks side has no lockfile at all: every job's dependencies are just `%run` targets inside
`notebooks/`, which is source, not a distribution — there's nothing to `uv sync`.

### 6.2 Jobs stay notebook tasks — job/app definitions deployed by CI/CD, the agent deployed by a job

No `databricks.yml`, no bundle targets, no `databricks bundle deploy`. Two different actors, on
purpose: **job and app definitions are deployed by the CI/CD pipeline itself** — a script on the
GitHub Actions runner calling the Databricks SDK directly, authenticated via OIDC — while **the
agent is deployed by a Databricks job that runs a notebook to deploy it**, because logging and
registering the MLflow model is itself something that belongs on Databricks, not on a GitHub-hosted
runner.

**Jobs reference notebooks inside a Databricks Repos checkout — not `git_source`, not an upload.**
The repo (as a Databricks Repo / git folder) is always checked out under a folder named
`finhive-2026`, but *where* that folder sits differs by branch (§3.3):

- on a feature branch, whoever is working on it checks the repo out under their own user folder —
  `/Workspace/Users/<their-email>/finhive-2026/` — and points a job at notebooks under that path
  while iterating;
- on `main`, the repo lives at exactly one fixed location — `/Workspace/Shared/finhive-2026/` — kept
  in sync with `main` by CI/CD (below), and that is the only path a promoted job spec is ever allowed
  to reference.

`notebook_task.notebook_path` is therefore a plain Workspace path into one of those checkouts, not a
`git_url`/`git_commit` pair resolved at run time:

```yaml
# deploy/databricks/jobs/ingest_prices.yaml — environment-agnostic; no profile, no bundle vars
name: finhive_ingest_prices
tasks:
  - task_key: ingest
    notebook_task:
      notebook_path: /Workspace/Shared/finhive-2026/notebooks/data_ingestion/ingest_prices
      base_parameters: { profile: "{profile}" }
    environment_key: pipelines
environments:
  - environment_key: pipelines
    spec:
      client: "3"
      dependencies: ["databricks-sdk==0.71.0"]      # third-party only; notebooks/{setup,data_ingestion,...}/ is %run, not installed
queue: { enabled: true }
email_notifications: { on_failure: ["{alert_email}"] }
```

Checked-in job specs under `deploy/databricks/jobs/` are written against whichever `finhive-2026`
checkout a developer is using while iterating on a feature branch — typically their own user folder.
**Promotion is a path rewrite, not a redeploy of different logic**: CI/CD on merge to `main` replaces
any `/Workspace/Users/<anyone>/finhive-2026/` prefix in every `notebook_path` with
`/Workspace/Shared/finhive-2026/` before upserting the job. The match is keyed on the `finhive-2026`
folder name, not on a specific user, precisely because a feature branch can legitimately have been
checked out under any engineer's folder.

**Promotion order matters, and is three strict steps, in this sequence:**

1. **Rewrite first.** `promote_job_specs.py` reads every `deploy/databricks/jobs/*.yaml`, string-replaces
   the `/Workspace/Users/*/finhive-2026/` prefix on every `notebook_path` with
   `/Workspace/Shared/finhive-2026/`, and produces the specs that will actually be deployed.
2. **Create/update the job second.** `deploy_databricks_jobs.py` upserts each rewritten job definition
   via the SDK, by name, idempotently — this can happen even before the `Shared` checkout has the new
   notebooks, because a job definition only names a path, it doesn't validate that the path resolves
   yet.
3. **Pull the shared checkout last.** Only after the job(s) point at `/Workspace/Shared/finhive-2026/`
   does CI/CD call the Repos API to update that checkout to the merged `main` commit — guaranteeing
   that by the time this step finishes, every notebook path the job now references actually exists.
   Doing the pull any earlier would leave a window where a job could already be re-pointed at a path
   that hasn't received its notebooks yet.

```python
# notebooks/data_ingestion/ingest_prices.py
# Databricks notebook source
# MAGIC %run ../setup/container

# COMMAND ----------
dbutils.widgets.text("profile", "free")
c = build_container(dbutils.widgets.get("profile"))     # brought into scope by the %run above

report = run_price_ingestion(c, symbols=c.config.universe.all_symbols)   # from data_ingestion/pipelines/prices, %run locally
print(report.as_dict())
if report.failures:
    raise RuntimeError(f"{len(report.failures)} symbols failed: {report.failures}")
```

Each domain job (`finhive_ingest_prices`, `finhive_ingest_macro_fundamentals`,
`finhive_ingest_filings`, `finhive_build_index`) stays defined exactly like this — independently
runnable by hand or from another trigger. `deploy/databricks/jobs/` additionally defines one more job,
`finhive_daily_pipeline.yaml`, whose four tasks call the same four notebooks with `depends_on` between
them; that's the single job ADF's `pl_finhive_daily` would trigger if ADF is ever stood up (§13).

**`.github/scripts/promote_job_specs.py`** (run first, from `cd-databricks.yml`, §8.0) reads every
file in `deploy/databricks/jobs/`, resolves the `{profile}` / `{alert_email}` placeholders, and
rewrites every `notebook_path` whose prefix matches `/Workspace/Users/*/finhive-2026/` to
`/Workspace/Shared/finhive-2026/` — the mechanical half of promotion (§6.2):

```python
USER_PREFIX = re.compile(r"^/Workspace/Users/[^/]+/finhive-2026/")
SHARED_PREFIX = "/Workspace/Shared/finhive-2026/"

def promote(spec: dict) -> dict:
    for task in spec.get("tasks", []):
        nb = task.get("notebook_task", {})
        if "notebook_path" in nb:
            nb["notebook_path"] = USER_PREFIX.sub(SHARED_PREFIX, nb["notebook_path"])
    return spec
```

**`.github/scripts/deploy_databricks_jobs.py`** (run second) resolves the remaining placeholders and
upserts each already-promoted job by name via the SDK — idempotent, so re-running a deploy never
duplicates a job:

```python
def deploy(profile: str) -> None:
    cfg = load_config(profile)
    w = WorkspaceClient(host=cfg.workspace_url, token=get_databricks_token())   # CI's own OIDC identity
    for spec_path in sorted(Path("deploy/databricks/jobs").glob("*.yaml")):
        spec = promote(render(yaml.safe_load(spec_path.read_text()), profile=cfg.name,
                               alert_email=cfg.alert_email))
        job_name = f"[{cfg.name}] {spec['name']}"
        existing = next((j for j in w.jobs.list(name=job_name)), None)
        if existing:
            w.jobs.reset(job_id=existing.job_id, new_settings=JobSettings(**spec))
        else:
            w.jobs.create(**spec)
```

**`.github/scripts/sync_shared_repo.py`** (run third, last) calls the Repos API to update
`/Workspace/Shared/finhive-2026/` to the merged `main` commit — deliberately the final step, so every
`notebook_path` a job now references already resolves by the time CI/CD finishes (§6.2):

```python
def sync_shared_repo(profile: str) -> None:
    cfg = load_config(profile)
    w = WorkspaceClient(host=cfg.workspace_url, token=get_databricks_token())
    repo = next(r for r in w.repos.list() if r.path == "/Workspace/Shared/finhive-2026")
    w.repos.update(repo_id=repo.id, branch="main")
```

This job list includes an otherwise ordinary-looking `finhive_deploy_agent` job, whose only task runs
`notebooks/agents/serve_agent.py`:

```yaml
# deploy/databricks/jobs/deploy_agent.yaml
name: finhive_deploy_agent
tasks:
  - task_key: deploy
    notebook_task: { notebook_path: /Workspace/Shared/finhive-2026/notebooks/agents/serve_agent, base_parameters: { profile: "{profile}" } }
max_retries: 0
```

`notebooks/agents/serve_agent.py` (§11) logs the graph as an MLflow `ResponsesAgent`, registers it in Unity
Catalog, and upserts the Model Serving endpoint entirely in code — the endpoint name, workload size and
scale-to-zero settings are plain arguments to the Databricks SDK call inside that notebook, not a
separate YAML spec to keep in sync. All of that runs *inside Databricks*, under
`finhive_deploy_agent`'s own service principal, because MLflow's experiment tracking, model
registration and endpoint permissions are naturally scoped to a workspace identity, not a
GitHub-hosted one.

**`.github/scripts/deploy_databricks_app.py`** does the same job as `deploy_databricks_jobs.py` for
`deploy/databricks/app.yaml`, upserting the console's Databricks App directly via `w.apps` from the
runner.

Putting it together, `cd-databricks.yml` (§8.2) is:

```
deploy_databricks_jobs.py --profile free --commit "$GITHUB_SHA"      # creates/updates every job, incl. finhive_deploy_agent;
                                                                       # also run_now's finhive_deploy_agent and finhive_capability_probe — fire-and-forget, not awaited
  → deploy_databricks_app.py  --profile free
```

What this buys over v1: no upload step to drift from git, no bundle DSL, and the two identities stay
where they naturally belong — CI's OIDC service principal creates and updates job/app *definitions*,
which is a workspace-admin-shaped action; the agent's own MLflow registration and endpoint update run
under a Databricks-native identity, which is what MLflow model registration expects. There's no
bootstrapping problem to solve, unlike a fully self-deploying job would have: `finhive_deploy_agent`
is defined the same way as every other job, by the same script, and CI simply triggers it once its
definition exists.

The index-build job keeps its v1 hardening verbatim — `max_retries: 0`, `timeout_seconds: 10800`,
progress-based waiting — because those values were paid for by a real incident. They're hardcoded directly
into `deploy/databricks/jobs/build_index.yaml`, with a comment pointing at the incident, precisely
because that job is not supposed to be casually re-templated.

### 6.3 Container images

`apps/*/Dockerfile`, multi-stage, distroless runtime, non-root, built once in CI and deployed by
digest — rebuilt only when the code changes, not on every deploy.

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/api/ apps/api/                          # self-contained: its own models, nothing from notebooks/
RUN uv sync --frozen --package finhive-api --no-dev

FROM gcr.io/distroless/python3-debian12
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" FINHIVE_PROFILE=free PYTHONPATH=/app
USER 65532
ENTRYPOINT ["/app/.venv/bin/finhive-api"]
```

A useful consequence of the FIPS trap Databricks runs into: container images are **not** subject to it. The
libraries v1 had to avoid — `yfinance`, `psycopg3` — are available here. So the honest split is
"real-time tick ingestion runs in `apps/market-ingestor` on AKS; batch daily-bar ingestion and Spark
transforms run in Databricks notebooks." They're two independent implementations against the same
upstream APIs, not one `MarketDataSource` port shared across the boundary — the notebook-side port
(§4.1) lives in `notebooks/setup/ports/`, which a container can't `%run`, and duplicating a thin HTTP
client is cheaper than turning it into a package just to share it once.

---

## 7. Environments

**There is exactly one environment, called `free`.** Every earlier draft of this document assumed a
`dev`/`stg`/`prd` progression on paid Azure Databricks, each with its own catalog, node pool and
Event Hubs tier. That progression doesn't exist here — not as a cost optimisation, as a fact about
what's available: this is built entirely on free tiers, across whichever provider offers the
relevant free service, and there is no paid tier sitting behind it to promote into. `stg`/`prd`
gates, environment approvals and multi-tier promotion are removed from the CI/CD, environments and
cost sections below rather than left as unreachable dead code.

| Resource | Source | Constraint that shapes the design |
|---|---|---|
| Databricks workspace | **Databricks Free Edition** | Full feature parity with the paid product (Unity Catalog, Vector Search, Model Serving, MLflow) — the one real constraint is **serverless-only compute**: no classic/interactive job clusters, so every job's `environment` block targets serverless, and anything that assumes a long-lived cluster (Structured Streaming included) has to run on serverless Structured Streaming rather than a dedicated streaming cluster |
| AKS | **Azure for Students** | The control plane is free on Azure regardless of subscription; worker nodes are paid, so they're funded by the student credit/free allowances rather than a company subscription — the scale-to-zero and single-small-node-pool discipline in §15 is what keeps a student credit from running out, not a nice-to-have |
| Kafka | **Aiven for Apache Kafka, free plan** | Not Azure Event Hubs — a real external Kafka cluster on its own provider. Every place this document previously said "Event Hubs" now means Aiven; `confluent-kafka` clients in `apps/*` and Spark's native Kafka connector on the Databricks side are unaffected, since both already spoke the Kafka protocol rather than an Event-Hubs-specific one. **Workload Identity no longer applies to Kafka auth** — Aiven isn't an Azure resource, so pods authenticate with a SASL username/password or API token, which is one more secret in the Databricks scope (§5.2), read by the same per-service `SecretProvider` pattern as everything else |
| ADF | **Azure Data Factory, free tier** | Capped at **5 activities per hour**. `pl_finhive_daily` (§13) as originally drawn chains roughly nine separate activities, which blows the quota if they fire back-to-back; the pipeline needs to be collapsed so ADF triggers **one** multi-task Databricks job (which does its own internal sequencing via task dependencies) instead of one ADF activity per Databricks job, bringing the per-run activity count under five |
| Storage, compute overflow, anything not yet named | **GCP free tier, MongoDB Atlas free tier, or any other provider's always-free offering**, picked per need | No component is pinned to one of these today; they're named here as the standing policy — when a future milestone needs a resource this list doesn't already cover, the free tier of whatever provider offers it is the default answer, not a paid Azure SKU |

The universe stays 35 symbols and the LLM stays the AI Gateway regardless — neither is a paid-tier
concern. `free` is the only thing `README.md`'s environment map needs to describe, and it's what a
reviewer with no Azure subscription of their own can still reproduce end to end, since every piece of
it — including the Azure pieces — sits inside a free allowance rather than requiring a company
subscription.

Isolation: one resource group, one Databricks secret scope, one Aiven Kafka project, one catalog and
one service principal — one of each, not one per tier.

### 7.1 Azure resources are provisioned by hand — no IaC

**There is no `infra/bicep/`, no `deploy/adf/` as code, and no `cd-infra.yml`.** Every Azure resource
this build uses — the resource group, AKS, ACR, ADLS storage, monitoring, and the ADF pipeline itself
— is created by hand, once, against the single `free` environment (§7), the same way the Databricks
secret scope already is (§5.2). This is a deliberate scope cut, not an oversight: with exactly one
environment and no `stg`/`prd` to keep in lockstep, Bicep/Terraform buys reproducibility across
environments that don't exist here, at the cost of a tool and a workflow to maintain. If a second
environment is ever needed, that's the point at which provisioning-as-code earns its cost back — not
before.

What this means concretely:

- `docs/runbooks/azure-resource-provisioning.md` documents the exact `az` CLI commands / portal steps
  used to stand the environment up, so re-creating it after a teardown is a runbook, not tribal
  knowledge — the same honesty standard §5.2 already applies to manual secret entry.
- ADF pipelines (§13) are built directly in the ADF portal/Studio, not deployed from a checked-in
  spec — the pipeline's *shape* (one multi-task Databricks job trigger, under the 5-activities/hour
  cap) is still documented here, just not machine-applied.
- `az deployment group what-if` on PRs, the Bicep `checkov` scan and `az bicep lint` all drop out of
  CI (§8.1) along with the workflow that ran them.

---

## 8. CI/CD

### 8.0 Workflows are thin YAML; Python does the work

GitHub Actions YAML is a poor place for real logic — no types, no imports, hard to run outside CI,
hard to review as a diff. The rule here: a workflow step either calls a marketplace/composite action,
or it calls one script in `.github/scripts/`. It never inlines a multi-line `run: |` shell block that
parses JSON, computes a threshold or talks to an API.

```yaml
# .github/workflows/cd-databricks.yml (excerpt)
- uses: astral-sh/setup-uv@v3
- run: uv sync --frozen --group ci-scripts
- name: Deploy every job definition
  run: uv run python .github/scripts/deploy_databricks_jobs.py --profile free --commit "$GITHUB_SHA"
  env:
    DATABRICKS_TOKEN: ${{ steps.oidc.outputs.token }}     # from the OIDC step, never a stored PAT
```

That single line is the entire step. `deploy_databricks_jobs.py` is an ordinary, typed Python module
running on the GitHub Actions runner — it reads `deploy/databricks/jobs/*.yaml`, resolves
placeholders and upserts each job by name via the SDK. It is invoked with `uv run`, so its
third-party dependencies resolve against the **same lockfile** as every app in the repo — a workflow
script never gets its own ad hoc `pip install` line with unpinned versions, which would otherwise
reopen the exact `>=` resolution problem already paid for once. This is the general shape of every
`.github/scripts/` script now: it **triggers or inspects** Databricks-side work over the SDK/SQL, it
never imports Databricks-only *logic* directly — only the shared schemas, off the same checkout, when
it needs them.

Deliberately absent from this repo: any script that polls a Databricks job run to completion from
GitHub Actions. Every deploy trigger here is **fire-and-forget** — `deploy_databricks_jobs.py` upserts
the job definitions and `run_now`s `finhive_deploy_agent` without waiting on it, and CI's job ends
there. Whether that run succeeded is visible in the Databricks Jobs UI and in the `email_notifications`
on-failure config already on every job spec (§6.2), not as a GitHub Actions status check. This is a
narrower CI/CD than a polling-and-gating pipeline would give — a bad `serve_agent.py` deploy is caught
by the failure email and Databricks' own run history, not by a red PR check — and that's a deliberate
trade for a smaller pipeline over a stronger deploy gate, appropriate to this scope. The same applies
to the index build: nothing in `.github/` waits on it.

Because these scripts can reach `notebooks/setup/contracts` directly, they are also the natural place
for anything a workflow needs that isn't "run this CLI and check the exit code":

| Script | Called from | Does |
|---|---|---|
| `validate_job_specs.py` | `ci.yml` | loads every `deploy/databricks/jobs/*.yaml` against a pydantic schema and fails on an unknown field or missing placeholder — the structural check that replaces `bundle validate` |
| `promote_job_specs.py` | `cd-databricks.yml` | rewrites every job spec's `/Workspace/Users/*/finhive-2026/` notebook path prefix to `/Workspace/Shared/finhive-2026/`, run first, before anything is deployed (§6.2) |
| `deploy_databricks_jobs.py` | `cd-databricks.yml` | upserts every already-promoted job spec (incl. `finhive_deploy_agent`) via the SDK and triggers a `run_now` on the ones that should run immediately after a deploy, idempotent by job name, fire-and-forget (§6.2) |
| `sync_shared_repo.py` | `cd-databricks.yml` | calls the Repos API to update `/Workspace/Shared/finhive-2026/` to the merged `main` commit, run last, after jobs are upserted, so every notebook path they now reference already exists (§6.2) |
| `deploy_databricks_app.py` | `cd-databricks.yml` | creates or updates the console's Databricks App from `deploy/databricks/app.yaml` |
| `retag_image.py` | `cd-services.yml` | re-tags a container image digest as `stable` once its smoke test passes — the closest thing to promotion this single-environment build has (§8.3) |

The one thing deliberately absent from this table: nothing here calls `serving_endpoints.update` or
touches MLflow model registration directly. That happens inside Databricks, in
`notebooks/agents/serve_agent.py`, run by `finhive_deploy_agent` — the one deployment step that belongs on
Databricks rather than on the runner (§6.2).

A script here is a normal, importable Python module with a `main(argv)` — the same shape as the
pipeline entrypoints in §6.2 — just triggered by a workflow instead of a job scheduler. There is no
separate `tools/` folder: `.github/scripts/` holds both what a workflow calls and the handful of
scripts a person runs by hand (schema generation, checking a secret exists, `bootstrap_env.py`) —
one folder for "Python that isn't domain code," rather than a second one that exists only to draw a
line nobody needs.

### 8.1 `ci.yml` — every pull request

No pytest suite by design (see §0's scope note) — CI here is static and structural checks only, which stay
cheap forever because nobody has to write or maintain a test:

```
detect changed packages (dorny/paths-filter)
 ├─ lint          ruff check + ruff format --check
 ├─ types         mypy on apps/ and notebooks/ (per-file — no cross-`%run` resolution, §3.1)
 ├─ architecture  import-linter        ← the layering contracts from §3.1
 ├─ secrets       gitleaks
 ├─ job specs     validate_job_specs.py       ← §6.2, §8.0 — replaces `bundle validate`
 └─ containers    docker build (no push) + trivy scan
```

Branch protection requires all of these plus one review. `CODEOWNERS` routes `notebooks/setup/core/`
and `notebooks/agents/` to the data scientist and everything under `notebooks/setup/{ports,adapters}/`,
`notebooks/data_ingestion/`, `notebooks/data_modeling/`, `apps/`, `deploy/`, `.github/` to
the data engineer — which is also a clean way to show, on a public repo, that two people owned
distinct surfaces.

### 8.2 `cd-databricks.yml` — merge to `main`

```
uv run python .github/scripts/promote_job_specs.py     --profile free              (rewrite notebook_path prefixes → /Workspace/Shared/finhive-2026/)
  → uv run python .github/scripts/deploy_databricks_jobs.py --profile free         (OIDC service principal, no PAT — upsert the now-promoted specs)
  → uv run python .github/scripts/sync_shared_repo.py    --profile free            (pull /Workspace/Shared/finhive-2026/ to the merged main commit)
  → uv run python .github/scripts/deploy_databricks_app.py  --profile free
```

This order is load-bearing, not incidental (§6.2): the job specs are promoted (path prefixes
rewritten) *before* they're upserted, so the Databricks API only ever sees job definitions that
already point at `/Workspace/Shared/finhive-2026/`; the shared Repos checkout is pulled to the new
`main` commit *after* those jobs exist, so by the time this workflow finishes, every notebook path a
job references is guaranteed to resolve. Pulling the shared checkout any earlier would create a
window where a promoted job could point at notebooks that haven't landed yet.

`deploy_databricks_jobs.py` upserts every job definition, including `finhive_deploy_agent` itself,
then `run_now`s it and the capability probe (outbound internet, gateway reachable, Vector Search
endpoint creatable, FIPS-sensitive imports in a subprocess) — triggered, not awaited. CI's job ends
once both runs have started; whether `notebooks/agents/serve_agent.py` actually finished deploying is
visible in the Databricks Jobs UI and its `email_notifications` on failure (§6.2), not as a GitHub
Actions status. There is no promotion step after this beyond the path rewrite and repo sync above:
`free` is the only workspace, so a merge to `main` triggering a deploy here *is* the release, whether
or not that deploy has finished by the time CI goes green.

### 8.3 `cd-services.yml` — AKS

```
build+push image to ACR (tag = git sha)  →  trivy  →  cosign sign
  → helm upgrade --install finhive deploy/helm/finhive -f values-free.yaml --set image.tag=$SHA
  → kubectl rollout status  →  k6 smoke  →  retag_image.py (mark the digest `stable` once smoke passes)
```

`stable` is a rollback pointer, not a promotion target — there's nowhere to promote *to*; it just
lets a rollback re-point Helm at "the last digest that passed smoke" without re-running CI.

### 8.4 Retrieval eval — a harness, not a job

There is no `finhive_eval_retrieval` job and no CI/CD hook for it at all. `notebooks/agents/
evaluation.py` (§4.4) stays a `%run`-able eval harness against the checked-in golden set, run
interactively from a notebook when it's useful to know retrieval quality — after a retrieval-affecting
change, before a demo — not on a schedule, not wired into `finhive_daily_pipeline` (§13), and not
appending to any results table. This is a smaller commitment than the nightly-monitoring version of
this would be (an unattended schedule trending results on a dashboard), and it's out of scope for this
build on purpose.

---

## 9. Data platform — what changes and what does not

The medallion model is unchanged: bronze append-only with `source`/`ingested_at`, silver
deduplicated by `row_number()` over the natural key, gold rebuilt with `overwriteSchema`. Table
names are unchanged. Every transform keeps its existing semantics.

Three additions:

1. **The catalog is named, not implicit.** `finhive_free.analytics.gold_price_daily` etc., instead of
   whichever workspace default happened to be active. The `table(name)` helper takes the catalog and
   schema from config rather than module constants — the same code would resolve a different catalog
   if a second environment ever existed (§7), it just never has to today.
2. **A streaming bronze layer beside the batch one.** `bronze_ticks_raw` is written by serverless
   Structured Streaming from Aiven Kafka with checkpointing in ADLS — Free Edition's serverless-only
   compute (§7) means this runs on serverless Structured Streaming rather than a dedicated streaming
   cluster; the batch `bronze_prices_raw` is untouched. Silver merges both, preferring the batch daily
   bar as the record of truth and using ticks only for intraday state. This fixes the known
   `period2 = now` bug class for free: the partial current session becomes an explicitly intraday
   artifact instead of a mislabelled daily bar.
3. **Data quality as code.** Expectations declared next to each transform and executed on write —
   non-null keys, `close > 0`, monotonic `trade_date` per symbol, row-count deltas within bounds.
   Failures land in `quality_violations` and fail the task.

**Deliberately not DLT / Lakeflow.** The medallion transforms stay explicit `notebook_task` jobs
(§6.2), not a declarative pipeline, and that's a design choice, not an oversight: one of the things
this build exists to demonstrate is the *paid*-tier win of **job compute** — a job cluster is
materially cheaper than the all-purpose/DLT-managed compute it would otherwise run on — and DLT
doesn't run on plain job compute the same way a `notebook_task` does. Staying on explicit
`notebook_task` jobs here is what makes that cost story visible in the repo rather than abstracted
away behind a managed pipeline.

Governance to show off, cheaply: Unity Catalog lineage is automatic, table and column comments are
generated from the `finhive-contracts` schemas by `.github/scripts/gen_schemas.py`, and gold tables
are tagged with an owner and a freshness SLA.

---

## 10. Kafka (Aiven) — capability, not a build target

**Status: not built.** There is no concrete use case for streaming yet — no specific alert or
real-time feature anyone is waiting on. What follows is a design that proves the *architecture*
accommodates Kafka cleanly — a real producer, a real consumer on each side, versioned contracts —
without committing to build it until a use case shows up. The value of this section is that a
reviewer can see the seam is already there: adding Kafka later is "write `apps/market-ingestor` and
`apps/alert-worker` against the shapes below," not "redesign the ports/adapters boundary to make room
for streaming." This distinction is stated plainly here rather than glossed over — it's a capability
check, not a shipped feature.

**Why it belongs in the architecture at all, even unbuilt:** FinHive answers questions about market
state. A price that is one batch-run stale is a defensible product decision, but a streaming path is
what turns a "notebook project" into a system, and it is the natural home for alerting, whenever that
need materializes.

The Kafka cluster itself is **Aiven for Apache Kafka's free plan** — not Azure Event Hubs' Kafka-
protocol compatibility layer, a real Kafka cluster on its own provider (§7). That turns out to be a
simplification: every client here already spoke plain Kafka protocol rather than anything
Event-Hubs-specific, so nothing downstream had to change shape, only its connection details and
credentials (§5.2, §5.3). Two different clients touch it, deliberately not unified behind one port:
`apps/market-ingestor` and `apps/alert-worker` (containers) use `confluent-kafka` directly — no code
changes needed if you later move to Confluent Cloud or self-managed Kafka. The Databricks-side
consumer (§9's `bronze_ticks_raw`) uses Spark Structured Streaming's **native** Kafka connector
(`spark.readStream.format("kafka")`), which is the idiomatic and far more resilient choice on that
side and has nothing to do with `confluent-kafka` at all — there's no shared `EventConsumer` port
between them, and adding one would just wrap a Spark API that already does checkpointing and offset
management correctly.

The one real constraint worth naming: Aiven's free Kafka plan is a single small node with capped
storage and throughput, not a tier meant for production load. That's an acceptable trade for a
portfolio build at 35 symbols, but it's the reason partition counts and retention (§10.1) stay
deliberately small, and the reason this was never going to be Event Hubs Standard's autoscaled
throughput units in the first place.

### 10.1 Topics

| Topic | Producer | Consumers | Contract |
|---|---|---|---|
| `market.ticks.v1` | `apps/market-ingestor` (AKS, polls Yahoo/CoinGecko) | Databricks Structured Streaming → `bronze_ticks_raw`; `apps/alert-worker` | `MarketTick` |
| `finhive.signals.v1` | Databricks streaming job (threshold crossings from `gold_technical_indicators`) | `apps/alert-worker` | `Signal` |
| `finhive.questions.v1` | `apps/api` (async submissions) | Databricks job or the API's own worker | `QuestionSubmitted` |
| `finhive.answers.v1` | agent runtime | `apps/console`, `apps/alert-worker` | `AnswerProduced` |
| `*.dlq` | any consumer | operator | original payload + error envelope |

Keys are the instrument symbol so per-symbol ordering holds. Partitions sized to the consumer group,
not the universe.

### 10.2 Contracts

The canonical definition of each event lives in `notebooks/setup/contracts/events/`, is generated to
JSON Schema by `.github/scripts/gen_schemas.py`, and is registered in a schema registry, which
rejects a non-additive change at publish time. Versioning is in the topic name (`.v1`); a breaking
change ships as `.v2` alongside the old topic rather than mutating it in place.

```python
# notebooks/setup/contracts/events/market_tick.py — the canonical definition
class MarketTick(Event):
    schema_version: Literal[1] = 1
    symbol: Symbol
    price: Decimal
    volume: int | None
    currency: str
    observed_at: datetime          # source timestamp
    ingested_at: datetime          # our clock, from the Clock port
    source: Literal["yahoo_chart_api", "coingecko"]
```

The producer (`apps/market-ingestor`) and every consumer on the `apps/*` side (`apps/alert-worker`)
each carry their own small, hand-written equivalent of this shape (a `dataclass` or local pydantic
model, not the notebook class) — the JSON Schema in the registry is what keeps them honest, not a
shared Python import (§2 principle 5). A schema-registry validation step in CI, run against fixtures
on both sides, is the actual guardrail against drift.

### 10.3 Consumer discipline

Idempotent writes keyed on `(symbol, observed_at, source)`; manual offset commit after the write;
bounded retry then DLQ with the original payload and a structured error; consumer lag read directly
from Aiven via KEDA's own Kafka scaler (not routed through Azure Monitor, since Aiven isn't an Azure
resource) and used as the scaling metric for `alert-worker`. Exactly the properties an interviewer
will ask about.

---

## 11. AKS — the serving tier

`apps/api` is a FastAPI service and the only public surface. It is deliberately a **thin gateway,
not a second home for the agent** — the agent only exists on Databricks now (§4), so there is nowhere
else for it to run. `notebooks/agents/serve_agent.py` logs the graph as an MLflow `ResponsesAgent`,
registers it in Unity Catalog, and deploys it behind a Databricks **Model Serving** endpoint — this
was a planned future milestone, brought forward by the notebooks-first decision rather than left for
later. This also resolves what used to be an open question (§16): there is no "run it in the pod" alternative to
weigh, because there's no importable agent package left to run there.

| Endpoint | Purpose |
|---|---|
| `POST /v1/ask` | question in; calls the Model Serving endpoint over HTTPS, shapes the response into the `OpinionCard`/`Consensus` contract; SSE streaming variant at `/v1/ask/stream` proxies the endpoint's `predict_stream` |
| `POST /v1/ask/async` | publishes to `finhive.questions.v1`, returns a job id |
| `GET  /v1/instruments`, `/v1/snapshot/{symbol}`, `/v1/risk/{symbol}` | thin, direct SQL warehouse queries against the gold tables — no LLM, cheap and cacheable |
| `GET  /healthz`, `/readyz`, `/metrics` | liveness, readiness (checks the Databricks secret scope + warehouse + the serving endpoint), Prometheus |

The response shape mirrors the agent's own `OpinionCard` / `Consensus` contract, but `apps/api` owns
its own copy of that shape rather than importing it — the API returns the cards, the consensus
arithmetic and the evidence refs, not just prose, by shaping whatever JSON the Model Serving endpoint
returns into its own local model (§2 principle 5). That's a better demo than a chat box, because it
shows the auditability. Worth stating plainly: the three read-only `GET` endpoints are **not** built
by reusing `notebooks/setup/core/tools/bodies.py` — that code only exists inside the Databricks
workspace, and `apps/api` never depends on notebook code of any kind. They're a handful of
independent, deliberately simple SQL queries in `apps/api` itself.

Kubernetes specifics worth having in the repo because they are what a platform reviewer looks for:

- **No Azure Workload Identity here** — Aiven Kafka isn't an Azure AD resource, so there's no
  federated-identity path to it; its SASL credential comes from the Databricks secret scope instead
  (§5.3). The one secret that *is* mounted directly as a Kubernetes Secret — the Databricks service
  principal's own client secret — is the sole exception to "no secrets in manifests" (§5.2, §5.3).
- **HPA** on the API (CPU + request latency), **KEDA** on `alert-worker` (Aiven Kafka lag, §10.3).
- `PodDisruptionBudget`, `topologySpreadConstraints`, resource requests/limits, `NetworkPolicy`
  restricting egress to Databricks (secret scope + SQL warehouse + Model Serving) and Aiven's Kafka
  endpoint.
- Ingress via NGINX + cert-manager; rate limiting per API key at the ingress.
- One Helm values file for the `free` environment; image always referenced **by digest**, never a
  moving tag, so `retag_image.py`'s `stable` pointer (§8.3) is what rollback re-points Helm at.
- The Model Serving endpoint is authenticated the same way as everything else Databricks-side here —
  service principal OAuth using that one mounted client secret (§5.2, §5.3), never a stored PAT.

---

## 12. Databricks Apps — the console (capability, not a build target)

**Status: not built.** There's no product need for an internal console right now. What this section
proves is that the architecture can host one cleanly — a Databricks App authenticating OBO, calling
the same Model Serving endpoint `apps/api` calls, with no special-casing anywhere else in the system
to make that possible. If a UI is ever needed, this is the shape it takes; nothing else has to change
to accommodate it.

`apps/console` would be a Streamlit app deployed as a Databricks App from `deploy/databricks/app.yaml`
by `.github/scripts/deploy_databricks_app.py`, run directly from `cd-databricks.yml` (§6.2). It would
be the internal-facing UI and the fastest thing to screenshot for a post, if built:

- Ask a question, watch experts run in parallel, see each `OpinionCard` with its stance, confidence,
  tool evidence and as-of dates, then the computed consensus and the synthesised answer.
- A retrieval inspector: query, HyDE passage, candidates with CRAG grades, what MMR selected and what
  dedupe dropped, and the citation for each surviving passage.
- An operations tab reading job run history and index row-count drift.

It authenticates with Databricks OBO (on-behalf-of user), so Unity Catalog permissions apply to the
viewer — a governance point worth making explicitly. It calls the **same Model Serving endpoint**
`apps/api` calls (§11) — the two are just two different HTTP clients in front of one served agent,
which is the whole point of having pulled the agent out into its own served artifact rather than
letting each caller run its own copy.

---

## 13. Azure Data Factory — the cross-system orchestrator (capability, not a build target)

**Status: not built.** FinHive has no external system today that only ADF can reach — no SFTP broker,
no on-prem SQL Server, no partner blob drop. What follows documents how ADF would slot in if one shows
up, so that adding it later is "stand up the pipeline below," not "figure out where cross-system
orchestration fits in an architecture that never considered it."

The honest framing, which is also the strongest one in an interview: **Databricks Workflows
orchestrates Databricks; ADF orchestrates across systems.** Using ADF to run a single notebook would
be resume-driven. Using it as the enterprise control plane is not.

**The free tier's binding constraint: 5 activities per hour.** Azure Data Factory's free tier caps
pipeline activity executions at five per hour, which a naive per-job-per-activity design would blow
through in a single run (one Copy per source, one Databricks Job activity per downstream job). The fix
is structural, not a workaround: ADF triggers **one** Databricks multi-task job, and that job does its
own internal sequencing via task dependencies — `finhive_ingest_prices →
finhive_ingest_macro_fundamentals → finhive_ingest_filings → finhive_build_index` become four tasks
inside one Databricks Workflow, not four separate ADF activities. This is a better design independent
of the quota (Databricks Workflows' own DAG and retry semantics are a better fit for chaining
Databricks jobs than polling from outside anyway), and it happens to be what makes the free tier's
5/hour cap survivable: the pipeline below is five ADF-level activities, not one per Databricks task.

ADF owns:

1. **Landing external data FinHive does not own** — an SFTP broker extract, an on-prem SQL Server
   table via a Self-Hosted Integration Runtime, a partner blob drop — into ADLS `raw/`. This is the
   one thing Databricks genuinely cannot do well, and it justifies ADF's existence in the diagram.
2. **Triggering the one Databricks multi-task job**, via the Databricks Job activity with a managed
   identity, with `Until`/`Wait` polling for the run to finish (the long index-build task inside it
   included).
3. **Event-driven starts** — a Storage Event trigger on a blob arrival in `raw/filings/` kicks the
   pipeline instead of waiting for a schedule.
4. **Cross-cloud/system fan-out on completion** — write a curated extract to Synapse/Fabric or push a
   notification.

```
[Storage Event: raw/**]        [Schedule: 06:00 UTC]
          │                              │
          └──────────────┬───────────────┘
                         ▼
             ADF pipeline  pl_finhive_daily        (5 activities — inside the free tier's 5/hour cap)
                         │
      ┌──────────────────┼────────────────────┐
      ▼                  ▼                    ▼
 Copy: SFTP→ADLS    Copy: SQL→ADLS      (validate landing)
      └──────────────────┴────────────────────┘
                         ▼
        Databricks Job: finhive_daily_pipeline   (Until: status == SUCCEEDED, timeout 3 h)
                         │   one multi-task Databricks Workflow, sequenced internally:
                         │   ingest_prices → ingest_macro_fundamentals → ingest_filings
                         │   → build_index
                         ▼
             Web activity → alert on failure
```

ADF pipelines are built directly in the portal/Studio and provisioned by hand (§7.1), like every other
Azure resource here — there is no checked-in spec deploying them from CI. The pipeline shape above
(one multi-task Databricks job trigger, five activities to fit the free-tier cap) is what a reviewer
recreates by hand against `docs/runbooks/azure-resource-provisioning.md`.

---

## 14. Observability

| Layer | Tool | What it answers |
|---|---|---|
| Agent internals | MLflow tracing (`mlflow.langchain.autolog()`, unchanged) | which node, which tool, how many tokens, which role |
| Services | OpenTelemetry → Azure Monitor / App Insights | latency, error rate, dependency map |
| Correlation | one `trace_id` minted at the API edge, propagated into the Kafka header, the MLflow run tag and every log line | "this slow answer" traced from HTTP through the graph to the vector search call |
| Pipelines | a Delta table (`operations.pipeline_logs`), written to instead of stdout/JSON files, plus job run metrics → Log Analytics | which symbols failed, how long ingestion took — queryable with SQL, not grepped out of a log file |
| Data | freshness and row-count checks on gold tables, `quality_violations` | is the answer built on stale data |

**Dashboards: not built yet.** There is no Grafana/Azure Workbook today — everything above is queried
directly (the MLflow UI, the `pipeline_logs` Delta table, Log Analytics) rather than surfaced on a
screen. The tables and traces are already shaped for one (structured, queryable, correlated by
`trace_id`), so adding a dashboard later is a visualization layer on top of what already exists, not a
redesign of what gets logged.

Alerting routes to the same Aiven Kafka topic the alert worker consumes, so the system's own health
uses its own transport, whenever that path is built (§10). Cheap, and it demonstrates the point.

---

## 15. Security and cost

**Security.** No PATs (§5.3) — the one credential mounted anywhere is the AKS→Databricks service
principal's OAuth client secret (§5.2), which is not a PAT and is scoped to `CAN_MANAGE_RUN`/read
grants, not a workspace-admin token. Private endpoints for Storage in this Azure resource group; AKS
egress restricted by `NetworkPolicy` (§11) — Aiven Kafka doesn't support Azure Private Link on its
free plan, so that leg relies on Aiven's own IP allowlisting plus TLS/SASL rather than network
isolation. Unity Catalog grants on the `free` environment's service principal, read-only on gold for
the API. Images signed with cosign and scanned with trivy; `gitleaks` on every PR. The
prompt-injection guardrail is the application-layer control and stays exactly as it was, including
failing open on the input side and safe on the output side.

**Cost, because this is a free-tier build and staying inside that is the whole game, not a nice-to-have.**

| Control | Effect |
|---|---|
| AKS node pool scaled to zero out of hours by a scheduled workflow | keeps the student-credit-funded worker nodes (§7) from being the thing that runs out |
| Aiven Kafka free plan; a handful of small, short-retention topics (§10) | $0, by design — the trade-off is capped throughput, not a discount on a paid tier |
| Databricks Free Edition — serverless-only compute, no idle cluster to forget running (§7) | no idle compute, structurally, not just by discipline |
| `pl_finhive_daily` collapsed to one multi-task Databricks job so ADF stays under 5 activities/hour (§13) | keeps the pipeline inside ADF's free-tier quota, not just its cost |
| Index rebuild is manual-approval only | 80–115 min of embedding is the most expensive single operation in the system, and there's no `stg` to absorb a wasted run |
| Azure budget alert on the resource group | cost visible without a paid tier to accidentally provision into, even with resources provisioned by hand rather than from a reviewable IaC diff (§7.1) |

---

---

## 16. Open decisions

Flagging these rather than deciding them silently, because each has a real trade-off and one of them
changes the cost profile.

1. ~~Azure Databricks vs Free Edition as the primary~~ — **resolved**: Free Edition, exclusively
   (§7). Free Edition turns out to have the same feature surface as the paid product — Unity
   Catalog, Vector Search, Model Serving, MLflow — modulo serverless-only compute, so the thing that
   used to force a paid workspace (Model Serving needing a "real" Azure Databricks tier) doesn't hold
   anymore. Every non-Databricks resource (AKS, Aiven, ADF) is funded from its own separate free
   tier instead.
2. ~~Where the agent runs in production~~ — **resolved**: a Databricks Model Serving endpoint (§11),
   forced by the decision to keep all agent code as Databricks notebooks rather than an importable
   package. Free Edition's two-custom-model ceiling is still a real constraint, worth
   watching if the agent and a future second model both need to live there at once.
3. **Schema registry** — Aiven doesn't ship Confluent Schema Registry, but its Kafka service bundles
   **Karapace**, a Confluent-API-compatible schema registry, at no extra cost on the free plan; the
   alternative is a home-rolled JSON Schema check in CI with no runtime registry at all. Default:
   Karapace, since it's free and gives the runtime rejection-on-publish behaviour §2 principle 5
   assumes.
4. ~~Terraform vs Bicep~~ — **resolved differently**: neither. With exactly one environment and no
   `stg`/`prd` to keep reproducible alongside it, IaC's core value — replaying the same infra
   definition across environments — has nothing to apply to here. Every Azure resource is provisioned
   by hand, once, documented in `docs/runbooks/azure-resource-provisioning.md` (§7.1). Revisit if a
   second environment is ever added.
5. ~~DLT / Lakeflow for the medallion transforms~~ — **resolved**: no. Explicit `notebook_task` jobs on
   job compute, deliberately, to demonstrate the job-compute cost story (§9).
6. **Monorepo vs split repos.** Monorepo, for atomic contract changes across producer and consumer.
   Revisit only if build times exceed a few minutes.
