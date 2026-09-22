# ADR 0001 — The agent tree is plain Python modules, composed by imports, not `%run`

**Status:** Accepted
**Date:** 2026-09-22
**Supersedes for the agent half:** `docs/architecture/ARCHITECTURE_V2.md` sections 3, 3.1 and 4
**Closes:** `agent-design.md` section 22, open item 8

## Context

`ARCHITECTURE_V2.md` prescribes one composition mechanism for everything that runs on
Databricks: layered notebooks wired together with `%run`, with ports and adapters under
`notebooks/setup/` and a composition root every job notebook `%run`s first (sections 3.1, 4.3).
That mechanism works for the ingestion and modeling jobs, which only ever execute as
`notebook_task`s inside a Databricks job.

The agent does not only execute that way. `agent-design.md` section 17 requires the graph to be
logged as an MLflow `ResponsesAgent` and served behind a Model Serving endpoint:

```python
mlflow.pyfunc.log_model(
    python_model="notebooks/serving/model_entry.py",   # models-from-code
    code_paths=["notebooks"],
    pip_requirements=[...],
)
```

Three facts collide with `%run` here:

1. **`%run` is a notebook magic. It does not exist inside a Model Serving container.** The
   container runs plain CPython against the files `code_paths` copied in. Any file whose
   composition depends on `%run` cannot load there.
2. **`code_paths` copies files that must be importable modules.** A `.ipynb` file is JSON, not a
   Python module; `import tools.symbols` cannot resolve one.
3. **`%run` shares one global namespace.** Two modules that each define `SYSTEM_PROMPT` overwrite
   each other silently — a failure mode with no error and no stack trace.

The same code has to load in three places: a stage notebook inside a Databricks job, an
interactive notebook during development, and the serving container. Only one mechanism satisfies
all three.

## Decision

**Every file in the agent tree is a plain `.py` module, imported with ordinary Python imports.
`%run` is not used anywhere under the agent folders.**

Concretely:

1. **Layout.** The agent folders sit flat under `notebooks/`, as `agent-design.md` section 3
   specifies — `setup/`, `llm/`, `tools/`, `agents/`, `graph/`, `guardrails/`, `cache/`,
   `evaluation/`, `pipeline/`, `serving/` — not nested under `notebooks/agents/`. With
   `notebooks/` on `sys.path`, `llm`, `tools`, `graph` and the rest become top-level import
   names, which is what `code_paths=["notebooks"]` produces inside the container.
2. **Format.** Databricks notebook source (`.py` whose first line is
   `# Databricks notebook source` and whose cell breaks are `# COMMAND ----------` comments).
   These render as notebooks in the Workspace and import as modules everywhere else.
3. **Notebooks define; only `pipeline/` acts.** No `dbutils`, no `spark`, no widget read, no
   client construction and no `print` at module level anywhere except `pipeline/`. Each module
   also defines `check(ctx) -> dict`, its own smoke test, which is a definition like any other
   and runs only when a stage notebook calls it.
4. **Dependencies arrive as arguments.** Every function that needs a model, a table reader or an
   index takes it as a parameter. There is no ambient container, no module-level singleton and no
   import-time I/O.
5. **`setup/` holds only names and values** — table names, model names, role settings, thresholds
   that are configuration rather than measured properties of an algorithm. No function with logic.

The ingestion notebooks under `notebooks/data_ingestion/` are unaffected. They only ever run as
`notebook_task`s, they are never logged into a model, and they keep their current `.ipynb` form.

## Consequences

**What this costs.**

- `ARCHITECTURE_V2.md` sections 3, 3.1 and 4 no longer describe the agent half accurately. They
  remain authoritative for `data_ingestion/` and `data_modeling/`. That document needs an edit;
  this ADR is the record until it lands.
- **There are no ports and adapters in the agent tree.** The `SecretProvider` / `TableStore` /
  `ChatModelProvider` protocols of `ARCHITECTURE_V2.md` section 4.1 are replaced by argument
  passing: the table reader and the chat factory are parameters, so there is nothing to isolate
  behind a Protocol. This is a genuine reduction in indirection, and also a genuine loss of the
  explicit interface those Protocols documented.
- `check_notebook_layering.py`'s `%run` pass sees nothing in these folders. Layering is enforced
  instead by `.github/scripts/check_agent_layering.py`, which reads real `import` statements.
- **The top-level import names are generic and shadowable.** `agents`, `graph`, `tools` and `llm`
  can collide with installed distributions — `openai-agents` installs as `agents`, for one. The
  job and serving environments pin dependencies exactly, and the capability probe checks that each
  of these names resolves to our own module before anything else runs.

**What this buys.**

- One mechanism for all three execution contexts, instead of a dual-mode file that `%run`s and
  falls back to `import` on `NameError`.
- Namespaced constants, so `planner.PLANNER_SYSTEM_PROMPT` and
  `synthesizer.SYNTHESIZER_SYSTEM_PROMPT` cannot overwrite each other.
- A real AST-checkable dependency graph, which is what makes the layering claim enforceable rather
  than aspirational.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| An importable `finhive-agent` package installed as a wheel | Contradicts `ARCHITECTURE_V2.md` section 0, which states there is no second home for agent code and nothing under `notebooks/` is packaged. It also reintroduces a build and a lockfile on the Databricks side, which section 6.1 deliberately removed |
| Dual-mode files that `%run` and re-import on `NameError` | Unreadable, and the failure mode is a silent shadowing rather than an error |
| Assembling the model source by string concatenation at deploy time | Cannot be reviewed as a diff |
| Nesting the agent tree under `notebooks/agents/` per `ARCHITECTURE_V2.md` section 3 | Changes every import name (`agents.llm.gateway`) and puts the served code one level below the `code_paths` root for no benefit. The flat layout is what `agent-design.md` section 3.3's `log_model` call assumes |
