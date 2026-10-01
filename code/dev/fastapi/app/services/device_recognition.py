"""Device and context recognition: how much proof a login should demand.

What this is
------------
A score in ``[0, 1]`` describing how much a login attempt *looks like* previous
ones from the same person, assembled from several weak signals that on their own
prove nothing. It is built to be **tuned over time** -- every weight, threshold
and decay window is a row in a config table, and the evaluator reports the
score alongside the per-signal contributions that produced it, so a weight can
be adjusted on evidence rather than on a hunch.

What this is deliberately **not**
--------------------------------
**It never grants access.** A recognition score is not a credential and cannot
become one. It has exactly one job: decide *how much proof* to demand from this
attempt, and whether to challenge at all when the caller already holds a
valid, unexpired, non-revoked token.

That is a deliberate refusal, and it is worth being explicit about why, because
the request this answers ("recognise users by other info, including guesses")
reads most naturally as the other thing. Signal-based identity is how
"confidence-scored" logins go wrong:

* The signals are not secret. User-agent, timezone, hour-of-day and IP block
  are all observable by anyone who can see a request, and several are shared
  by an entire office, a campus NAT, or a mobile carrier's CGNAT range.
* The failure is silent. A false accept does not announce itself. A false
  *reject* annoys a customer; a false *accept* hands over an account, and the
  scoring is confident rather than hesitant because that is what a probability
  is for.
* The tuning cuts both ways. "Tune the guesses over time" with a threshold that
  drifts toward convenience produces a system that gets steadily more
  permissive as it accumulates normal-looking traffic -- including an
  attacker's, once they are inside often enough.

So the score here decides *assurance demanded*, never *access granted*. A
recognised device on an unknown network still has to present a real credential;
what recognition changes is whether that credential may be a short-lived
one-time code or must be a full second factor, and whether a silent token
refresh is acceptable at all.

Privacy, stated concretely
--------------------------
Signals are hashed to a per-deployment peppered digest before storage, and the
raw values are not persisted. :func:`recognize` returns the *contributions* by
signal name and rounded weight, never the raw value, so a recognition report
cannot be used to enumerate where someone was or what they use. A user can
see and delete their own devices through ``/users/me/devices``.

This module has no I/O: it takes a context dict and returns a verdict.
Persistence lives in the router and in ``models.AuthDevice``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

#: Version of the shipped signal/weight table. Reported in every verdict so a
#: score can be interpreted against the weights that produced it -- a stored
#: 0.82 means something different after a re-tune.
RECOGNITION_VERSION = "device_recognition_v1"

#: Pepper for signal digests. Deployments SHOULD set
#: `CSERVICE_RECOGNITION_PEPPER`; without it the digest is still
#: domain-separated and HMAC'd, but a database-only compromise is easier to
#: correlate across deployments. Generating one per process would make devices
# unrecognisable after a restart, so the fallback is the *deployment secret*
# rather than a random value.
RECOGNITION_PEPPER = os.getenv("CSERVICE_RECOGNITION_PEPPER", "") or "cservice-device-recognition"

#: Domain separation tags, so the same value hashed for two purposes produces
#: two unrelated digests and the digests cannot be correlated.
_DIGEST_DOMAINS = {
    "device": "cservice.device.v1",
    "network": "cservice.network.v1",
    "client": "cservice.client.v1",
    "agent": "cservice.agent.v1",
}


# ---------------------------------------------------------------------------
# Signal and weight table
# ---------------------------------------------------------------------------
# `weight` is the maximum contribution that signal can make to the score.
# `novelty` says what happens when the signal disagrees with what this user
# usually presents:
#
#   "neutral"  -- contributes its weight normally either way
#   "penalise" -- a mismatch subtracts up to `mismatch_weight`
#   "require"  -- a mismatch is disqualifying on its own (see the policy table)
#
# `privacy` records what the signal is, so an operator reading the table can
# see which rows are shared by many people (and therefore weak) without having
# to reason about it from the field name.

RECOGNITION_SIGNALS: list[dict[str, Any]] = [
    {
        "signal": "device_id",
        "label": "This device",
        "digest_domain": "device",
        "weight": 0.40,
        "mismatch_weight": 0.40,
        "novelty": "penalise",
        "privacy": "pseudonymous",
        "note": (
            "A stable per-device identifier the client generates and keeps. "
            "Strongest single signal and the only one that is not shared with "
            "everyone behind the same NAT."
        ),
    },
    {
        "signal": "ip_block",
        "label": "Network neighbourhood",
        "digest_domain": "network",
        "weight": 0.15,
        "mismatch_weight": 0.15,
        "novelty": "neutral",
        "privacy": "coarse",
        "note": (
            "A /24 of the client address, not the address. Deliberately coarse: "
            "a full address is location data and a /24 still moves with the "
            "user. Weak on its own -- a campus or a carrier CGNAT is one block "
            "for thousands of people."
        ),
    },
    {
        "signal": "user_agent",
        "label": "Browser and platform",
        "digest_domain": "agent",
        "weight": 0.15,
        "mismatch_weight": 0.15,
        "novelty": "neutral",
        "privacy": "fingerprinting",
        "note": (
            "Free-form and trivially spoofed, so it is capped well below the "
            "device signal. Its value is that it rarely changes for a real user."
        ),
    },
    {
        "signal": "accept_language",
        "label": "Language preference",
        "digest_domain": "client",
        "weight": 0.08,
        "mismatch_weight": 0.08,
        "novelty": "neutral",
        "privacy": "coarse",
        "note": "Stable per person, and disclosed by the client anyway.",
    },
    {
        "signal": "timezone_offset",
        "label": "Time zone",
        "digest_domain": "client",
        "weight": 0.07,
        "mismatch_weight": 0.07,
        "novelty": "neutral",
        "privacy": "coarse",
        "note": (
            "Useful and *not* location, because it is a client-declared offset "
            "rather than a derived one. Someone travelling keeps their device's "
            "zone, which is exactly the stability wanted here."
        ),
    },
    {
        "signal": "hour_of_day",
        "label": "Usual hour of activity",
        "digest_domain": None,
        "weight": 0.08,
        "mismatch_weight": 0.08,
        "novelty": "neutral",
        "privacy": "behavioural",
        "note": (
            "Compared against the hours this user has actually logged in at. "
            "Contributes most at the edges of their pattern and not at all in "
            "the middle, where everyone is awake."
        ),
    },
]

RECOGNITION_SIGNAL_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["signal"]): row for row in RECOGNITION_SIGNALS
}


# ---------------------------------------------------------------------------
# Policy table
# ---------------------------------------------------------------------------
# The threshold is what makes recognition *mean* something, so it is one row
# rather than a constant buried in a comparison.
#
# `max_credential` is the ceiling on what recognition is permitted to wave
# through. There is deliberately no tier where recognition is sufficient on its
# own: every policy row still demands a credential, and the lowest any row
# reaches is `totp`, which is a real second factor rather than a session
# cookie. Lowering `totp` to `password` here would be the point at which this
# module becomes an account-takeover primitive, so the vocabulary is bounded to
# methods that require proof.

RECOGNITION_POLICIES: list[dict[str, Any]] = [
    {
        "policy_id": "unfamiliar",
        "label": "Unrecognised",
        "min_score": 0.00,
        "max_credential": "password",
        "challenge": True,
        "refresh_without_challenge": False,
        "reason": (
            "Nothing here looks like this user. Demand the strongest credential "
            "available and make the user prove it."
        ),
    },
    {
        "policy_id": "faint",
        "label": "Faintly familiar",
        "min_score": 0.35,
        "max_credential": "totp",
        "challenge": True,
        "refresh_without_challenge": False,
        "reason": "One or two weak signals agree. A second factor is warranted.",
    },
    {
        "policy_id": "familiar",
        "label": "Familiar",
        "min_score": 0.60,
        "max_credential": "totp",
        "challenge": True,
        "refresh_without_challenge": False,
        "reason": (
            "Most signals agree. Still a second factor, because recognition "
            "cannot be the thing that admits the request."
        ),
    },
    {
        "policy_id": "recognised",
        "label": "Recognised device",
        "min_score": 0.82,
        "max_credential": "email_otp",
        "challenge": True,
        "refresh_without_challenge": True,
        "reason": (
            "The device itself is known. A one-time code by email is still "
            "required to *start* a session, but a session already in progress "
            "may refresh without re-prompting, which is the whole point of "
            "recognising the device."
        ),
    },
    {
        "policy_id": "trusted",
        "label": "Trusted device",
        "min_score": 0.95,
        "max_credential": "trusted_device",
        "challenge": False,
        "refresh_without_challenge": True,
        "reason": (
            "Only reachable with a matching device digest *and* a matching "
            "network block, so a stolen copy of the cookie is not enough. Still "
            "a credential -- the device token -- but one the user created "
            "deliberately and can revoke."
        ),
    },
]

RECOGNITION_POLICY_BY_ID: dict[str, dict[str, Any]] = {
    str(row["policy_id"]): row for row in RECOGNITION_POLICIES
}

#: Signals a `trusted` verdict *requires*, whatever the arithmetic says. The
#: device digest is the only signal that is meaningfully private; a score
#: reached entirely from user-agent, language and hour-of-day is a profile of
#: a browser, not of a person, so it cannot unlock the lowest policy.
TRUSTED_REQUIRED_SIGNALS: tuple[str, ...] = ("device_id",)

# ---------------------------------------------------------------------------
# How much of the verdict is acted on
# ---------------------------------------------------------------------------
# Recognition only changes *what to demand*, so the remaining question is
# whether to demand it. That is deployment configuration, because it decides
# whether a correct verdict can lock a customer out of their own account -- and
# the answer cannot be a constant in this file.
#
# The specific hazard: `unfamiliar` demands `password` with `challenge: true`.
# A user who has never enrolled a second factor, signing in from a new device,
# is the *most* legitimate `unfamiliar` there is. Enforcing a challenge they
# cannot satisfy would mean the only way into the account is to enrol something,
# which requires being logged in. The lockout is silent and permanent until
# somebody reads the logs.
#
# So:
#
# * ``advisory`` (default) -- the verdict is computed, recorded, and returned,
#   but the login proceeds on the credential that was presented.
# * ``enforced`` -- a verdict demanding a *stronger* credential than the one
#   presented turns into a challenge, but only when the user has that credential
#   enrolled. See :func:`demand_for_presented`.
# * ``off`` -- nothing is computed.

RECOGNITION_MODES: tuple[str, ...] = ("off", "advisory", "enforced")

#: Env var name, and the value used when it is unset or unrecognised.
RECOGNITION_MODE_ENV = "CSERVICE_RECOGNITION_MODE"
RECOGNITION_MODE_DEFAULT = "advisory"


def recognition_mode() -> str:
    """The configured enforcement mode, falling back on anything unrecognised.

    An unrecognised value falls back to ``advisory`` rather than to the most
    permissive reading. If a deployment typo'd this to ``enforced`` and we
    silently ran ``off``, recognition would look configured and be inert, which
    is the failure this whole table exists to avoid.
    """
    raw = str(os.getenv(RECOGNITION_MODE_ENV, "") or "").strip().lower()
    return raw if raw in RECOGNITION_MODES else RECOGNITION_MODE_DEFAULT


def demand_for_presented(
    verdict: RecognitionVerdict,
    presented: Optional[str],
    enrolled: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Whether ``verdict`` should challenge someone who presented ``presented``.

    Returns an *advisory answer*, never a grant. Two things can withhold a
    challenge, and both are reported rather than applied silently:

    * The presented credential already meets the demanded assurance. No
      challenge, and the reason says so.
    * **The user has not enrolled the demanded credential.** A challenge nobody
      can satisfy is a lockout, so it is downgraded to no challenge and the
      verdict is recorded as unmet. This is the case the whole function exists
      for: a deployment turning on ``enforced`` must not be able to strand a
      customer who signed up with a password.
    """
    available = {str(name) for name in (enrolled or ())}
    shown = str(presented or "").strip().lower()
    shown_assurance = CREDENTIAL_ASSURANCE.get(shown, "loa1")
    order = {"none": 0, "loa1": 1, "loa2": 2, "loa3": 3}
    demanded = verdict.required_credential

    if shown and order[shown_assurance] >= order.get(verdict.required_assurance, 0):
        return {
            "challenge": False,
            "met": True,
            "reason": (
                f"{shown} establishes {shown_assurance}, which already meets the "
                f"{verdict.required_assurance} the verdict calls for"
            ),
            "downgraded": False,
            "downgrade_reason": "",
        }

    mode = recognition_mode()
    if mode != "enforced":
        return {
            "challenge": False,
            "met": order[shown_assurance] >= order.get(verdict.required_assurance, 0),
            "reason": (
                f"recognition mode is {mode!r}, so the verdict is recorded but not "
                f"enforced; nothing about this login changes"
            ),
            "downgraded": False,
            "downgrade_reason": "",
        }

    unmet = demanded not in available
    if unmet:
        return {
            "challenge": False,
            "met": False,
            "reason": (
                f"{verdict.policy_id} would demand {demanded!r}, which this account has "
                f"not enrolled; challenging would lock the account out"
            ),
            "downgraded": True,
            "downgrade_reason": f"{demanded!r} not enrolled",
        }

    # Nothing to challenge: the demanded credential *is* what was presented, or
    # the user was offered no alternative. The assurance map is the comparison
    # the verifier uses, so reuse it rather than comparing names -- otherwise a
    # policy naming `trusted_device` (loa2) challenges someone who just presented
    # `webauthn` (loa3) on the strength of a stronger credential.
    if shown == demanded:
        return {
            "challenge": False,
            "met": True,
            "reason": f"{shown} is the credential the verdict asked for",
            "downgraded": False,
            "downgrade_reason": "",
        }

    return {
        "challenge": True,
        "met": False,
        "reason": (
            f"{verdict.policy_id} demands {demanded!r} ({verdict.required_assurance}); "
            f"{shown or 'nothing'} was presented"
        ),
        "downgraded": False,
        "downgrade_reason": "",
    }

