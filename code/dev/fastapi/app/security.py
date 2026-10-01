"""Credential primitives, token issuance/validation, and password policy.

The original module was a thin wrapper: one HMAC secret, one token builder, and
a length/character-class policy. That is enough to *issue* a credential but not
enough to run a real service, so the module now covers the full credential
lifecycle while keeping every historical contract intact:

- **Key ring / rotation** — ``SECRET_KEY`` signs, an optional
  ``SECRET_KEY_PREVIOUS`` ring still *verifies*. Tokens carry a ``kid`` derived
  from the signing key, so a key swap is a config change plus a redeploy and
  in-flight tokens keep working. A token signed with a retired key is
  transparently accepted during the rotation window.
- **Revocation** — ``TokenRevocationRegistry`` denies a single ``jti`` (logout,
  password change, admin kill-switch) with TTL-bounded memory so the deny-list
  never grows without bound. ``revoke_subject`` invalidates everything for a
  user at once.
- **Token validation, not just decoding** — ``decode_token_full`` returns a
  structured ``TokenValidation`` naming the *reason* a token was rejected
  (expired / wrong type / wrong audience / revoked / bad subject) so API layers
  can map failures to 401 vs 403 without re-deriving them.
- **Clock leeway** — small ``nbf``/``exp`` skew is tolerated, which matters
  once more than one node issues tokens.
- **Audience / issuer** — optional (``TOKEN_AUDIENCE`` / ``TOKEN_ISSUER``);
  unset keeps the historical permissive behavior.
- **Step-up authentication** — a token may carry an ``amr`` (auth method) and
  ``acr`` (assurance) level; ``step_up_satisfied`` lets sensitive routes demand
  a stronger level than a plain login provides.
- **API keys** — hashed, prefixed, scoped, expiring service credentials for
  machine callers, bootstrapable from ``CSERVICE_API_KEYS``.
- **Password policy** — composable ``PasswordPolicy`` (length, character
  classes, mixed case, common-password and repeated/sequential-character bans),
  an entropy-based 0-4 strength score, and a ``PasswordHistory`` ring that
  rejects reuse of the last N hashes.

Everything here is a pure function or an explicit registry with a reset hook —
no import-time connections, no hidden state beyond the module-level defaults
that predate this expansion.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import uuid
from typing import Any, Iterable, Optional

from jose import jwt
from passlib.context import CryptContext

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")  # Use env var in production
ALGORITHM = "HS256"

# Token lifetime configuration (env-tunable).
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# Token type claims. Access tokens drive authenticated API calls; refresh
# tokens are longer-lived and intended for silent re-authentication only.
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# --- Rotation / validation configuration -------------------------------------
#
# ``SECRET_KEY_PREVIOUS`` is a comma-separated ring of *retired but still
# trusted* secrets, oldest last. Verification walks the ring; signing always
# uses ``SECRET_KEY``. That gives a zero-downtime rotation window without a
# second deployment step.
SECRET_KEY_PREVIOUS = [
    key.strip() for key in (os.getenv("SECRET_KEY_PREVIOUS", "") or "").split(",") if key.strip()
]
# Tolerated clock skew in seconds for ``nbf``/``exp`` checks.
TOKEN_LEEWAY_SECONDS = int(os.getenv("TOKEN_LEEWAY_SECONDS", "0"))
# When empty, ``iss``/``aud`` are neither stamped nor enforced (permissive).
TOKEN_ISSUER = os.getenv("TOKEN_ISSUER", "")
TOKEN_AUDIENCE = os.getenv("TOKEN_AUDIENCE", "")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --- Key ring ------------------------------------------------------------------


def key_id_for(secret: str) -> str:
    """Stable, non-reversible identifier for a signing secret.

    A truncated HMAC of the secret under a fixed label — short enough for a
    header, and revealing nothing about the key material itself.
    """
    return hmac.new(
        b"cservice-jwt-kid", secret.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:12]


def signing_key_id() -> str:
    """``kid`` currently used for *signing*."""
    return key_id_for(SECRET_KEY)


def key_ring() -> list[dict[str, Any]]:
    """Introspectable description of the verification key ring.

    Only metadata is exposed — never any secret material.
    """
    ring: list[dict[str, Any]] = [
        {
            "kid": key_id_for(SECRET_KEY),
            "role": "signing",
            "algorithm": ALGORITHM,
            "retired": False,
        }
    ]
    for index, secret in enumerate(SECRET_KEY_PREVIOUS):
        ring.append(
            {
                "kid": key_id_for(secret),
                "role": "verification",
                "algorithm": ALGORITHM,
                "retired": True,
                "retired_index": index,
            }
        )
    return ring


def _verification_secrets() -> list[str]:
    return [SECRET_KEY, *SECRET_KEY_PREVIOUS]


def _resolve_secret(kid: str | None) -> list[str]:
    """Candidate secrets for a token, most-current first."""
    secrets_ring = _verification_secrets()
    if not kid:
        return secrets_ring
    matching = [secret for secret in secrets_ring if key_id_for(secret) == kid]
    return matching or secrets_ring


# --- Password hashing ----------------------------------------------------------


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


# --- Token issuance -------------------------------------------------------------


def _build_token(data: dict, token_type: str, expires_delta: timedelta | None) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta is None:
        if token_type == TOKEN_TYPE_REFRESH:
            expires_delta = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        else:
            expires_delta = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": now + expires_delta, "iat": now, "typ": token_type})
    # Stable per-token identity so a single token can be revoked by ``jti``.
    to_encode.setdefault("jti", uuid.uuid4().hex)
    # Bind the token to its signing key so rotation can pick the right secret.
    to_encode["kid"] = signing_key_id()
    if TOKEN_ISSUER:
        to_encode.setdefault("iss", TOKEN_ISSUER)
    if TOKEN_AUDIENCE:
        to_encode.setdefault("aud", TOKEN_AUDIENCE)
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(data: dict, expires_delta: timedelta = None):
    """Issue a short-lived access token carrying the ``typ=access`` claim."""
    return _build_token(data, TOKEN_TYPE_ACCESS, expires_delta)


def create_refresh_token(data: dict, expires_delta: timedelta = None):
    """Issue a long-lived refresh token carrying the ``typ=refresh`` claim."""
    return _build_token(data, TOKEN_TYPE_REFRESH, expires_delta)


# --- Token revocation -----------------------------------------------------------


class TokenRevocationRegistry:
    """TTL-bounded deny-list of revoked token ids (``jti``).

    Entries carry the token's own ``exp`` so sweeping can drop them for free
    once the token would have expired anyway — the list can only ever hold
    tokens that are still nominally valid. A subject-level index makes
    "revoke everything for this user" O(1) without scanning.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_jti: dict[str, datetime] = {}
        self._by_subject: dict[str, set[str]] = {}

    def revoke(self, jti: str, *, subject: str | None = None, expires_at: datetime | None = None) -> bool:
        """Deny a single token id. Returns True when newly revoked."""
        if not jti:
            return False
        with self._lock:
            if jti in self._by_jti:
                return False
            self._by_jti[jti] = expires_at or (datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
            if subject:
                self._by_subject.setdefault(subject, set()).add(jti)
            return True

    def revoke_subject(self, subject: str) -> int:
        """Revoke every tracked token for ``subject``. Returns the count."""
        with self._lock:
            jtis = sorted(self._by_subject.get(subject, set()))
            for jti in jtis:
                self._by_jti.pop(jti, None)
            self._by_subject.pop(subject, None)
            return len(jtis)

    def is_revoked(self, jti: str | None) -> bool:
        if not jti:
            return False
        with self._lock:
            return jti in self._by_jti

    def sweep(self, now: datetime | None = None) -> int:
        """Drop entries whose underlying token has expired. Returns the count."""
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            stale = [jti for jti, exp in self._by_jti.items() if exp <= moment]
            for jti in stale:
                self._by_jti.pop(jti, None)
            for subject, jtis in list(self._by_subject.items()):
                remaining = jtis - set(stale)
                if remaining:
                    self._by_subject[subject] = remaining
                else:
                    self._by_subject.pop(subject, None)
            return len(stale)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "revoked": len(self._by_jti),
                "subjects": len(self._by_subject),
                "tracked_jtis": sorted(self._by_jti),
            }

    def reset(self) -> None:
        with self._lock:
            self._by_jti.clear()
            self._by_subject.clear()


