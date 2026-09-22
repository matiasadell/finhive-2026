# Databricks notebook source
"""The only code in the repository that constructs a model client.

Its surface is `get_chat_model(role, temperature, max_tokens)` and `get_embedding_model()`. Every
other folder receives that factory as an argument and never learns what a gateway is.

Everything goes through the same AI Gateway base URL and the same client; only `model=` differs,
and the role decides which. Three details here were each paid for by a failure
(`agent-design.md` section 21):

- **A 300-token floor on every chat call.** Reasoning models spend the output budget thinking, and
  roughly 40% of calls came back empty below this.
- **The cap travels in `extra_body`.** `langchain-openai` rewrites `max_tokens` into
  `max_completion_tokens`, which the gateway rejects with `400 unknown field`.
- **Credentials are cached, not re-resolved per call**, and refreshed on a TTL because a notebook
  context token is short-lived.

There is no fallback path if a model is down. The failure is visible, and each guardrail has a
defined posture for it (sections 11.1, 11.2).
"""

# COMMAND ----------

from __future__ import annotations

import os
import threading
import time

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from llm.parsing import message_text
from setup import config

# COMMAND ----------

# Reasoning models spend the output budget thinking before they answer. Below this floor about
# 40% of calls returned empty content (section 21).
MIN_OUTPUT_TOKENS = 300

# A notebook context token expires; an OAuth M2M token lives about an hour. Re-resolving every
# call is wasteful and re-resolving never is a 401 forty minutes in.
CREDENTIAL_TTL_SECONDS = 1800

_LOCK = threading.Lock()
_CREDENTIALS: dict[str, object] = {"host": "", "token": "", "expires_at": 0.0, "generation": 0}
_CLIENTS: dict[tuple, ChatOpenAI] = {}


class UnknownModelRoleError(ValueError):
    """A role that is not in `setup.config.ROLES`."""


class CredentialError(RuntimeError):
    """No workspace host or token could be resolved."""


# COMMAND ----------


def set_credentials(host: str, token: str) -> None:
    """Inject a host and token explicitly, overriding the resolution chain below.

    A stage notebook uses this when the automatic chain cannot see the context -- it is the one
    place `dbutils` can supply what this module is forbidden from reading itself (ADR 0001).
    """
    with _LOCK:
        _CREDENTIALS.update(
            host=host.rstrip("/"),
            token=token,
            expires_at=time.time() + CREDENTIAL_TTL_SECONDS,
            generation=int(_CREDENTIALS["generation"]) + 1,
        )
        _CLIENTS.clear()


def _resolve_credentials() -> tuple[str, str]:
    """Host and token, from the environment or the ambient Databricks identity.

    v1's resolution chain, intact: an explicit environment pair first, then whatever the SDK can
    authenticate as -- the notebook's identity in a job, the served model's inside the serving
    container, an OAuth M2M service principal anywhere else. No personal access token is ever
    stored (`ARCHITECTURE_V2.md` section 5.3).
    """
    host = os.getenv("DATABRICKS_HOST", "").rstrip("/")
    token = os.getenv("DATABRICKS_TOKEN", "")
    if host and token:
        return host, token

    try:
        # Imported here, not at module scope: this file has to import cleanly wherever the SDK is
        # absent, and the environment pair above is the path that needs no SDK at all.
        from databricks.sdk import WorkspaceClient

        cfg = WorkspaceClient().config
        resolved_host = (cfg.host or host).rstrip("/")
        header = (cfg.authenticate() or {}).get("Authorization", "")
        resolved_token = header.removeprefix("Bearer ").strip() or token
    except Exception as exc:
        raise CredentialError(
            "could not resolve Databricks credentials: set DATABRICKS_HOST and DATABRICKS_TOKEN, "
            "or call llm.gateway.set_credentials(host, token) from the stage notebook"
        ) from exc

    if not resolved_host or not resolved_token:
        raise CredentialError(
            f"incomplete Databricks credentials (host={'set' if resolved_host else 'missing'}, "
            f"token={'set' if resolved_token else 'missing'})"
        )
    return resolved_host, resolved_token


def _credentials() -> tuple[str, str, int]:
    """The cached host, token and cache generation, refreshed on expiry."""
    with _LOCK:
        if time.time() < float(_CREDENTIALS["expires_at"]) and _CREDENTIALS["token"]:
            return str(_CREDENTIALS["host"]), str(_CREDENTIALS["token"]), int(_CREDENTIALS["generation"])

    host, token = _resolve_credentials()

    with _LOCK:
        _CREDENTIALS.update(
            host=host,
            token=token,
            expires_at=time.time() + CREDENTIAL_TTL_SECONDS,
            generation=int(_CREDENTIALS["generation"]) + 1,
        )
        _CLIENTS.clear()
        return host, token, int(_CREDENTIALS["generation"])


def _base_url(host: str) -> str:
    return config.AI_GATEWAY_BASE_URL.rstrip("/") or f"{host}/serving-endpoints"


# COMMAND ----------


