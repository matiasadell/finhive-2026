# Databricks notebook source
"""The two nodes that can skip a run, and the interface a cache has to satisfy.

**The nodes are built; the backend is not.** `cache=None` makes both a pass-through, which is the
shipped configuration (`enable_cache=false`) until the three checks of `agent-design.md`
section 14.1 pass -- vector search on Aiven's plan, reachability from Databricks serverless, and
the client under FIPS-mode OpenSSL. Writing the nodes now means the graph's shape is final and
step 9 adds a backend rather than re-wiring the graph.

**Why a node and not a tool.** A tool finds evidence for one worker; this decides whether the
whole run can be skipped. Different job, different failure mode, different store -- and a node
that can end the run is routing, which lives in `graph/`.

**A hit must not cross instruments.** "What do you think of NVDA?" and "What do you think of AMD?"
are almost identical to an embedding, and answering one with the other is the classic failure of a
semantic cache. So a hit needs **both** similarity above the threshold **and** the same resolved
instruments. That check is the backend's to enforce; this file states it as a requirement.

**Both directions fail open.** Unreachable, slow or returning garbage means answer normally, never
an error. Same posture as the input guardrail.
"""

# COMMAND ----------

from __future__ import annotations

from langchain_core.messages import AIMessage
from langgraph.graph import END
from langgraph.types import Command

from llm.parsing import message_text
from setup import config

# COMMAND ----------

# What `graph/build.py` will hand these nodes once a backend exists (step 9):
#
#   cache.lookup(question: str) -> dict | None
#       A fresh entry, or None. The entry carries `answer`, `cards`, `consensus`, `symbols`,
#       `experts` and `cached_at`. Returning None for any reason -- miss, expired, entity
#       mismatch, outage -- is correct; raising is not.
#
#   cache.store(question: str, entry: dict) -> None
#       Never raises. The TTL is the backend's to compute from the plan's experts, using
#       `config.CACHE_TTL_SECONDS_BY_EXPERT`: an answer is only as fresh as its stalest input,
#       so the TTL is the *shortest* among the experts that ran.

CACHED_PREFIX = "Cached answer from {when}.\n\n"


def make_cache_lookup(cache, chat):
    """A fresh hit ends the run; anything else continues to the planner."""

    def cache_lookup(state: dict) -> Command:
        if cache is None:
            return Command(goto=config.NODE_PLANNER)

        messages = state.get("messages") or []
        question = ""
        for message in reversed(messages):
            role = str(
                (message.get("role") if isinstance(message, dict) else None)
                or getattr(message, "type", "")
            ).lower()
            if role in {"human", "user"}:
                question = message_text(message)
                break

        try:
            entry = cache.lookup(question)
        except Exception:
            # Fails open, deliberately and silently: a cache outage must not cost an answer.
            entry = None

        if not entry or not entry.get("answer"):
            return Command(goto=config.NODE_PLANNER, update={"cache_hit": False})

        # The "cached at" line is added in code, from the entry's own timestamp. A reader should
        # always be able to tell a replayed answer from a fresh one.
        answer = CACHED_PREFIX.format(when=entry.get("cached_at", "an earlier run")) + entry["answer"]
        return Command(
            goto=END,
            update={
                "cache_hit": True,
                "cards": entry.get("cards") or [],
                "consensus": entry.get("consensus") or {},
                "messages": [
                    AIMessage(content=answer, name="cache", id=config.FINAL_ANSWER_ID)
                ],
            },
        )

    return cache_lookup


def make_cache_store(cache, chat):
    """Store only a clean, grounded answer. Then end the run."""

    def cache_store(state: dict) -> Command:
        if cache is None:
            return Command(goto=END)

        cards = state.get("cards") or []
        plan = state.get("plan") or {}
        messages = state.get("messages") or []

        # A refusal is cheap to recompute, and a warned or degraded answer must never be replayed
        # to the next reader as if it were sound.
        clean = (
            not state.get("blocked")
            and not state.get("cache_hit")
            and state.get("grounded") is not False
            and bool(cards)
            and not any(card.get("error") or card.get("degraded") for card in cards)
            and int(state.get("synthesis_attempts") or 0) < config.MAX_SYNTHESIS_ATTEMPTS
        )
        if not clean or not messages:
            return Command(goto=END)

        try:
            cache.store(
                plan.get("question", ""),
                {
                    "answer": message_text(messages[-1]),
                    "cards": cards,
                    "consensus": state.get("consensus") or {},
                    "symbols": plan.get("symbols") or [],
                    "experts": plan.get("experts") or [],
                },
            )
        except Exception:
            pass  # Fails open in this direction too: a failed write is not a failed answer.

        return Command(goto=END)

    return cache_store


# COMMAND ----------


def check(ctx: dict) -> dict:
    """A miss continues, a hit ends the run, an outage is a miss.

    `ctx` keys used:
      enable_cache  bool

    With the cache off this returns "cache disabled" as a **pass**, not a skip: the cache is
    optional, and a pass is more robust because `graph/build.py` then needs no special handling
    (`agent-design.md` section 19.5).
    """
    if not ctx.get("enable_cache"):
        return {"detail": "cache disabled (enable_cache=false); nodes are pass-through"}

    class _Hit:
        def lookup(self, question):
            return {"answer": "a previous answer", "cached_at": "2026-09-22T10:00:00Z", "cards": []}

        def store(self, question, entry):
            raise AssertionError("store must not be called on a hit path")

    class _Outage:
        def lookup(self, question):
            raise RuntimeError("valkey unreachable")

        def store(self, question, entry):
            raise RuntimeError("valkey unreachable")

    question = {"messages": [{"role": "user", "content": "What do you think of NVDA?"}]}

    miss = make_cache_lookup(None, None)(question)
    if miss.goto != config.NODE_PLANNER:
        raise AssertionError(f"a miss must continue to the planner; got {miss.goto}")

    hit = make_cache_lookup(_Hit(), None)(question)
    if hit.goto != END or not hit.update.get("cache_hit"):
        raise AssertionError(f"a fresh hit must end the run; got {hit.goto}")
    if not message_text(hit.update["messages"][0]).startswith("Cached answer from"):
        raise AssertionError("a replayed answer must say so, in code, from its own timestamp")

    outage = make_cache_lookup(_Outage(), None)(question)
    if outage.goto != config.NODE_PLANNER:
        raise AssertionError("an outage must read as a miss, never as an error")

    store_outage = make_cache_store(_Outage(), None)(
        {
            "cards": [{"expert": "technical_analyst", "error": None, "degraded": False}],
            "plan": {"question": "q"},
            "messages": [AIMessage(content="an answer")],
            "grounded": True,
            "synthesis_attempts": 1,
        }
    )
    if store_outage.goto != END:
        raise AssertionError("a failed write must not fail the run")

    return {"detail": "miss continues, hit ends the run, outage reads as a miss, write fails open"}
