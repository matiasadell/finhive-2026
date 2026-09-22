# Databricks notebook source
"""Composing the panel's cards into one answer.

**The consensus is computed before the model is called, and the model is told not to recompute
it.** Agreement is arithmetic; handing it to a language model to re-derive is how "mixed, 100%
agreement" gets written.

**Disagreement is the most valuable thing on the page.** A panel that splits has found something a
single analyst would have smoothed over, so the prompt requires naming who holds which view and
what drives the difference -- never averaging it into a vague middle.

**An empty model reply does not lose the answer.** `fallback_answer` renders every card verbatim
with the consensus line. The panel already did the work; a silent model must not throw it away.

**On a retry** the node reads `unsupported` and `groundedness_reason` off the state and appends a
revision block. The experts are not re-run: their evidence is fixed and grounded by construction,
and a fabricated figure almost always appears when the synthesizer paraphrases.
"""

# COMMAND ----------

from __future__ import annotations

from langchain_core.messages import AIMessage
from langgraph.types import Command

from graph.opinion import compute_consensus
from llm.parsing import message_text
from setup import config

# COMMAND ----------

SYNTHESIZER_SYSTEM_PROMPT = """You write the final answer from a panel of independent expert views.

You have no tools and no data of your own. Everything you may state is in the cards below.

RULES:

1. **Use only figures present in the cards.** You cannot look anything up, and a figure you add is
   a figure nobody can check. If something is missing, say it is missing.

2. **Quote the computed consensus; never recompute or contradict it.** The agreement percentage,
   the label and the dispersion were calculated in code from the cards. Restate them as given.

3. **Disagreement is the most valuable thing you have.** Where experts differ, name which expert
   holds which view and what evidence drives the difference. Never split it into a vague middle --
   "the picture is mixed" tells the reader nothing, "the technical analyst reads the trend as
   constructive while the risk analyst flags that the volatility behind it has doubled" tells them
   everything.

4. **Report what the panel could not see.** An expert that was unavailable, had no data, or
   returned insufficient_data is a stated limit of the answer, not something to pass over.

5. **Never advise.** No buy, sell or hold. No position size. No price target. You describe what
   the evidence shows.

STRUCTURE:

- The direct answer, in two or three sentences.
- The evidence, grouped by domain, with as-of dates and attributed to the expert that found it.
- Where the panel agrees and where it does not.
- What would change the picture, drawn from the experts' caveats.

Prose, not JSON. No headings unless the answer is long enough to need them."""

REVISION_BLOCK = """

---

REVISION REQUIRED. Your previous draft stated these figures, which are not in the evidence above:

{figures}

The verifier's reason: {reason}

Rewrite the answer using only the figures in the cards. If a figure you wanted is genuinely not
there, say that it is not available rather than restating it."""


# COMMAND ----------


def render_card(card: dict) -> str:
    """One expert's view, as the synthesizer sees it."""
    lines = [
        f"### {card.get('expert')}",
        f"stance: {card.get('stance')} | confidence: {card.get('confidence')} | "
        f"horizon: {card.get('horizon')} | tool calls: {card.get('tool_calls')}",
    ]
    if card.get("error"):
        lines.append(f"DID NOT COMPLETE: {card['error']}")
    if card.get("degraded"):
        lines.append(
            "NOTE: this expert's judgement block could not be parsed. Its stance below is a "
            "default, not its own. Use its narrative, not its stance."
        )
    if card.get("key_findings"):
        lines.append("findings:\n" + "\n".join(f"  - {f}" for f in card["key_findings"]))
    if card.get("caveats"):
        lines.append("caveats:\n" + "\n".join(f"  - {c}" for c in card["caveats"]))
    if card.get("narrative"):
        lines.append(f"narrative:\n{card['narrative']}")

    dated = sorted({e["as_of"] for e in card.get("evidence") or [] if e.get("as_of")})
    if dated:
        lines.append(f"evidence dated: {', '.join(dated)}")
    return "\n".join(lines)


def render_cards_for_synthesis(cards: list[dict], consensus: dict) -> str:
    """Every card, then the computed consensus, explicitly fenced off from recomputation."""
    blocks = [render_card(card) for card in cards]
    blocks.append(
        "### COMPUTED CONSENSUS (calculated in code from the cards above -- do not recompute, "
        "do not contradict)\n"
        + "\n".join(f"{key}: {value}" for key, value in consensus.items())
    )
    return "\n\n".join(blocks)


def fallback_answer(cards: list[dict], consensus: dict) -> str:
    """Every card verbatim, with the consensus line. Used when the model returns nothing."""
    header = (
        f"The panel reached a {consensus.get('label', 'unknown')} view with "
        f"{consensus.get('agreement_pct', 0)}% agreement across "
        f"{len(consensus.get('participating_experts') or [])} expert(s). The summary below could "
        f"not be composed, so each expert's own view follows unedited."
    )
    return header + "\n\n" + "\n\n".join(render_card(card) for card in cards)