def model_for_role(role: str) -> str:
    """The model name a role resolves to. The only mapping from role to name in the codebase."""
    if role not in config.KNOWN_MODEL_ROLES:
        raise UnknownModelRoleError(
            f"unknown role {role!r}; known roles are {sorted(config.KNOWN_MODEL_ROLES)}"
        )
    return str(config.ROLES[role]["model"])


def get_chat_model(
    role: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """A chat client for `role`, with its temperature and token cap applied.

    Passing `temperature` or `max_tokens` overrides the role's defaults; the 300-token floor is
    applied to the result either way, because it guards the model's behaviour and not the
    caller's intent.
    """
    if role == config.EMBEDDING:
        raise UnknownModelRoleError("the embedding role has no chat model; use get_embedding_model")

    model = model_for_role(role)
    spec = config.ROLES[role]
    resolved_temperature = float(spec["temperature"] if temperature is None else temperature)
    requested = int(spec["max_tokens"] if max_tokens is None else max_tokens)
    cap = max(requested, MIN_OUTPUT_TOKENS)

    return chat_model_for_name(model, resolved_temperature, cap, role=role)


def chat_model_for_name(
    model: str,
    temperature: float = 0.0,
    max_tokens: int = MIN_OUTPUT_TOKENS,
    *,
    role: str = "direct",
) -> ChatOpenAI:
    """A chat client for a model addressed by name rather than by role.

    The one caller is `pipeline/probe_model_capabilities.py`, which has to measure each model
    behind `finhive_router` separately -- the router picks per request, so a probe through a role
    cannot attribute a failure. It lives here because this stays the only file that constructs a
    client; the probe reads its names from `setup/config.py` like everything else.
    """
    cap = max(int(max_tokens), MIN_OUTPUT_TOKENS)
    host, token, generation = _credentials()
    key = (role, model, float(temperature), cap, generation)

    with _LOCK:
        cached = _CLIENTS.get(key)
    if cached is not None:
        return cached

    client = ChatOpenAI(
        model=model,
        temperature=float(temperature),
        base_url=_base_url(host),
        api_key=token,
        timeout=config.MODEL_TIMEOUT_SECONDS,
        max_retries=config.MODEL_MAX_RETRIES,
        # NOT `max_tokens=`: langchain-openai rewrites that into `max_completion_tokens`, which
        # the gateway rejects with `400 unknown field` (section 21).
        extra_body={"max_tokens": cap},
        # What lets a trace answer "which part of the graph spent these tokens".
        tags=[f"finhive_role:{role}"],
        metadata={"finhive_role": role, "finhive_model": model},
    )

    with _LOCK:
        _CLIENTS[key] = client
    return client


def get_embedding_model() -> OpenAIEmbeddings:
    """The embeddings client. Its only consumer today is the semantic cache's query vectors.

    The endpoint the news index embeds *with* is chosen where the index is built, in
    `data_modeling`. Agent code never names it.
    """
    host, token, _ = _credentials()
    return OpenAIEmbeddings(
        model=model_for_role(config.EMBEDDING),
        base_url=_base_url(host),
        api_key=token,
        timeout=config.MODEL_TIMEOUT_SECONDS,
        max_retries=config.MODEL_MAX_RETRIES,
    )


# COMMAND ----------


def check(ctx: dict) -> dict:
    """Every role answers one prompt, the embedding role returns a vector, the floor is applied.

    Six model calls: one per chat role, plus one embedding. `ctx` is unused -- this check needs
    only what the module itself resolves.
    """
    del ctx

    floor_client = get_chat_model(config.GUARD_IN, max_tokens=10)
    applied = floor_client.extra_body.get("max_tokens")
    if applied != MIN_OUTPUT_TOKENS:
        raise AssertionError(
            f"token floor not applied: asked for 10, client carries {applied!r}, "
            f"expected {MIN_OUTPUT_TOKENS}"
        )
    if "max_tokens" in (floor_client.model_kwargs or {}):
        raise AssertionError("max_tokens leaked into model_kwargs; it must travel in extra_body")

    answered: dict[str, int] = {}
    for role in config.CHAT_ROLES:
        text = message_text(
            get_chat_model(role).invoke(
                [{"role": "user", "content": "Reply with the single word: ready"}]
            )
        )
        if not text.strip():
            raise AssertionError(
                f"role {role!r} ({model_for_role(role)}) returned empty content -- the usual "
                "cause is a token cap consumed by reasoning before any answer was emitted"
            )
        answered[role] = len(text)

    vector = get_embedding_model().embed_query("probe")
    if not isinstance(vector, list) or not vector or not isinstance(vector[0], float):
        raise AssertionError(f"embedding role returned {type(vector).__name__}, expected a vector")

    host, _, _ = _credentials()
    return {
        "detail": (
            f"{len(answered)} chat roles answered {answered} | embedding dim {len(vector)} "
            f"| floor {MIN_OUTPUT_TOKENS} applied | base_url {_base_url(host)}"
        ),
        "embedding_dim": len(vector),
    }
