"""Login methods: what a person can log in *with*, and what each one proves.

The request this answers is "let people log in by any available means". Taken
literally that is a request for a method that logs someone in without proof,
and that is not built here -- see :data:`AUTH_METHODS` and
:data:`REFUSED_METHOD_PATTERNS` below, which is where that decision is written
down rather than left as a gap someone notices later.

What *is* built is the useful and legitimate version of it: several real
methods, each declaring the assurance it actually establishes, so a caller can
choose one and a policy engine can say which is enough. A person can use a
password, a TOTP code, a one-time code by email, a passkey, or a device token
they created deliberately. They are not *equally* strong, and the table says
so, because a UI that offers five options without saying which is which is
just as bad as offering none.

The two halves of the refusal, stated once
-----------------------------------------
**"Log in by any means, secure or not"** would mean shipping a method that
establishes identity with no proof of identity. Every one of those methods has
a name -- PIN-only, "trusted network", IP allowlist, a security question, a
cached device cookie issued without a second factor -- and every one is a
known account-takeover route, because the person who wants in does not have to
guess a password when the *service* is what will accept anything. Shipping one
would also weaken the ones that work: a bypass sitting next to real
authentication is an attack surface for the whole product, not a convenience
for the users who use it.

**"Recognise them by other info"** is implemented, in
:mod:`app.services.device_recognition`, but as something that decides *how much
proof to demand* rather than *whether to accept*. That is the difference
between a signal-based convenience and a signal-based identity, and it is the
whole of the design.

What the user-facing consequence is
----------------------------------
Someone who has a recognised device can log in with a one-time code by email
and can refresh a live session without being re-prompted, which is the friction
the request was actually about. Someone on a new device in a new country gets
asked for a second factor. Nobody is let in by their timezone.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

#: Version of the shipped method table.
AUTH_METHODS_VERSION = "auth_methods_v1"


# ---------------------------------------------------------------------------
# The method table
# ---------------------------------------------------------------------------
# `assurance` uses the vocabulary `app.security.STEP_UP_RANKS` already defines
# (`loa1` ordinary password, `loa2` second factor, `loa3` hardware/biometric),
# and `amr` reuses the same evidence names, so a token issued by any of these
# is understood by the existing `require_step_up` machinery with no new
# concept on the verification side.
#
# `enrollment_required` distinguishes a method that needs prior setup from one
# that works on first contact, which is what makes `POST /users/auth/methods`
# able to tell a client "you can use this now" versus "set this up first".
#
# `revocable` matters: a method a user cannot take back is a method they cannot
# leave. All four credential methods are revocable.

AUTH_METHODS: list[dict[str, Any]] = [
    {
        "method": "password",
        "label": "Password",
        "assurance": "loa1",
        "amr": ["pwd"],
        "enrollment_required": False,
        "enabled_by_default": True,
        "revocable": True,
        "channel": "direct",
        "rate_tier": "interactive",
        "challenge_ttl_minutes": 0,
        "description": (
            "What you typed at sign-up. The baseline, and the only method that "
            "works before anything has been set up."
        ),
        "caveat": (
            "Weaker than the others here and weaker than it should be. If this "
            "is the only method on an account, adding a second factor is the "
            "single highest-value change available."
        ),
    },
    {
        "method": "totp",
        "label": "Authenticator app",
        "assurance": "loa2",
        "amr": ["mfa"],
        "enrollment_required": True,
        "enabled_by_default": True,
        "revocable": True,
        "channel": "direct",
        "rate_tier": "sensitive",
        "challenge_ttl_minutes": 0,
        "description": (
            "A six-digit code from an authenticator app, changing every thirty "
            "seconds."
        ),
        "caveat": "Phishable under social pressure; a passkey is better.",
    },
    {
        "method": "email_otp",
        "label": "One-time code by email",
        "assurance": "loa2",
        "amr": ["otp"],
        "enrollment_required": False,
        "enabled_by_default": True,
        "revocable": True,
        "channel": "email",
        "rate_tier": "sensitive",
        "challenge_ttl_minutes": 10,
        "description": (
            "A short code sent to the address on the account. Works without "
            "any prior setup, which is what makes it the fallback when "
            "everything else is unavailable."
        ),
        "caveat": (
            "Establishes control of an inbox, not of a device. It is the "
            "recovery path, so it is deliberately not offered as a way to "
            "replace a lost authenticator on its own."
        ),
    },
    {
        "method": "webauthn",
        "label": "Passkey",
        "assurance": "loa3",
        "amr": ["webauthn", "mfa"],
        "enrollment_required": True,
        "enabled_by_default": True,
        "revocable": True,
        "channel": "device",
        "rate_tier": "sensitive",
        "challenge_ttl_minutes": 5,
        "description": (
            "A key held by the device's secure hardware or its platform "
            "authenticator. Phishing-resistant, and nothing to phish because "
            "nothing is transmitted."
        ),
        "caveat": None,
    },
    {
        "method": "trusted_device",
        "label": "Remember this device",
        "assurance": "loa2",
        "amr": ["hwk"],
        "enrollment_required": True,
        "enabled_by_default": False,
        "revocable": True,
        "channel": "device_cookie",
        "rate_tier": "interactive",
        "challenge_ttl_minutes": 0,
        "description": (
            "A credential you create on a device you have just proved "
            "ownership of, so you are not challenged on it again for a while."
        ),
        "caveat": (
            "Off by default and revocable from /users/me/devices. It is a "
            "real credential, not a bypass: creating one still requires a "
            "second factor, and it does nothing on a device an attacker "
            "already controls."
        ),
    },
]

AUTH_METHOD_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["method"]): row for row in AUTH_METHODS
}

#: Methods that can establish a session with no prior setup, in the order a
#: client should try them. `email_otp` is here rather than only after
#: enrolment because a locked-out account with no second factor would otherwise
#: have no way back in.
BOOTSTRAP_METHODS: tuple[str, ...] = ("password", "email_otp")

#: How long a one-time code stays valid, and how many wrong guesses it
#: tolerates. Both are deliberately short: a code that survives ten minutes and
#: unlimited attempts is a password with a delivery delay.
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
#: One-time codes are six digits, which is 20 bits. That is a real limit, so it
#: is bounded by a per-code attempt counter and a per-account/IP issuance rate
#: rather than left to the sheer size of the keyspace.
OTP_CODE_LENGTH = 6
OTP_ISSUANCE_WINDOW_MINUTES = 15
OTP_MAX_ISSUES_PER_WINDOW = 3


# ---------------------------------------------------------------------------
# The refusal, written down
# ---------------------------------------------------------------------------
# Recorded as data rather than as an absence so that "why can I not log in with
# X" has an answer in the repository, and so that adding one is a deliberate
# act someone has to argue for instead of a method that appears by omission.

REFUSED_METHOD_PATTERNS: list[dict[str, Any]] = [
    {
        "pattern": "pin_only",
        "label": "PIN with no second factor",
        "why_not": (
            "A short secret is still a secret, and this service has no way to "
            "tell a short secret from a guess. A four-digit PIN is ten "
            "thousand possibilities and every one of them is tryable."
        ),
    },
    {
        "pattern": "network_trust",
        "label": "Trusted IP or network",
        "why_not": (
            "The network is not the person. It is shared by a whole office, a "
            "hotel, a university or a mobile carrier's CGNAT range, and it is "
            "the part of a request an attacker controls most easily."
        ),
    },
    {
        "pattern": "security_question",
        "label": "Security questions",
        "why_not": (
            "The answers are public, discoverable, and often inherited from "
            "public records. A question whose answer is on someone's old "
            "profile is not a factor."
        ),
    },
    {
        "pattern": "silent_device_cookie",
        "label": "Automatic sign-in from a cookie",
        "why_not": (
            "Convenient and unauthenticated. A stolen cookie is a "
            "compromise with no attacker effort, and 'remember me' is exactly "
            "the mechanism that turns one malware infection into a standing "
            "account. `trusted_device` is the version of this that is safe: the "
            "token is only ever issued *after* a second factor, and it can be "
            "revoked."
        ),
    },
    {
        "pattern": "recognition_only",
        "label": "Sign in recognised from context alone",
        "why_not": (
            "This is the whole substance of the request, so it is answered "
            "directly rather than omitted. Device recognition exists and is "
            "implemented, and it is wired to decide *how much proof to demand* "
            "-- never *whether to accept*. A score is a probability drawn from "
            "signals that are all observable by anyone who can see a request, "
            "and a false accept does not announce itself the way a false "
            "reject does."
        ),
    },
]

REFUSAL_POLICY = (
    "No method is offered that establishes a session without proving something "
    "the user holds and an attacker does not. Where friction is the real "
    "problem, it is removed by recognising a device -- not by accepting weaker "
    "proof of who is asking."
)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def describe_methods(
    available: Optional[set[str]] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The method list, annotated with what this user can actually do now.

    ``available`` is the set of methods this user has enrolled. A method they
    have not set up is still listed -- with ``usable: false`` and what setting
    it up needs -- because a settings screen that only shows what is already
    configured cannot be used to configure anything.
    """
    enrolled = set(available or ())
    moment = now or datetime.now(timezone.utc)
    methods: list[dict[str, Any]] = []
    for row in AUTH_METHODS:
        name = str(row["method"])
        setup_done = name in enrolled
        usable = setup_done or not bool(row["enrollment_required"])
        methods.append(
            {
                **dict(row),
                "usable": usable,
                "enrolled": setup_done,
                "setup_needed": None if usable else f"set up {row['label'].lower()} first",
                "bootstrap": name in BOOTSTRAP_METHODS,
                "assurance_rank": _assurance_rank(str(row["assurance"])),
            }
        )
    methods.sort(key=lambda item: (item["assurance_rank"], item["method"]))
    return {
        "generated_at": moment,
        "version": AUTH_METHODS_VERSION,
        "methods": methods,
        "usable_now": [m["method"] for m in methods if m["usable"]],
        "enrolled": sorted(enrolled),
        "weakest_first": True,
        "refusal": {"policy": REFUSAL_POLICY, "patterns": REFUSED_METHOD_PATTERNS},
        "summary": (
            f"{len(methods)} method(s); {len([m for m in methods if m['usable']])} "
            f"usable now; weakest usable is "
            f"{min((m['assurance'] for m in methods if m['usable']), default='none')}"
        ),
    }