# COMMAND ----------


def make_synthesizer_node(chat):
    """Build the node. Increments `synthesis_attempts` on every run, including revisions."""

    def synthesizer(state: dict) -> Command:
        cards = state.get("cards") or []
        consensus = compute_consensus(cards)
        attempts = int(state.get("synthesis_attempts") or 0) + 1

        plan = state.get("plan") or {}
        question = plan.get("question") or ""

        user_content = f"QUESTION:\n{question}\n\nPANEL:\n{render_cards_for_synthesis(cards, consensus)}"

        unsupported = state.get("unsupported") or []
        if unsupported or state.get("grounded") is False:
            user_content += REVISION_BLOCK.format(
                figures="\n".join(f"  - {item}" for item in unsupported) or "  - (unspecified)",
                reason=state.get("groundedness_reason") or "not stated",
            )

        answer = message_text(
            chat("synthesizer").invoke(
                [
                    {"role": "system", "content": SYNTHESIZER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ]
            )
        ).strip()

        if not answer:
            answer = fallback_answer(cards, consensus)

        return Command(
            goto=config.NODE_OUTPUT_GUARDRAIL,
            update={
                "consensus": consensus,
                "synthesis_attempts": attempts,
                "messages": [
                    AIMessage(content=answer, name="synthesizer", id=config.FINAL_ANSWER_ID)
                ],
            },
        )

    return synthesizer


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Fixture cards give an answer; an empty reply gives the fallback. One model call.

    `ctx` keys used:
      chat  callable(role) -> chat model
    """
    cards = [
        {
            "expert": config.TECHNICAL_ANALYST,
            "stance": "bullish",
            "confidence": 0.7,
            "horizon": "short",
            "narrative": "Trading above its 200-day average.",
            "key_findings": ["Price above the 200-day average"],
            "caveats": ["Momentum is close to overbought"],
            "evidence": [{"ref": "get_trend_signals", "as_of": "2026-09-19", "detail": "RSI 68"}],
            "tool_calls": 1,
            "degraded": False,
            "error": None,
        },
        {
            "expert": config.QUANT_RISK_ANALYST,
            "stance": "bearish",
            "confidence": 0.6,
            "horizon": "medium",
            "narrative": "Volatility has doubled while the price rose.",
            "key_findings": ["20-day volatility is well above the 60-day"],
            "caveats": [],
            "evidence": [{"ref": "get_risk_profile", "as_of": "2026-09-19", "detail": "vol 0.34"}],
            "tool_calls": 1,
            "degraded": False,
            "error": None,
        },
    ]
    consensus = compute_consensus(cards)

    rendered = render_cards_for_synthesis(cards, consensus)
    if "do not recompute" not in rendered:
        raise AssertionError("the consensus block must tell the model not to recompute it")

    fallback = fallback_answer(cards, consensus)
    if config.TECHNICAL_ANALYST not in fallback or config.QUANT_RISK_ANALYST not in fallback:
        raise AssertionError("the fallback must render every card; the panel already did the work")

    chat = ctx.get("chat")
    if chat is None:
        return {"detail": f"rendering ok; consensus={consensus['label']}; no chat in ctx"}

    node = make_synthesizer_node(chat)
    command = node({"cards": cards, "plan": {"question": "What do you think of AAPL?"}})

    answer = message_text(command.update["messages"][0])
    if not answer.strip():
        raise AssertionError("the synthesizer produced nothing and did not fall back")
    if command.update["synthesis_attempts"] != 1:
        raise AssertionError(f"attempts should be 1, got {command.update['synthesis_attempts']}")
    if command.update["messages"][0].id != config.FINAL_ANSWER_ID:
        raise AssertionError("the answer must occupy FINAL_ANSWER_ID so a retry replaces it")

    # A revision must carry what to fix; without it a second draft reproduces the mistake.
    revised = node(
        {
            "cards": cards,
            "plan": {"question": "What do you think of AAPL?"},
            "synthesis_attempts": 1,
            "grounded": False,
            "unsupported": ["P/E of 42"],
            "groundedness_reason": "no P/E appears in the evidence",
        }
    )
    if revised.update["synthesis_attempts"] != 2:
        raise AssertionError("a revision must increment the attempt count")

    return {
        "detail": (
            f"consensus={consensus['label']} at {consensus['agreement_pct']}% with "
            f"{len(consensus['conflicts'])} conflict(s); answer {len(answer)} chars; "
            f"revision carried 1 unsupported figure"
        )
    }
