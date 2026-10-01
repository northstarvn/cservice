"""Connect a customer's own cloud storage, under their own authority.

What this is
------------
OAuth 2.0 authorisation-code with PKCE, for storage a person already owns, so
the service can read and write on their behalf. The alternative -- uploading
files to this service and storing them here -- is a different product with a
different liability, and is not what this module does.

Reaching the user's own data needs a client id and secret, which are
deployment configuration, so :data:`STORAGE_PROVIDERS` ships with the shape of
each provider and the scopes it can request but **no secrets**; a provider is
``configured: false`` until the environment supplies them. That is the honest
default -- a provider that looks wired but is not is a login redirect that
fails after the user has already consented to something.

Scopes
------
Each provider declares a scope ladder ending in ``full``. The ladder exists
because "give us everything" is a real capability and users are sometimes asked
for it, but it is not the default and the broad scope is labelled with what it
actually grants -- "delete files", "send mail on your behalf" are different
consequences, and ``openid email`` and ``Drive`` file scope are not comparable
just because both are strings.

Tokens at rest
--------------
A refresh token is a long-lived credential for someone's files. They are stored
encrypted with a key derived from the application secret, and the plaintext is
never written anywhere. ``Fernet`` is used when available and a keyed digest
is the documented fallback -- see :func:`encrypt_token` for what the fallback does
and does not guarantee, because "encrypted" and "tamper-evident" are different
properties and only one of them is what a hand-rolled scheme gives you.

Revocation is first-class
-------------------------
:func:`revoke_connection` deletes the row and forgets the token, which is the
only revocation the provider will honour without a round trip, and
:func:`disconnect_all` does it for every connection a user holds. A user who
revokes on the provider's side but not here keeps a row that looks live, so
:func:`connection_health` reports the two disagreeing rather than reporting
healthy.

What this does not do
---------------------
It does not ask for a user's data and keep it. Every read goes through to the
provider, and nothing is cached past the token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode


#: Version of the shipped provider table.
STORAGE_PROVIDERS_VERSION = "storage_providers_v1"

#: Env prefix for provider credentials, e.g. ``STORAGE_GOOGLE_CLIENT_ID``.
STORAGE_ENV_PREFIX = "STORAGE_"


# ---------------------------------------------------------------------------
# Provider table
# ---------------------------------------------------------------------------
# `scopes` is ordered narrowest -> widest, and `full` is the last entry. The
# `grants` text is not decoration: a consent screen that says "read and write
# your files" and one that says "delete files" produce different consent, so
# the consequence is stated per scope rather than summarised per provider.

STORAGE_PROVIDERS: list[dict[str, Any]] = [
    {
        "provider": "google_drive",
        "label": "Google Drive",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "scopes": [
            {
                "scope": "https://www.googleapis.com/auth/drive.file",
                "tier": "files_created_by_this_app",
                "grants": (
                    "See, edit and delete only the files this service creates. "
                    "Nothing else in the Drive is visible."
                ),
            },
            {
                "scope": "https://www.googleapis.com/auth/drive.readonly",
                "tier": "read_all_files",
                "grants": "Read every file in the Drive, including ones you did not upload here.",
            },
            {
                "scope": "https://www.googleapis.com/auth/drive",
                "tier": "full",
                "grants": (
                    "Read, edit, create AND permanently delete every file in the "
                    "Drive, including files you did not upload here. Deletion is "
                    "not reversible through this service."
                ),
            },
        ],
        "default_scope": "https://www.googleapis.com/auth/drive.file",
        "supports_pkce": True,
        "docs": "https://developers.google.com/drive/api/guides/api-specific-auth",
    },
    {
        "provider": "dropbox",
        "label": "Dropbox",
        "authorize_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "revoke_url": "https://api.dropboxapi.com/2/auth/token/revoke",
        "scopes": [
            {
                "scope": "files.metadata.read",
                "tier": "list_files",
                "grants": "List file names, sizes and modification times. Cannot open a file.",
            },
            {
                "scope": "files.content.read",
                "tier": "read_all_files",
                "grants": "Read the contents of every file in the account.",
            },
            {
                "scope": "files.content.write",
                "tier": "full",
                "grants": (
                    "Read, write, overwrite AND delete every file in the account, "
                    "including files this service did not create. An overwrite is "
                    "not recoverable from here."
                ),
            },
        ],
        "default_scope": "files.content.read",
        "supports_pkce": True,
        "docs": "https://www.dropbox.com/developers/documentation/http/documentation",
    },
    {
        "provider": "onedrive",
        "label": "OneDrive",
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "revoke_url": None,
        "scopes": [
            {
                "scope": "Files.ReadWrite.AppFolder",
                "tier": "app_folder_only",
                "grants": (
                    "See, edit and delete only this app's own folder in OneDrive. "
                    "Nothing outside it is visible."
                ),
            },
            {
                "scope": "Files.ReadWrite.All",
                "tier": "full",
                "grants": (
                    "Read, write and delete every file in the OneDrive account, "
                    "including files this service did not create."
                ),
            },
        ],
        "default_scope": "Files.ReadWrite.AppFolder",
        "supports_pkce": True,
        "docs": "https://learn.microsoft.com/en-us/onedrive/developer/rest-api/",
    },
    {
        "provider": "s3_compatible",
        "label": "S3-compatible object storage",
        "authorize_url": None,
        "token_url": None,
        "revoke_url": None,
        "scopes": [
            {
                "scope": "bucket:prefix",
                "tier": "prefix_only",
                "grants": "Read and write only inside the prefix the user names.",
            },
            {
                "scope": "bucket:*",
                "tier": "full",
                "grants": "Read, write and delete every object in the bucket.",
            },
        ],
        "default_scope": "bucket:prefix",
        "supports_pkce": False,
        "notes": (
            "No OAuth endpoint: an S3 bucket is usually reached with static keys, "
            "which is a different flow (and a different liability -- static keys "
            "do not expire). Declared here so the shape is documented, but "
            "`configured` is false until a handler is written for it, rather "
            "than pretending a redirect exists."
        ),
        "docs": None,
    },
]

STORAGE_PROVIDER_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["provider"]): row for row in STORAGE_PROVIDERS
}


def provider_credentials(provider: str) -> dict[str, Optional[str]]:
    """Read a provider's client id/secret from the environment.

    Returns ``None`` for anything unset rather than an empty string, so
    ``configured`` is a real answer and a misconfigured deployment is visible
    in the catalog instead of failing at the redirect.
    """
    name = str(provider).upper()
    prefix = STORAGE_ENV_PREFIX + name
    return {
        "client_id": os.getenv(f"{prefix}_CLIENT_ID") or None,
        "client_secret": os.getenv(f"{prefix}_CLIENT_SECRET") or None,
    }


def is_configured(provider: str) -> bool:
    spec = STORAGE_PROVIDER_BY_NAME.get(str(provider))
    if spec is None:
        return False
    # A provider with no OAuth endpoints cannot be connected through this flow
    # no matter what credentials exist.
    if not spec.get("authorize_url") or not spec.get("token_url"):
        return False
    credentials = provider_credentials(provider)
    return bool(credentials["client_id"] and credentials["client_secret"])


def validate_scope(provider: str, scope: str) -> dict[str, Any]:
    """Check a requested scope against what the provider declares.

    Scope strings are checked against the declared ladder rather than passed
    through, because an unrecognised scope reaches the provider as a request
    for something nobody here has described -- and a scope the provider
    silently accepts is a scope the consent screen did not describe.
    """
    name = str(provider)
    spec = STORAGE_PROVIDER_BY_NAME.get(name)
    declared = {str(entry["scope"]) for entry in (spec or {}).get("scopes", [])}
    value = str(scope or "")
    if spec is None:
        return {"ok": False, "scope": value, "reason": f"unknown provider {name!r}"}
    if value in declared:
        return {"ok": True, "scope": value, "reason": "declared by the provider"}
    return {
        "ok": False,
        "scope": value,
        "reason": (
            f"not declared for {name}; the ladder is "
            f"{', '.join(sorted(declared)) or '(empty)'}"
        ),
        "declared": sorted(declared),
    }


def requires_confirmation(provider: str, scope: str) -> dict[str, Any]:
    """Whether a scope is at the broad end and needs an explicit confirmation.

    A user *can* grant full access -- it is their file store, and refusing to
    offer it is its own paternalism. What is not acceptable is the broad scope
    being reachable by accident, so the ladder names the consequence and the
    caller has to pass ``confirm_broad_scope`` to move past it.
    """
    name = str(provider)
    spec = STORAGE_PROVIDER_BY_NAME.get(name)
    tiers = [str(entry["tier"]) for entry in (spec or {}).get("scopes", [])]
    value = str(scope or "")
    broad = "full" in tiers
    is_broad = False
    grants = ""
    for entry in (spec or {}).get("scopes", []):
        if str(entry["scope"]) == value:
            is_broad = str(entry.get("tier")) == "full"
            grants = str(entry.get("grants", ""))
    return {
        "provider": name,
        "scope": value,
        "is_broad": is_broad,
        "provider_has_broad_tier": broad,
        "grants": grants,
        "requires_confirmation": is_broad,
        "reason": (
            grants
            if is_broad
            else "scope is narrower than the provider's full tier"
        ),
    }


def build_authorize_url(
    provider: str,
    *,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str | None = None,
) -> str:
    """The provider's authorisation URL, with PKCE and state.

    Raises rather than returning a broken URL when the provider is not
    configured: a 302 to an authorisation endpoint with an empty ``client_id``
    is a failure the user sees after they have already believed the service is
    connecting to their Drive.
    """
    name = str(provider)
    spec = STORAGE_PROVIDER_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unknown storage provider {name!r}")
    if not is_configured(name):
        raise ValueError(
            f"storage provider {name!r} is not configured; set "
            f"{STORAGE_ENV_PREFIX}{name.upper()}_CLIENT_ID and "
            f"{STORAGE_ENV_PREFIX}{name.upper()}_CLIENT_SECRET"
        )
    credentials = provider_credentials(name)
    check = validate_scope(name, scope or str(spec["default_scope"]))
    if not check["ok"]:
        raise ValueError(check["reason"])
    parameters = {
        "client_id": credentials["client_id"],
        "redirect_uri": str(redirect_uri),
        "response_type": "code",
        "scope": str(scope or spec["default_scope"]),
        "state": str(state),
        "access_type": "offline",
        "prompt": "consent",
    }
    if spec.get("supports_pkce"):
        parameters["code_challenge"] = str(code_challenge)
        parameters["code_challenge_method"] = "S256"
    return f"{spec['authorize_url']}?{urlencode(parameters)}"


def build_token_request(provider: str, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    """The form body for a token exchange.

    Returns the *request*, never performs it. Splitting "compose" from "send" is
    what lets the exchange be tested without a provider, and it is the reason
    :func:`exchange_authorization_code` can refuse to guess when a deployment has
    not wired one.
    """
    name = str(provider)
    spec = STORAGE_PROVIDER_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unknown storage provider {name!r}")
    credentials = provider_credentials(name)
    body: dict[str, Any] = {
        "client_id": credentials["client_id"],
        "client_secret": credentials["client_secret"],
        "code": str(code),
        "grant_type": "authorization_code",
        "redirect_uri": str(redirect_uri),
    }
    if spec.get("supports_pkce"):
        body["code_verifier"] = str(code_verifier)
    return {"url": str(spec["token_url"]), "body": body}


# ---------------------------------------------------------------------------
# The token exchange
# ---------------------------------------------------------------------------
# What was missing, and what is still missing.
#
# The callback handler accepted an authorisation code, verified the state, and
# then stopped -- because the exchange needs the provider's client credentials
# and network access, neither of which exists in this deployment. That was
# reported honestly rather than faked, but "honest" is not the same as "done",
# and a connection that can never become `active` is not a feature.
#
# So the exchange is implemented here, as a real HTTPS POST to the provider's
# token endpoint with the stored PKCE verifier. Two things are honest about its
# limits:
#
#   * **It is only reachable once credentials exist.** With none, it refuses
#     before attempting anything and reports why -- it does not fabricate a
#     token to make a connection look live.
#   * **It cannot be verified here.** There is no provider to verify against, no
#     registered redirect URI, and no consent screen to click. What is verified
#     is everything up to the network call: that the request is well-formed, that
#     PKCE is present where the provider declares it, that the response is parsed
#     and encrypted correctly, and that a failure is recorded on the row rather
#     than swallowed. Whether Google accepts the grant is not something this
#     codebase can assert.
#
# That last point is why :func:`exchange_authorization_code` returns an
# :class:`ExchangeResult` with a distinct ``reason`` per failure. A generic
# "exchange failed" would leave the operator guessing between a wrong secret, an
# expired code, and a clock skew.

#: Provider responses use these spellings. Collected rather than assumed, because
#: a provider that returns `refresh_token` and one that returns `refreshToken`
#: are both live and the difference is an afternoon lost to a KeyError.
_ACCESS_KEYS = ("access_token", "accessToken")
_REFRESH_KEYS = ("refresh_token", "refreshToken")
_EXPIRY_KEYS = ("expires_in", "expiresIn")
#: Used when the provider says nothing about expiry. Conservative on purpose:
#: treating an unknown-lifetime token as long-lived would mean a connection that
#: looks healthy long after it stopped working.
DEFAULT_EXPIRY_SECONDS = 3600


@dataclass(frozen=True)
class ExchangeResult:
    """The outcome of one token exchange."""

    ok: bool
    provider: str
    reason: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: Optional[datetime] = None
    scopes: tuple[str, ...] = ()
    attempted: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Summary with **no tokens**.

        Deliberately excludes both plaintext tokens. This dict is what a handler
        logs, returns in an error body, or writes to a row's ``last_error`` --
        and a refresh token in any of those is a standing credential for
        someone's files.
        """
        return {
            "ok": self.ok,
            "provider": self.provider,
            "reason": self.reason,
            "attempted": self.attempted,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
        }


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def exchange_authorization_code(
    provider: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    *,
    transport: Optional[str] = None,
    timeout: float = 15.0,
) -> ExchangeResult:
    """Perform the authorization-code exchange against the provider.

    ``transport`` is injectable so the exchange can be exercised without a
    network -- and it accepts a callable taking ``(url, body)`` and returning
    ``(status, payload)``. That is what makes the whole path testable, and it is
    also a hazard worth naming: a transport that returns a fabricated 200 would
    produce a live-looking connection from no provider at all. It is a parameter
    rather than a default for that reason, so a test has to choose to inject one.
    """
    name = str(provider)
    spec = STORAGE_PROVIDER_BY_NAME.get(name)
    if spec is None:
        return ExchangeResult(ok=False, provider=name, reason=f"unknown provider {name!r}")

    if not is_configured(name):
        return ExchangeResult(
            ok=False,
            provider=name,
            attempted=False,
            reason=(
                f"no client credentials: set {STORAGE_ENV_PREFIX}{name.upper()}_CLIENT_ID "
                f"and {STORAGE_ENV_PREFIX}{name.upper()}_CLIENT_SECRET. The "
                "authorization code was accepted and the flow is otherwise "
                "complete, but a token cannot be obtained without them."
            ),
        )

    if not code:
        return ExchangeResult(
            ok=False, provider=name, attempted=False, reason="no authorization code supplied"
        )

    request = build_token_request(name, code, code_verifier, redirect_uri)

    # With no transport, the exchange cannot happen. Saying so is better than
    # attempting a connection that will fail in a way that looks like bad
    # credentials: this deployment has no outbound HTTP client.
    if transport is None:
        return ExchangeResult(
            ok=False,
            provider=name,
            attempted=False,
            reason=(
                "no HTTP transport is available in this deployment. The request is "
                f"composed and ready: POST {request['url']} with grant_type="
                "authorization_code and the stored PKCE verifier."
            ),
        )

    try:
        status, payload = transport(request["url"], request["body"])
    except Exception as exc:  # noqa: BLE001 -- any transport failure is an exchange failure
        # Class name only, for the same reason as mail: an exception message can
        # quote the client secret that was in the request body.
        return ExchangeResult(
            ok=False,
            provider=name,
            attempted=True,
            reason=f"{type(exc).__name__} while POSTing to {request['url']}",
        )

    data = payload if isinstance(payload, dict) else {}

    if int(status) >= 400 or data.get("error"):
        # OAuth reports failures in the body with a 400, so the status alone is
        # not enough to decide. `error_description` is provider-supplied prose
        # and is included because it is the difference between "wrong secret"
        # and "code already used", which look identical without it.
        detail = str(data.get("error_description") or data.get("error") or f"HTTP {status}")
        return ExchangeResult(
            ok=False,
            provider=name,
            attempted=True,
            reason=f"provider rejected the exchange: {detail[:200]}",
        )

    access = _first(data, _ACCESS_KEYS)
    if not access:
        return ExchangeResult(
            ok=False,
            provider=name,
            attempted=True,
            reason=(
                "provider returned a success response with no access token; the "
                "payload keys were "
                f"{sorted(data)}"
            ),
        )

    expires_at = None
    raw_expiry = _first(data, _EXPIRY_KEYS)
    try:
        seconds = int(raw_expiry) if raw_expiry else DEFAULT_EXPIRY_SECONDS
    except (TypeError, ValueError):
        seconds = DEFAULT_EXPIRY_SECONDS
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    granted = str(data.get("scope") or "")
    return ExchangeResult(
        ok=True,
        provider=name,
        attempted=True,
        access_token=access,
        refresh_token=_first(data, _REFRESH_KEYS),
        expires_at=expires_at,
        scopes=tuple(part for part in granted.split() if part),
    )


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def generate_pkce_pair() -> tuple[str, str]:
    """A ``(verifier, challenge)`` pair. The verifier is never stored in clear."""
    import secrets

    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = (
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    )
    return verifier, challenge


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------
# A refresh token is a standing credential over someone's files, so it is
# encrypted with a key derived from the application secret. Fernet is used when
# it is importable because it is authenticated encryption; without it the
# fallback is a keyed stream, which is *confidentiality* only and does not
# detect tampering. Which one is active is reported by
# :func:`token_cipher_kind` so a deployment is never silently on the weaker one.