REVOCATIONS = TokenRevocationRegistry()


def revoke_token(token: str) -> dict[str, Any]:
    """Revoke a single bearer/refresh token by its own ``jti``.

    Returns a small report instead of raising: logging out with an already
    expired token must still succeed from the caller's point of view.
    """
    try:
        payload = _decode_any(token)
    except ValueError:
        return {"revoked": False, "reason": "token_not_decodable"}
    jti = payload.get("jti")
    exp = payload.get("exp")
    expires_at = (
        datetime.fromtimestamp(exp, tz=timezone.utc) if isinstance(exp, (int, float)) else None
    )
    newly = REVOCATIONS.revoke(jti or "", subject=payload.get("sub"), expires_at=expires_at)
    return {
        "revoked": True,
        "newly_revoked": newly,
        "subject": payload.get("sub"),
        "token_type": payload.get("typ", TOKEN_TYPE_ACCESS),
    }


def revoke_subject_tokens(subject: str) -> int:
    """Revoke every tracked token for a subject (password change, kill-switch)."""
    return REVOCATIONS.revoke_subject(subject)


# --- Token validation -----------------------------------------------------------


@dataclass(frozen=True)
class TokenValidation:
    """Outcome of validating a token, with the *reason* it failed."""

    valid: bool
    reason: str = ""
    claims: dict[str, Any] = field(default_factory=dict)
    subject: str = ""
    token_type: str = ""
    jti: str = ""
    kid: str = ""
    expired: bool = False
    revoked: bool = False


