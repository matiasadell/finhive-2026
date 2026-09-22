# Agent build plan — the step sequence, and where it currently stands

**Purpose:** a resumable record of how `agent-design.md` gets built. Read this first when picking
the work back up. It holds the decisions already taken (so they are not re-litigated), the step
sequence with its gates, and what is blocked on whom.

**Keep it current.** Update the status table in section 1 at the end of every step. A stale plan is
worse than none.

---

## 1. Where this stands

| | |
|---|---|
| Repository | `github.com/matiasadell/finhive-2026` |
| Working branch | **`matias`** — every step is one or more commits here, not a branch of its own |
| Pull request | **One**, `matias` → `main`, opened once CI/CD works end to end (after step 6) |
| Last update | 2026-09-22 |

### Step status

| # | Step | Gate | Status |
|---|---|---|---|
| 0 | Foundations: ADR, data contract, layering check | CI green; contract acknowledged | **Done** — `1bccf1f`, `20e103a` |
| 1 | `setup/config.py` + `llm/` + the runner | A1 | **Code complete — gate A1 pending a workspace run** |
| 2 | `tools/` | A2 | **Code complete — 19 tools green on the fixture; gate A2 pending gold** |
| 3 | `guardrails/` + `graph/opinion.py` | A3 | Not started |
| 4 | `agents/` + planner + synthesizer | A4 (part 1) | Not started |
| 5 | `graph/build.py` + assemble stage | A4 (part 2) | Not started — **first that needs real gold tables** |
| 6 | `serving/` + release stages + job spec | A7 | Not started — **the PR to `main` opens here** |
| 7 | News analyst | A5 + A6 | Not started — blocked on the data side |
| 8 | Golden set and judges | A8 | Not started |
| 9 | Semantic cache | A9 | Optional, deferred |
| 10 | Lakebase memory | A10 | Optional, deferred |

Gate letters are `agent-design.md` section 20's build order. Do not start a step until the previous
gate passes.

### Workflow

All work lands as commits on `matias`. There is no branch or pull request per step — the sequence
below is a sequence of **commits**, each one self-contained and named after its step, so the branch
history reads as the build order.

The single pull request to `main` opens after **step 6**, which is when the CI/CD pipeline actually
deploys something: `deploy_agent.yaml` exists, `promote_job_specs.py` is fixed, and
`finhive_deploy_agent` runs green. Until then `main` is untouched.

Steps 7 and 8 continue on `matias` after that PR merges, under the same rule.

---

## 2. Decisions already taken — do not re-open without a reason

| Decision | Why | Recorded in |
|---|---|---|
| The agent tree is plain `.py` modules composed by imports, flat under `notebooks/`, never `%run` | `%run` does not exist in a Model Serving container; a `.ipynb` is not importable | ADR 0001 |
| There are **no ports and adapters** in the agent tree; dependencies arrive as arguments | Follows from the above — a Protocol has nothing to isolate once the reader is a parameter | ADR 0001 |
| The gold layer is the **data engineer's** deliverable, not this workstream's | Division of ownership; `agent-design.md` says the data side is not built by that document | `docs/architecture/agent_data_contract.md` |
| Fundamentals come from yfinance | Free, already a dependency, covers most of the needed columns for equities | Contract section 3.5 |
| The news analyst is **deferred to step 7** | Needs a provider, a `gold_news` table and a vector index, none of which exist | Contract section 6 |
| `finhive_router` and `finhive_embeddings` already exist in the workspace | Confirmed by the user | — |
| The semantic cache stays off (`enable_cache=false`) | `agent-design.md` section 14.1 gates it on three unverified facts | `agent-design.md` section 19.5 |
| No pytest suite | `ARCHITECTURE_V2.md` section 0. Verification is the per-module `check(ctx)` plus the golden set in MLflow | — |
| The `promote_job_specs.py` bug is deliberately left unfixed until step 6 | It only becomes blocking when the first agent job spec is deployed | — |
| Everything is committed to `matias`; one PR to `main` once CI/CD works | Keeps the deploy pipeline provable before `main` moves | — |

---

## 3. Conventions

