# Databricks notebook source
"""The OpinionCard, and the arithmetic that turns a panel of them into a consensus.

Two rules, and everything here follows from them.

**The model declares judgement; code records evidence.** An expert states its stance, confidence
and horizon in a JSON block. It does *not* get to write its own evidence list -- that is rebuilt
here from the tool messages in its own conversation, so a card claiming tool-backed figures with
`tool_calls == 0` is visible immediately.

**Consensus is arithmetic, never a model.** Weighted mean, weighted dispersion, a threshold and a
label. No second LLM call decides whether the panel agreed, because "how much did five experts
agree" is a computation and a computed number can be audited from the trace.

Parsing is defensive throughout: a malformed judgement becomes a **degraded card**, never an
exception. An expert that fails must cost its own opinion and nothing else.

Layering: pure. This file may not import langchain, mlflow, pyspark or databricks -- messages are
read by duck-typing, so an `AIMessage` and a plain dict both work.
"""

# COMMAND ----------

from __future__ import annotations

import json
import math
import re
from typing import Any

# COMMAND ----------

STANCES = ("bullish", "bearish", "neutral", "insufficient_data")
HORIZONS = ("intraday", "short", "medium", "long")

# `insufficient_data` is deliberately absent: an expert with nothing to say must not drag the
# panel toward neutral. It is excluded from the arithmetic and reported as excluded.
STANCE_SCORE = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}

# Measured, not chosen. At 0.34 the panel labelled *one bearish plus two neutral* as "bearish"
# with 36% agreement -- a direction nobody held.
DIRECTIONAL_THRESHOLD = 0.5

# Without a separate dispersion test, a split panel and a uniformly neutral one both came out
# "neutral", and one run reported "mixed, 100% agreement".
DISAGREEMENT_DISPERSION = 0.35

# A card with zero confidence would otherwise carry zero weight and vanish from the mean.
MIN_WEIGHT = 0.05

# What a degraded card claims: present, honest about being unparseable, and barely weighted.
DEGRADED_CONFIDENCE = 0.25

# What an unparseable confidence becomes. Low enough to matter little, not so low it disappears.
FALLBACK_CONFIDENCE = 0.3

MAX_LIST_ITEMS = 8
MAX_EVIDENCE_DETAIL = 1200

# The dating contract every tool honours (`tools/formatting.py`). Parsed, not decorative.
AS_OF_PATTERN = re.compile(r"as of (\d{4}-\d{2}-\d{2})")

_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BRACED = re.compile(r"\{.*?\}", re.DOTALL)


# COMMAND ----------


def _attr(message: Any, name: str, default: Any = None) -> Any:
    """Read a field off a message that may be an object or a dict."""
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _text_of(message: Any) -> str:
    content = _attr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return "" if content is None else str(content)


def _is_tool_message(message: Any) -> bool:
    return str(_attr(message, "type", "") or _attr(message, "role", "")).lower() == "tool"


# COMMAND ----------


def extract_evidence(messages: list[Any]) -> list[dict[str, Any]]:
    """Rebuild the evidence list from the expert's own tool messages.

    The model never writes this. `ref` is the tool's name, `detail` the head of what it returned,
    and `as_of` the date the tool stated -- which is why that date is a parsed contract and not a
    nicety.
    """
    evidence = []
    for message in messages or []:
        if not _is_tool_message(message):
            continue
        detail = _text_of(message)
        match = AS_OF_PATTERN.search(detail)
        evidence.append(
            {
                "source_type": "tool",
                "ref": str(_attr(message, "name", "") or "unnamed_tool"),
                "detail": detail[:MAX_EVIDENCE_DETAIL],
                "as_of": match.group(1) if match else None,
            }
        )
    return evidence


