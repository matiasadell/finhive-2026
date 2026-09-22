# Databricks notebook source
"""The supervisor: who to ask, and what to ask them.

**The model does the routing.** Which experts a question deserves is decided by this prompt, not
by a capability matrix -- the one deliberate exception to "the model proposes, code decides"
(`agent-design.md` invariant 2, section 6). A table of asset classes intersected with the model's
choice was more machinery than the rules deserve.

**What that trades away, stated plainly.** Routing is now a property of a prompt, and the model
behind the gateway changes per request. Nothing in code stops the planner from convening the
fundamental analyst for Bitcoin. What remains is the cheap visible failure: that expert's tools
have no data, it returns `insufficient_data`, it is excluded from the consensus, and the answer
says so. A wasted call, not a wrong answer. The safeguard is a gate, not a function -- the golden
set's panel cases assert the rules and are run before a release.

**Two things stay in code, because they still need a guarantee.** Ticker resolution, because a
hallucinated instrument must never reach an expert; and the fallback, so a failed planner call
still produces a panel rather than an empty one.
"""

# COMMAND ----------

from __future__ import annotations

from langgraph.types import Command, Send
from pydantic import BaseModel, Field

from llm.parsing import ask_structured
from setup import config
from tools.symbols import UnknownSymbolError, resolve_symbol

# COMMAND ----------


class PlannerOutput(BaseModel):
    """Flat, required, described -- the shape Databricks structured outputs accept (section 4.2).

    Five sub-questions as five fields rather than a dictionary: a dictionary becomes
    `additionalProperties` in JSON Schema, which the gateway may reject, and an empty string is an
    unambiguous way to say "do not consult".
    """

    mentions: list[str] = Field(
        description=(
            "The instruments the user named, exactly as they wrote them. Never expand a sector or "
            "an index into its members. An empty list if they named none."
        )
    )
    technical_analyst: str = Field(
        description="The sub-question for the technical analyst, in their terms. '' to skip them."
    )
    quant_risk_analyst: str = Field(
        description="The sub-question for the quant/risk analyst, in their terms. '' to skip them."
    )
    fundamental_analyst: str = Field(
        description="The sub-question for the fundamental analyst, in their terms. '' to skip them."
    )
    macro_analyst: str = Field(
        description="The sub-question for the macro analyst, in their terms. '' to skip them."
    )
    news_analyst: str = Field(
        description="The sub-question for the news analyst, in their terms. '' to skip them."
    )
    reasoning: str = Field(description="One sentence on why these experts and not the others.")


DEFAULT_SUB_QUESTION = "Give your domain's read on this question: {question}"

# The routing rules of section 6, written for the model rather than encoded as a matrix.
ROUTING_RULES = """
RULES, each with the reason it exists:

- **Never convene the fundamental analyst for crypto or FX.** There is no issuer, no financial
  statements and no earnings, so there is nothing for them to read.
- **Never convene the news analyst when no instrument is named.** The news feed is indexed per
  instrument; a question about rates or inflation belongs to the macro analyst.
- **For a pure macro question with no instrument, convene only the macro analyst.** The
  instrument experts have nothing to read.
- **For an ETF, convene the fundamental analyst only if the question is about valuation or
  holdings.** Fundamentals data for ETFs is partial.
- **A broad question about an instrument ("what do you think of X") convenes every expert not
  excluded above.** A panel is the product.
- **Convene an expert only if it genuinely contributes.** A panel of five on a question that needs
  one is waste, and a wasted expert dilutes the consensus with a view nobody asked for.
"""


def build_planner_prompt(covers: dict[str, str]) -> str:
    """The prompt, assembled from the experts that actually exist.

    Built rather than written out so that adding the news analyst at step 7 is one entry in
    `agents/panel.py` and nothing here.
    """
    roster = "\n".join(f"- **{name}** -- {description}" for name, description in covers.items())
    unavailable = sorted(set(config.EXPERTS) - set(covers))
    absent = (
        ""
        if not unavailable
        else "\nNOT AVAILABLE in this deployment, leave their field empty whatever the question "
        f"asks for: {', '.join(unavailable)}.\n"
    )
    return f"""You route a financial research question to a panel of independent experts.

Each expert forms its view alone, without seeing the others, which is what makes agreement between
them evidence rather than an artefact of who went first. Your job is to decide who should see this
question, and to write each of them a sub-question in their own terms.

THE PANEL:
{roster}
{absent}
{ROUTING_RULES}

ALSO:

- List in `mentions` only the instruments the user actually named, as they wrote them. Do not
  expand "tech stocks" or "the S&P" into constituents, and do not add instruments they did not
  mention.
- Write each sub-question in that expert's own vocabulary, not as a copy of the user's question.
  The technical analyst should be asked about price action; the macro analyst about the backdrop
  and how it reaches the instrument.
- Leave an expert's field as an empty string to skip them. That is the only way to skip them."""


# COMMAND ----------


def resolve_mentions(data, mentions: list[str]) -> tuple[list[str], list[str]]:
    """Resolve what the model named. **A hallucinated ticker dies here, not downstream.**"""
    symbols: list[str] = []
    unresolved: list[str] = []
    for mention in mentions or []:
        try:
            resolved = resolve_symbol(data, mention)
        except UnknownSymbolError:
            unresolved.append(str(mention))
            continue
        if resolved not in symbols:
            symbols.append(resolved)
    return symbols, unresolved