def _assurance_rank(level: str) -> int:
    return {"none": 0, "loa1": 1, "loa2": 2, "loa3": 3}.get(level, 0)


def method_assurance(method: str) -> str:
    """The assurance a method establishes, or ``loa1`` for an unknown one.

    Unknown methods floor to the weakest level rather than raising: a token
    claim naming a method this build has never heard of is a downgrade attempt,
    and the safe reading of it is "the weakest thing I know about".
    """
    return str(AUTH_METHOD_BY_NAME.get(str(method), {}).get("assurance", "loa1"))


def resolve_login_method(
    requested: str,
    *,
    enrolled: Optional[set[str]] = None,
    recognised: bool = False,
) -> dict[str, Any]:
    """Decide whether a requested method may be used, and what it proves.

    Two gates, and the order matters:

    1. **Known?** An unrecognised method is refused. This is the gate that
       turns "log in by any means" into a closed vocabulary on purpose.
    2. **Enrolled?** A method requiring setup cannot be used by someone who
       has not done it. Without this check a passkey login would be accepted
       for a user who has never registered a passkey, which is worse than
       having no passkey support.

    ``recognised`` may substitute for enrolment of a *device* method on a
    device that is already known -- that is the one place recognition is
    allowed to change an outcome, and it substitutes for "you already proved
    this device", not for "you have proved anything now".
    """
    name = str(requested or "").strip().lower()
    spec = AUTH_METHOD_BY_NAME.get(name)
    if spec is None:
        return {
            "allowed": False,
            "method": name,
            "assurance": "none",
            "reason": f"{name!r} is not a login method this build offers",
            "suggested": list(BOOTSTRAP_METHODS),
        }

    if spec["enrollment_required"] and name not in (enrolled or set()):
        if not (recognised and name in {"trusted_device", "webauthn"}):
            return {
                "allowed": False,
                "method": name,
                "assurance": "none",
                "reason": (
                    f"{spec['label']} has not been set up on this account; "
                    f"enrol it first, or use {', '.join(BOOTSTRAP_METHODS)}"
                ),
                "suggested": list(BOOTSTRAP_METHODS),
            }

    return {
        "allowed": True,
        "method": name,
        "assurance": str(spec["assurance"]),
        "amr": list(spec["amr"]),
        "reason": f"{spec['label']} accepted; establishes {spec['assurance']}",
        "rate_tier": str(spec.get("rate_tier", "interactive")),
    }


