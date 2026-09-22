# Runbook — model capability probe

**What it answers:** whether `response_format` is honoured by every model the agent talks to, or
whether some node has to fall back to forced function calling. This is open item 11 of
`agent-design.md` section 22, and three nodes rest on it: the planner and both guardrails.

**Why it cannot be inferred.** `finhive_router` sends roughly 70% of requests to Llama 3.3 70B and
30% to GPT-OSS 120B, **chosen per request**. The documentation consulted covers `response_format`
for the GPT-OSS models and function calling for all four, but not `response_format` for Llama 3.3
70B or Llama 3.1 8B. If one of the routed pair ignores it, a schema enforced only by prompt would
drift on a share of calls, and the failure would look random rather than structural.

**When to run it:** once, before step 3 of `docs/plan/agent-build-plan.md`; again whenever a model
behind `finhive_router` changes, or a guardrail endpoint is swapped. Model catalogues churn on
published retirement dates, which is why the names are variables in `setup/config.py`.

---

## Running it

`notebooks/pipeline/probe_model_capabilities.py`, by hand, as a notebook. It is deliberately not a
task of `finhive_deploy_agent`: it spends real tokens answering a design question, not a
per-deploy one.

| Widget | Default | Meaning |
|---|---|---|
| `profile` | `free` | Must match `setup.config.PROFILE` |
| `attempts` | `5` | Calls per (model, schema). Below 5 a flaky endpoint reads as a working one |

Cost at the default: 4 models × 3 schemas × 5 = 60 calls with `response_format`, plus up to 5 more
per combination that was not perfect. Small, but not free.

## Reading the result

The probe prints a markdown table and a verdict. **Paste the table into section "Results" below**,
with the date and who ran it — a measurement nobody wrote down has to be paid for twice.

| Outcome | What it means | What to do |
|---|---|---|
| `5/5` everywhere under `response_format` | The server constrains output on every model | Nothing. `ask_structured` keeps its current binding and open item 22.11 closes |
| A model is short of `5/5`, function calling is `5/5` | That model ignores or partially honours `response_format` | Switch the nodes that run on it to forced function calling inside `llm/parsing.ask_structured`. **Nothing about the schemas or the callers changes** |
| Both mechanisms short of `5/5` | Usually the token cap, not the mechanism | Check the note column for `empty content`: a reasoning model spent the budget thinking. Raise the cap for that role before concluding anything about the mechanism |
| An endpoint errors on every call | It may not exist on this workspace | Free Edition lists "certain models not available" for Model Serving. Confirm the endpoint exists before blaming the schema (`agent-design.md` section 11.3) |

**Do not loosen a prompt to make a schema pass.** If `guard_in` cannot hold its shape on
`databricks-meta-llama-3-1-8b-instruct`, move up one size — `databricks-gpt-oss-120b` at 2.143 /
8.571 DBU per 1M tokens — rather than weakening what the guardrail asks for.

## A note on the schemas

The probe defines `PlannerOutput`, `InputVerdict` and `GroundednessVerdict` locally, because
`graph/planner.py` and `guardrails/` do not exist yet and the decision has to be made before they
are written.

**When steps 3 and 4 land, change the probe to import the real schemas and delete the local
copies.** A schema measured here and a schema shipped there that have drifted apart would make
this whole runbook a lie.

---

## Results

_Not yet run. The first run belongs here._

| Date | Ran by | `attempts` | Verdict |
|---|---|---|---|
| | | | |

<!--
Paste the probe's markdown table here, unedited:

| role | model | schema | response_format | function calling | note |
|---|---|---|---|---|---|
-->
