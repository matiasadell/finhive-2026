# Databricks notebook source
"""Reading a model's answer, and getting a typed one out of it.

Three functions, each solving a problem the gateway creates rather than one the domain has:

`message_text`  -- `finhive_router` routes per request, so the *shape* of a reply changes between
                   calls: a plain string from Llama 3.3 70B, a list of content blocks with
                   reasoning parts from GPT-OSS 120B. Call this on every model output in the
                   codebase; there is no case where reading the raw content is safe.

`ask_structured` -- the schema is sent as `response_format` so the server constrains the output,
                   and Pydantic validates regardless. One correction carrying Pydantic's own
                   error, then the caller's default. Never raises.

`check_schema_supported` -- Databricks structured outputs accept a subset of JSON Schema, and
                   Pydantic emits exactly the unsupported constructs by default. This catches a
                   schema that drifts out of that subset in a verification stage rather than on
                   the first question.

Layering: standard library and pydantic only (`agent-design.md` section 3.3).
"""

# COMMAND ----------

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

# COMMAND ----------

# Reasoning models put their thinking in separate content blocks. It is never part of the answer,
# and feeding it back into a parser is how a fenced JSON hunt starts finding the wrong object.
REASONING_BLOCK_TYPES = frozenset(
    {"reasoning", "thinking", "reasoning_content", "redacted_reasoning", "redacted_thinking"}
)

# Databricks structured outputs reject these outright. Pydantic produces every one of them from
# ordinary annotations: `Optional[X]` and `X | None` become `anyOf`, a nested model becomes
# `$ref` plus `$defs`, `dict[str, str]` becomes a mapping-valued `additionalProperties`.
UNSUPPORTED_SCHEMA_KEYS = ("pattern", "anyOf", "oneOf", "allOf", "prefixItems", "$ref", "$defs")

# 64 for structured outputs, 16 when function calling is used instead. The lower bound is the
# useful one to hold to: a node that has to fall back to forced function calling should not need
# a different schema to do it.
MAX_SCHEMA_PROPERTIES_STRUCTURED = 64
MAX_SCHEMA_PROPERTIES_FUNCTION_CALLING = 16


class UnsupportedSchemaError(ValueError):
    """A Pydantic model that the gateway would reject, or degrade badly on."""


# COMMAND ----------


def _block_text(block: Any) -> str:
    """The text of one content block, or empty for a reasoning block."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    if block.get("type") in REASONING_BLOCK_TYPES:
        return ""
    for key in ("text", "content", "value"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    return ""


def message_text(content: Any) -> str:
    """Normalize any model output to a string. Never returns None, never raises.

    Accepts a message object (anything with `.content`), a `{"role", "content"}` dict, a plain
    string, a list of content blocks, or None.
    """
    try:
        if content is None:
            return ""
        if isinstance(content, str):
            return content

        # A message object -- LangChain's AIMessage and anything else shaped like it.
        inner = getattr(content, "content", None)
        if inner is not None and not isinstance(content, (dict, list)):
            return message_text(inner)

        if isinstance(content, dict):
            if "content" in content:
                return message_text(content["content"])
            return _block_text(content)

        if isinstance(content, (list, tuple)):
            parts = [_block_text(block) for block in content]
            return "".join(part for part in parts if part)

        # Every shape the gateway actually produces is handled above. A scalar is still worth
        # rendering; anything else would put a repr with a memory address into a prompt.
        if isinstance(content, (int, float, bool)):
            return str(content)
        return ""
    except Exception:
        # The contract is that a caller can read this without a try/except of its own. An output
        # we cannot parse is an empty answer, which every caller already has a path for.
        return ""


# COMMAND ----------


def _count_properties(schema: Any) -> int:
    total = 0
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            total += len(properties)
        for value in schema.values():
            total += _count_properties(value)
    elif isinstance(schema, list):
        for value in schema:
            total += _count_properties(value)
    return total


def _find_unsupported(schema: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            where = f"{path}.{key}"
            if key in UNSUPPORTED_SCHEMA_KEYS:
                found.append(where)
            # `"additionalProperties": false` is what strict mode requires. A mapping there is a
            # `dict[...]` annotation, which is the construct that gets rejected.
            elif key == "additionalProperties" and isinstance(value, dict):
                found.append(where)
            else:
                found.extend(_find_unsupported(value, where))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found.extend(_find_unsupported(value, f"{path}[{index}]"))
    return found


def check_schema_supported(schema: type[BaseModel], *, function_calling: bool = False) -> dict:
    """Raise unless the gateway can constrain output to this schema.

    The rules every schema in this codebase follows: flat -- no nested model, no `dict`, no
    `Optional` or `Union`; every field required, with absence encoded as a value (an empty string,
    an empty list) because strict mode requires all keys; and the meaning carried in the field
    descriptions, because the model reads them as part of the schema.
    """
    json_schema = schema.model_json_schema()

    unsupported = _find_unsupported(json_schema)
    if unsupported:
        raise UnsupportedSchemaError(
            f"{schema.__name__} uses JSON Schema the gateway rejects at {unsupported}. "
            "Flatten it: Literal/bool/str/float/list[str] only, every field required, "
            "absence encoded as an empty value."
        )

    limit = (
        MAX_SCHEMA_PROPERTIES_FUNCTION_CALLING
        if function_calling
        else MAX_SCHEMA_PROPERTIES_STRUCTURED
    )
    count = _count_properties(json_schema)
    if count > limit:
        raise UnsupportedSchemaError(
            f"{schema.__name__} has {count} properties, over the {limit}-key limit"
        )

    undescribed = sorted(
        name
        for name, field in (json_schema.get("properties") or {}).items()
        if not (isinstance(field, dict) and field.get("description"))
    )
    return {
        "schema": schema.__name__,
        "properties": count,
        "limit": limit,
        "fields_without_description": undescribed,
    }


# COMMAND ----------


def _compact(error: ValidationError) -> str:
    """Pydantic's complaint, short enough to hand back to a model."""
    parts = [
        f"{'.'.join(str(p) for p in item.get('loc', ())) or '<root>'}: {item.get('msg', '')}"
        for item in error.errors()[:5]
    ]
    return "; ".join(parts)