- **Branch:** `matias`. One commit (or a few) per step, message headed by the step.
- **Notebook format:** `.py` whose first line is `# Databricks notebook source`, cell breaks are
  `# COMMAND ----------`. Renders as a notebook in the Workspace, imports as a module everywhere
  else.
- **Every module defines `check(ctx) -> dict`**, its own smoke test, except the four
  `agent-design.md` section 3 exempts: `graph/state.py`, `tools/safe_tool.py`, `agents/base.py`,
  `serving/endpoint.py`.
- **Only `pipeline/` acts.** No `dbutils`, `spark`, widget read, client construction or `print` at
  module level anywhere else. `check_agent_layering.py` enforces this in CI.
- **Prompts live with the node that owns them**, as module-level constants with subject-prefixed
  names (`PLANNER_SYSTEM_PROMPT`). `OPINION_CONTRACT` exists exactly once, in `agents/base.py`.
- **Thresholds stay in the file that owns them**, next to the note saying how they were measured
  (`0.62`, `0.5`, `0.35`, `MIN_AMBIGUOUS_COUNT`). Only names, paths and role settings go in
  `setup/config.py`.
- **Commit trailer:** `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **PR body ends with:** `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Documentation is English**, matching the rest of the repo.

---

## 4. The critical dependency, and how it is worked around

`agent-design.md` section 19.2 requires five gold tables before the agent can be verified. They do
not exist, and they belong to the data engineer.

**The workaround is structural, not a hack.** `tools/panel_data.py` receives the table reader as an
argument (`agent-design.md` section 5.3), and every `*_body(frame, ...)` is pure. So:

- Tool logic is verified against a **checked-in `PanelData` fixture**, and lands without gold.
- The only check that hard-requires real tables is `tools/panel_data.check`. It stays red, on
  purpose, as the visible marker of the dependency.
- **Step 5 is the first that genuinely cannot pass** without real data, because its gate is three
  questions answered end to end.

If gold slips past step 4, keep building steps 6 and 8 — they do not depend on it either — and
hold step 5's gate open.

---

## 5. The steps

### Step 0 — Foundations · **done**, commit `1bccf1f`

No agent code. Fixes the rules and unblocks the data side.

- `docs/adr/0001-agent-tree-plain-python-imports.md` — closes `agent-design.md` open item 22.8
- `docs/architecture/agent_data_contract.md` — the six gold tables, for the data engineer
- `.github/scripts/check_agent_layering.py` — the import-based layering check
- `.github/workflows/ci.yml` — the check joins the matrix
- `.gitignore` with `/data/` anchored; `test_empty_file.txt` removed

**Gate:** CI green, and the data engineer answers the six questions in contract section 7.

**Done in step 1:** `ci.yml` gained a `push` trigger on `matias`, so every commit is checked.

### Step 1 — `setup/config.py` + `llm/` + the runner · gate A1 · **code complete**

Written and verified as far as a machine without workspace credentials can:

- `notebooks/setup/config.py` — every name, path and role setting. Standard library only, no logic.
- `notebooks/llm/parsing.py` — `message_text`, `ask_structured`, `check_schema_supported`
- `notebooks/llm/gateway.py` — `get_chat_model(role)`, `chat_model_for_name`, credential cache,
  `extra_body`, the 300-token floor
- `notebooks/pipeline/runner.py` — `run_stage`, `check_import_names`
- `notebooks/pipeline/verify_foundations.py` — the stage
- `notebooks/pipeline/probe_model_capabilities.py` + `docs/runbooks/model-capability-probe.md`

**Verified locally:** the layering check is clean; `message_text` handles all six shapes;
`check_schema_supported` catches `anyOf`, `$ref`/`$defs`, `additionalProperties` and `pattern` with
the exact JSON pointer; the runner passes, fails, skips a blocked check and raises.

**Gate A1 is NOT met yet.** It needs a workspace, and cannot be closed from a laptop:

1. Run `pipeline/verify_foundations` on Databricks. Three green rows: `setup/config`,
   `llm/gateway`, `llm/parsing`.
2. Run `pipeline/probe_model_capabilities` and paste its table into the runbook.

Only then is step 2 worth starting — every `tools/` check runs on the gateway this step builds.

**Known risks, both now guarded rather than theoretical:**

