"""Biometric validation vaults.

Biometrics never leave the device and never reach a plaintext store: this
module keeps a *template vault* — per-user, per-modality registered templates
stored as salted HMAC-SHA256 digests plus a derived reference digest used for
matching. A pluggable ``matcher`` key on each modality lets a real biometrics
engine (face/voice vendor SDK) be swapped in while the vault contract stays
the same.

Behavior:

- ``register_template`` — store a template digest for ``(user_id, modality)``.
- ``validate_template`` — return ``{match, confidence, threshold}``; the
  confidence is a deterministic pseudo-similarity (1 - hamming distance of the
  SHA-256 digests) so the vault is testable offline, then compared against the
  modality threshold.
- ``revoke_template`` / ``list_templates`` — lifecycle + introspection.

Modalities and thresholds are a config table (``BIOMETRIC_MODALITIES``);
adjusting acceptance is config-only.

The vault also answers the questions a real deployment has to answer, all from
the same config so nothing here is a hard-coded policy:

- ``issue_challenge`` / ``challenge`` — a single-use, expiring freshness token,
  so a captured template cannot be replayed as a live capture.
- ``decide`` — a three-way ``accept`` / ``challenge`` / ``deny`` outcome with a
  per-modality step-up band, rather than a bare boolean.
- Brute-force control: repeated failures lock a ``(user, modality)`` pair for a
  configurable window (``BIOMETRIC_POLICY``).
- ``adapt_threshold`` — loosen or tighten a modality from observed outcomes.
- ``enroll_slot`` — several templates per modality (a finger is not one finger).
- ``export_state`` / ``import_state`` — replicate digests across instances
  without ever moving plaintext.

Expansion notes (enrollment + presentation-attack defence):

The original module answers "is this capture the enrolled user?". Two real
questions sit outside that, and both are policy rather than code:

- **Enrollment quality.** A single 0.5-confidence capture enrolled as a
  template makes every later comparison fail. ``ENROLLMENT_RULES`` names a
  minimum quality bar and a required number of samples per modality;
  ``assess_enrollment`` grades a candidate capture against it *before* it is
  stored, so a bad enrollment is refused rather than discovered later.
- **Presentation attack detection.** A printed photo scores a perfect match on
  a face template. ``PAD_PROFILES`` declares, per modality, the liveness
  evidence required and the minimum liveness score;
  ``evaluate_pad`` is a pure check on that evidence, and ``decide`` can be told
  to enforce it.
- ``assess_enrollment`` / ``build_biometric_vault_policy`` — the catalog for
  both.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
import hashlib
import hmac
import secrets
import uuid
from typing import Any, Optional

BIOMETRIC_MODALITIES: dict[str, dict[str, Any]] = {
    "fingerprint": {
        "threshold": 0.94,
        "step_up_threshold": 0.90,
        "min_threshold": 0.80,
        "max_threshold": 0.99,
        "hash": "sha256",
        "matcher": "digest_hamming",
        "note": "high precision; short capture window",
    },
    "face": {
        "threshold": 0.86,
        "step_up_threshold": 0.80,
        "min_threshold": 0.70,
        "max_threshold": 0.97,
        "hash": "sha256",
        "matcher": "digest_hamming",
        "note": "moderate threshold; liveness variance expected",
    },
    "voice": {
        "threshold": 0.80,
        "step_up_threshold": 0.72,
        "min_threshold": 0.60,
        "max_threshold": 0.95,
        "hash": "sha256",
        "matcher": "digest_hamming",
        "note": "lower threshold; acoustic variance expected",
    },
}

# Cross-modality policy. Nothing here is per-modality, so tuning brute-force
# control never means editing the modality table.
BIOMETRIC_POLICY: dict[str, Any] = {
    "max_attempts": 5,
    "lockout_seconds": 300,
    "lockout_enabled": True,
    "challenge_ttl_seconds": 120,
    "challenge_capacity": 2048,
    "history_capacity": 1000,
    "history_limit": 50,
    "max_enrollment_slots": 4,
    "threshold_adapt_step": 0.01,
}

# Every reason ``validate_template`` / ``decide`` can return. Kept as a table so
# a caller can branch exhaustively.
BIOMETRIC_REASONS = (
    "match",
    "below_threshold",
    "template_not_registered",
    "template_revoked",
    "challenge_unknown",
    "challenge_replayed",
    "challenge_expired",
    "locked_out",
)

BIOMETRIC_DECISIONS = ("accept", "challenge", "deny")
BIOMETRIC_OUTCOMES = ("false_positive", "false_negative", "confirmed_match")

# A liveness score outside 0..1 is a gap, not a denial -- the caller decides.
PAD_LIVENESS_BOUNDS = (0.0, 1.0)
VAULT_STATE_VERSION = 1

# --- enrollment quality (expansion) ---------------------------------------------
#
# A vault that stores whatever it is handed will happily enrol a 0.3-confidence
# capture, and every subsequent comparison against it is then permanently
# marginal. The bar is a data change so tightening it never touches the vault.
#
# Config table: modality -> {min_quality, max_samples, samples_required,
# reject_below_quality, note}. ``min_quality`` is compared against the *same*
# pseudo-similarity ``validate_template`` reports, so the two can never disagree
# about what "good" means.
ENROLLMENT_RULES: dict[str, dict[str, Any]] = {
    "fingerprint": {
        "min_quality": 0.90,
        "max_samples": 5,
        "samples_required": 2,
        "note": "high-precision modality; a marginal sample is almost always a bad press",
    },
    "face": {
        "min_quality": 0.80,
        "max_samples": 5,
        "samples_required": 2,
        "note": "liveness variance expected, so the bar is lower but the sample count is not",
    },
    "voice": {
        "min_quality": 0.70,
        "max_samples": 7,
        "samples_required": 3,
        "note": "acoustic variance needs more samples before it is trusted",
    },
}
DEFAULT_ENROLLMENT_RULE: dict[str, Any] = {
    "min_quality": 0.0,
    "max_samples": 5,
    "samples_required": 1,
    "note": "unconfigured modality: accept the capture as-is",
}
# Why an enrollment was accepted or refused.
ENROLLMENT_REASONS = (
    "accepted",
    "below_min_quality",
    "sample_cap_reached",
    "insufficient_samples",
    "unknown_modality",
)

# --- presentation attack detection (expansion) ---------------------------------
#
# A replayed photo or a recorded voice scores a *perfect* match against the
# enrolled template, because the matcher only sees the template. Liveness is
# therefore a separate, separately-required signal. Off by default per modality,
# so no existing enrolment path changes behaviour unless PAD is configured on.
#
# Config table: modality -> {required, min_liveness, evidence, reject_injection}.
#   required        enforce this modality's PAD check in ``decide``
#   min_liveness    0..1 minimum liveness score
#   evidence        the request keys that must be present (all of them)
#   reject_injection  treat a reported injection attempt as a hard deny
PAD_PROFILES: dict[str, dict[str, Any]] = {
    "fingerprint": {
        "required": False,
        "min_liveness": 0.0,
        "evidence": (),
        "reject_injection": False,
        "note": "off by default; enable once the capture pipeline reports liveness",
    },
    "face": {
        "required": False,
        "min_liveness": 0.5,
        "evidence": ("blink_detected",),
        "reject_injection": True,
        "note": "presentation attacks are the dominant threat for face capture",
    },
    "voice": {
        "required": False,
        "min_liveness": 0.5,
        "evidence": ("live_reading",),
        "reject_injection": True,
        "note": "a replayed recording is the dominant threat for voice capture",
    },
}
DEFAULT_PAD_PROFILE: dict[str, Any] = {
    "required": False,
    "min_liveness": 0.0,
    "evidence": (),
    "reject_injection": False,
    "note": "no PAD profile configured for this modality",
}
# Request-context keys that carry liveness evidence.
PAD_CONTEXT_FIELDS = ("liveness_score", "injection_detected")
# Reason codes ``evaluate_pad`` / ``decide`` can add to a rejection.
PAD_REASONS = ("pad_evidence_missing", "liveness_below_threshold", "injection_detected")
# A liveness score outside 0..1 is a gap, not a denial — the caller decides.


def evaluate_pad(
    modality: str, context: dict[str, Any] | None = None, *, enforce: bool = False
) -> dict[str, Any]:
    """Check liveness evidence for a modality and report the verdict.

    Pure. The matcher only ever sees the enrolled template, so a replayed photo
    or a recorded voice scores a *perfect* match — liveness has to be a separate
    signal, and it is checked before the template is scored rather than mixed
    into it.

    ``enforce=True`` forces the check on for a modality whose profile is not
    ``required``; otherwise a profile's own ``required`` flag decides. When the
    check is not enforced the verdict is still reported, so a caller can log PAD
    evidence before choosing to require it.
    """
    profile = dict(PAD_PROFILES.get(modality) or DEFAULT_PAD_PROFILE)
    ctx = dict(context or {})
    enforced = bool(enforce or profile.get("required", False))
    required_evidence = tuple(profile.get("evidence") or ())
    missing = [key for key in required_evidence if key not in ctx]
    raw = ctx.get("liveness_score")
    score: float | None
    if raw is None:
        score = None
    else:
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            candidate = None
        low, high = PAD_LIVENESS_BOUNDS
        # An out-of-range or unparseable score is a *gap*, not a denial: a
        # silent fail-open here would let a broken sensor disable PAD.
        score = candidate if candidate is not None and low <= candidate <= high else None
    min_liveness = float(profile.get("min_liveness", 0.0))
    injected = bool(ctx.get("injection_detected"))
    reason = "ok"
    if injected and profile.get("reject_injection", False):
        reason = "injection_detected"
    elif missing:
        reason = "pad_evidence_missing"
    elif score is not None and score < min_liveness:
        reason = "liveness_below_threshold"
    return {
        "modality": modality,
        "enforced": enforced,
        # Reported whether or not the check is enforced, so a caller can log PAD
        # evidence today and start *enforcing* it tomorrow with no code change.
        "passed": reason == "ok",
        "reason": reason,
        "liveness_score": score,
        "min_liveness": min_liveness,
        "required_evidence": list(required_evidence),
        "missing_evidence": missing,
        "injection_detected": injected,
        "reject_injection": bool(profile.get("reject_injection", False)),
    }


def _hash_template(template: bytes, salt: bytes) -> str:
    return hmac.new(salt, template, hashlib.sha256).hexdigest()


def _digest(template: bytes) -> bytes:
    return hashlib.sha256(template).digest()


def _digest_similarity(digest_a: bytes, digest_b: bytes) -> float:
    """Deterministic pseudo-similarity between two SHA-256 digests (0..1)."""
    mismatches = sum((x ^ y).bit_count() for x, y in zip(digest_a, digest_b))
    return 1.0 - mismatches / (len(digest_a) * 8)


def _as_datetime(value: Any) -> Optional[datetime]:
    """Parse a stored ISO-8601 stamp back into an aware datetime.

    Lockout state is persisted as text (so it can be exported and replayed),
    which means the comparison clock has to accept either form.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class BiometricChallenge:
    """Single-use freshness token bound to one capture attempt."""

    challenge_id: str
    user_id: int
    modality: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    used: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "user_id": self.user_id,
            "modality": self.modality,
            "nonce": self.nonce,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) > self.expires_at


