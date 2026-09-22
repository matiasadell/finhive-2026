# Databricks notebook source
"""The only way out. Checks every figure against the evidence, and **fails safe**.

**The evidence base has three sources, and the third is the one that was missing.** Card tool
output, card judgements, and the **computed consensus**. Omitting the consensus flagged 3 of 6
correct answers as ungrounded, because the synthesizer was quoting agreement figures FinHive
itself had computed but had never shown the verifier.

**Three drafts in total**, not three retries: the first synthesis plus at most two revisions. The
retry carries the verifier's `unsupported` list, because without it a second draft at temperature
0.2 reproduces the same mistake. Only the synthesizer is re-run -- the experts' evidence is fixed
and grounded by construction, and a fabricated figure almost always appears when the synthesizer
paraphrases.

**Exhausting the drafts does not withhold the answer.** The last draft ships with a warning
prepended to the same message, then the disclaimer. The reader always gets an answer; the retry
only reduces how often it carries a warning.

**One message, replaced not appended.** The answer always occupies `FINAL_ANSWER_ID`, so a
consumer that reads only `messages[-1]` never sees an earlier draft.

**The disclaimer is appended in code, once, on the final message only.** Something legally
load-bearing must not depend on a model remembering it, and must not be stacked once per draft.
"""

# COMMAND ----------

from __future__ import annotations

from langchain_core.messages import AIMessage
from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel, Field

from llm.parsing import ask_structured, message_text
from setup import config
from setup.config import FINAL_ANSWER_ID, NODE_CACHE_STORE, NODE_SYNTHESIZER

# COMMAND ----------

# Measured against the verifier's context, not chosen for elegance.
MAX_EVIDENCE_CHARS = 24_000

DISCLAIMER = (
    "\n\n---\n"
    "This is research output, not financial advice. Every figure above comes from market data "
    "through deterministic tools, with its as-of date stated. It takes no account of anyone's "
    "circumstances, holdings, tax position or risk tolerance. Markets move; a figure dated "
    "yesterday may be wrong today."
)

UNGROUNDED_WARNING = (
    "NOTE -- this answer could not be fully verified against the evidence the panel gathered. "
    "The following figures could not be traced back to a tool result: {figures}. Treat them with "
    "particular caution and check them before relying on them.\n\n"
)


class GroundednessVerdict(BaseModel):
    """Flat, required, described -- the shape Databricks structured outputs accept (section 4.2)."""

    grounded: bool = Field(
        description="False only if a specific figure in the draft appears nowhere in the evidence."
    )
    reason: str = Field(description="One sentence.")
    unsupported: list[str] = Field(
        description=(
            "The figures the draft states that the evidence does not support, e.g. "
            "['P/E of 42', 'an 18% drop']. An empty list if there are none."
        )
    )


OUTPUT_GUARDRAIL_SYSTEM_PROMPT = """You verify that a draft answer is grounded in its evidence.

You are given TOOL OUTPUT -- everything the panel actually retrieved and computed -- and a DRAFT
ANSWER written from it. Your only question is whether every specific figure in the draft can be
traced to the evidence.

Mark it ungrounded ONLY when a specific figure appears nowhere in the evidence and cannot be read
off it. Each of these is FINE and is not a reason to fail a draft:

  - rounding (evidence 231.40, draft "about 231")
  - rephrasing a figure in words ("roughly a fifth" for 18.4%)
  - restating an agreement or consensus figure that appears in the COMPUTED CONSENSUS block
  - a qualitative characterisation the evidence supports ("elevated volatility" where the tools
    labelled the regime elevated)
  - naming a date, a tool or an expert that appears in the evidence

List in `unsupported` only the figures that are genuinely absent. That list is handed straight
back to the writer as the thing to fix, so a figure listed there that was in fact supported costs
a wasted rewrite."""

# COMMAND ----------


def build_evidence(cards: list[dict], consensus: dict) -> str:
    """Everything the verifier is allowed to check against, capped.

    Three sources. The consensus is the one that was learned the hard way.
    """
    blocks: list[str] = []

    for card in cards or []:
        expert = card.get("expert", "unknown")
        for item in card.get("evidence") or []:
            blocks.append(f"[{expert} -> {item.get('ref')}]\n{item.get('detail', '')}")

    for card in cards or []:
        expert = card.get("expert", "unknown")
        blocks.append(
            f"[{expert} declared]\nstance={card.get('stance')} "
            f"confidence={card.get('confidence')} horizon={card.get('horizon')}\n"
            f"findings: {'; '.join(card.get('key_findings') or []) or 'none'}\n"
            f"caveats: {'; '.join(card.get('caveats') or []) or 'none'}"
        )

    if consensus:
        blocks.append(
            "[COMPUTED CONSENSUS -- these figures were computed by FinHive itself, in code. A "
            "draft that quotes them is grounded.]\n"
            + "\n".join(f"{key}: {value}" for key, value in consensus.items())
        )

    evidence = "\n\n".join(blocks)
    return evidence[:MAX_EVIDENCE_CHARS]


def finish(answer: str, warning: str = "") -> Command:
    """Write the final message: warning first, then the answer, then the disclaimer, once."""
    return Command(
        goto=NODE_CACHE_STORE,
        update={
            "messages": [
                AIMessage(
                    content=warning + answer + DISCLAIMER,
                    name="final_answer",
                    id=FINAL_ANSWER_ID,
                )
            ]
        },
    )