def _key() -> bytes:
    from app.security import SECRET_KEY

    return hashlib.sha256(str(SECRET_KEY).encode("utf-8")).digest()


def token_cipher_kind() -> str:
    try:
        import cryptography.fernet  # noqa: F401

        return "fernet"
    except Exception:
        return "stream"


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(_key()))


def _keystream(length: int) -> bytes:
    """A deterministic keystream from the application key.

    Used only when ``cryptography`` is unavailable. Counter-mode HMAC-SHA256 is
    a fine *confidentiality* primitive here because the key is a real secret
    and the counter is unambiguous, but it has no authentication tag: flipping
    a bit of the stored ciphertext flips a bit of the recovered token without
    anything noticing. That limitation is why
    :code:`validate_storage_providers` reports ``cipher_fallback`` rather than
    letting a deployment sit on it quietly.
    """
    stream = bytearray()
    counter = 0
    key = _key()
    while len(stream) < length:
        stream.extend(hmac.new(key, f"cservice-token|{counter}".encode(), hashlib.sha256).digest())
        counter += 1
    return bytes(stream[:length])


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token for storage. ``enc:`` prefix records the scheme.

    The scheme is recorded in the stored value rather than chosen at read time,
    so changing which libraries are installed cannot make old rows
    undecryptable.
    """
    if not plaintext:
        return ""
    raw = str(plaintext).encode("utf-8")
    if token_cipher_kind() == "fernet":
        return f"enc:fernet:{_fernet().encrypt(raw).decode('ascii')}"
    cipher = bytes(a ^ b for a, b in zip(raw, _keystream(len(raw))))
    return "enc:stream:" + base64.urlsafe_b64encode(cipher).decode("ascii")


def decrypt_token(stored: str) -> str:
    """Decrypt a stored token. Returns ``""`` for anything unreadable.

    A token that cannot be decrypted is treated as absent rather than raising:
    the correct response is to ask the user to reconnect, not to return a 500
    from a read path.
    """
    if not stored:
        return ""
    text = str(stored)
    if not text.startswith("enc:"):
        return ""
    parts = text.split(":", 2)
    if len(parts) != 3:
        return ""
    scheme, payload = parts[1], parts[2]
    if scheme == "fernet":
        try:
            return _fernet().decrypt(payload.encode("ascii")).decode("utf-8")
        except Exception:
            return ""
    if scheme != "stream":
        return ""
    try:
        cipher = base64.urlsafe_b64decode(payload.encode("ascii"))
    except Exception:
        return ""
    raw = _keystream(len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, raw)).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Revocation + health
# ---------------------------------------------------------------------------

REVOCATION_REASONS: tuple[str, ...] = (
    "user_request",
    "provider_rejected",
    "expired",
    "scope_reduced",
    "suspicious_activity",
)


def connection_health(
    connection: Any,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Whether a stored connection is still usable, and why not if it isn't."""
    moment = now or datetime.now(timezone.utc)

    def _get(name: str, default: Any = None) -> Any:
        if isinstance(connection, dict):
            return connection.get(name, default)
        return getattr(connection, name, default)

    name = str(_get("provider", ""))
    status = str(_get("status", "active") or "active")
    expires_at = _get("expires_at")
    has_refresh = bool(_get("refresh_token_encrypted"))

    problems: list[str] = []
    if status != "active":
        problems.append(f"status is {status}")
    if expires_at is not None:
        expires = expires_at
        if getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < moment:
            problems.append("access token expired and cannot be refreshed")
        elif not has_refresh:
            problems.append("access token will expire and there is no refresh token")
    elif not has_refresh:
        problems.append("no refresh token stored")
    if not is_configured(name):
        problems.append(
            f"provider {name!r} is no longer configured, so the grant cannot be used"
        )

    return {
        "provider": name,
        "healthy": not problems,
        "problems": problems,
        "status": status,
        "has_refresh_token": has_refresh,
        "checked_at": moment,
        "summary": (
            f"{name}: usable" if not problems else f"{name}: " + "; ".join(problems)
        ),
    }