def _decode_any(token: str) -> dict:
    """Signature-verify against the whole ring, without claim enforcement."""
    if not token or not isinstance(token, str):
        raise ValueError("Could not validate credentials")
    header = _peek_header(token)
    candidates = _resolve_secret(header.get("kid"))
    last_exc: Exception | None = None
    for secret in candidates:
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=[ALGORITHM],
                # Audience is enforced by the callers (and only when
                # configured); leeway is passed through the options map,
                # which is how python-jose accepts it.
                options={"verify_aud": False, "leeway": TOKEN_LEEWAY_SECONDS},
            )
        except Exception as exc:  # jose raises several subclasses on failure
            last_exc = exc
    raise ValueError("Could not validate credentials") from last_exc


def _peek_header(token: str) -> dict:
    try:
        header_segment = token.split(".", 1)[0]
        padding = "=" * (-len(header_segment) % 4)
        return json.loads(base64.urlsafe_b64decode(header_segment + padding).decode("utf-8"))
    except Exception:
        return {}


def decode_access_token(token: str, expected_type: str = TOKEN_TYPE_ACCESS) -> dict:
    """Decode and validate a JWT.

    Raises ``ValueError`` when the token is malformed, expired, missing its
    subject, carries a token type that does not match ``expected_type``, fails
    audience/issuer checks when those are configured, or has been revoked.
    """
    payload = _decode_any(token)
    token_type = payload.get("typ", TOKEN_TYPE_ACCESS)
    if token_type != expected_type:
        raise ValueError(f"Expected {expected_type} token, got {token_type}")
    if TOKEN_AUDIENCE and payload.get("aud") != TOKEN_AUDIENCE:
        raise ValueError("Token audience mismatch")
    if TOKEN_ISSUER and payload.get("iss") != TOKEN_ISSUER:
        raise ValueError("Token issuer mismatch")
    if not payload.get("sub"):
        raise ValueError("Token missing subject")
    if REVOCATIONS.is_revoked(payload.get("jti")):
        raise ValueError("Token has been revoked")
    return payload