def build_plan(plan_out: PlannerOutput, question: str, data, available: tuple[str, ...]) -> dict:
    """Turn the model's routing into a plan, with the guarantees code still owns."""
    symbols, unresolved = resolve_mentions(data, plan_out.mentions)

    sub_questions = {
        expert: getattr(plan_out, expert).strip()
        for expert in config.EXPERTS
        if expert in available and getattr(plan_out, expert, "").strip()
    }

    if not sub_questions:
        # The planner failed, returned nothing, or routed only to experts that do not exist here.
        # A panel is still better than silence.
        fallback = list(available) if symbols else [config.MACRO_ANALYST]
        sub_questions = {
            expert: DEFAULT_SUB_QUESTION.format(question=question)
            for expert in fallback
            if expert in available
        }

    return {
        "question": question,
        "symbols": symbols,
        "experts": list(sub_questions),
        "sub_questions": sub_questions,
        "unresolved_mentions": unresolved,
        "reasoning": plan_out.reasoning,
    }


def make_planner_node(chat, data, covers: dict[str, str]):
    """Build the node. `covers` is the roster of experts that exist in this deployment."""
    system_prompt = build_planner_prompt(covers)
    available = tuple(covers)

    def planner(state: dict) -> Command:
        messages = state.get("messages") or []
        question = ""
        for message in reversed(messages):
            role = str(
                (message.get("role") if isinstance(message, dict) else None)
                or getattr(message, "type", "")
            ).lower()
            if role in {"human", "user"}:
                from llm.parsing import message_text

                question = message_text(message)
                break

        plan_out = ask_structured(
            chat,
            "router",
            system_prompt,
            question,
            PlannerOutput,
            # No mentions and every sub-question empty, which `build_plan` turns into the full
            # panel -- or the macro analyst alone if no instrument resolved.
            default=PlannerOutput(
                mentions=[],
                technical_analyst="",
                quant_risk_analyst="",
                fundamental_analyst="",
                macro_analyst="",
                news_analyst="",
                reasoning="planner unavailable; falling back to the full panel",
            ),
        )

        plan = build_plan(plan_out, question, data, available)

        return Command(
            goto=[
                Send(
                    config.NODE_RUN_EXPERT,
                    {
                        "expert": expert,
                        "sub_question": sub_question,
                        "question": question,
                    },
                )
                for expert, sub_question in plan["sub_questions"].items()
            ],
            update={"plan": plan},
        )

    return planner


# COMMAND ----------


def check(ctx: dict) -> dict:
    """The schema, and three canned questions routed as section 6 says. Three model calls.

    `ctx` keys used:
      chat   callable(role) -> chat model
      panel  PanelData, real or fixture
    """
    from llm.parsing import check_schema_supported

    supported = check_schema_supported(PlannerOutput)

    from agents.panel import COVERS

    chat = ctx.get("chat")
    if chat is None:
        return {"detail": f"schema ok ({supported['properties']} properties); no chat in ctx"}

    node = make_planner_node(chat, ctx["panel"], COVERS)

    def plan_for(question: str) -> dict:
        command = node({"messages": [{"role": "user", "content": question}]})
        return command.update["plan"]

    crypto = plan_for("What do you think of Bitcoin right now?")
    if config.FUNDAMENTAL_ANALYST in crypto["experts"]:
        raise AssertionError(
            "a crypto question convened the fundamental analyst, which has no issuer to read "
            f"(section 6). Plan: {crypto['experts']}"
        )

    macro = plan_for("What is the yield curve telling us about the economy?")
    if macro["experts"] != [config.MACRO_ANALYST]:
        raise AssertionError(
            f"a pure macro question must convene only the macro analyst; got {macro['experts']}"
        )

    broad = plan_for("What do you think of Apple at the moment?")
    if len(broad["experts"]) < 2:
        raise AssertionError(
            f"a broad equity question should convene several experts; got {broad['experts']}"
        )
    if "AAPL" not in broad["symbols"]:
        raise AssertionError(f"'Apple' should resolve to AAPL; got {broad['symbols']}")

    # A ticker the model invents must not reach an expert.
    invented = build_plan(
        PlannerOutput(
            mentions=["AAPL", "ZZZZ"],
            technical_analyst="price action",
            quant_risk_analyst="",
            fundamental_analyst="",
            macro_analyst="",
            news_analyst="",
            reasoning="",
        ),
        "q",
        ctx["panel"],
        tuple(COVERS),
    )
    if invented["symbols"] != ["AAPL"] or invented["unresolved_mentions"] != ["ZZZZ"]:
        raise AssertionError(f"a hallucinated ticker must die in the planner: {invented}")

    return {
        "detail": (
            f"schema ok ({supported['properties']} properties); crypto -> {crypto['experts']}; "
            f"macro -> {macro['experts']}; broad -> {len(broad['experts'])} experts on "
            f"{broad['symbols']}; invented ticker dropped"
        )
    }