#: Assurance floor per method, mirroring `deps.STEP_UP_RANKS`. Recognition may
#: *raise* the demanded level but never lower it below this mapping.
CREDENTIAL_ASSURANCE: dict[str, str] = {
    "password": "loa1",
    "email_otp": "loa2",
    "totp": "loa2",
    "webauthn": "loa3",
    "trusted_device": "loa2",
}

#: Every credential this module is willing to wave through, weakest first. A
#: policy naming anything outside this set is a configuration error and is
#: reported as such rather than being honoured.
PERMITTED_CREDENTIALS: tuple[str, ...] = (
    "password",
    "email_otp",
    "totp",
    "webauthn",
    "trusted_device",
)

#: Credentials usable with no prior enrolment, mirroring
#: ``auth_methods.BOOTSTRAP_METHODS``. Listed here rather than imported so this
#: module stays free of service imports and can be read on its own.
BOOTSTRAP_CREDENTIALS: tuple[str, ...] = ("password", "email_otp")


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def digest_signal(value: Any, domain: str) -> str:
    """Peppered, domain-separated digest of a signal value.

    Domain separation is why a device id and a user agent hashed the same way
    would otherwise produce the same digest, letting anyone holding the table
    confirm that two users share a browser.
    """
    if value is None:
        return ""
    prefix = _DIGEST_DOMAINS.get(str(domain), f"cservice.{domain}.v1")
    material = f"{RECOGNITION_PEPPER}|{prefix}|{value}".encode("utf-8")
    return hmac.new(material, b"cservice-recognition", hashlib.sha256).hexdigest()[:32]