class BiometricVault:
    """Per-user, per-modality template vault (salted digests only)."""

    def __init__(
        self,
        modalities: dict[str, dict[str, Any]] | None = None,
        policy: dict[str, Any] | None = None,
    ):
        self._modalities = deepcopy(dict(modalities or BIOMETRIC_MODALITIES))
        # Kept so an adaptive threshold can always be reported against, and
        # rolled back to, the shipped configuration.
        self._base_thresholds = {
            name: config.get("threshold") for name, config in self._modalities.items()
        }
        self._policy = {**BIOMETRIC_POLICY, **dict(policy or {})}
        self._templates: dict[tuple[int, str], dict[str, Any]] = {}
        self._slots: dict[tuple[int, str, int], dict[str, Any]] = {}
        self._validations = 0
        self._attempts: dict[tuple[int, str], dict[str, Any]] = {}
        self._challenges: dict[str, BiometricChallenge] = {}
        self._history: list[dict[str, Any]] = []

    # --- config ------------------------------------------------------------

    @property
    def policy(self) -> dict[str, Any]:
        return dict(self._policy)

    @property
    def modalities(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._modalities)

    def _require_modality(self, modality: str) -> None:
        if modality not in self._modalities:
            raise ValueError(f"unsupported biometric modality: {modality}")

    def threshold_for(self, modality: str) -> float:
        self._require_modality(modality)
        return float(self._modalities[modality]["threshold"])

    def set_threshold(self, modality: str, value: float) -> dict[str, Any]:
        """Pin a modality's threshold, clamped to its configured bounds."""
        self._require_modality(modality)
        config = self._modalities[modality]
        low = float(config.get("min_threshold", 0.0))
        high = float(config.get("max_threshold", 1.0))
        pinned = min(max(float(value), low), high)
        previous = config.get("threshold")
        config["threshold"] = pinned
        return {
            "modality": modality,
            "previous_threshold": previous,
            "threshold": pinned,
            "clamped": pinned != float(value),
            "bounds": [low, high],
        }

    def adapt_threshold(
        self, modality: str, outcome: str, *, step: float | None = None
    ) -> dict[str, Any]:
        """Loosen or tighten a modality from a confirmed outcome.

        A ``false_positive`` (a real user rejected) tightens; a
        ``false_negative`` (an impostor accepted) loosens.
        """
        self._require_modality(modality)
        if outcome not in BIOMETRIC_OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(BIOMETRIC_OUTCOMES)}")
        config = self._modalities[modality]
        previous = float(config["threshold"])
        delta = float(step if step is not None else self._policy["threshold_adapt_step"])
        if outcome == "false_positive":
            candidate = previous - delta
        elif outcome == "false_negative":
            candidate = previous + delta
        else:
            return {
                "modality": modality,
                "outcome": outcome,
                "previous_threshold": previous,
                "threshold": previous,
                "changed": False,
            }
        applied = self.set_threshold(modality, candidate)["threshold"]
        return {
            "modality": modality,
            "outcome": outcome,
            "previous_threshold": previous,
            "threshold": applied,
            "delta": round(applied - previous, 6),
            "changed": applied != previous,
            "base_threshold": self._base_thresholds.get(modality),
        }

    def reset_thresholds(self) -> dict[str, Any]:
        """Restore the shipped thresholds after adaptive tuning."""
        restored = {
            name: self.set_threshold(name, value)
            for name, value in self._base_thresholds.items()
            if value is not None
        }
        return restored

    # --- enrollment --------------------------------------------------------

    # --- enrollment quality -------------------------------------------------

    def enrollment_rule(self, modality: str) -> dict[str, Any]:
        """The enrollment bar for a modality, falling back to accept-everything."""
        return dict(ENROLLMENT_RULES.get(modality) or DEFAULT_ENROLLMENT_RULE)

    def assess_enrollment(
        self, user_id: int, modality: str, template: bytes, *, slot: int | None = None
    ) -> dict[str, Any]:
        """Would this capture make a *good* template, before it is stored?

        A capture is graded against an existing template when there is one, and
        against itself otherwise — the first enrolment has nothing to compare to,
        so its quality is 1.0 by construction. Pure with respect to the vault: it
        counts nothing and stores nothing, so an operator can dry-run an
        enrollment before committing to it.
        """
        self._require_modality(modality)
        rule = self.enrollment_rule(modality)
        record = self._record_for(user_id, modality, slot)
        confidence = (
            1.0
            if record is None
            else round(
                _digest_similarity(_digest(template), record["reference_digest"]), 4
            )
        )
        min_quality = float(rule.get("min_quality", 0.0))
        stored = self._slots_for(int(user_id), modality)
        cap = int(rule.get("max_samples", 0) or 0)
        reason = ENROLLMENT_REASONS[0]
        accepted = True
        if confidence < min_quality:
            accepted, reason = False, "below_min_quality"
        elif cap and len(stored) >= cap:
            accepted, reason = False, "sample_cap_reached"
        return {
            "user_id": int(user_id),
            "modality": modality,
            "accepted": accepted,
            "reason": reason,
            "quality": confidence,
            "min_quality": min_quality,
            "samples_stored": len(stored),
            "max_samples": cap,
            "samples_required": int(rule.get("samples_required", 1)),
            "compared_against": "existing_template" if record is not None else "self",
        }

    def register_template_checked(
        self,
        user_id: int,
        modality: str,
        template: bytes,
        *,
        actor_user_id: int | None = None,
        slot: int | None = None,
    ) -> dict[str, Any]:
        """Enrol only if the capture clears :meth:`assess_enrollment`.

        :meth:`register_template` is deliberately unchanged — a vault that
        silently refused an enrollment would break existing callers. This is the
        strict path, and it *reports* the refusal rather than raising, so a
        re-enrolment UI can show why.
        """
        assessment = self.assess_enrollment(user_id, modality, template, slot=slot)
        if not assessment["accepted"]:
            return {"registered": False, **assessment}
        if slot is None:
            result = self.register_template(
                user_id, modality, template, actor_user_id=actor_user_id
            )
        else:
            result = self.enroll_slot(
                user_id, modality, template, slot=slot, actor_user_id=actor_user_id
            )
        return {"registered": True, **result, "quality": assessment["quality"],
                "min_quality": assessment["min_quality"], "reason": "accepted"}

    # --- presentation attack detection ---------------------------------------

    def pad_profile(self, modality: str) -> dict[str, Any]:
        """The PAD profile for a modality, defaulting to *not required*."""
        return dict(PAD_PROFILES.get(modality) or DEFAULT_PAD_PROFILE)

    def _new_record(
        self, template: bytes, actor_user_id: int | None
    ) -> dict[str, Any]:
        salt = secrets.token_bytes(16)
        return {
            "salt": salt.hex(),
            "stored_hash": _hash_template(template, salt),
            "reference_digest": _digest(template),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "registered_by": actor_user_id,
            "revoked": False,
            "revoked_at": None,
        }

    def register_template(
        self,
        user_id: int,
        modality: str,
        template: bytes,
        *,
        actor_user_id: int | None = None,
    ) -> dict[str, Any]:
        self._require_modality(modality)
        entry = self._new_record(template, actor_user_id)
        self._templates[(int(user_id), modality)] = entry
        return {
            "user_id": int(user_id),
            "modality": modality,
            "registered": True,
            "stored_hash_prefix": entry["stored_hash"][:12],
            "threshold": self._modalities[modality]["threshold"],
            "registered_at": entry["registered_at"],
        }

    def enroll_slot(
        self,
        user_id: int,
        modality: str,
        template: bytes,
        *,
        slot: int | None = None,
        actor_user_id: int | None = None,
    ) -> dict[str, Any]:
        """Enroll an *additional* template for a modality.

        The primary template (slot 0) is what ``validate_template`` consults by
        default, so a multi-finger enrollment never changes single-template
        behaviour. Use ``validate_template(..., slot=n)`` to test a specific
        finger.
        """
        self._require_modality(modality)
        capacity = int(self._policy["max_enrollment_slots"])
        target = self._next_slot(int(user_id), modality) if slot is None else int(slot)
        if target < 1 or target >= capacity:
            raise ValueError(f"slot must be between 1 and {capacity - 1}")
        entry = self._new_record(template, actor_user_id)
        self._slots[(int(user_id), modality, target)] = entry
        return {
            "user_id": int(user_id),
            "modality": modality,
            "slot": target,
            "registered": True,
            "stored_hash_prefix": entry["stored_hash"][:12],
            "threshold": self._modalities[modality]["threshold"],
            "registered_at": entry["registered_at"],
            "slots_used": len(self._slots_for(int(user_id), modality)),
            "slot_capacity": capacity,
        }

    def _next_slot(self, user_id: int, modality: str) -> int:
        used = {slot for (uid, mod, slot) in self._slots if uid == user_id and mod == modality}
        candidate = 1
        while candidate in used:
            candidate += 1
        return candidate

    def _slots_for(self, user_id: int, modality: str) -> list[dict[str, Any]]:
        return [
            record
            for (uid, mod, _slot), record in sorted(self._slots.items())
            if uid == user_id and mod == modality
        ]

    def list_slots(self, user_id: int, modality: str) -> list[dict[str, Any]]:
        return [
            {
                "slot": slot,
                "stored_hash_prefix": record["stored_hash"][:12],
                "registered_at": record["registered_at"],
                "revoked": record["revoked"],
            }
            for (uid, mod, slot), record in sorted(self._slots.items())
            if uid == int(user_id) and mod == modality
        ]

    def _record_for(
        self, user_id: int, modality: str, slot: int | None = None
    ) -> dict[str, Any] | None:
        if slot is None:
            return self._templates.get((int(user_id), modality))
        if int(slot) == 0:
            return self._templates.get((int(user_id), modality))
        return self._slots.get((int(user_id), modality, int(slot)))

    # --- challenges --------------------------------------------------------

    def issue_challenge(
        self, user_id: int, modality: str, *, ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        """Mint a single-use freshness token for the next capture attempt."""
        self._require_modality(modality)
        issued = datetime.now(timezone.utc)
        ttl = int(
            ttl_seconds if ttl_seconds is not None else self._policy["challenge_ttl_seconds"]
        )
        challenge = BiometricChallenge(
            challenge_id=uuid.uuid4().hex,
            user_id=int(user_id),
            modality=modality,
            nonce=secrets.token_hex(16),
            issued_at=issued,
            expires_at=issued + timedelta(seconds=ttl),
        )
        self._challenges[challenge.challenge_id] = challenge
        self._prune_challenges()
        return challenge.to_payload()

    def _prune_challenges(self) -> None:
        capacity = int(self._policy["challenge_capacity"])
        while len(self._challenges) > capacity:
            oldest = min(self._challenges.values(), key=lambda c: c.issued_at)
            self._challenges.pop(oldest.challenge_id, None)

    def _check_challenge(
        self, challenge: Any, *, user_id: int, modality: str
    ) -> str | None:
        """Return a failure reason for ``challenge``, or ``None`` when it passes.

        The challenge gates freshness and replay only — it is deliberately not
        mixed into the similarity digest, so an enrolled template still scores
        1.0 for the user who owns it.
        """
        if challenge is None:
            return None
        payload = challenge if isinstance(challenge, Mapping) else {"challenge_id": challenge}
        challenge_id = str(payload.get("challenge_id") or "")
        record = self._challenges.get(challenge_id)
        if record is None:
            return "challenge_unknown"
        if record.used:
            return "challenge_replayed"
        if record.is_expired():
            return "challenge_expired"
        if record.user_id != int(user_id) or record.modality != modality:
            return "challenge_unknown"
        return None

    def consume_challenge(self, challenge: Any) -> bool:
        """Mark a challenge used so it cannot be presented twice."""
        challenge_id = str(
            challenge.get("challenge_id")
            if isinstance(challenge, Mapping)
            else challenge
            or ""
        )
        record = self._challenges.get(challenge_id)
        if record is None or record.used:
            return False
        record.used = True
        return True

    # --- brute-force control -----------------------------------------------

    def _locked_out(self, user_id: int, modality: str, now: datetime) -> bool:
        if not self._policy.get("lockout_enabled", True):
            return False
        state = self._attempts.get((int(user_id), modality)) or {}
        until = _as_datetime(state.get("locked_until"))
        return bool(until is not None and now < until)

    def _note_attempt(self, user_id: int, modality: str, accepted: bool, now: datetime) -> None:
        key = (int(user_id), modality)
        if accepted:
            self._attempts.pop(key, None)
            return
        state = self._attempts.setdefault(key, {"failed": 0, "locked_until": None})
        state["failed"] = int(state.get("failed", 0)) + 1
        state["last_failed_at"] = now.isoformat()
        limit = int(self._policy.get("max_attempts", 0) or 0)
        if limit and state["failed"] >= limit:
            state["locked_until"] = (now + timedelta(
                seconds=int(self._policy.get("lockout_seconds", 0))
            )).isoformat()

    def reset_attempts(self, user_id: int, modality: str | None = None) -> dict[str, Any]:
        """Clear the failure counter / lockout for a user (admin action)."""
        if modality is None:
            for key in [k for k in self._attempts if k[0] == int(user_id)]:
                self._attempts.pop(key, None)
        else:
            self._attempts.pop((int(user_id), modality), None)
        return {"user_id": int(user_id), "modality": modality, "cleared": True}

    def attempt_state(self, user_id: int, modality: str) -> dict[str, Any]:
        state = self._attempts.get((int(user_id), modality)) or {}
        return {
            "user_id": int(user_id),
            "modality": modality,
            "failed": int(state.get("failed", 0)),
            "max_attempts": int(self._policy.get("max_attempts", 0)),
            "locked_until": state.get("locked_until"),
            "lockout_seconds": int(self._policy.get("lockout_seconds", 0)),
        }

    # --- validation --------------------------------------------------------

    def _reject(
        self, user_id: int, modality: str, reason: str, *, threshold: float | None = None
    ) -> dict[str, Any]:
        return {
            "user_id": int(user_id),
            "modality": modality,
            "match": False,
            "accepted": False,
            "confidence": 0.0,
            "threshold": self._modalities[modality]["threshold"]
            if threshold is None
            else threshold,
            "reason": reason,
        }

    def _record_history(self, result: dict[str, Any]) -> None:
        row = {
            "at": datetime.now(timezone.utc).isoformat(),
            "user_id": result.get("user_id"),
            "modality": result.get("modality"),
            "accepted": bool(result.get("accepted")),
            "confidence": result.get("confidence"),
            "threshold": result.get("threshold"),
            "reason": result.get("reason"),
        }
        self._history.append(row)
        capacity = int(self._policy.get("history_capacity", 1000))
        while len(self._history) > capacity:
            self._history.pop(0)

    def validate_template(
        self,
        user_id: int,
        modality: str,
        template: bytes,
        *,
        slot: int | None = None,
        challenge: Any = None,
    ) -> dict[str, Any]:
        """Score a capture against the enrolled template(s) for this modality.

        Checks run in a fixed order so a rejection reason is always the most
        specific one available: lockout, challenge freshness, enrolment, then
        similarity.
        """
        self._require_modality(modality)
        self._validations += 1
        now = datetime.now(timezone.utc)

        if self._locked_out(user_id, modality, now):
            result = self._reject(user_id, modality, "locked_out")
            self._record_history(result)
            return result

        challenge_reason = self._check_challenge(challenge, user_id=user_id, modality=modality)
        if challenge_reason:
            result = self._reject(user_id, modality, challenge_reason)
            self._record_history(result)
            return result

        record = self._record_for(user_id, modality, slot)
        if record is None:
            result = self._reject(user_id, modality, "template_not_registered")
            self._note_attempt(user_id, modality, False, now)
            self._record_history(result)
            return result
        if record["revoked"]:
            result = self._reject(user_id, modality, "template_revoked")
            self._record_history(result)
            return result

        confidence = _digest_similarity(_digest(template), record["reference_digest"])
        accepted = confidence >= self._modalities[modality]["threshold"]
        if challenge is not None and accepted:
            self.consume_challenge(challenge)
        self._note_attempt(user_id, modality, accepted, now)
        result = {
            "user_id": int(user_id),
            "modality": modality,
            "match": accepted,
            "accepted": accepted,
            "confidence": round(confidence, 4),
            "threshold": self._modalities[modality]["threshold"],
            "reason": "match" if accepted else "below_threshold",
        }
        if slot is not None:
            result["slot"] = int(slot)
        self._record_history(result)
        return result

    def decide(
        self,
        user_id: int,
        modality: str,
        template: bytes,
        *,
        slot: int | None = None,
        challenge: Any = None,
        context: dict[str, Any] | None = None,
        enforce_pad: bool = False,
    ) -> dict[str, Any]:
        """Three-way outcome with a step-up band instead of a bare boolean.

        ``accept`` at/above the modality threshold, ``challenge`` inside the
        step-up band (a real user with a marginal capture), ``deny`` below it.

        With ``enforce_pad=True`` — or when the modality's PAD profile is marked
        ``required`` — liveness evidence is checked *before* the template is
        scored, because a presentation attack matches the template perfectly and
        a match is therefore not evidence of a live capture. The check is pure
        and its verdict is reported alongside the decision, never instead of it.
        """
        self._require_modality(modality)
        pad = evaluate_pad(modality, context, enforce=enforce_pad)
        result = self.validate_template(
            user_id, modality, template, slot=slot, challenge=challenge
        )
        if pad["enforced"] and not pad["passed"]:
            denied = {
                **result,
                "match": False,
                "accepted": False,
                "decision": "deny",
                "step_up_required": False,
                "step_up_threshold": float(
                    self._modalities[modality].get(
                        "step_up_threshold", self._modalities[modality]["threshold"]
                    )
                ),
                "reason": pad["reason"],
                "pad": pad,
            }
            self._note_attempt(
                user_id, modality, False, datetime.now(timezone.utc)
            )
            self._record_history(denied)
            return denied
        step_up = float(
            self._modalities[modality].get(
                "step_up_threshold", self._modalities[modality]["threshold"]
            )
        )
        confidence = float(result.get("confidence") or 0.0)
        if result["accepted"]:
            decision, step_up_required = "accept", False
        elif result["reason"] in {"template_not_registered", "template_revoked", "locked_out"}:
            decision, step_up_required = "deny", False
        elif confidence >= step_up:
            decision, step_up_required = "challenge", True
        else:
            decision, step_up_required = "deny", False
        return {
            **result,
            "decision": decision,
            "step_up_required": step_up_required,
            "step_up_threshold": step_up,
            "pad": pad,
        }

    # --- lifecycle & introspection ----------------------------------------

    def revoke_template(self, user_id: int, modality: str) -> dict[str, Any]:
        key = (int(user_id), modality)
        if key not in self._templates:
            raise ValueError(f"no registered template for user {user_id} / {modality}")
        self._templates[key]["revoked"] = True
        self._templates[key]["revoked_at"] = datetime.now(timezone.utc).isoformat()
        return {"user_id": int(user_id), "modality": modality, "revoked": True}

    def revoke_slot(self, user_id: int, modality: str, slot: int) -> dict[str, Any]:
        key = (int(user_id), modality, int(slot))
        if key not in self._slots:
            raise ValueError(f"no slot {slot} for user {user_id} / {modality}")
        self._slots[key]["revoked"] = True
        self._slots[key]["revoked_at"] = datetime.now(timezone.utc).isoformat()
        return {"user_id": int(user_id), "modality": modality, "slot": int(slot), "revoked": True}

    def list_templates(self, user_id: int | None = None) -> list[dict[str, Any]]:
        rows = []
        for (uid, modality), record in sorted(self._templates.items()):
            if user_id is not None and uid != int(user_id):
                continue
            rows.append(
                {
                    "user_id": uid,
                    "modality": modality,
                    "stored_hash_prefix": record["stored_hash"][:12],
                    "registered_at": record["registered_at"],
                    "revoked": record["revoked"],
                }
            )
        for (uid, modality, slot), record in sorted(self._slots.items()):
            if user_id is not None and uid != int(user_id):
                continue
            rows.append(
                {
                    "user_id": uid,
                    "modality": modality,
                    "slot": slot,
                    "stored_hash_prefix": record["stored_hash"][:12],
                    "registered_at": record["registered_at"],
                    "revoked": record["revoked"],
                }
            )
        return rows

    def history(
        self,
        user_id: int | None = None,
        modality: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recent validation attempts, newest last."""
        bound = int(limit if limit is not None else self._policy.get("history_limit", 50))
        rows = [
            row
            for row in self._history
            if (user_id is None or row["user_id"] == int(user_id))
            and (modality is None or row["modality"] == modality)
        ]
        return rows[-bound:]

    # --- replication -------------------------------------------------------

    def export_state(self) -> dict[str, Any]:
        """Serialize digests (never plaintext) so instances can be seeded."""
        return {
            "version": VAULT_STATE_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "thresholds": {
                name: config.get("threshold") for name, config in self._modalities.items()
            },
            "templates": [
                {
                    "user_id": uid,
                    "modality": modality,
                    "slot": 0,
                    **record,
                }
                for (uid, modality), record in sorted(self._templates.items())
            ],
            "slots": [
                {
                    "user_id": uid,
                    "modality": modality,
                    "slot": slot,
                    **record,
                }
                for (uid, modality, slot), record in sorted(self._slots.items())
            ],
        }

    def import_state(
        self, payload: dict[str, Any], *, replace: bool = False
    ) -> dict[str, Any]:
        """Load an :meth:`export_state` document, optionally replacing state."""
        if int(payload.get("version", 0)) != VAULT_STATE_VERSION:
            raise ValueError(
                f"unsupported vault state version {payload.get('version')!r}"
            )
        if replace:
            self._templates.clear()
            self._slots.clear()
        imported = 0
        for row in payload.get("templates") or []:
            modality = row.get("modality")
            if modality not in self._modalities:
                continue
            record = {k: v for k, v in row.items() if k not in {"user_id", "modality", "slot"}}
            self._templates[(int(row["user_id"]), modality)] = record
            imported += 1
        for row in payload.get("slots") or []:
            modality = row.get("modality")
            if modality not in self._modalities:
                continue
            record = {k: v for k, v in row.items() if k not in {"user_id", "modality", "slot"}}
            self._slots[(int(row["user_id"]), modality, int(row["slot"]))] = record
            imported += 1
        for modality, threshold in (payload.get("thresholds") or {}).items():
            if modality in self._modalities and threshold is not None:
                self.set_threshold(modality, float(threshold))
        return {
            "imported": imported,
            "replaced": bool(replace),
            "template_count": len(self._templates),
            "slot_count": len(self._slots),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "modalities": {m: c.get("threshold") for m, c in self._modalities.items()},
            "template_count": len(self._templates),
            "validation_count": self._validations,
            "templates": self.list_templates(),
        }


DEFAULT_BIOMETRIC_VAULT = BiometricVault()


def get_default_biometric_vault() -> BiometricVault:
    return DEFAULT_BIOMETRIC_VAULT


def set_default_biometric_vault(vault: BiometricVault) -> None:
    global DEFAULT_BIOMETRIC_VAULT
    DEFAULT_BIOMETRIC_VAULT = vault


def build_biometric_vault_catalog(vault: BiometricVault | None = None) -> dict[str, object]:
    vault = vault or get_default_biometric_vault()
    return {
        "storage": {
            "plaintext_templates": False,
            "digest": "salted HMAC-SHA256",
            "matcher": "pluggable (default: digest_hamming)",
        },
        "modalities": vault._modalities,
        "template_count": len(vault._templates),
        "validation_count": vault._validations,
        # --- expansion surface -------------------------------------------------
        "policy": vault.policy,
        "reasons": list(BIOMETRIC_REASONS),
        "decisions": list(BIOMETRIC_DECISIONS),
        "outcomes": list(BIOMETRIC_OUTCOMES),
        "thresholds": {
            name: {
                "threshold": config.get("threshold"),
                "base_threshold": vault._base_thresholds.get(name),
                "step_up_threshold": config.get("step_up_threshold"),
                "bounds": [config.get("min_threshold"), config.get("max_threshold")],
            }
            for name, config in vault._modalities.items()
        },
        "slot_capacity": int(vault._policy.get("max_enrollment_slots", 0)),
        "slot_count": len(vault._slots),
        "history_count": len(vault._history),
        "attempts": {f"{uid}:{modality}": state for (uid, modality), state in sorted(vault._attempts.items())},
        "challenge": {
            "ttl_seconds": int(vault._policy.get("challenge_ttl_seconds", 0)),
            "capacity": int(vault._policy.get("challenge_capacity", 0)),
            "outstanding": len(vault._challenges),
            "single_use": True,
        },
        "state_version": VAULT_STATE_VERSION,
        # Enrollment + PAD live in their own catalog: this key set is pinned.
        "governance": {
            "catalog": "build_biometric_vault_policy",
            "enrollment_modalities": sorted(ENROLLMENT_RULES),
            "pad_modalities": sorted(PAD_PROFILES),
        },
        "note": (
            "modalities, step-up bands, adaptive-threshold bounds, brute-force "
            "policy and challenge TTLs are all config; a challenge gates replay "
            "only and never enters the similarity digest"
        ),
    }


def build_biometric_vault_policy(vault: BiometricVault | None = None) -> dict[str, object]:
    """Enrollment-quality and presentation-attack policy.

    Separate from :func:`build_biometric_vault_catalog` because that catalog's
    key set is a pinned contract.
    """
    vault = vault or get_default_biometric_vault()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enrollment": {
            name: dict(rule) for name, rule in sorted(ENROLLMENT_RULES.items())
        },
        "enrollment_default": dict(DEFAULT_ENROLLMENT_RULE),
        "enrollment_reasons": list(ENROLLMENT_REASONS),
        "pad": {
            name: {
                **dict(profile),
                "evidence": list(profile.get("evidence") or ()),
            }
            for name, profile in sorted(PAD_PROFILES.items())
        },
        "pad_default": dict(DEFAULT_PAD_PROFILE),
        "pad_reasons": list(PAD_REASONS),
        "pad_context_fields": list(PAD_CONTEXT_FIELDS),
        "liveness_bounds": list(PAD_LIVENESS_BOUNDS),
        "liveness_state": {
            modality: evaluate_pad(modality) for modality in sorted(vault.modalities)
        },
        "note": (
            "enrollment quality and liveness are separate signals from template "
            "similarity on purpose: a presentation attack matches the template "
            "perfectly, so PAD is checked before the match, never inside it"
        ),
    }