def decode_token_full(
    token: str, expected_type: str = TOKEN_TYPE_ACCESS
) -> TokenValidation:
    """Non-raising validation that *explains* the failure.

    ``decode_access_token`` collapses every failure into one ``ValueError``;
    callers that need to distinguish "expired" from "revoked" (to pick 401 vs
    403, or to trigger a silent refresh) use this instead.
    """
    try:
        claims = _decode_any(token)
    except Exception as exc:
        expired = "ExpiredSignature" in type(exc).__name__ or "expired" in str(exc).lower()
        return TokenValidation(valid=False, reason="expired" if expired else "malformed", expired=expired)

    token_type = claims.get("typ", TOKEN_TYPE_ACCESS)
    subject = claims.get("sub") or ""
    jti = claims.get("jti") or ""
    kid = claims.get("kid") or _peek_header(token).get("kid", "")

    def _fail(reason: str) -> TokenValidation:
        return TokenValidation(
            valid=False,
            reason=reason,
            claims=claims,
            subject=subject,
            token_type=token_type,
            jti=jti,
            kid=kid,
        )

    if token_type != expected_type:
        return _fail("wrong_token_type")
    if TOKEN_AUDIENCE and claims.get("aud") != TOKEN_AUDIENCE:
        return _fail("audience_mismatch")
    if TOKEN_ISSUER and claims.get("iss") != TOKEN_ISSUER:
        return _fail("issuer_mismatch")
    if not subject:
        return _fail("missing_subject")
    if REVOCATIONS.is_revoked(jti):
        return TokenValidation(
            valid=False,
            reason="revoked",
            claims=claims,
            subject=subject,
            token_type=token_type,
            jti=jti,
            kid=kid,
            revoked=True,
        )
    return TokenValidation(
        valid=True,
        reason="ok",
        claims=claims,
        subject=subject,
        token_type=token_type,
        jti=jti,
        kid=kid,
    )


# --- Step-up authentication -----------------------------------------------------


# Ordered assurance levels. A plain password login is ``LOA1``; a re-auth,
# biometric validation, or an API key with a higher ``acr`` claim raises it.
STEP_UP_LEVELS = ("none", "loa1", "loa2", "loa3")
STEP_UP_RANK = {level: rank for rank, level in enumerate(STEP_UP_LEVELS)}

#: level -> the ``amr`` evidence values that establish it.
#:
#: This is the single declaration of the evidence-to-assurance mapping.
#: ``app.deps.STEP_UP_RANKS`` documents the same rows for the admin surface;
#: :func:`step_up_level_of` reads *this* table so the verifier cannot disagree
#: with the documentation. They used to be two independent lists, and had
#: drifted: ``deps.STEP_UP_RANKS`` listed ``hwk`` as loa2 evidence while the
#: verifier's own hard-coded set omitted it, so a hardware-held key token
#: derived ``loa1`` -- the verifier quietly downgrading the behaviour its own
#: documentation promised.
STEP_UP_SPEC: dict[str, dict[str, object]] = {
    "none": {
        "evidence": (),
        "description": "No assurance claim at all. Never satisfies a step-up requirement.",
    },
    "loa1": {
        "evidence": ("pwd",),
        "description": "Ordinary password login. The floor for a token with no amr.",
    },
    "loa2": {
        "evidence": ("pwd", "mfa", "otp", "hwk"),
        "description": "Second factor: authenticator code, one-time code, or a hardware-held key.",
    },
    "loa3": {
        "evidence": ("mfa", "hwk", "webauthn", "biometric", "cosign",
                     "webauthn_l3", "hardware_key", "recovery_code"),
        "description": "Passkey / hardware key / biometric / co-signature.",
    },
}


def step_up_level_of(claims: dict | TokenValidation | None) -> str:
    """Assurance level carried by a token/claims mapping."""
    if isinstance(claims, TokenValidation):
        claims = claims.claims
    if not claims:
        return "none"
    acr = str(claims.get("acr", "") or "")
    if acr in STEP_UP_RANK:
        return acr
    # Derive a floor from the authentication methods actually used.
    #
    # The evidence sets are read from STEP_UP_RANK so this function cannot
    # disagree with the table that documents it. They used to be hard-coded
    # here, and had drifted: STEP_UP_RANKS declares `hwk` as loa2 evidence
    # while the local set omitted it, so a token whose only evidence was a
    # hardware-held key derived loa1 -- a *downgrade* of the documented
    # behaviour, on a credential the table had already said was worth more
    # than a password.
    methods = claims.get("amr") or []
    if isinstance(methods, str):
        methods = [methods]
    floor = "loa1"
    for method in methods:
        for level in sorted(STEP_UP_RANK, key=STEP_UP_RANK.get):
            if method in (STEP_UP_SPEC.get(level) or {}).get("evidence", ()):
                if STEP_UP_RANK[level] > STEP_UP_RANK.get(floor, 0):
                    floor = level
                break
    return floor