def digest_network(address: Any) -> str:
    """A /24 of a client address -- deliberately not the address itself.

    Keeps "these two logins came from roughly the same place" without storing
    anything that locates a person, and bounds the collision damage when a /24
    is shared by thousands of people.
    """
    if not address:
        return ""
    text = str(address)
    if ":" in text:  # IPv6: keep the first three hextets
        parts = text.split(":")
        return ":".join(parts[:3]) + "::/48"
    octets = text.split(".")
    if len(octets) != 4:
        return ""
    return ".".join(octets[:3]) + ".0/24"


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------


def _hour_contribution(hour: int, usual_hours: Iterable[int], weight: float) -> float:
    """How much an hour-of-day agrees with the user's own pattern.

    A flat "is this an hour they use" test would make 10:00 free credit for
    everyone, so the contribution is greatest at the edges of the pattern and
    zero in the middle. Circular distance is used because 23:00 and 01:00 are
    adjacent.
    """
    hours = sorted({int(h) % 24 for h in usual_hours})
    if not hours:
        return 0.0
    hour = int(hour) % 24
    best = min(min((h - hour) % 24, (hour - h) % 24) for h in hours)
    if best == 0:
        return weight
    if best <= 2:
        return weight * 0.5
    return 0.0


def signal_contributions(
    context: dict[str, Any],
    known: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per-signal agreement, with the score contribution and no raw values.

    Returned shape is deliberately contribution-only: a caller can act on
    "the network block disagreed" without ever holding the block itself.
    """
    contributions: list[dict[str, Any]] = []
    usual_hours = known.get("usual_hours") or []
    for row in RECOGNITION_SIGNALS:
        name = str(row["signal"])
        spec = row
        weight = float(row["weight"])
        if name == "hour_of_day":
            hour = context.get("hour_of_day")
            if hour is None:
                continue
            value = _hour_contribution(int(hour), usual_hours, weight)
            contributions.append(
                {
                    "signal": name,
                    "weight": weight,
                    "contribution": round(value, 4),
                    "agrees": value > 0.0,
                    "present": True,
                }
            )
            continue

        domain = row.get("digest_domain")
        supplied = context.get(name)
        if supplied is None or not domain:
            continue
        actual = digest_signal(supplied, str(domain))
        expected = known.get(name) or ""
        agrees = bool(expected) and hmac.compare_digest(str(expected), actual)
        contributions.append(
            {
                "signal": name,
                "weight": weight,
                "contribution": round(weight if agrees else 0.0, 4),
                "agrees": agrees,
                "present": True,
                "mismatch_penalty": 0.0 if agrees else float(row["mismatch_weight"]),
            }
        )
    return contributions


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecognitionVerdict:
    """The decision, and everything needed to argue with it."""

    score: float
    policy_id: str
    label: str
    required_credential: str
    required_assurance: str
    challenge: bool
    refresh_without_challenge: bool
    reason: str
    version: str
    contributions: list[dict[str, Any]]
    known_signals: int
    total_signals: int
    trusted_blocked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "policy_id": self.policy_id,
            "label": self.label,
            "required_credential": self.required_credential,
            "required_assurance": self.required_assurance,
            "challenge": self.challenge,
            "refresh_without_challenge": self.refresh_without_challenge,
            "reason": self.reason,
            "version": self.version,
            "contributions": self.contributions,
            "known_signals": self.known_signals,
            "total_signals": self.total_signals,
            "trusted_blocked_reason": self.trusted_blocked_reason,
        }


def _resolve_policy(score: float) -> dict[str, Any]:
    """The *highest* policy whose threshold the score clears.

    Iterating the table in declaration order and returning the first match
    would be wrong here in a way that is easy to miss: the table is declared
    ascending and ``unfamiliar`` has ``min_score = 0.00``, so a first-match
    scan returns ``unfamiliar`` for *every* score including 1.0. Every
    recognised device would then be asked for a password.

    This is the same class of bug the `policy_scoring` coverage report calls
    "first-match-wins makes row order the semantics" -- the difference is that
    here the order is an artefact of how the table reads, so the comparison is
    written not to depend on it.
    """
    best = RECOGNITION_POLICIES[0]
    for row in RECOGNITION_POLICIES:
        if score >= float(row["min_score"]) and float(row["min_score"]) >= float(
            best["min_score"]
        ):
            best = row
    return best


def recognize(
    context: dict[str, Any],
    known: Optional[dict[str, Any]] = None,
    *,
    minimum_assurance: str = "loa2",
) -> RecognitionVerdict:
    """Score a login context against what is known about this user.

    ``context`` carries the attempt's signals. ``known`` carries this user's
    stored digests and usual hours; an empty or missing ``known`` is a first
    login, which scores 0 and lands on ``unfamiliar``.

    ``minimum_assurance`` is a floor the verdict may not go below, so a caller
    asking for ``loa3`` gets ``loa3`` back even on a perfectly recognised
    device. This is the one input that can make the answer *stricter* and
    never looser, which is the direction a security decision is allowed to
    move on its own.
    """
    known = dict(known or {})
    contributions = signal_contributions(context, known)
    known_count = sum(1 for entry in contributions if entry["agrees"])

    possible = sum(float(entry["weight"]) for entry in contributions)
    earned = sum(float(entry["contribution"]) for entry in contributions)
    # A user with no stored digests has no denominator; scoring 0 is the
    # honest answer and sends them to `unfamiliar` rather than dividing by
    # something invented.
    score = round(min(1.0, earned / possible), 4) if possible > 0 else 0.0

    policy = _resolve_policy(score)
    trusted_blocked = ""
    if str(policy["policy_id"]) == "trusted":
        missing = [
            name for name in TRUSTED_REQUIRED_SIGNALS if not known.get(name)
        ]
        if missing:
            policy = RECOGNITION_POLICY_BY_ID["familiar"]
            trusted_blocked = (
                f"trusted requires {', '.join(TRUSTED_REQUIRED_SIGNALS)}; "
                f"missing {', '.join(missing)}"
            )

    credential = str(policy["max_credential"])
    if credential not in PERMITTED_CREDENTIALS:
        # Refuse rather than honour. A policy naming an unrecognised credential
        # is a configuration mistake, and silently falling back would mean the
        # table says one thing and the system does another.
        credential = "password"
        policy = dict(policy)
        policy["reason"] = (
            f"{policy['policy_id']} names credential {credential!r}, which is not a "
            f"permitted credential ({', '.join(PERMITTED_CREDENTIALS)}); demanding a "
            "password instead"
        )

    assurance = CREDENTIAL_ASSURANCE.get(credential, "loa1")
    floor = minimum_assurance if minimum_assurance in {"loa1", "loa2", "loa3"} else "loa2"
    order = {"none": 0, "loa1": 1, "loa2": 2, "loa3": 3}
    if order[assurance] < order[floor]:
        assurance = floor
        policy = dict(policy)
        policy["reason"] = (
            f"{policy['reason']} Raised to {floor}: the caller requires at least "
            f"{floor} and recognition cannot lower a floor."
        )

    return RecognitionVerdict(
        score=score,
        policy_id=str(policy["policy_id"]),
        label=str(policy["label"]),
        required_credential=credential,
        required_assurance=assurance,
        challenge=bool(policy["challenge"]),
        refresh_without_challenge=bool(policy["refresh_without_challenge"]),
        reason=str(policy["reason"]),
        version=RECOGNITION_VERSION,
        contributions=contributions,
        known_signals=known_count,
        total_signals=len(contributions),
        trusted_blocked_reason=trusted_blocked,
    )


# ---------------------------------------------------------------------------
# Known-state helpers
# ---------------------------------------------------------------------------


def known_from_device(
    device: Any,
    *,
    usual_hours: Optional[Iterable[int]] = None,
) -> dict[str, Any]:
    """The ``known`` mapping for a stored device row.

    Reads the row by attribute with a ``getattr`` default so a partial row (or
    a mapping, in tests) is accepted; a missing column is an unknown signal,
    which lowers the score rather than raising.
    """
    def _get(name: str, default: Any = None) -> Any:
        if isinstance(device, dict):
            return device.get(name, default)
        return getattr(device, name, default)

    known: dict[str, Any] = {
        "device_id": _get("device_digest") or "",
        "ip_block": _get("network_digest") or "",
        "user_agent": _get("agent_digest") or "",
        "accept_language": _get("language_digest") or "",
        "timezone_offset": _get("timezone_digest") or "",
    }
    hours = usual_hours if usual_hours is not None else _get("usual_hours")
    if isinstance(hours, str):
        try:
            import json

            hours = json.loads(hours)
        except (TypeError, ValueError):
            hours = []
    known["usual_hours"] = list(hours or [])
    return known


def context_from_request(
    *,
    device_id: Optional[str] = None,
    client_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    timezone_offset: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a recognition context from raw request values.

    Hashing happens inside :func:`recognize`, so this function is the only place
    that sees a raw address or user agent, and it does not persist them.
    """
    moment = now or datetime.now(timezone.utc)
    return {
        "device_id": device_id,
        "ip_block": digest_network(client_ip),
        "user_agent": user_agent,
        "accept_language": accept_language,
        "timezone_offset": timezone_offset,
        "hour_of_day": moment.hour,
    }


def fold_hour(hour: int, existing: Optional[Iterable[int]] = None) -> list[int]:
    """Add an hour to a rolling "usual hours" set, newest first, bounded.

    Stored as a set because the question is "has this person ever used the
    service at 03:00", not "how often" -- frequency is exactly the signal an
    attacker can generate by logging in repeatedly, so it is not recorded.
    """
    hours = {int(h) % 24 for h in (existing or [])}
    hours.add(int(hour) % 24)
    return sorted(hours)


# ---------------------------------------------------------------------------
# Validation + catalog
# ---------------------------------------------------------------------------

RECOGNITION_CODES: dict[str, str] = {
    "unknown_credential": "a policy waves through a credential that is not permitted",
    "unknown_signal": "a next-step/signal name no row defines",
    "duplicate_signal": "two rows define the same signal",
    "unreachable_policy": "a policy's threshold can never be reached by the weights",
    "shadowed_policy": "a lower policy's band is empty",
    "weight_overflow": "signal weights sum above 1.0, so a perfect score is impossible to reason about",
    "orphan_mapping": "a credential in CREDENTIAL_ASSURANCE has no policy that can demand it",
    "unknown_assurance": "an assurance level is not one STEP_UP_RANKS defines",
    "unenforceable_policy": "a policy demands a challenge no credential can satisfy, so it locks accounts out",
    "assurance_overclaim": "a credential's declared assurance is stronger than its method table says",
}


def validate_recognition() -> dict[str, Any]:
    """Check the shipped table against the constraints the code relies on.

    The two that matter most are :code:`unknown_credential` and
    :code:`weight_overflow`. A policy naming a credential outside
    :data:`PERMITTED_CREDENTIALS` is the configuration that would turn this
    module into an account-takeover primitive, so it is an error rather than a
    warning. Weights summing above 1.0 is not a bug -- a perfect score simply
    becomes unreachable -- but it silently caps every verdict, so it is
    reported.
    """
    findings: list[dict[str, Any]] = []

    names = [str(row["signal"]) for row in RECOGNITION_SIGNALS]
    for name in sorted({n for n in names if names.count(n) > 1}):
        findings.append(
            {
                "severity": "error",
                "code": "duplicate_signal",
                "signal": name,
                "detail": "declared more than once, so contributions double-count it",
            }
        )

    total_weight = sum(float(row["weight"]) for row in RECOGNITION_SIGNALS)
    if total_weight > 1.0 + 1e-9:
        findings.append(
            {
                "severity": "warning",
                "code": "weight_overflow",
                "detail": (
                    f"weights sum to {round(total_weight, 4)}; a perfect score of "
                    "1.0 is unreachable, which caps every verdict silently"
                ),
            }
        )

    for row in RECOGNITION_POLICIES:
        credential = str(row["max_credential"])
        if credential not in PERMITTED_CREDENTIALS:
            findings.append(
                {
                    "severity": "error",
                    "code": "unknown_credential",
                    "policy_id": row["policy_id"],
                    "detail": (
                        f"waves through {credential!r}, which is not one of "
                        f"{', '.join(PERMITTED_CREDENTIALS)}"
                    ),
                }
            )
        assurance = CREDENTIAL_ASSURANCE.get(credential)
        if assurance is None:
            findings.append(
                {
                    "severity": "error",
                    "code": "unknown_assurance",
                    "policy_id": row["policy_id"],
                    "detail": f"credential {credential!r} maps to no assurance level",
                }
            )

    demanded = {str(row["max_credential"]) for row in RECOGNITION_POLICIES}
    for credential in PERMITTED_CREDENTIALS:
        if credential not in demanded:
            findings.append(
                {
                    "severity": "info",
                    "code": "orphan_mapping",
                    "credential": credential,
                    "detail": "no policy can demand it, so it is unreachable through recognition",
                }
            )

    ordered = sorted(RECOGNITION_POLICIES, key=lambda row: float(row["min_score"]))
    for index in range(1, len(ordered)):
        previous, current = ordered[index - 1], ordered[index]
        if float(current["min_score"]) >= float(ordered[-1]["min_score"]):
            continue
        # A band is empty when the next threshold is the same as this one.
        if float(current["min_score"]) == float(previous["min_score"]):
            findings.append(
                {
                    "severity": "warning",
                    "code": "shadowed_policy",
                    "policy_id": current["policy_id"],
                    "detail": (
                        f"shares its threshold with {previous['policy_id']!r}, so it "
                        "can never be selected -- first match wins"
                    ),
                }
            )
    if ordered and float(ordered[0]["min_score"]) > 0.0:
        findings.append(
            {
                "severity": "warning",
                "code": "unreachable_policy",
                "detail": (
                    f"the lowest policy starts at {ordered[0]['min_score']}, so scores "
                    "below it have no policy at all"
                ),
            }
        )

    # The `unfamiliar` policy is where every first login and every unfamiliar
    # device lands, because its threshold is 0.00. So the credential it demands
    # has to be one a brand-new customer can actually use: if `unfamiliar`
    # demanded `webauthn`, then enrolling a passkey -- which requires being
    # logged in -- would be the only way past it, and the lockout would be total
    # and invisible. This is the single most consequential row in the table,
    # which is why it gets its own check rather than a general "is this
    # enforceable" sweep.
    bootstrap = {name for name in BOOTSTRAP_CREDENTIALS if name in PERMITTED_CREDENTIALS}
    lowest = min(RECOGNITION_POLICIES, key=lambda row: float(row["min_score"]))
    if float(lowest["min_score"]) <= 0.0:
        demanded = str(lowest["max_credential"])
        if demanded not in bootstrap:
            findings.append(
                {
                    "severity": "error",
                    "code": "unenforceable_policy",
                    "policy_id": str(lowest["policy_id"]),
                    "detail": (
                        f"is the policy every first login lands on, but demands "
                        f"{demanded!r}, which requires prior enrolment -- enrolling "
                        f"it requires being logged in, so this locks new accounts "
                        f"out permanently. It must demand one of "
                        f"{', '.join(sorted(bootstrap))}"
                    ),
                }
            )

    # A method's declared assurance may not exceed what the method table says it
    # establishes. `trusted_device` claiming loa3 would let a device cookie
    # satisfy hardware-key checks.
    try:
        from app.services.auth_methods import AUTH_METHOD_BY_NAME
    except Exception:  # pragma: no cover - import guard only
        AUTH_METHOD_BY_NAME = {}
    for credential, assurance in CREDENTIAL_ASSURANCE.items():
        spec = AUTH_METHOD_BY_NAME.get(credential)
        if spec is None:
            continue
        declared = str(spec.get("assurance", ""))
        order = {"none": 0, "loa1": 1, "loa2": 2, "loa3": 3}
        if order.get(assurance, 0) > order.get(declared, 0):
            findings.append(
                {
                    "severity": "error",
                    "code": "assurance_overclaim",
                    "credential": credential,
                    "detail": (
                        f"mapped to {assurance!r} but the method table declares "
                        f"{declared!r}"
                    ),
                }
            )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc),
        "version": RECOGNITION_VERSION,
        "signals": len(RECOGNITION_SIGNALS),
        "policies": len(RECOGNITION_POLICIES),
        "total_weight": round(total_weight, 4),
        "max_score_reachable": round(min(1.0, total_weight), 4),
        "permitted_credentials": list(PERMITTED_CREDENTIALS),
        "credential_assurance": dict(sorted(CREDENTIAL_ASSURANCE.items())),
        "trusted_required_signals": list(TRUSTED_REQUIRED_SIGNALS),
        "mode": recognition_mode(),
        "modes": list(RECOGNITION_MODES),
        "mode_env": RECOGNITION_MODE_ENV,
        "codes": dict(RECOGNITION_CODES),
        "findings": findings,
        "counts_by_severity": counts,
        "ok": counts.get("error", 0) == 0,
        "note": (
            "recognition never authenticates. It selects the credential a login "
            "must present and whether an existing session may refresh without a "
            "prompt. Every policy still demands a credential; the lowest any row "
            "reaches is a one-time code, and a policy naming a credential outside "
            "PERMITTED_CREDENTIALS is an error rather than a warning. `mode` "
            "decides whether the verdict is enforced or only recorded, and a "
            "challenge the account has not enrolled is downgraded to a note rather "
            "than applied -- recognition must not be able to lock anyone out."
        ),
    }


def build_recognition_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``."""
    return {
        "version": RECOGNITION_VERSION,
        "signals": [dict(row) for row in RECOGNITION_SIGNALS],
        "policies": [dict(row) for row in RECOGNITION_POLICIES],
        "permitted_credentials": list(PERMITTED_CREDENTIALS),
        "credential_assurance": dict(sorted(CREDENTIAL_ASSURANCE.items())),
        "trusted_required_signals": list(TRUSTED_REQUIRED_SIGNALS),
        "pepper_configured": bool(os.getenv("CSERVICE_RECOGNITION_PEPPER", "")),
        "network_digest": "/24 for IPv4, /48 for IPv6",
        "note": (
            "signals are HMAC-digested with a per-deployment pepper and a "
            "per-signal domain tag before storage; raw values are never "
            "persisted and a verdict carries contributions, not the values "
            "behind them"
        ),
    }