# ---------------------------------------------------------------------------
# One-time codes
# ---------------------------------------------------------------------------


def generate_otp(length: int = OTP_CODE_LENGTH) -> str:
    """A numeric one-time code of the configured length.

    :func:`secrets.randbelow` rather than ``random``, because a code generated
    from a predictable PRNG is a code that can be predicted.
    """
    return "".join(str(secrets.randbelow(10)) for _ in range(max(4, int(length))))


def otp_expiry(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(minutes=OTP_TTL_MINUTES)


def otp_is_expired(issued_at: datetime, now: Optional[datetime] = None) -> bool:
    moment = now or datetime.now(timezone.utc)
    issued = issued_at
    if getattr(issued, "tzinfo", None) is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return moment > issued + timedelta(minutes=OTP_TTL_MINUTES)


def otp_attempts_remaining(attempts: int) -> int:
    return max(0, OTP_MAX_ATTEMPTS - max(0, int(attempts)))


def otp_is_spent(attempts: int) -> bool:
    return otp_attempts_remaining(attempts) <= 0


# ---------------------------------------------------------------------------
# Trusted-device tokens
# ---------------------------------------------------------------------------
# Opaque and random, stored hashed, exactly like a password. It is a bearer
# credential, which is the point -- but it is a bearer credential the user
# issued to a specific device after proving they could use that device, and it
# can be withdrawn.

TRUSTED_DEVICE_TOKEN_BYTES = 32
TRUSTED_DEVICE_DEFAULT_DAYS = 30
TRUSTED_DEVICE_MAX_DAYS = 365
#: After this many uses the token is re-issued, so a leaked one has a
#: bounded useful life even if nobody notices it was stolen.
TRUSTED_DEVICE_ROTATE_AFTER_USES = 50


def generate_trusted_device_token() -> str:
    return f"cdev_{secrets.token_urlsafe(TRUSTED_DEVICE_TOKEN_BYTES)}"


def hash_trusted_device_token(token: str) -> str:
    """Digest for storage. The token itself is never persisted."""
    from app.security import get_password_hash

    return get_password_hash(token)


def trusted_device_expiry(days: int = TRUSTED_DEVICE_DEFAULT_DAYS) -> datetime:
    span = max(1, min(int(days), TRUSTED_DEVICE_MAX_DAYS))
    return datetime.now(timezone.utc) + timedelta(days=span)


def trusted_device_needs_rotation(uses: int) -> bool:
    return int(uses or 0) >= TRUSTED_DEVICE_ROTATE_AFTER_USES


# ---------------------------------------------------------------------------
# Validation + catalog
# ---------------------------------------------------------------------------

AUTH_METHOD_CODES: dict[str, str] = {
    "duplicate_method": "two rows define the same method",
    "unknown_assurance": "a method declares an assurance the security layer does not define",
    "bootstrap_needs_enrolment": "a bootstrap method requires setup, so a locked-out account has no way back",
    "unrevocable_method": "a method a user cannot withdraw",
    "no_strong_method": "no method establishes more than a password",
    "otp_sizing": "the one-time code is too short to rate-limit into safety",
    "orphan_amr": "an amr value is not one the assurance derivation recognises",
}

def _known_amr() -> tuple[str, ...]:
    """Every ``amr`` value the assurance derivation actually reads.

    Read from ``app.security.STEP_UP_SPEC`` rather than restated here, because
    a second list is a second thing to forget to update -- which is exactly how
    ``hwk`` ended up declared as loa2 evidence in one place and unknown in
    another.
    """
    from app.security import STEP_UP_SPEC

    values: set[str] = set()
    for spec in STEP_UP_SPEC.values():
        values.update(spec.get("evidence") or ())
    return tuple(sorted(values))


#: Values the assurance derivation recognises, resolved at call time.
KNOWN_AMR: tuple[str, ...] = _known_amr()


def validate_auth_methods() -> dict[str, Any]:
    """Check the shipped method table against the constraints the code relies on.

    :code:`no_strong_method` is the one that matters: a deployment where every
    available method is a password is a deployment that has shipped a login
    form and no authentication, and that should be a loud finding rather than
    something a reader has to notice by looking at the assurance column.
    """
    findings: list[dict[str, Any]] = []
    names = [str(row["method"]) for row in AUTH_METHODS]
    for name in sorted({n for n in names if names.count(n) > 1}):
        findings.append(
            {
                "severity": "error",
                "code": "duplicate_method",
                "method": name,
                "detail": "declared more than once",
            }
        )

    try:
        from app.security import STEP_UP_RANK, STEP_UP_SPEC
    except Exception:  # pragma: no cover - import guard only
        STEP_UP_RANK = {}
        STEP_UP_SPEC = {}
    for row in AUTH_METHODS:
        name = str(row["method"])
        if STEP_UP_RANK and str(row["assurance"]) not in STEP_UP_RANK:
            findings.append(
                {
                    "severity": "error",
                    "code": "unknown_assurance",
                    "method": name,
                    "detail": f"declares {row['assurance']!r}, which the security layer does not define",
                }
            )
        if not row.get("revocable", False):
            findings.append(
                {
                    "severity": "error",
                    "code": "unrevocable_method",
                    "method": name,
                    "detail": "a method a user cannot withdraw is a method they cannot leave",
                }
            )
        for evidence in row.get("amr", []):
            if evidence not in KNOWN_AMR:
                findings.append(
                    {
                        "severity": "error",
                        "code": "orphan_amr",
                        "method": name,
                        "detail": (
                            f"claims amr={evidence!r}, which the assurance derivation "
                            "does not read as evidence"
                        ),
                    }
                )

    for name in BOOTSTRAP_METHODS:
        spec = AUTH_METHOD_BY_NAME.get(str(name))
        if spec is not None and spec.get("enrollment_required"):
            findings.append(
                {
                    "severity": "error",
                    "code": "bootstrap_needs_enrolment",
                    "method": name,
                    "detail": "cannot bootstrap without setup, so a locked-out account has no way in",
                }
            )

    strongest = max((_assurance_rank(str(r["assurance"])) for r in AUTH_METHODS), default=0)
    if strongest <= 1:
        findings.append(
            {
                "severity": "error",
                "code": "no_strong_method",
                "detail": (
                    "no method establishes more than a password; a login form with "
                    "no factor is not authentication"
                ),
            }
        )

    if OTP_CODE_LENGTH < 6:
        findings.append(
            {
                "severity": "error",
                "code": "otp_sizing",
                "detail": (
                    f"a {OTP_CODE_LENGTH}-digit code is too small to be made safe by "
                    "rate limiting alone"
                ),
            }
        )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc),
        "version": AUTH_METHODS_VERSION,
        "methods": len(AUTH_METHODS),
        "strongest_assurance": {0: "none", 1: "loa1", 2: "loa2", 3: "loa3"}.get(
            strongest, "unknown"
        ),
        "bootstrap_methods": list(BOOTSTRAP_METHODS),
        "otp": {
            "length": OTP_CODE_LENGTH,
            "ttl_minutes": OTP_TTL_MINUTES,
            "max_attempts": OTP_MAX_ATTEMPTS,
            "issues_per_window": OTP_MAX_ISSUES_PER_WINDOW,
            "window_minutes": OTP_ISSUANCE_WINDOW_MINUTES,
        },
        "trusted_device": {
            "default_days": TRUSTED_DEVICE_DEFAULT_DAYS,
            "max_days": TRUSTED_DEVICE_MAX_DAYS,
            "rotate_after_uses": TRUSTED_DEVICE_ROTATE_AFTER_USES,
            "enabled_by_default": False,
        },
        "codes": dict(AUTH_METHOD_CODES),
        "findings": findings,
        "counts_by_severity": counts,
        "ok": counts.get("error", 0) == 0,
        "refusal": {"policy": REFUSAL_POLICY, "patterns": REFUSED_METHOD_PATTERNS},
    }


def build_auth_methods_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``."""
    return {
        "version": AUTH_METHODS_VERSION,
        "methods": [dict(row) for row in AUTH_METHODS],
        "bootstrap_methods": list(BOOTSTRAP_METHODS),
        "known_amr": list(KNOWN_AMR),
        "otp": {
            "length": OTP_CODE_LENGTH,
            "ttl_minutes": OTP_TTL_MINUTES,
            "max_attempts": OTP_MAX_ATTEMPTS,
        },
        "trusted_device": {
            "default_days": TRUSTED_DEVICE_DEFAULT_DAYS,
            "max_days": TRUSTED_DEVICE_MAX_DAYS,
            "rotate_after_uses": TRUSTED_DEVICE_ROTATE_AFTER_USES,
        },
        "refused": [dict(row) for row in REFUSED_METHOD_PATTERNS],
        "refusal_policy": REFUSAL_POLICY,
    }