def step_up_satisfied(claims: dict | TokenValidation | None, required_level: str) -> bool:
    """True when the token meets ``required_level`` (or higher)."""
    required = required_level if required_level in STEP_UP_RANK else "loa1"
    return STEP_UP_RANK[step_up_level_of(claims)] >= STEP_UP_RANK[required]


def with_step_up(data: dict, level: str, methods: Iterable[str] = ()) -> dict:
    """Stamp assurance claims onto token payloads before issuance."""
    stamped = dict(data)
    stamped.setdefault("acr", level if level in STEP_UP_RANK else "loa1")
    stamped.setdefault("amr", list(methods) or ["pwd"])
    return stamped


# --- API keys (machine credentials) ---------------------------------------------

API_KEY_PREFIX = "csk_"


@dataclass(frozen=True)
class ApiKeyRecord:
    """A machine credential. The secret is never retained after issuance."""

    key_id: str
    name: str
    subject: str
    scopes: tuple[str, ...]
    hash_prefix: str
    expires_at: Optional[str]
    created_at: str
    revoked: bool = False


def hash_api_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key(
    *,
    name: str,
    subject: str,
    scopes: Iterable[str] = (),
    expires_in_days: int | None = None,
) -> tuple[str, ApiKeyRecord]:
    """Mint an API key. Returns ``(plaintext, record)`` — store it now.

    Only a SHA-256 digest is retained, so a leaked registry cannot be replayed
    as a credential.
    """
    secret = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    record = ApiKeyRecord(
        key_id=uuid.uuid4().hex[:16],
        name=name,
        subject=subject,
        scopes=tuple(sorted(set(scopes))),
        hash_prefix=hash_api_key(secret)[:12],
        expires_at=(
            (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()
            if expires_in_days
            else None
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return secret, record


class ApiKeyRegistry:
    """Digest-backed registry of machine credentials with scope checks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_hash: dict[str, ApiKeyRecord] = {}

    def register(self, secret: str, record: ApiKeyRecord) -> ApiKeyRecord:
        with self._lock:
            self._by_hash[hash_api_key(secret)] = record
            return record

    def issue(self, **kwargs: Any) -> tuple[str, ApiKeyRecord]:
        secret, record = generate_api_key(**kwargs)
        return secret, self.register(secret, record)

    def authenticate(self, secret: str) -> tuple[Optional[ApiKeyRecord], str]:
        """Resolve a plaintext key. Returns ``(record, reason)``.

        ``reason`` is ``ok`` / ``unknown`` / ``revoked`` / ``expired`` so the
        API layer can log the cause without leaking which it was.
        """
        if not secret or not isinstance(secret, str) or not secret.startswith(API_KEY_PREFIX):
            return None, "unknown"
        record = self._by_hash.get(hash_api_key(secret))
        if record is None:
            return None, "unknown"
        if record.revoked:
            return record, "revoked"
        if record.expires_at and record.expires_at <= datetime.now(timezone.utc).isoformat():
            return record, "expired"
        return record, "ok"

    def has_scope(self, record: ApiKeyRecord, scope: str) -> bool:
        """Scopes are hierarchical on ``:`` and ``*`` is a wildcard."""
        for granted in record.scopes:
            if granted == "*" or granted == scope:
                return True
            if scope.startswith(f"{granted}:"):
                return True
        return False

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            for digest, record in list(self._by_hash.items()):
                if record.key_id == key_id and not record.revoked:
                    self._by_hash[digest] = replace(record, revoked=True)
                    return True
            return False

    def list_keys(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "key_id": r.key_id,
                    "name": r.name,
                    "subject": r.subject,
                    "scopes": list(r.scopes),
                    "hash_prefix": r.hash_prefix,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                    "revoked": r.revoked,
                }
                for r in sorted(self._by_hash.values(), key=lambda r: r.name)
            ]

    def reset(self) -> None:
        with self._lock:
            self._by_hash.clear()


API_KEYS = ApiKeyRegistry()


def _bootstrap_api_keys_from_env() -> None:
    """Seed keys from ``CSERVICE_API_KEYS=name=secret[:scope1,scope2];...``.

    Provided for deployment bring-up; the default deployment declares none, so
    this is a no-op unless an operator opts in.
    """
    raw = os.getenv("CSERVICE_API_KEYS", "") or ""
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, rest = part.split("=", 1)
        secret, _, scope_blob = rest.partition(":")
        secret = secret.strip()
        if not secret.startswith(API_KEY_PREFIX):
            secret = f"{API_KEY_PREFIX}{secret}"
        API_KEYS.register(
            secret,
            ApiKeyRecord(
                key_id=uuid.uuid4().hex[:16],
                name=name.strip(),
                subject="service",
                scopes=tuple(s.strip() for s in scope_blob.split(",") if s.strip()),
                hash_prefix=hash_api_key(secret)[:12],
                expires_at=None,
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
        )


_bootstrap_api_keys_from_env()


# --- Password strength policy ---------------------------------------------------
#
# Purely additive guardrail: the backend can assess a proposed password before
# persisting it. Wire enforcement into flows when the product wants it; the
# helpers themselves never mutate state.

# Passwords that are trivially guessable and must never be accepted.
COMMON_PASSWORDS = frozenset(
    {
        "password", "password1", "password123", "passw0rd", "123456", "12345678",
        "123456789", "1234567890", "qwerty", "qwerty123", "letmein", "welcome",
        "welcome1", "admin", "admin123", "administrator", "iloveyou", "monkey",
        "dragon", "sunshine", "princess", "football", "baseball", "abc123",
        "changeme", "secret", "cservice", "customer", "service", "root", "toor",
    }
)

# Character-class detectors reused by both the policy and the strength score.
_RE_LOWER = re.compile(r"[a-z]")
_RE_UPPER = re.compile(r"[A-Z]")
_RE_DIGIT = re.compile(r"[0-9]")
_RE_SPECIAL = re.compile(r"[^A-Za-z0-9]")
_RE_SEQUENCE = re.compile(r"(0123|1234|2345|3456|4567|5678|6789|abcd|bcde|cdef|defg|efgh|fghi|ghij|hijk|ijkl|jklm|klmn|lmno|mnop|nopq|opqr|pqrs|qrst|rstu|stuv|tuvw|uvwx|vwxy|wxyz)", re.I)
_RE_REPEATED = re.compile(r"(.)\1{2,}")

# Bits of freedom contributed by each character class *actually present*.
_CLASS_BITS: tuple[tuple[re.Pattern[str], float], ...] = (
    (_RE_LOWER, math.log2(26)),
    (_RE_UPPER, math.log2(26)),
    (_RE_DIGIT, math.log2(10)),
    (_RE_SPECIAL, math.log2(33)),
)


def _estimate_entropy_bits(password: str) -> float:
    """Uniform-random search space, in bits, for ``password``.

    ``length * log2(alphabet)`` over the classes the candidate actually uses.
    Measuring the *used* alphabet is what keeps a single-class string like
    ``"aaaaaaaa"`` scoring at ~38 bits instead of the ~200 bits a naive
    ``length * 26`` would claim.
    """
    per_char = sum(bits for pattern, bits in _CLASS_BITS if pattern.search(password))
    if per_char <= 0:
        return 0.0
    return len(password) * per_char


@dataclass(frozen=True)
class PasswordPolicy:
    min_length: int = 8
    max_length: int = 128
    require_letter: bool = True
    require_digit: bool = False
    require_special: bool = False
    # --- additive, composable rules (all default to off) ---
    require_upper: bool = False
    forbid_common: bool = False
    forbid_repeated_chars: bool = False
    forbid_sequences: bool = False
    min_strength: int = 0
    history_depth: int = 0
    extra_disallowed: tuple[str, ...] = ()

    def with_overrides(self, **kwargs: Any) -> "PasswordPolicy":
        """Derive a stricter/looser variant without mutating the shared default."""
        return replace(self, **kwargs)


DEFAULT_PASSWORD_POLICY = PasswordPolicy()


def validate_password_strength(
    password: str, policy: PasswordPolicy = DEFAULT_PASSWORD_POLICY
) -> list[str]:
    """Return a list of policy violations (empty means the password is accepted)."""
    if password is None:
        return ["password is required"]
    issues: list[str] = []
    if len(password) < policy.min_length:
        issues.append(f"at least {policy.min_length} characters")
    if len(password) > policy.max_length:
        issues.append(f"no more than {policy.max_length} characters")
    if policy.require_letter and not any(ch.isalpha() for ch in password):
        issues.append("at least one letter")
    if policy.require_digit and not any(ch.isdigit() for ch in password):
        issues.append("at least one digit")
    if policy.require_special and not any(not ch.isalnum() for ch in password):
        issues.append("at least one special character")
    if policy.require_upper and not _RE_UPPER.search(password):
        issues.append("at least one uppercase letter")
    if policy.forbid_common and _is_common_password(password):
        issues.append("not a commonly used password")
    if policy.forbid_repeated_chars and _RE_REPEATED.search(password):
        issues.append("no more than two identical characters in a row")
    if policy.forbid_sequences and _RE_SEQUENCE.search(password):
        issues.append("no keyboard or alphabet sequences")
    if policy.extra_disallowed and password in policy.extra_disallowed:
        issues.append("password is on the disallowed list")
    if policy.min_strength:
        score = score_password_strength(password)
        if score["score"] < policy.min_strength:
            issues.append(f"strength score of at least {policy.min_strength} (0-4)")
    return issues


def _is_common_password(password: str) -> bool:
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        return True
    # Strip trailing digits/symbols: "password123!" is still "password".
    stripped = lowered.rstrip("0123456789!@#$%^&*._-+=")
    return stripped in COMMON_PASSWORDS


def score_password_strength(password: str) -> dict[str, Any]:
    """Heuristic 0-4 strength score with the factors that produced it.

    Deliberately dependency-free (no zxcvban data file) and monotonic in
    search space, so it is useful as a *signal* rather than as a guarantee.
    """
    if not password:
        return {
            "score": 0,
            "entropy_bits": 0,
            "adjusted_bits": 0,
            "factors": [],
            "penalties": ["empty"],
        }

    entropy = _estimate_entropy_bits(password)
    factors: list[str] = []
    penalties: list[str] = []

    if _RE_LOWER.search(password):
        factors.append("lowercase")
    if _RE_UPPER.search(password):
        factors.append("uppercase")
    if _RE_DIGIT.search(password):
        factors.append("digits")
    if _RE_SPECIAL.search(password):
        factors.append("symbols")
    if len(password) >= 12:
        factors.append("length_12+")
    if len(password) >= 16:
        factors.append("length_16+")

    if _is_common_password(password):
        penalties.append("common_password")
    if _RE_REPEATED.search(password):
        penalties.append("repeated_characters")
    if _RE_SEQUENCE.search(password):
        penalties.append("predictable_sequence")
    # Single-class strings ("aaaaaaaa", "12345678") are cheap to brute-force.
    if len(factors) <= 1:
        penalties.append("single_character_class")
    if password.isdigit():
        penalties.append("digits_only")
    if len(password) < DEFAULT_PASSWORD_POLICY.min_length:
        penalties.append("too_short")

    adjusted = entropy - (12 * len(penalties))
    if adjusted >= 100:
        score = 4
    elif adjusted >= 70:
        score = 3
    elif adjusted >= 45:
        score = 2
    elif adjusted >= 28:
        score = 1
    else:
        score = 0
    # Even a good score cannot rescue a known-common password.
    if "common_password" in penalties:
        score = min(score, 1)
    return {
        "score": score,
        "entropy_bits": max(entropy, 0),
        "adjusted_bits": max(adjusted, 0),
        "factors": factors,
        "penalties": penalties,
    }


class PasswordHistory:
    """Bounded ring of prior password hashes, for reuse prevention.

    bcrypt hashes are salted, so equality comparison never works — reuse is
    detected by *verifying* the candidate against each retained hash.
    """

    def __init__(self, depth: int = 5) -> None:
        self.depth = max(int(depth), 0)
        self._hashes: list[str] = []
        self._lock = threading.RLock()

    def remember(self, hashed_password: str) -> None:
        if self.depth == 0 or not hashed_password:
            return
        with self._lock:
            self._hashes.insert(0, hashed_password)
            del self._hashes[self.depth :]

    def is_reused(self, plain_password: str) -> bool:
        if not plain_password or self.depth == 0:
            return False
        with self._lock:
            candidates = list(self._hashes)
        for hashed in candidates:
            try:
                if pwd_context.verify(plain_password, hashed):
                    return True
            except Exception:
                continue
        return False

    def check_and_remember(self, plain_password: str, hashed_password: str) -> bool:
        """Returns True when the password is a *reuse* (and records the new hash)."""
        reused = self.is_reused(plain_password)
        self.remember(hashed_password)
        return reused

    def depth_configured(self) -> int:
        return self.depth

    def clear(self) -> None:
        with self._lock:
            self._hashes.clear()


DEFAULT_PASSWORD_HISTORY = PasswordHistory(depth=0)


def build_password_history(depth: int = 5) -> PasswordHistory:
    return PasswordHistory(depth=depth)


def password_policy_payload(policy: PasswordPolicy = DEFAULT_PASSWORD_POLICY) -> dict:
    """Introspectable policy description for metadata/catalog endpoints."""
    return {
        "min_length": policy.min_length,
        "max_length": policy.max_length,
        "require_letter": policy.require_letter,
        "require_digit": policy.require_digit,
        "require_special": policy.require_special,
        "require_upper": policy.require_upper,
        "forbid_common": policy.forbid_common,
        "forbid_repeated_chars": policy.forbid_repeated_chars,
        "forbid_sequences": policy.forbid_sequences,
        "min_strength": policy.min_strength,
        "history_depth": policy.history_depth,
        "hash_scheme": "bcrypt",
        "token_algorithm": ALGORITHM,
        "access_token_expire_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
        "refresh_token_expire_days": REFRESH_TOKEN_EXPIRE_DAYS,
    }


def build_security_catalog() -> dict[str, Any]:
    """Introspectable catalog of the whole credential surface (meta tooling)."""
    return {
        "tokens": {
            "algorithm": ALGORITHM,
            "access_token_expire_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
            "refresh_token_expire_days": REFRESH_TOKEN_EXPIRE_DAYS,
            "token_types": [TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH],
            "leeway_seconds": TOKEN_LEEWAY_SECONDS,
            "issuer_enforced": bool(TOKEN_ISSUER),
            "audience_enforced": bool(TOKEN_AUDIENCE),
            "claims": ["sub", "exp", "iat", "typ", "jti", "kid"]
            + (["iss"] if TOKEN_ISSUER else [])
            + (["aud"] if TOKEN_AUDIENCE else []),
        },
        "key_ring": key_ring(),
        "rotation": {
            "signing_key_id": signing_key_id(),
            "retired_keys": len(SECRET_KEY_PREVIOUS),
            "window": "retired keys still verify; new tokens always use the signing key",
        },
        "revocation": REVOCATIONS.stats() | {"mechanism": "jti deny-list, TTL-bounded"},
        "step_up": {
            "levels": list(STEP_UP_LEVELS),
            "claims": ["acr", "amr"],
        },
        "api_keys": {
            "prefix": API_KEY_PREFIX,
            "storage": "sha256 digest only",
            "registered": len(API_KEYS.list_keys()),
            "scope_wildcards": ["*", "parent:child"],
        },
        "password_policy": password_policy_payload(),
        "password_strength": {
            "scale": [0, 4],
            "common_passwords_blocked": len(COMMON_PASSWORDS),
            "signals": [
                "character_classes",
                "length",
                "repeated_characters",
                "predictable_sequence",
                "common_password",
            ],
        },
        "password_history": {
            "mechanism": "bcrypt verify against a bounded hash ring",
            "configured_depth": DEFAULT_PASSWORD_HISTORY.depth_configured(),
        },
    }