def ask_structured(
    chat,
    role: str,
    system_prompt: str,
    user_content: str,
    schema: type[BaseModel],
    default: BaseModel,
):
    """One model call that returns an instance of `schema`, or `default`. Never raises.

    `chat` is the factory (`llm.gateway.get_chat_model`), not a model: the role selects the
    model, its temperature and its token cap, and tags the trace.

    `default` is a risk decision, and it is the most important line in each caller. The input
    guardrail defaults to `allow` -- an unavailable classifier must not break the product. The
    output guardrail defaults to `grounded` -- the answer still ships, with its disclaimer. The
    planner defaults to no mentions and empty sub-questions, which code turns into the full panel.
    """
    model = chat(role)
    bound = model.bind(
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
                "strict": True,
            },
        }
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    text = ""
    for _ in range(2):  # first attempt, then one correction
        try:
            text = message_text(bound.invoke(messages))
            return schema.model_validate_json(text)
        except ValidationError as err:
            # The retry carries Pydantic's error, which names the exact field that was wrong. A
            # generic "answer again" reproduces the same mistake.
            messages += [
                {"role": "assistant", "content": text[:500]},
                {
                    "role": "user",
                    "content": (
                        f"That did not validate: {_compact(err)}. "
                        "Answer again with an object that satisfies the schema."
                    ),
                },
            ]
        except Exception:
            # Network, timeout, refusal. A second call would fail the same way.
            break

    return default


# COMMAND ----------


class _ProbeVerdict(BaseModel):
    """The shape every schema in this codebase has: flat, required, described."""

    ok: bool
    reason: str


def check(ctx: dict) -> dict:
    """The four content shapes, the schema scanner, and one structured round trip per chat role.

    `ctx` keys used:
      chat  callable(role) -> chat model, from `llm.gateway.get_chat_model`

    Which of the two models behind `finhive_router` honoured `response_format` is not answerable
    here -- the router picks per request. `pipeline/probe_model_capabilities.py` measures that,
    once, by addressing each model directly.
    """
    shapes = {
        "string": ("plain answer", "plain answer"),
        "none": (None, ""),
        "dict": ({"role": "assistant", "content": "from a dict"}, "from a dict"),
        "blocks": (
            [
                {"type": "reasoning", "text": "thinking out loud"},
                {"type": "text", "text": "the answer"},
            ],
            "the answer",
        ),
    }
    for name, (value, expected) in shapes.items():
        got = message_text(value)
        if got != expected:
            raise AssertionError(f"message_text({name}) returned {got!r}, expected {expected!r}")

    supported = check_schema_supported(_ProbeVerdict)

    chat = ctx.get("chat")
    if chat is None:
        return {"detail": f"shapes ok; {supported['properties']} properties; no chat in ctx"}

    roles = ["router", "guard_in"]
    rounds = []
    for role in roles:
        verdict = ask_structured(
            chat,
            role,
            "You answer only with the requested object.",
            "Set ok to true and reason to the single word 'probe'.",
            _ProbeVerdict,
            default=_ProbeVerdict(ok=False, reason="default returned"),
        )
        rounds.append(f"{role}={'ok' if verdict.ok else 'DEFAULT'}")

    defaulted = [r for r in rounds if r.endswith("DEFAULT")]
    if defaulted:
        raise AssertionError(
            f"structured output fell through to the default for {defaulted} -- the endpoint may "
            "not honour response_format; run pipeline/probe_model_capabilities"
        )

    return {"detail": f"four shapes ok; schema scan ok; structured round trips: {', '.join(rounds)}"}