- **`setup` and `tools` are shadowable top-level names.** Both collisions were observed on the
  development laptop: a stray `setup.py` on an `easy-install.pth`, and an installed `tools`
  distribution. `check_import_names` fails the stage on a name we own that resolves elsewhere, and
  warns on a name we are about to own. It runs before any other import in every stage notebook.
- **`notebooks/` on `sys.path`** (open item 22.12) is a four-line prologue at the head of each
  stage notebook. It cannot be a helper: nothing can import the thing that makes imports work.

### Step 2 — `tools/` · gate A2 · **code complete**

Nineteen tools, plus the symbol resolver, the error wrapper and the panel loader. `search_news` is
the twentieth and belongs to step 7.

- `tools/formatting.py` — where the two contracts live: `as of YYYY-MM-DD` (parsed, not
  decorative) and `n/a` for a missing value (never `0`)
- `tools/symbols.py` — `resolve_symbol`, 40 aliases, `UnknownSymbolError`. **Never near-matches**
- `tools/safe_tool.py` — the exception-to-instruction wrapper, the one file importing LangChain
- `tools/panel_data.py` — `PanelData` (seven frames), `load_panel_data`, `describe`
- `tools/{technical,risk,fundamental,macro}_tools.py` — 6 + 4 + 4 + 5 tools
- `tools/fixtures.py` — a generated `PanelData`, deliberately awkward
- `tools/checks.py` — the shared contract check all four families run
- `pipeline/verify_foundations.py` extended, with the fixture fallback

**Verified locally, against the fixture:** all 19 tools return, every output carries a parseable
as-of date, none errors, an unknown symbol raises instead of near-matching, and the stage runner
turns `tools/panel_data` red while the rest stay green.

**Gate A2 needs the gold tables.** Until then `tools/panel_data.check` is red on purpose and every
other row proves logic, not data. `verify_foundations` prints a loud warning when it falls back.

**One design decision worth knowing about.** A structural absence is an answer, not an error.
`get_fundamentals("BTC-USD")` returns a plain statement that crypto has no issuer and never will,
rather than going through `safe_tool` — because the error text tells the model to try different
arguments, and for crypto there are none that would work. Unknown symbols and missing rows still
error normally.

**Two departures from the letter of `agent-design.md`, both deliberate:**

- `PanelData` carries **seven** frames, not the five of section 5.3. `instruments` supplies the
  `asset_class` the planner's routing rules key off and `resolve_symbol` matches names against;
  `macro_series` supplies the `unit` that decides points versus percent. Neither is inferable.
- Two files the tree in section 3 does not list: `formatting.py`, because duplicating the as-of
  contract across four families is how it drifts, and `checks.py`, for the same reason.

### Step 3 — `guardrails/` + `graph/opinion.py` · gate A3

- `notebooks/guardrails/input_guardrail.py` — `InputVerdict`, **fails open**. The line is
  personalization, not topic.
- `notebooks/guardrails/output_guardrail.py` — `GroundednessVerdict`, `DISCLAIMER`, **fails safe**,
  three drafts total, one message replaced not appended (`FINAL_ANSWER_ID`)
- `notebooks/graph/state.py` — `FinHiveState` with the `operator.add` reducer on `cards`. Without
  it the parallel experts overwrite each other.
- `notebooks/graph/opinion.py` — card shape, evidence rebuilt from tool messages, coercion rules,
  consensus arithmetic (`0.5` directional, `0.35` dispersion)
- `notebooks/evaluation/build_golden_set.py` — the 11+ guardrail cases
- `notebooks/pipeline/verify_components.py`

**Gate A3:** guardrail cases pass 100% in MLflow; disclaimer appended in code, once, on the final
message only.

**Watch for:** the research/advice line was calibrated on a larger model. If the small `guard_in`
endpoint fails the cases, move up one size rather than loosening the prompt.

### Step 4 — `agents/` + planner + synthesizer · gate A4, part 1

- `notebooks/agents/base.py` — `OPINION_CONTRACT` (defined exactly once) + `build_expert_agent`
- `notebooks/agents/{technical,quant_risk,fundamental,macro}_analyst.py`
- `notebooks/graph/planner.py` — `PlannerOutput`; the routing rules live in the prompt, ticker
  resolution and the full-panel fallback live in code