# COMMAND ----------


def make_output_guardrail(chat):
    """Build the node. It is a conditional router, not a terminal step."""

    def output_guardrail(state: dict) -> Command:
        messages = state.get("messages") or []
        answer = message_text(messages[-1]) if messages else ""
        cards = state.get("cards") or []
        consensus = state.get("consensus") or {}
        evidence = build_evidence(cards, consensus)

        if not evidence:
            # Nothing to check against. Withholding an answer here would punish the reader for a
            # panel failure they can already see in the cards.
            return finish(answer)

        verdict = ask_structured(
            chat,
            "guard_out",
            OUTPUT_GUARDRAIL_SYSTEM_PROMPT,
            f"TOOL OUTPUT:\n{evidence}\n\nDRAFT ANSWER:\n{answer}",
            GroundednessVerdict,
            # Fails SAFE: the answer still ships, with its disclaimer. A verifier that is down
            # must not become a second way to lose an answer the panel already paid for.
            default=GroundednessVerdict(
                grounded=True, reason="verifier unavailable", unsupported=[]
            ),
        )

        if verdict.grounded:
            return finish(answer)

        attempts = int(state.get("synthesis_attempts") or 0)
        unsupported = [str(item) for item in verdict.unsupported if str(item).strip()]

        if attempts < config.MAX_SYNTHESIS_ATTEMPTS:
            return Command(
                goto=NODE_SYNTHESIZER,
                update={
                    "grounded": False,
                    "groundedness_reason": verdict.reason,
                    "unsupported": unsupported,
                },
            )

        # Drafts exhausted. Ship it, visibly flagged.
        return finish(
            answer,
            warning=UNGROUNDED_WARNING.format(
                figures="; ".join(unsupported) or "unspecified figures"
            ),
        )

    return output_guardrail


# COMMAND ----------


def check(ctx: dict) -> dict:
    """The schema; a grounded draft passes and an invented figure is caught. Two model calls.

    `ctx` keys used:
      chat  callable(role) -> chat model
    """
    from llm.parsing import check_schema_supported

    supported = check_schema_supported(GroundednessVerdict)

    cards = [
        {
            "expert": "technical_analyst",
            "stance": "bullish",
            "confidence": 0.7,
            "horizon": "short",
            "key_findings": ["Trading above its 200-day average"],
            "caveats": [],
            "evidence": [
                {
                    "source_type": "tool",
                    "ref": "get_price_snapshot",
                    "detail": "AAPL close 231.40, 21-day change +4.2%\n\nas of 2026-09-19",
                    "as_of": "2026-09-19",
                }
            ],
        }
    ]
    consensus = {"label": "bullish", "agreement_pct": 100.0, "participating_experts": ["technical_analyst"]}

    evidence = build_evidence(cards, consensus)
    if "COMPUTED CONSENSUS" not in evidence:
        raise AssertionError(
            "the consensus must be in the evidence base -- omitting it flagged 3 of 6 correct "
            "answers as ungrounded (section 11.2)"
        )
    if "get_price_snapshot" not in evidence or "declared" not in evidence:
        raise AssertionError("the evidence base is missing one of its three sources")

    chat = ctx.get("chat")
    if chat is None:
        return {"detail": f"schema ok ({supported['properties']} properties); no chat in ctx"}

    node = make_output_guardrail(chat)

    grounded = node(
        {
            "messages": [AIMessage(content="AAPL closed at 231.40, up 4.2% over 21 days.")],
            "cards": cards,
            "consensus": consensus,
            "synthesis_attempts": 1,
        }
    )
    if grounded.goto != NODE_CACHE_STORE:
        raise AssertionError(f"a grounded draft must ship; got goto={grounded.goto}")
    if DISCLAIMER not in message_text(grounded.update["messages"][0]):
        raise AssertionError("the disclaimer must be appended in code on the final message")

    invented = node(
        {
            "messages": [
                AIMessage(content="AAPL closed at 231.40 and trades on a P/E of 42 with an 18% drop.")
            ],
            "cards": cards,
            "consensus": consensus,
            "synthesis_attempts": 1,
        }
    )
    if invented.goto != NODE_SYNTHESIZER:
        raise AssertionError(
            "a draft stating a P/E and a drawdown that appear nowhere in the evidence must go "
            f"back to the synthesizer; got goto={invented.goto}"
        )
    if not invented.update.get("unsupported"):
        raise AssertionError(
            "the retry must carry what to fix; without it a second draft reproduces the mistake"
        )

    exhausted = node(
        {
            "messages": [AIMessage(content="AAPL trades on a P/E of 42.")],
            "cards": cards,
            "consensus": consensus,
            "synthesis_attempts": config.MAX_SYNTHESIS_ATTEMPTS,
        }
    )
    if exhausted.goto != NODE_CACHE_STORE:
        raise AssertionError("exhausting the drafts must still ship the answer, flagged")
    shipped = message_text(exhausted.update["messages"][0])
    if "NOTE --" not in shipped or DISCLAIMER not in shipped:
        raise AssertionError("the exhausted draft must carry a warning and the disclaimer")

    return {
        "detail": (
            f"schema ok ({supported['properties']} properties); grounded draft shipped, "
            f"invented figures caught ({invented.update['unsupported']}), "
            f"exhausted draft shipped with a warning"
        )
    }