def parse_judgement(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split an expert's message into its judgement block and the prose around it.

    Takes the **last** JSON object containing `"stance"`, fenced first and bare as a fallback,
    because the contract says the block ends the message and prose before it may well contain
    braces of its own.
    """
    if not text:
        return None, ""

    candidates = [(m.start(), m.end(), m.group(1)) for m in _FENCED.finditer(text)]
    if not any('"stance"' in c[2] for c in candidates):
        candidates = [(m.start(), m.end(), m.group(0)) for m in _BRACED.finditer(text)]

    for start, end, blob in reversed(candidates):
        if '"stance"' not in blob:
            continue
        try:
            parsed = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            narrative = (text[:start] + text[end:]).strip()
            return parsed, narrative

    return None, text.strip()


# COMMAND ----------


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return FALLBACK_CONFIDENCE
    if math.isnan(number) or math.isinf(number):
        return FALLBACK_CONFIDENCE
    # Models write confidence as a percentage often enough that reading 85 as 0.85 is worth more
    # than being strict about it.
    if number > 2.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()][:MAX_LIST_ITEMS]


def build_card(expert: str, messages: list[Any]) -> dict[str, Any]:
    """One expert's conversation, turned into a card. Never raises."""
    final_text = ""
    for message in reversed(messages or []):
        if _is_tool_message(message):
            continue
        text = _text_of(message)
        if text.strip():
            final_text = text
            break

    judgement, narrative = parse_judgement(final_text)
    evidence = extract_evidence(messages)

    if judgement is None:
        return degraded_card(expert, narrative or final_text, evidence)

    stance = str(judgement.get("stance", "")).strip().lower()
    horizon = str(judgement.get("horizon", "")).strip().lower()

    return {
        "expert": expert,
        "stance": stance if stance in STANCES else "neutral",
        "confidence": _coerce_confidence(judgement.get("confidence")),
        "horizon": horizon if horizon in HORIZONS else "medium",
        "narrative": narrative,
        "key_findings": _coerce_list(judgement.get("key_findings")),
        "caveats": _coerce_list(judgement.get("caveats")),
        "evidence": evidence,
        "tool_calls": len(evidence),
        "degraded": False,
        "error": None,
    }


def degraded_card(expert: str, narrative: str, evidence: list[dict] | None = None) -> dict[str, Any]:
    """An expert that answered but whose judgement block could not be read.

    Its prose and its evidence are still worth having, so this is a card and not an exception --
    it just says plainly that the stance below is the parser's and not the expert's.
    """
    evidence = evidence or []
    return {
        "expert": expert,
        "stance": "neutral",
        "confidence": DEGRADED_CONFIDENCE,
        "horizon": "medium",
        "narrative": narrative,
        "key_findings": [],
        "caveats": [
            "This expert's judgement block could not be parsed, so its stance and confidence "
            "here are defaults rather than its own. Read its narrative, not its stance."
        ],
        "evidence": evidence,
        "tool_calls": len(evidence),
        "degraded": True,
        "error": None,
    }


def failed_card(expert: str, error: str) -> dict[str, Any]:
    """An expert that raised. Produced so the panel continues without it, visibly."""
    return {
        "expert": expert,
        "stance": "insufficient_data",
        "confidence": 0.0,
        "horizon": "medium",
        "narrative": "",
        "key_findings": [],
        "caveats": [f"This expert did not complete: {error}"],
        "evidence": [],
        "tool_calls": 0,
        "degraded": False,
        "error": error,
    }


# COMMAND ----------


def compute_consensus(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Weighted agreement across the panel. Arithmetic only."""
    participating = [
        c for c in cards if c.get("stance") in STANCE_SCORE and not c.get("error")
    ]
    excluded = [c["expert"] for c in cards if c not in participating]

    if not participating:
        return {
            "participating_experts": [],
            "excluded_experts": excluded,
            "net_stance_score": 0.0,
            "label": "no_view",
            "agreement_pct": 0.0,
            "dispersion": 0.0,
            "conflicts": [],
            "mean_confidence": 0.0,
        }

    weights = [max(float(c.get("confidence") or 0.0), MIN_WEIGHT) for c in participating]
    scores = [STANCE_SCORE[c["stance"]] for c in participating]
    total_weight = sum(weights)

    net = sum(s * w for s, w in zip(scores, weights)) / total_weight
    variance = sum(w * (s - net) ** 2 for s, w in zip(scores, weights)) / total_weight
    dispersion = min(math.sqrt(variance), 1.0)

    if net > DIRECTIONAL_THRESHOLD:
        label = "bullish"
    elif net < -DIRECTIONAL_THRESHOLD:
        label = "bearish"
    elif dispersion >= DISAGREEMENT_DISPERSION:
        # Split, not undecided. Naming the difference is the most valuable thing on the page.
        label = "mixed"
    else:
        label = "neutral"

    majority_sign = 1.0 if net > 0 else -1.0 if net < 0 else 0.0
    if majority_sign == 0.0:
        majority_weight = sum(w for s, w in zip(scores, weights) if s == 0.0)
    else:
        majority_weight = sum(w for s, w in zip(scores, weights) if s * majority_sign > 0)
    agreement = majority_weight / total_weight * 100.0

    conflicts = []
    for i, a in enumerate(participating):
        for b in participating[i + 1 :]:
            if STANCE_SCORE[a["stance"]] * STANCE_SCORE[b["stance"]] < 0:
                conflicts.append(
                    {
                        "experts": [a["expert"], b["expert"]],
                        "stances": [a["stance"], b["stance"]],
                    }
                )

    return {
        "participating_experts": [c["expert"] for c in participating],
        "excluded_experts": excluded,
        "net_stance_score": round(net, 3),
        "label": label,
        "agreement_pct": round(agreement, 1),
        "dispersion": round(dispersion, 3),
        "conflicts": conflicts,
        "mean_confidence": round(
            sum(float(c.get("confidence") or 0.0) for c in participating) / len(participating), 3
        ),
    }


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Fixture cards: evidence rebuilt, a malformed judgement degraded, consensus labels right.

    `ctx` is unused -- this is pure arithmetic over fixtures, and needs no platform at all.
    """
    del ctx

    messages = [
        {"type": "ai", "content": "Let me look."},
        {"type": "tool", "name": "get_price_snapshot", "content": "close 231.40\n\nas of 2026-09-19"},
        {"type": "tool", "name": "get_trend_signals", "content": "RSI 58\n\nas of 2026-09-19"},
        {
            "type": "ai",
            "content": 'Constructive.\n\n```json\n{"stance": "bullish", "confidence": 85, '
            '"horizon": "short", "key_findings": ["a", "b"], "caveats": ["c"]}\n```',
        },
    ]
    card = build_card("technical_analyst", messages)
    if card["tool_calls"] != 2:
        raise AssertionError(f"expected 2 tool calls recorded, got {card['tool_calls']}")
    if [e["as_of"] for e in card["evidence"]] != ["2026-09-19", "2026-09-19"]:
        raise AssertionError(f"as-of dates not extracted: {card['evidence']}")
    if card["confidence"] != 0.85:
        raise AssertionError(f"85 should be read as 0.85, got {card['confidence']}")
    if card["stance"] != "bullish" or card["degraded"]:
        raise AssertionError(f"judgement not parsed: {card}")
    if "```" in card["narrative"] or "stance" in card["narrative"]:
        raise AssertionError(f"judgement block left in the narrative: {card['narrative']!r}")

    broken = build_card("macro_analyst", [{"type": "ai", "content": "No block here at all."}])
    if not broken["degraded"] or broken["stance"] != "neutral":
        raise AssertionError(f"unparseable judgement should degrade, got {broken}")

    coerced = build_card(
        "quant_risk_analyst",
        [{"type": "ai", "content": '{"stance": "sideways", "confidence": "n/a", "horizon": "eon"}'}],
    )
    if (coerced["stance"], coerced["horizon"], coerced["confidence"]) != (
        "neutral",
        "medium",
        FALLBACK_CONFIDENCE,
    ):
        raise AssertionError(f"coercion wrong: {coerced}")

    def card_of(expert, stance, confidence):
        return {"expert": expert, "stance": stance, "confidence": confidence, "error": None}

    labels = {}

    # The calibration record of section 9.2: one bearish plus two neutral must not read bearish.
    labels["one bearish, two neutral"] = compute_consensus(
        [card_of("a", "bearish", 0.7), card_of("b", "neutral", 0.6), card_of("c", "neutral", 0.6)]
    )
    if labels["one bearish, two neutral"]["label"] == "bearish":
        raise AssertionError("a lone bearish view must not label the panel bearish")

    labels["split"] = compute_consensus(
        [card_of("a", "bullish", 0.8), card_of("b", "bearish", 0.8)]
    )
    if labels["split"]["label"] != "mixed" or not labels["split"]["conflicts"]:
        raise AssertionError(f"a split panel must read mixed with a conflict: {labels['split']}")

    labels["agreed"] = compute_consensus(
        [card_of("a", "bullish", 0.9), card_of("b", "bullish", 0.8), card_of("c", "bullish", 0.7)]
    )
    if labels["agreed"]["label"] != "bullish" or labels["agreed"]["agreement_pct"] != 100.0:
        raise AssertionError(f"a unanimous panel must read bullish at 100%: {labels['agreed']}")

    labels["all neutral"] = compute_consensus(
        [card_of("a", "neutral", 0.6), card_of("b", "neutral", 0.6)]
    )
    if labels["all neutral"]["label"] != "neutral":
        raise AssertionError(f"a uniformly neutral panel must read neutral: {labels['all neutral']}")

    # An expert with nothing to say must not drag the panel toward neutral.
    with_silent = compute_consensus(
        [card_of("a", "bullish", 0.9), {"expert": "b", "stance": "insufficient_data", "confidence": 0.0, "error": None}]
    )
    if with_silent["participating_experts"] != ["a"] or with_silent["excluded_experts"] != ["b"]:
        raise AssertionError(f"insufficient_data must be excluded, not averaged: {with_silent}")

    failed = failed_card("news_analyst", "TimeoutError: index unreachable")
    if failed["stance"] != "insufficient_data" or not failed["error"]:
        raise AssertionError(f"failed card wrong: {failed}")

    return {
        "detail": (
            "evidence rebuilt from tool messages; 85 read as 0.85; unparseable degraded; "
            "labels "
            + ", ".join(f"{k}={v['label']}" for k, v in labels.items())
            + "; insufficient_data excluded"
        )
    }