- `notebooks/graph/synthesizer.py` — `fallback_answer`; the revision block on a retry
- Panel cases of the golden set; extends `verify_components.py`

**Gate:** crypto convenes no fundamental analyst, a pure macro question convenes only the macro
one, each expert makes at least one tool call and emits a well-formed judgement.

### Step 5 — `graph/build.py` + assemble stage · gate A4, part 2 · **needs real gold**

- `notebooks/graph/build.py` — nodes, `Send` fan-out, the single join edge, compile
- `notebooks/pipeline/assemble_agent.py`

**Gate A4:** three questions end to end; `blocked`, cards, consensus, disclaimer and `messages[-1]`
all correct; none exhausts the three drafts.

### Step 6 — `serving/` + release stages + job spec · gate A7 · **the PR to `main` opens here**

- `notebooks/serving/model_entry.py` — the `ResponsesAgent`; `PanelData` loads in `load_context`,
  never per request
- `notebooks/serving/endpoint.py` — `champion_version`, `mark_champion`, `set_served_version`
- `notebooks/pipeline/{package_agent,deploy_agent,rollback_agent}.py`
- `deploy/databricks/jobs/deploy_agent.yaml`
- `docs/runbooks/agent-deploy-rollback.md`
- **Fix `promote_job_specs.py`** — it calls `path.split("finhive-2026", 1)[1]` on every
  `base_parameters` value, and `deploy_agent.yaml`'s parameters do not contain that string, so it
  raises `IndexError`. It also writes `/Workspace/shared/…` in lowercase while
  `sync_shared_repo.py` and the architecture use `Shared`.

**Gate A7:** a green `finhive_deploy_agent` run; the served answer matches the in-process one;
`predict_stream` yields increments. **Once this gate passes, open the pull request from `matias` to
`main`** — that is the "CI/CD works" milestone the whole branch was building toward.

**Prove deliberately:** that `run_if: AT_LEAST_ONE_FAILED` actually runs `rollback_agent` when
`deploy_agent` fails, and that the Unity Catalog alias behaves as section 17 assumes (open item
22.12). Force a failure once.

**Remember:** `package_agent`'s `pip_requirements` must equal the job environment's pins exactly.
The serving container is another machine.

### Step 7 — News analyst · gates A5 + A6 · blocked

- `notebooks/tools/news_tools.py` — hybrid query, filters, MMR (normalize by **best in set**, not
  min-max)
- `notebooks/agents/news_analyst.py` + the news routing rule in the planner prompt
- ~15 news cases, including deliberately unanswerable ones

**Blocked on:** `gold_news`, its Delta Sync index, and a provider key in the secret scope.

### Step 8 — Golden set and judges · gate A8

Golden set to ~40 cases; `docs/runbooks/agent-evaluation.md` with the name and instructions of
every judge — judges live in the workspace, not in git, and one nobody wrote down cannot be
recreated.

### Steps 9 and 10 — optional

Semantic cache (blocked on the three checks of section 14.1) and Lakebase memory.

---

## 6. Open questions, and who owns them

| # | Question | Owner | Blocks |
|---|---|---|---|
| 1 | Catalog name — `finhive` or `finhive_free`? | Data engineer | Step 2's config |
| 2 | Are the freshness thresholds achievable? | Data engineer | Step 2 |
| 3 | Which `gold_fundamentals` columns will be systematically `NULL`? | Data engineer | Step 2 |
| 4 | Benchmark for `beta`, including for crypto | Data engineer | Step 2 |
| 5 | Correlation window and universe | Data engineer | Step 2 |
| 6 | The eight extra FRED series — in scope? | Data engineer | Step 2's macro tools |
| 7 | Does `response_format` hold on all four models? | **Probe written, not yet run** | Step 3 |
| 8 | Do the generic import names resolve to our modules? | **Resolved** — `check_import_names`, in every stage | — |

---

## 7. How to resume

1. Read this file, then `agent-design.md` section 20 for the gate the next step has to pass.
2. `git fetch && git status` — confirm `matias` is current and check what moved while away.
3. Check the status table in section 1 against `git log --oneline origin/main..matias`.
4. Build the next step, verify against its gate, commit to `matias`.
5. **Update section 1's status table before finishing.**
