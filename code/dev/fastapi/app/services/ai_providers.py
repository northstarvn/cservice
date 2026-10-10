"""Several AI services behind one external brain, with quota failover.

Why this module exists
----------------------
``brain_router`` can route a question to an "external AI", but it routes to
exactly *one*: the single callable a deployment installs with
``chat.set_external_brain``. One callable is enough to prove the seam and not
enough to run on, because an AI service has a quota and a quota running out is
not an exception -- it is the normal end of a month. A deployment with one
provider is a deployment whose external brain stops answering on the day that
provider says no.

This module keeps a *list* of providers, each with an ordered list of models and
a credential, and answers from the first one that is not known to be exhausted.
When a provider refuses -- a hard quota, a rate limit, a bad credential, a 5xx,
a timeout -- the refusal is classified, that ``(provider, model)`` is cooled
down for a period that matches the kind of refusal, and the next candidate is
tried *inside the same request budget*. A quota ending becomes a detour, not an
outage.

Credentials, and the two kinds
------------------------------
Two ways to hold a provider's authority, and both are real:

* an **API key**, one secret the provider minted for this deployment;
* a **browser session**, the token a person's logged-in browser presents.

They are modelled the same way -- an auth mode and a secret -- because the
pool's job (failover) does not depend on which it is; the difference lives in
how the request is built, not in how the pool decides. A credential is read from
the environment, which is how a deployment is configured, and is *overridden* by
a row an operator writes through the admin surface, which is how a session is
rotated without a redeploy. The database copy wins, because the point of
rotating is not to have to change what the process started with.

Purity, and where the I/O is
----------------------------
``brain_router.decide()`` performs no I/O and a test asserts it. This module
keeps that property one level out: the *classification* of a refusal, the
*ordering* of the candidates, and the *cooldown* a refusal earns are pure
functions over an in-memory table, and every network call goes through a single
injectable transport. A test that wants to know what happens when the first
provider returns ``429 insufficient_quota`` injects a transport that says so; it
never has to reach the internet, and it never has to sleep.

Honesty about browser sessions
------------------------------
An API key is a stable contract. A browser session token is not: it is a
credential against a consumer web application that changes without notice and
whose terms of use are the operator's business, not this module's. The browser
providers below are therefore marked ``browser_best_effort`` and the request
builder for each is documented as the shape *at the time of writing*. The
transport is the seam a deployment overrides when the web flow moves -- which is
the same seam the API-key providers use, so the failover machinery does not care
which kind of credential is behind it.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.services.storage_providers import decrypt_token, encrypt_token

#: Bumped when the provider table or the failure vocabulary changes shape. The
#: admin surfaces report it so a client can tell which contract it is reading.
AI_PROVIDERS_VERSION = "ai_providers_v1"

#: The two ways to hold a provider's authority. Never a free-form string: the
#: database CHECK constraint and the credential resolver both read this pair.
AUTH_API_KEY = "api_key"
AUTH_BROWSER_SESSION = "browser_session"
AUTH_MODES: tuple[str, ...] = (AUTH_API_KEY, AUTH_BROWSER_SESSION)

# ---------------------------------------------------------------------------
# Failure vocabulary
# ---------------------------------------------------------------------------
# A refusal is not one thing. "Come back in a minute" and "your card was
# declined" and "this key was revoked" call for three different responses, and
# collapsing them into one boolean is how a rate limit turns into a provider
# that is disabled until somebody notices.
OK = "ok"
QUOTA_EXHAUSTED = "quota_exhausted"
RATE_LIMITED = "rate_limited"
AUTH_FAILED = "auth_failed"
SERVER_ERROR = "server_error"
TIMEOUT = "timeout"
NETWORK_ERROR = "network_error"
BAD_REQUEST = "bad_request"
UNKNOWN_ERROR = "unknown_error"

FAILURE_KINDS: tuple[str, ...] = (
    QUOTA_EXHAUSTED,
    RATE_LIMITED,
    AUTH_FAILED,
    SERVER_ERROR,
    TIMEOUT,
    NETWORK_ERROR,
    BAD_REQUEST,
    UNKNOWN_ERROR,
)

#: Seconds a refusal keeps a ``(provider, model)`` out of the running list.
#:
#: These are cold-start numbers, overridable per deployment. The shape matters
#: more than the values: a hard quota is out for a long time because paying the
#: provider is a human action, a rate limit is out for about as long as the
#: provider asked, and a timeout is out briefly because it is usually the
#: network and usually passes.
DEFAULT_COOLDOWNS: dict[str, int] = {
    QUOTA_EXHAUSTED: 3600,
    RATE_LIMITED: 60,
    SERVER_ERROR: 45,
    TIMEOUT: 20,
    NETWORK_ERROR: 15,
    BAD_REQUEST: 300,
    UNKNOWN_ERROR: 60,
}

#: A rejected credential is not a wait, it is a fact: the key is wrong or the
#: session expired. It stays out until an operator rotates it. The number is
#: finite only so that a credential repaired by other means is eventually
#: retried without an explicit reset.
AUTH_COOLDOWN_SECONDS = 6 * 3600

#: Body fragments that turn a generic rate-limit response into a hard quota.
#: Providers disagree on the status code for "you are out of money" -- some use
#: 429, some 402 -- so the body is what disambiguates a monthly ceiling from a
#: per-minute one. Both are strings a provider has no incentive to hide.
QUOTA_BODY_TOKENS: tuple[str, ...] = (
    "insufficient_quota",
    "resource_exhausted",
    "exceeded your current quota",
    "out of credits",
    "credit balance",
    "billing",
    "quota",
)

#: The system instruction sent ahead of every question. Deliberately generic and
#: deliberately *not* the customer's own words: this module answers general
#: questions, and anything customer-specific is withheld upstream by
#: ``brain_router.redact_for_egress`` before it ever reaches here.
SYSTEM_PROMPT = os.getenv(
    "CSERVICE_AI_SYSTEM_PROMPT",
    "You are the general-knowledge assistant for a customer-service backend. "
    "Answer briefly, plainly, and only from general knowledge.",
)

# ---------------------------------------------------------------------------
# The provider table
# ---------------------------------------------------------------------------
# Data, not code. Each row is one service this deployment might answer from, the
# models it will use in priority order, and the environment variables that hold
# its credential. Nothing here is a secret; a provider with no credential
# resolves to "configured: false" and is skipped, which is the honest default.


@dataclass(frozen=True)
class AiModel:
    """One model on one provider, with the note explaining why it is listed."""

    model: str
    label: str = ""
    note: str = ""


@dataclass(frozen=True)
class AiProvider:
    """One AI service, its request family, and how to authenticate to it."""

    provider: str
    label: str
    kind: str
    endpoint: str
    models: tuple[AiModel, ...]
    auth_modes: tuple[str, ...] = (AUTH_API_KEY,)
    api_key_envs: tuple[str, ...] = ()
    session_envs: tuple[str, ...] = ()
    browser_best_effort: bool = False
    docs: str = ""


def _models(*names: str) -> tuple[AiModel, ...]:
    return tuple(AiModel(model=name) for name in names)


#: Order is provider priority: the first provider with an available credential
#: and an un-cooled model answers. Within a provider, the model order is the
#: model priority. A deployment can reorder the providers without editing this
#: table by setting ``CSERVICE_AI_PROVIDER_ORDER``.
AI_PROVIDERS: tuple[AiProvider, ...] = (
    AiProvider(
        provider="openai",
        label="OpenAI",
        kind="openai",
        endpoint="https://api.openai.com/v1/chat/completions",
        models=_models("gpt-4o-mini", "gpt-4o"),
        api_key_envs=("AI_OPENAI_API_KEY", "OPENAI_API_KEY"),
        docs="Chat Completions. The mini model is first because it is the cheap "
        "one and the failover order should spend the cheap capacity first.",
    ),
    AiProvider(
        provider="anthropic",
        label="Anthropic",
        kind="anthropic",
        endpoint="https://api.anthropic.com/v1/messages",
        models=_models("claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"),
        api_key_envs=("AI_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        docs="Messages API. Haiku before Sonnet for the same reason as above.",
    ),
    AiProvider(
        provider="openrouter",
        label="OpenRouter",
        kind="openai",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        models=_models(
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
            "google/gemini-flash-1.5",
        ),
        api_key_envs=("AI_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
        docs="One key, several upstreams. OpenAI-compatible, so it reuses the "
        "openai request family. Useful as a final fallback because one quota "
        "covers many models.",
    ),
    AiProvider(
        provider="google_gemini",
        label="Google Gemini",
        kind="gemini",
        endpoint="https://generativelanguage.googleapis.com/v1beta/models",
        models=_models("gemini-1.5-flash", "gemini-1.5-pro"),
        api_key_envs=("AI_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        docs="generateContent. The key travels as a query parameter, which is "
        "the provider's documented shape and the reason the built URL is never "
        "logged.",
    ),
    AiProvider(
        provider="deepseek",
        label="DeepSeek",
        kind="openai",
        endpoint="https://api.deepseek.com/chat/completions",
        models=_models("deepseek-chat"),
        api_key_envs=("AI_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
        docs="OpenAI-compatible.",
    ),
    # --- Browser sessions ---------------------------------------------------
    AiProvider(
        provider="chatgpt_web",
        label="ChatGPT (browser session)",
        kind="chatgpt_web",
        endpoint="https://chatgpt.com/backend-api/conversation",
        models=_models("gpt-4o", "gpt-4o-mini"),
        auth_modes=(AUTH_BROWSER_SESSION,),
        session_envs=("AI_CHATGPT_WEB_SESSION", "AI_CHATGPT_WEB_ACCESS_TOKEN"),
        browser_best_effort=True,
        docs="The token the ChatGPT web app holds. The response is a server-sent "
        "event stream; the last message event carries the answer. Rotate it from "
        "the admin surface when the browser session refreshes.",
    ),
    AiProvider(
        provider="claude_web",
        label="Claude (browser session)",
        kind="claude_web",
        endpoint="https://claude.ai/api",
        models=_models("claude-3-5-sonnet"),
        auth_modes=(AUTH_BROWSER_SESSION,),
        session_envs=("AI_CLAUDE_WEB_SESSION", "AI_CLAUDE_WEB_SESSION_KEY"),
        browser_best_effort=True,
        docs="The sessionKey cookie. The web completion endpoint is scoped to an "
        "organization and a conversation, so this provider reads "
        "AI_CLAUDE_WEB_ORGANIZATION_ID and AI_CLAUDE_WEB_CONVERSATION_ID (or the "
        "same keys in the egress context) and is best-effort otherwise.",
    ),
    AiProvider(
        provider="gemini_web",
        label="Gemini (browser session)",
        kind="gemini_web",
        endpoint="https://gemini.google.com/_/BardChatUi/data/batchexecute",
        models=_models("gemini-1.5-pro"),
        auth_modes=(AUTH_BROWSER_SESSION,),
        session_envs=("AI_GEMINI_WEB_SESSION",),
        browser_best_effort=True,
        docs="The browser session cookie. Best-effort, like the other two.",
    ),
)

PROVIDER_BY_NAME: dict[str, AiProvider] = {p.provider: p for p in AI_PROVIDERS}


def provider_ids() -> tuple[str, ...]:
    return tuple(p.provider for p in AI_PROVIDERS)


def provider_order() -> tuple[str, ...]:
    """The configured provider priority, defaulting to the table order.

    An operator can name a subset and a different order; unknown names are
    dropped rather than raising, because a stale environment variable must not
    make the whole external brain unavailable.
    """
    raw = os.getenv("CSERVICE_AI_PROVIDER_ORDER", "").strip()
    if not raw:
        return provider_ids()
    wanted = [part.strip() for part in raw.split(",") if part.strip()]
    ordered = [name for name in wanted if name in PROVIDER_BY_NAME]
    for name in provider_ids():
        if name not in ordered:
            ordered.append(name)
    return tuple(ordered)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def env_credential(provider: AiProvider) -> Optional[tuple[str, str]]:
    """The credential the environment supplies, as ``(auth_mode, secret)``.

    API keys are checked before session tokens: a deployment that has configured
    both for one provider has expressed a preference for the one that does not
    expire when a browser tab closes.
    """
    if AUTH_API_KEY in provider.auth_modes:
        for name in provider.api_key_envs:
            value = (os.getenv(name) or "").strip()
            if value:
                return AUTH_API_KEY, value
    if AUTH_BROWSER_SESSION in provider.auth_modes:
        for name in provider.session_envs:
            value = (os.getenv(name) or "").strip()
            if value:
                return AUTH_BROWSER_SESSION, value
    return None


def seal_secret(secret: str) -> str:
    """Encrypt a credential for storage. ``""`` stays ``""``."""
    return encrypt_token(secret)


def unseal_secret(stored: str) -> str:
    """Decrypt a stored credential. Unreadable ciphertext reads as absent."""
    return decrypt_token(stored)


# ---------------------------------------------------------------------------
# Classification: turning a refusal into a decision
# ---------------------------------------------------------------------------

def parse_retry_after(headers: Optional[dict[str, Any]]) -> Optional[int]:
    """The provider's own ``Retry-After``, in seconds, when it gave one.

    Only the delta-seconds form is honoured; the HTTP-date form is rare for
    quota responses and parsing it wrongly is worse than ignoring it, so an
    unparseable value falls back to the kind's default cooldown.
    """
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            text = str(value).strip()
            if text.isdigit():
                return max(0, int(text))
    return None


def classify_failure(
    status_code: int,
    body_text: str = "",
    headers: Optional[dict[str, Any]] = None,
) -> str:
    """Which kind of refusal this response is. Pure; no I/O, easy to test.

    A 2xx is :data:`OK`. Everything else is mapped to the vocabulary above, and
    the 429 / 529 family is split by the body: a per-minute rate limit and an
    exhausted monthly quota arrive with the same status from several providers,
    and treating the second as the first is what makes a deployment retry a
    provider it cannot pay for, once a minute, forever.
    """
    status = int(status_code)
    if 200 <= status < 300:
        return OK
    low = str(body_text or "").lower()
    if status in (401, 403):
        return AUTH_FAILED
    if status == 402:
        return QUOTA_EXHAUSTED
    if status == 408:
        return TIMEOUT
    if status in (429, 529):
        if any(token in low for token in QUOTA_BODY_TOKENS):
            return QUOTA_EXHAUSTED
        return RATE_LIMITED
    if status in (400, 404, 405, 409, 422):
        return BAD_REQUEST
    if 500 <= status < 600:
        return SERVER_ERROR
    return UNKNOWN_ERROR


def cooldown_seconds(kind: str, retry_after: Optional[int] = None) -> int:
    """How long a refusal keeps the candidate out. Pure."""
    if kind == AUTH_FAILED:
        return AUTH_COOLDOWN_SECONDS
    if kind == RATE_LIMITED and retry_after is not None:
        # The provider named a duration; honour it, but never above an hour --
        # a provider asking for a day is not really rate-limiting.
        return max(1, min(int(retry_after), 3600))
    return int(DEFAULT_COOLDOWNS.get(kind, DEFAULT_COOLDOWNS[UNKNOWN_ERROR]))


def plan_attempts(
    *,
    order: tuple[str, ...],
    model_lists: dict[str, tuple[str, ...]],
    has_credential: Callable[[str], bool],
    cooldown_until: Callable[[str, str], Optional[datetime]],
    now: datetime,
) -> list[tuple[str, str]]:
    """The ordered ``(provider, model)`` pairs to try, skipping cooled ones.

    Pure, and the whole failover policy is visible in its twenty lines: walk the
    providers in priority order, keep the ones that have a credential, and
    within each take the models that are not currently cooled down.
    """
    plan: list[tuple[str, str]] = []
    for provider in order:
        if provider not in model_lists:
            continue
        if not has_credential(provider):
            continue
        for model in model_lists[provider]:
            until = cooldown_until(provider, model)
            if until is not None and until > now:
                continue
            plan.append((provider, model))
    return plan


# ---------------------------------------------------------------------------
# Request building and response reading
# ---------------------------------------------------------------------------

@dataclass
class AiRequest:
    """One outbound call, fully resolved. The unit the transport receives."""

    provider: str
    model: str
    kind: str
    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    timeout: float


@dataclass
class AiResponse:
    """What the transport returns. ``body`` is parsed JSON when it was JSON."""

    status: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


def _messages(question: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": str(question or "")},
    ]


def build_request(
    provider: AiProvider,
    model: str,
    question: str,
    auth_mode: str,
    secret: str,
    *,
    timeout: float,
    context: Optional[dict[str, Any]] = None,
) -> AiRequest:
    """The concrete HTTP call for one attempt. Pure: no I/O, no clock, no random.

    ``context`` is the *redacted* egress payload. Only the browser providers read
    it, and only for the routing ids their web endpoints require -- never to put
    customer fields into the prompt.
    """
    payload = context or {}
    headers: dict[str, str] = {"Content-Type": "application/json"}
    body: dict[str, Any]

    if provider.kind == "openai":
        headers["Authorization"] = f"Bearer {secret}"
        headers["Accept"] = "application/json"
        body = {"model": model, "messages": _messages(question), "max_tokens": 512}
        url = provider.endpoint
    elif provider.kind == "anthropic":
        headers["x-api-key"] = secret
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": model,
            "max_tokens": 512,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": str(question or "")}],
        }
        url = provider.endpoint
    elif provider.kind == "gemini":
        body = {
            "contents": [{"role": "user", "parts": [{"text": str(question or "")}]}],
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "generationConfig": {"maxOutputTokens": 512},
        }
        # The key is a query parameter by the provider's contract. It is never
        # written to a log line or an error message; see ``_scrub``.
        url = f"{provider.endpoint}/{model}:generateContent?key={secret}"
    elif provider.kind == "chatgpt_web":
        headers["Authorization"] = f"Bearer {secret}"
        headers["Accept"] = "text/event-stream"
        body = {
            "action": "next",
            "model": model,
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [str(question or "")]},
                }
            ],
            "parent_message_id": str(uuid.uuid4()),
            "conversation_mode": {"kind": "primary_assistant"},
        }
        url = provider.endpoint
    elif provider.kind == "claude_web":
        organization = str(
            payload.get("claude_organization_id")
            or os.getenv("AI_CLAUDE_WEB_ORGANIZATION_ID", "")
        )
        conversation = str(
            payload.get("claude_conversation_id")
            or os.getenv("AI_CLAUDE_WEB_CONVERSATION_ID", "")
        )
        headers["Cookie"] = f"sessionKey={secret}"
        headers["Accept"] = "application/json"
        body = {"prompt": str(question or ""), "timezone": "UTC"}
        url = (
            f"{provider.endpoint}/organizations/{organization}"
            f"/chat_conversations/{conversation}/completion"
        )
    elif provider.kind == "gemini_web":
        headers["Cookie"] = f"__Secure-1PSID={secret}"
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        # batchexecute takes a form-encoded RPC envelope; the shape is the
        # browser's, which is the reason this provider is best-effort.
        body = {
            "f.req": json.dumps(
                [None, json.dumps([[None, None, None, None, None, None, None, None,
                                    None, str(question or "")]])]
            )
        }
        url = provider.endpoint
    else:  # pragma: no cover - the table is closed; this is a guard, not a path
        raise ValueError(f"unknown AI provider kind: {provider.kind!r}")

    return AiRequest(
        provider=provider.provider,
        model=model,
        kind=provider.kind,
        method="POST",
        url=url,
        headers=headers,
        body=body,
        timeout=max(0.05, float(timeout)),
    )


def _first_text(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
            if isinstance(part, str):
                return part
    if isinstance(parts, dict) and isinstance(parts.get("text"), str):
        return parts["text"]
    return ""


def extract_text(kind: str, response: AiResponse) -> str:
    """Pull the answer out of a successful response. Pure."""
    body = response.body
    if body is None and response.text:
        try:
            body = json.loads(response.text)
        except (ValueError, TypeError):
            body = None

    if kind == "openai":
        choices = (body or {}).get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return str(message.get("content") or "").strip()
        return ""
    if kind == "anthropic":
        blocks = (body or {}).get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") in (None, "text"):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""
    if kind == "gemini":
        candidates = (body or {}).get("candidates") or []
        if candidates:
            content = candidates[0].get("content") or {}
            return _first_text(content.get("parts")).strip()
        return ""
    if kind == "chatgpt_web":
        return _extract_chatgpt_sse(response.text).strip()
    if kind == "claude_web":
        for key in ("completion", "text", "message"):
            value = (body or {}).get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if kind == "gemini_web":
        return _extract_gemini_web(response.text).strip()
    return ""


def _extract_chatgpt_sse(text: str) -> str:
    """The last complete message event in a ChatGPT web SSE stream.

    Pure, and deliberately forgiving: the stream interleaves delta events and a
    terminal event, and only the terminal one carries the whole answer. A parse
    that fails is an empty answer, which the pool treats as a soft failure and
    fails over -- never as a reason to raise into the chat handler.
    """
    answer = ""
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            continue
        message = (event or {}).get("message") or {}
        content = message.get("content") or {}
        parts = _first_text(content.get("parts"))
        if parts:
            answer = parts
    return answer


def _extract_gemini_web(text: str) -> str:
    """Best-effort text out of a ``batchexecute`` response. Pure."""
    candidate = ""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "[{\"":
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            continue
        candidate = _find_longest_string(parsed, candidate)
    return candidate


def _find_longest_string(node: Any, current: str) -> str:
    if isinstance(node, str):
        return node if len(node) > len(current) else current
    if isinstance(node, list):
        for item in node:
            current = _find_longest_string(item, current)
        return current
    if isinstance(node, dict):
        for item in node.values():
            current = _find_longest_string(item, current)
        return current
    return current


def _scrub(text: str, secret: str) -> str:
    """Remove a credential from anything a human might read."""
    if secret and secret in text:
        return text.replace(secret, "***redacted***")
    return text


# ---------------------------------------------------------------------------
# The default transport
# ---------------------------------------------------------------------------

class TransportUnavailable(RuntimeError):
    """Raised when no HTTP client is importable. Degrades like a failure."""


def requests_transport(request: AiRequest) -> AiResponse:
    """The real transport: ``requests``, with the request's own timeout.

    A deployment that wants connection pooling, proxies, or a provider flow this
    module does not model installs its own transport with
    :func:`set_transport`; nothing else changes, because the pool only ever sees
    ``(status, text, headers, body)``.
    """
    try:
        import requests
    except Exception as exc:  # pragma: no cover - requests is a deploy dep
        raise TransportUnavailable(str(exc)) from exc

    data = request.body
    kwargs: dict[str, Any] = {
        "headers": dict(request.headers),
        "timeout": request.timeout,
    }
    if request.headers.get("Content-Type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        kwargs["data"] = data
    else:
        kwargs["json"] = data

    response = requests.post(request.url, **kwargs)
    text = response.text or ""
    parsed: Any = None
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    return AiResponse(
        status=int(response.status_code),
        text=text,
        headers={str(k): str(v) for k, v in response.headers.items()},
        body=parsed,
    )


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------

class AllProvidersExhausted(RuntimeError):
    """No provider in the list would answer, and why.

    Raised to the caller (``brain_router.respond``), which is built to catch any
    exception from the external brain and degrade to system knowledge. The
    message is the audit trail: one line per attempt, with the kind of refusal.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AiProviderPool:
    """The live list of credentials and the cooldown state over the providers.

    In-memory and process-global, like the sentiment breaker next door, because
    the hot path is a synchronous external-brain call that cannot await a
    database read. Persistence is the durable shadow of this object: an operator
    rotation is written through to ``ai_provider_credentials`` and the cooldowns
    to ``ai_model_health``, and a restart rehydrates both. The in-memory copy is
    authoritative while the process runs; the rows are how it survives stopping.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # provider -> (auth_mode, secret). Present means "operator supplied it";
        # absent means "fall back to the environment". A cleared credential is
        # stored as an empty secret so that it also suppresses the env fallback.
        self._credentials: dict[str, tuple[str, str]] = {}
        self._credential_source: dict[str, str] = {}
        self._credential_label: dict[str, str] = {}
        # (provider, model) -> health dict.
        self._health: dict[tuple[str, str], dict[str, Any]] = {}
        # provider -> status string ("active"/"disabled") set by an operator.
        self._status: dict[str, str] = {}
        self._transport: Optional[Callable[[AiRequest], AiResponse]] = None

    # -- credentials --------------------------------------------------------

    def set_credential(
        self,
        provider: str,
        auth_mode: str,
        secret: str,
        *,
        source: str = "database",
        label: str = "",
        status: str = "active",
    ) -> None:
        """Install (or, with an empty secret, suppress) a provider credential."""
        if provider not in PROVIDER_BY_NAME:
            raise KeyError(provider)
        mode = auth_mode if auth_mode in AUTH_MODES else AUTH_API_KEY
        with self._lock:
            self._credentials[provider] = (mode, str(secret or ""))
            self._credential_source[provider] = str(source)
            self._credential_label[provider] = str(label or "")
            self._status[provider] = str(status or "active")

    def clear_credential(self, provider: str) -> None:
        """Forget the stored credential, including the env-suppressing empty."""
        with self._lock:
            self._credentials.pop(provider, None)
            self._credential_source.pop(provider, None)
            self._credential_label.pop(provider, None)

    def credential_for(self, provider: str) -> Optional[tuple[str, str]]:
        """The credential to use: the operator's, else the environment's."""
        with self._lock:
            stored = self._credentials.get(provider)
        if stored is not None:
            mode, secret = stored
            if secret:
                return mode, secret
            return None  # deliberately suppressed
        spec = PROVIDER_BY_NAME.get(provider)
        if spec is None:
            return None
        return env_credential(spec)

    def credential_source(self, provider: str) -> str:
        with self._lock:
            if provider in self._credentials:
                return self._credential_source.get(provider, "database")
        if provider in PROVIDER_BY_NAME and env_credential(PROVIDER_BY_NAME[provider]):
            return "environment"
        return "none"

    def has_credential(self, provider: str) -> bool:
        return self.credential_for(provider) is not None

    def available_providers(self) -> list[str]:
        return [p for p in provider_order() if self.has_credential(p)]

    # -- cooldown state -----------------------------------------------------

    def cooldown_until(self, provider: str, model: str) -> Optional[datetime]:
        with self._lock:
            entry = self._health.get((provider, model))
            if not entry:
                return None
            until = entry.get("cooldown_until")
            return until if isinstance(until, datetime) else None

    def record_success(self, provider: str, model: str) -> None:
        with self._lock:
            entry = self._health.get((provider, model))
            if entry is not None:
                entry.update(
                    {
                        "failure_kind": "",
                        "failure_count": 0,
                        "cooldown_until": None,
                        "last_error": "",
                        "last_checked_at": _utcnow(),
                    }
                )

    def record_failure(
        self,
        provider: str,
        model: str,
        kind: str,
        *,
        retry_after: Optional[int] = None,
        message: str = "",
        now: Optional[datetime] = None,
    ) -> datetime:
        """Cool a candidate down and return the moment it becomes usable again."""
        moment = now or _utcnow()
        seconds = cooldown_seconds(kind, retry_after)
        until = moment + timedelta(seconds=seconds)
        clean = _scrub(str(message or ""), self._secret_for(provider))[:255]
        targets = [(provider, model)]
        if kind == AUTH_FAILED:
            # A rejected credential is a property of the provider, not of one
            # model. Cooling only the model that happened to be tried would walk
            # the remaining models of a provider that has already said no.
            spec = PROVIDER_BY_NAME.get(provider)
            if spec is not None:
                targets = [(provider, m.model) for m in spec.models]
        with self._lock:
            for key in targets:
                entry = self._health.setdefault(key, {})
                entry.update(
                    {
                        "failure_kind": kind,
                        "failure_count": int(entry.get("failure_count") or 0) + 1,
                        "cooldown_until": until,
                        "last_error": clean,
                        "last_checked_at": moment,
                    }
                )
        return until

    def _secret_for(self, provider: str) -> str:
        cred = self.credential_for(provider)
        return cred[1] if cred else ""

    def reset(self, provider: Optional[str] = None, model: Optional[str] = None) -> int:
        """Clear cooldowns, optionally narrowed to one provider or one model."""
        with self._lock:
            keys = [
                key
                for key in self._health
                if (provider is None or key[0] == provider)
                and (model is None or key[1] == model)
            ]
            for key in keys:
                self._health.pop(key, None)
            return len(keys)

    def set_status(self, provider: str, status: str) -> None:
        with self._lock:
            self._status[provider] = str(status or "active")

    def status_of(self, provider: str) -> str:
        with self._lock:
            return self._status.get(provider, "active")

    # -- transport ----------------------------------------------------------

    def set_transport(self, transport: Optional[Callable[[AiRequest], AiResponse]]) -> None:
        self._transport = transport

    def _send(self, request: AiRequest) -> AiResponse:
        transport = self._transport or requests_transport
        return transport(request)

    # -- planning and answering --------------------------------------------

    def plan(self, *, now: Optional[datetime] = None) -> list[tuple[str, str]]:
        moment = now or _utcnow()
        model_lists = {
            name: tuple(m.model for m in spec.models)
            for name, spec in PROVIDER_BY_NAME.items()
        }
        order = tuple(
            name for name in provider_order() if self.status_of(name) != "disabled"
        )
        return plan_attempts(
            order=order,
            model_lists=model_lists,
            has_credential=self.has_credential,
            cooldown_until=self.cooldown_until,
            now=moment,
        )

    def _run_completion(
        self, question: str, context: dict[str, Any], budget_ms: float
    ) -> dict[str, Any]:
        """The failover loop itself, returning the full trace of what happened.

        Returns ``{"text", "provider", "model", "attempts", "exhausted"}`.
        ``complete`` turns an exhausted result into an exception for the chat
        path; the admin probe reads the same dict directly, so what an operator
        sees when they trigger a failover is the same run the customer would get,
        not a second code path that might disagree.
        """
        deadline = time.monotonic() + max(0.0, float(budget_ms)) / 1000.0
        attempts: list[str] = []
        plan = self.plan()
        if not plan:
            return {
                "text": "",
                "provider": None,
                "model": None,
                "attempts": attempts,
                "exhausted": True,
                "reason": "no AI provider is available: none is configured, or "
                "every configured model is cooling down",
            }

        for provider, model in plan:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                attempts.append(f"{provider}/{model}: skipped, budget exhausted")
                break
            # Re-check the cooldown rather than trusting the plan: a rejected
            # credential earlier in *this* loop cools every model of its
            # provider, and the plan was computed before that happened. Walking
            # on would try the remaining models of a provider that has already
            # said no.
            cooled = self.cooldown_until(provider, model)
            if cooled is not None and cooled > _utcnow():
                continue
            credential = self.credential_for(provider)
            if credential is None:
                continue
            auth_mode, secret = credential
            spec = PROVIDER_BY_NAME[provider]
            request = build_request(
                spec,
                model,
                question,
                auth_mode,
                secret,
                timeout=remaining,
                context=context,
            )
            try:
                response = self._send(request)
            except Exception as exc:  # noqa: BLE001 -- any transport failure fails over
                kind = TIMEOUT if "timeout" in type(exc).__name__.lower() else NETWORK_ERROR
                until = self.record_failure(
                    provider, model, kind, message=f"{type(exc).__name__}: {exc}"
                )
                attempts.append(f"{provider}/{model}: {kind} until {until.isoformat()}")
                continue

            kind = classify_failure(response.status, response.text, response.headers)
            if kind == OK:
                text = extract_text(spec.kind, response)
                if text:
                    self.record_success(provider, model)
                    return {
                        "text": text,
                        "provider": provider,
                        "model": model,
                        "attempts": attempts,
                        "exhausted": False,
                    }
                until = self.record_failure(
                    provider, model, UNKNOWN_ERROR, message="empty completion"
                )
                attempts.append(
                    f"{provider}/{model}: empty completion until {until.isoformat()}"
                )
                continue
            until = self.record_failure(
                provider,
                model,
                kind,
                retry_after=parse_retry_after(response.headers),
                message=_response_excerpt(response),
            )
            attempts.append(f"{provider}/{model}: {kind} until {until.isoformat()}")

        return {
            "text": "",
            "provider": None,
            "model": None,
            "attempts": attempts,
            "exhausted": True,
            "reason": "; ".join(attempts) or "no attempt was made",
        }

    def complete(self, question: str, context: dict[str, Any], budget_ms: float) -> str:
        """Answer a question from the first provider that will, or raise.

        Synchronous by contract: this is the ``ExternalCall`` shape
        ``brain_router.respond`` invokes. ``budget_ms`` is the whole allowance
        for every attempt together, so a failover chain is bounded by the same
        ceiling a single call would be.
        """
        result = self._run_completion(question, context, budget_ms)
        if not result["exhausted"]:
            return str(result["text"])
        raise AllProvidersExhausted(
            str(result.get("reason") or "; ".join(result.get("attempts") or []))
        )

    def complete_traced(
        self, question: str, context: dict[str, Any], budget_ms: float
    ) -> dict[str, Any]:
        """Like :meth:`complete`, but returns the attempt trace instead of raising.

        The admin failover control uses this so an operator sees exactly which
        provider answered and which were skipped, without a second code path that
        could disagree with the one the chat handler runs.
        """
        return self._run_completion(question, context, budget_ms)

    # -- persistence shadow -------------------------------------------------

    def health_records(self) -> list[dict[str, Any]]:
        """The cooldown state as rows for ``ai_model_health``."""
        with self._lock:
            out = []
            for (provider, model), entry in sorted(self._health.items()):
                out.append(
                    {
                        "provider": provider,
                        "model": model,
                        "failure_kind": str(entry.get("failure_kind") or ""),
                        "failure_count": int(entry.get("failure_count") or 0),
                        "cooldown_until": entry.get("cooldown_until"),
                        "last_error": str(entry.get("last_error") or "")[:255],
                        "last_checked_at": entry.get("last_checked_at"),
                    }
                )
            return out

    def apply_health_records(self, records: Any) -> int:
        """Rehydrate cooldown state from stored rows. Returns how many loaded."""
        loaded = 0
        for row in records or ():
            provider = str(_get(row, "provider", "") or "")
            model = str(_get(row, "model", "") or "")
            if not provider or not model:
                continue
            with self._lock:
                self._health[(provider, model)] = {
                    "failure_kind": str(_get(row, "failure_kind", "") or ""),
                    "failure_count": int(_get(row, "failure_count", 0) or 0),
                    "cooldown_until": _get(row, "cooldown_until"),
                    "last_error": str(_get(row, "last_error", "") or ""),
                    "last_checked_at": _get(row, "last_checked_at"),
                }
            loaded += 1
        return loaded

    # -- reporting ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The live view the admin surface returns. Never contains a secret."""
        now = _utcnow()
        providers = []
        for spec in AI_PROVIDERS:
            mode: Optional[str] = None
            credential = self.credential_for(spec.provider)
            if credential is not None:
                mode = credential[0]
            models = []
            for model in spec.models:
                until = self.cooldown_until(spec.provider, model.model)
                cooling = until is not None and until > now
                with self._lock:
                    entry = dict(self._health.get((spec.provider, model.model)) or {})
                models.append(
                    {
                        "model": model.model,
                        "available": self.has_credential(spec.provider) and not cooling,
                        "cooling_down": cooling,
                        "cooldown_until": until.isoformat() if until else None,
                        "failure_kind": str(entry.get("failure_kind") or ""),
                        "failure_count": int(entry.get("failure_count") or 0),
                        "last_error": str(entry.get("last_error") or ""),
                    }
                )
            providers.append(
                {
                    "provider": spec.provider,
                    "label": spec.label,
                    "kind": spec.kind,
                    "auth_modes": list(spec.auth_modes),
                    "configured": credential is not None,
                    "auth_mode": mode,
                    "credential_source": self.credential_source(spec.provider),
                    "status": self.status_of(spec.provider),
                    "browser_best_effort": spec.browser_best_effort,
                    "models": models,
                }
            )
        order = provider_order()
        return {
            "version": AI_PROVIDERS_VERSION,
            "generated_at": now.isoformat(),
            "provider_order": list(order),
            "transport": "injected" if self._transport is not None else "requests",
            "configured_providers": self.available_providers(),
            "next_attempt": [
                {"provider": p, "model": m} for p, m in self.plan(now=now)
            ],
            "providers": providers,
        }


def _get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _response_excerpt(response: AiResponse) -> str:
    text = str(response.text or "").strip()
    return text[:200]


#: The process-global pool, like the sentiment breaker in ``chat_analytics``.
POOL = AiProviderPool()


def set_transport(transport: Optional[Callable[[AiRequest], AiResponse]]) -> None:
    """Install (or clear) the transport. The one seam a test or deployment owns."""
    POOL.set_transport(transport)


def default_external_brain() -> Optional[Callable[[str, dict, float], str]]:
    """The external-brain callable, or ``None`` when nothing is configured.

    ``None`` is load-bearing: ``brain_router.respond`` treats it as "route to
    system with a reason" rather than a failed call, so a deployment with no AI
    provider keeps behaving exactly as it did before this module existed.
    """
    if not POOL.available_providers():
        return None
    return POOL.complete


def build_ai_providers_catalog(*, include_live: bool = False) -> dict[str, Any]:
    """The discovery payload for ``/meta/scoring-catalog``.

    Names, models, auth modes and whether a credential is present -- never a
    credential, a key fragment, or a URL that carries one. The catalog is how an
    operator learns what *could* be configured; the admin snapshot is how they
    learn what *is*.
    """
    providers = []
    for spec in AI_PROVIDERS:
        mode = None
        credential = POOL.credential_for(spec.provider)
        if credential is not None:
            mode = credential[0]
        providers.append(
            {
                "provider": spec.provider,
                "label": spec.label,
                "kind": spec.kind,
                "auth_modes": list(spec.auth_modes),
                "browser_best_effort": spec.browser_best_effort,
                "configured": credential is not None,
                "auth_mode": mode,
                "credential_source": POOL.credential_source(spec.provider),
                "env": {
                    "api_key": list(spec.api_key_envs),
                    "browser_session": list(spec.session_envs),
                },
                "models": [
                    {"model": m.model, "label": m.label, "note": m.note}
                    for m in spec.models
                ],
                "docs": spec.docs,
            }
        )
    payload: dict[str, Any] = {
        "version": AI_PROVIDERS_VERSION,
        "provider_order": list(provider_order()),
        "cooldowns_seconds": dict(DEFAULT_COOLDOWNS),
        "auth_cooldown_seconds": AUTH_COOLDOWN_SECONDS,
        "failure_kinds": list(FAILURE_KINDS),
        "browser_sessions_are_best_effort": True,
        "providers": providers,
    }
    if include_live:
        payload["live"] = POOL.snapshot()
    return payload


def validate_ai_providers() -> dict[str, Any]:
    """A self-check an operator or a test can run without the network.

    Reports the shape of the table, which providers are configured, and the two
    properties that make failover honest: every provider declares at least one
    model, and every auth mode it declares has somewhere to read the credential
    from.
    """
    problems: list[str] = []
    for spec in AI_PROVIDERS:
        if not spec.models:
            problems.append(f"{spec.provider}: declares no models")
        if AUTH_API_KEY in spec.auth_modes and not spec.api_key_envs:
            problems.append(f"{spec.provider}: api_key mode with no environment variable")
        if AUTH_BROWSER_SESSION in spec.auth_modes and not spec.session_envs:
            problems.append(
                f"{spec.provider}: browser_session mode with no environment variable"
            )
    unknown_order = [
        name
        for name in os.getenv("CSERVICE_AI_PROVIDER_ORDER", "").split(",")
        if name.strip() and name.strip() not in PROVIDER_BY_NAME
    ]
    if unknown_order:
        problems.append(
            "CSERVICE_AI_PROVIDER_ORDER names unknown providers: "
            + ", ".join(sorted(set(unknown_order)))
        )
    return {
        "version": AI_PROVIDERS_VERSION,
        "ok": not problems,
        "providers": len(AI_PROVIDERS),
        "configured": POOL.available_providers(),
        "problems": problems,
        "browser_providers": [
            spec.provider for spec in AI_PROVIDERS if spec.browser_best_effort
        ],
    }


def reset_ai_providers() -> None:
    """Return the pool to no credentials, no cooldowns and no transport.

    The module's globals are process-wide by design, so this is the autouse test
    fixture's reset entry point -- and a clean slate for a rotated credential in
    a long-lived process.
    """
    with POOL._lock:  # noqa: SLF001 -- the reset is the owner of the state
        POOL._credentials.clear()
        POOL._credential_source.clear()
        POOL._credential_label.clear()
        POOL._health.clear()
        POOL._status.clear()
    POOL.set_transport(None)


# ---------------------------------------------------------------------------
# Persistence: the durable shadow of the in-memory pool
# ---------------------------------------------------------------------------
# The hot path cannot await a database read, so the pool is authoritative while
# the process runs and these two functions are how it survives stopping. They
# live here rather than in the router because the pool owns its own state: a
# second writer that reimplemented "what a health row is" would eventually
# disagree with ``health_records``.


async def load_persisted_state(session: Any) -> dict[str, int]:
    """Rehydrate credentials and cooldowns from the database. Idempotent.

    A stored credential with an unreadable (or emptied) secret is *not* skipped
    silently: it is installed with an empty secret so it keeps suppressing the
    environment fallback, which is what an operator who disabled a provider
    meant. A row that is simply absent leaves the environment in charge.
    """
    from sqlalchemy import select

    from app import models

    loaded = {"credentials": 0, "health": 0, "disabled": 0}
    rows = (
        await session.execute(select(models.AiProviderCredential))
    ).scalars().all()
    for row in rows:
        secret = unseal_secret(row.secret_encrypted)
        POOL.set_credential(
            str(row.provider),
            str(row.auth_mode or AUTH_API_KEY),
            secret,
            source=str(row.credential_source or "database"),
            label=str(row.label or ""),
            status=str(row.status or "active"),
        )
        if secret:
            loaded["credentials"] += 1
        else:
            loaded["disabled"] += 1

    health_rows = (await session.execute(select(models.AiModelHealth))).scalars().all()
    loaded["health"] = POOL.apply_health_records(health_rows)
    return loaded


async def persist_health(session: Any) -> int:
    """Upsert the pool's cooldown state into ``ai_model_health``. Idempotent.

    Called after traffic that could have changed a cooldown, and by the admin
    reset. Whole-record upserts rather than diffs: the set is one row per
    provider model, which is small, and "write what is true now" cannot drift
    the way an incremental update can.
    """
    from sqlalchemy import select

    from app import models

    records = POOL.health_records()
    if not records:
        return 0
    existing = {
        (str(row.provider), str(row.model)): row
        for row in (
            await session.execute(select(models.AiModelHealth))
        ).scalars().all()
    }
    written = 0
    for record in records:
        key = (record["provider"], record["model"])
        row = existing.get(key)
        if row is None:
            row = models.AiModelHealth(provider=record["provider"], model=record["model"])
            session.add(row)
        row.failure_kind = record["failure_kind"]
        row.failure_count = record["failure_count"]
        row.cooldown_until = record["cooldown_until"]
        row.last_error = record["last_error"]
        row.last_checked_at = record["last_checked_at"]
        written += 1
    await session.commit()
    return written