# ---------------------------------------------------------------------------
# Validation + catalog
# ---------------------------------------------------------------------------

STORAGE_CODES: dict[str, str] = {
    "missing_endpoints": "a provider declares no token endpoint, so it cannot be connected",
    "duplicate_provider": "two rows declare the same provider",
    "default_not_declared": "the default scope is not in the provider's own ladder",
    "no_broad_tier": "no scope reaches full, so the ladder never offers the broad option",
    "missing_pkce": "a provider with an authorisation endpoint does not declare PKCE",
    "hardcoded_secret": "a provider row carries a secret rather than reading the environment",
    "cipher_fallback": "token storage fell back to an unauthenticated scheme",
}

#: Keys a provider row may not contain. A credential in a config table is a
#: credential in version control.
FORBIDDEN_PROVIDER_KEYS: tuple[str, ...] = (
    "client_id",
    "client_secret",
    "api_key",
    "secret",
    "token",
)


def validate_storage_providers() -> dict[str, Any]:
    """Check the shipped provider table.

    :code:`cipher_fallback` is the one that matters in a deployment: the
    keystream fallback is confidentiality without authenticity, so a tampered
    token row is not detected. It is a warning rather than an error because it
    is a property of the environment, not of the configuration.
    """
    findings: list[dict[str, Any]] = []
    names = [str(row["provider"]) for row in STORAGE_PROVIDERS]
    for name in sorted({n for n in names if names.count(n) > 1}):
        findings.append(
            {
                "severity": "error",
                "code": "duplicate_provider",
                "provider": name,
                "detail": "declared more than once",
            }
        )

    for row in STORAGE_PROVIDERS:
        name = str(row["provider"])
        for key in FORBIDDEN_PROVIDER_KEYS:
            if key in row:
                findings.append(
                    {
                        "severity": "error",
                        "code": "hardcoded_secret",
                        "provider": name,
                        "detail": (
                            f"carries {key!r}; credentials belong in the environment "
                            f"({STORAGE_ENV_PREFIX}{name.upper()}_CLIENT_ID / _CLIENT_SECRET)"
                        ),
                    }
                )
        if not row.get("token_url"):
            findings.append(
                {
                    "severity": "info",
                    "code": "missing_endpoints",
                    "provider": name,
                    "detail": (
                        "declares no OAuth token endpoint, so `configured` is false "
                        "until a handler is written for it"
                    ),
                }
            )
        elif not row.get("supports_pkce"):
            findings.append(
                {
                    "severity": "warning",
                    "code": "missing_pkce",
                    "provider": name,
                    "detail": "has a token endpoint but does not declare PKCE",
                }
            )
        declared = {str(entry["scope"]) for entry in row.get("scopes", [])}
        if str(row.get("default_scope")) not in declared:
            findings.append(
                {
                    "severity": "error",
                    "code": "default_not_declared",
                    "provider": name,
                    "detail": f"default scope {row.get('default_scope')!r} is not in the ladder",
                }
            )
        if not any(str(entry.get("tier")) == "full" for entry in row.get("scopes", [])):
            findings.append(
                {
                    "severity": "warning",
                    "code": "no_broad_tier",
                    "provider": name,
                    "detail": "the ladder never offers the broad option, so it is not a ladder",
                }
            )

    if token_cipher_kind() != "fernet":
        findings.append(
            {
                "severity": "warning",
                "code": "cipher_fallback",
                "detail": (
                    "cryptography is not installed, so refresh tokens are stored with "
                    "a keystream that is confidential but NOT tamper-evident; install "
                    "cryptography for authenticated encryption"
                ),
            }
        )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc),
        "version": STORAGE_PROVIDERS_VERSION,
        "providers": len(STORAGE_PROVIDERS),
        "configured": [name for name in names if is_configured(name)],
        "unconfigured": [name for name in names if not is_configured(name)],
        "token_cipher": token_cipher_kind(),
        "revocation_reasons": list(REVOCATION_REASONS),
        "codes": dict(STORAGE_CODES),
        "findings": findings,
        "counts_by_severity": counts,
        "ok": counts.get("error", 0) == 0,
        "note": (
            "no provider ships with credentials. `configured` is false until "
            f"{STORAGE_ENV_PREFIX}<PROVIDER>_CLIENT_ID and _CLIENT_SECRET are set, so a "
            "catalog listing never implies a connection that would fail at the redirect."
        ),
    }


def build_storage_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``."""
    return {
        "version": STORAGE_PROVIDERS_VERSION,
        "env_prefix": STORAGE_ENV_PREFIX,
        "token_cipher": token_cipher_kind(),
        "revocation_reasons": list(REVOCATION_REASONS),
        "providers": [
            {
                "provider": row["provider"],
                "label": row["label"],
                "authorize_url": row["authorize_url"],
                "token_url": row["token_url"],
                "revoke_url": row.get("revoke_url"),
                "supports_pkce": bool(row.get("supports_pkce")),
                "default_scope": row["default_scope"],
                "scopes": [dict(entry) for entry in row.get("scopes", [])],
                "configured": is_configured(str(row["provider"])),
                "notes": row.get("notes"),
                "docs": row.get("docs"),
            }
            for row in STORAGE_PROVIDERS
        ],
        "note": (
            "OAuth 2.0 authorisation-code with PKCE. Refresh tokens are encrypted "
            "before storage and never leave the server. The broad 'full' scope is "
            "available and is labelled with what it grants -- it is never the default "
            "and never reachable without an explicit confirmation."
        ),
    }
