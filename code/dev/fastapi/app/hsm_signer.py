"""Hardware-security-module signing abstractions.

The request path only ever sees an ``HsmSigner`` (``sign`` / ``verify``);
the concrete backend is chosen by configuration. A real PKCS#11 / cloud KMS
adapter can be dropped in by implementing the same two-method protocol —
nothing pins business code to a device library.

Backends (``HSM_BACKEND``):

- ``hmac`` (default): deterministic HMAC-SHA256 envelope signing, keyed off
  ``HSM_SIGNER_KEY`` (falling back to the app ``SECRET_KEY``). No key files.
- ``ed25519``: Ed25519 signature signing via ``cryptography``; persists the
  private key to ``HSM_SIGNER_KEY_PATH`` when set, otherwise per-process key.
- ``mock``: fixed deterministic signatures for tests/contracts.

Surfaces:

- ``HsmSignature`` / ``sign_bytes`` / ``verify_bytes`` — raw payload signing.
- ``sign_envelope`` / ``verify_envelope`` / ``decode_envelope`` — signed
  JSON envelopes (base64 payload + detached signature + key metadata).
- ``verify_envelope_detailed`` — the same check with a machine-readable reason
  code and an optional freshness / anti-replay policy.
- ``SIGNER_BACKENDS`` / ``register_signer_backend`` — a registry, so a real
  PKCS#11 or cloud-KMS adapter is registered rather than edited in.
- ``SIGNER_KEY_RING`` — rotation: several key ids can verify concurrently while
  exactly one signs.
- ``sign_multi`` / ``verify_multi`` — N-of-M co-signature for workflows that
  need more than one authority.
- ``signer_self_test`` / ``build_signer_health`` — round-trip + tamper probes.

Expansion notes (key governance):

Rotation is currently a two-step data change with no guard on the *result*:
nothing checks that a newly activated key is actually stronger than the one it
replaces, that a retiring key has passed its overlap window, or that a key
scheduled for rotation was rotated at all. The governance layer adds:

- ``KEY_STRENGTH_RANKS`` / ``rank_key`` — a total order over algorithms, so
  "downgrade" is a fact rather than an opinion.
- ``KEY_ROTATION_SCHEDULE`` — per-key ``rotate_after_days`` and
  ``overlap_days``; ``plan_key_rotation`` is a pure overdue/upcoming report.
- ``KEY_RETIREMENT_RULES`` — what must hold before a key may be retired
  (overlap elapsed, not active, envelopes still outstanding).
- ``can_retire_key`` / ``retire_key_guarded`` — the enforcement behind those
  rules, so a rotation cannot quietly orphan un-verifiable envelopes.
- ``build_signer_governance`` — the catalog for all of it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from app.security import SECRET_KEY

HSM_BACKEND = os.getenv("HSM_BACKEND", "hmac")
HSM_KEY_ID = os.getenv("HSM_KEY_ID", "cservice-hsm-key-001")
HSM_SIGNER_KEY = os.getenv("HSM_SIGNER_KEY", "") or SECRET_KEY
HSM_SIGNER_KEY_PATH = os.getenv("HSM_SIGNER_KEY_PATH", "")

SUPPORTED_BACKENDS = ("hmac", "ed25519", "mock")

# Verification reason codes. Callers branch on these instead of re-deriving
# *why* a signature was rejected.
VERIFY_REASONS = (
    "ok",
    "malformed_envelope",
    "payload_digest_mismatch",
    "unknown_algorithm",
    "key_id_mismatch",
    "signature_invalid",
    "expired",
    "not_yet_valid",
    "replayed",
    "threshold_not_met",
)

# Envelope freshness / anti-replay policy. Off by default so an envelope stays
# verifiable exactly as long as the key is; opt in per call site.
SIGNATURE_POLICY_DEFAULTS: dict[str, Any] = {
    "allowed_algorithms": ("HSM-HS256", "HSM-ED25519", "HSM-MOCK"),
    "check_algorithm": True,
    "check_key_id": True,
    "require_expiry": False,
    # How long past ``expires_at`` an envelope is still honoured. Zero by
    # default so a short TTL means what it says.
    "expiry_tolerance_seconds": 0,
    # How much *early* a ``not_before`` is honoured, to absorb clock skew
    # between the signer and the verifier.
    "not_before_skew_seconds": 300,
}

SIGNATURE_KEY_STATUSES = ("active", "previous", "retired")

# --- key governance (expansion) -------------------------------------------------
#
# A ring tells you which keys verify. It does not tell you whether the *set* of
# keys is sound, and a rotation that nobody checks is indistinguishable from no
# rotation at all. These tables make the rotation policy data.

# Total order over signature strengths. Higher is stronger, so a downgrade is a
# comparison rather than a judgement call. Unlisted algorithms rank 0 — a
# backend that registers a new algorithm is not silently treated as strongest.
KEY_STRENGTH_RANKS: dict[str, int] = {
    "HSM-MOCK": 0,
    "HSM-HS256": 1,
    "HSM-ED25519": 2,
}

# Config table: key id -> {rotate_after_days, overlap_days, note}. A key absent
# from the table is never scheduled for rotation, which is the right default for
# a deployment that has not adopted rotation yet.
KEY_ROTATION_SCHEDULE: dict[str, dict[str, Any]] = {
    "cservice-hsm-key-001": {
        "rotate_after_days": 90,
        "overlap_days": 14,
        "note": "shipped default key; rotate on a 90-day cadence with a 14-day overlap",
    },
}

# What must hold before a key may be retired. ``require_overlap_elapsed`` means
# the key must already have been superseded for at least its overlap window, so
# an envelope signed just before a cutover still verifies.
KEY_RETIREMENT_RULES: dict[str, Any] = {
    "require_overlap_elapsed": True,
    "require_not_active": True,
    "allow_retiring_unknown_keys": True,
}
# Why a retirement was refused; a closed set so a caller branches exhaustively.
RETIREMENT_REASONS = (
    "ok",
    "unknown_key",
    "still_active",
    "overlap_not_elapsed",
    "refused_by_policy",
)


def rank_key(algorithm: str) -> int:
    """Strength rank of an algorithm (higher is stronger; unlisted is 0)."""
    return int(KEY_STRENGTH_RANKS.get(str(algorithm), 0))


def key_overlap_days(key_id: str) -> int:
    """Days a superseded key must stay verifiable, from its rotation schedule."""
    return int((KEY_ROTATION_SCHEDULE.get(str(key_id)) or {}).get("overlap_days", 0) or 0)


def key_age_days(key_id: str, now: datetime | None = None) -> float | None:
    """Days since a key entered the ring, or ``None`` if it is not in the ring.

    ``age`` rather than ``created_at`` because a key imported with its original
    ``created_at`` is exactly the key whose rotation is most overdue.
    """
    key = SIGNER_KEY_RING.get(key_id)
    if key is None:
        return None
    created = _parse_ts(key.created_at)
    if created is None:
        return None
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round((moment - created).total_seconds() / 86400.0, 3)


def plan_key_rotation(now: datetime | None = None) -> dict[str, Any]:
    """Which scheduled keys are overdue, due soon, or fine — with no mutation.

    Pure, so a rotation dashboard, a pre-flight check and a test all reach the
    same verdict. ``overdue_by_days`` is negative while a key is still inside
    its window, so the sign answers "before or after" without a comparison.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    for key in SIGNER_KEY_RING.catalog()["keys"]:
        schedule = KEY_ROTATION_SCHEDULE.get(key["key_id"]) or {}
        rotate_after = schedule.get("rotate_after_days")
        created = _parse_ts(key["created_at"]) if key["created_at"] else None
        age = round((moment - created).total_seconds() / 86400.0, 3) if created else None
        overdue_by = None if rotate_after is None or age is None else round(age - float(rotate_after), 3)
        if overdue_by is None:
            verdict = "unscheduled"
        elif overdue_by > 0:
            verdict = "overdue"
        elif overdue_by > -7:
            verdict = "due_soon"
        else:
            verdict = "ok"
        rows.append(
            {
                "key_id": key["key_id"],
                "status": key["status"],
                "algorithm": key["algorithm"],
                "fingerprint": key["fingerprint"],
                "age_days": age,
                "rotate_after_days": rotate_after,
                "overdue_by_days": overdue_by,
                "verdict": verdict,
            }
        )
    return {
        "generated_at": moment.isoformat(),
        "schedule": {kid: dict(cfg) for kid, cfg in sorted(KEY_ROTATION_SCHEDULE.items())},
        "keys": rows,
        "overdue": [row["key_id"] for row in rows if row["verdict"] == "overdue"],
        "due_soon": [row["key_id"] for row in rows if row["verdict"] == "due_soon"],
        "rotatable": bool(SIGNER_KEY_RING.previous_key_ids()),
    }


def can_retire_key(key_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Whether a key may be retired, and which rule says so.

    Pure and advisory. :meth:`SigningKeyRing.retire` keeps its historical
    behaviour (it only refuses the active key); this is the stricter check an
    operator runs *before* deciding to retire one.
    """
    key = SIGNER_KEY_RING.get(key_id)
    if key is None:
        return {
            "key_id": key_id,
            "retirable": bool(KEY_RETIREMENT_RULES.get("allow_retiring_unknown_keys", True)),
            "reason": "ok" if KEY_RETIREMENT_RULES.get("allow_retiring_unknown_keys", True) else "unknown_key",
        }
    if KEY_RETIREMENT_RULES.get("require_not_active", True) and key.status == "active":
        return {"key_id": key_id, "retirable": False, "reason": "still_active",
                "status": key.status}
    if KEY_RETIREMENT_RULES.get("require_overlap_elapsed", True):
        overlap = key_overlap_days(key_id)
        if overlap > 0 and key.status == "previous":
            activated = _parse_ts(SIGNER_KEY_RING.active().created_at) if SIGNER_KEY_RING.active() else None
            superseded_at = _parse_ts(key.created_at)
            moment = now or datetime.now(timezone.utc)
            # The overlap runs from the *active* key's creation, which is the
            # instant the previous key stopped being the signing key.
            started = activated or superseded_at
            if started is not None:
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                elapsed = (moment - started).total_seconds() / 86400.0
                if elapsed < overlap:
                    return {
                        "key_id": key_id,
                        "retirable": False,
                        "reason": "overlap_not_elapsed",
                        "overlap_days": overlap,
                        "elapsed_days": round(elapsed, 3),
                        "remaining_days": round(overlap - elapsed, 3),
                    }
    return {"key_id": key_id, "retirable": True, "reason": "ok", "status": key.status}


def build_signer_governance(now: datetime | None = None) -> dict[str, object]:
    """Rotation and retirement governance for the key ring.

    Kept out of :func:`build_hsm_signer_catalog` because that catalog's key set
    is a pinned contract.
    """
    ring = SIGNER_KEY_RING.catalog()
    rotation = plan_key_rotation(now)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strength_ranks": dict(sorted(KEY_STRENGTH_RANKS.items())),
        "active_rank": rank_key(ring["keys"][0]["algorithm"]) if ring["keys"] else 0,
        "rotation": rotation,
        "retirement_rules": dict(KEY_RETIREMENT_RULES),
        "retirement_reasons": list(RETIREMENT_REASONS),
        "retirement": {
            key["key_id"]: can_retire_key(key["key_id"], now) for key in ring["keys"]
        },
        "overlap_days": {
            key["key_id"]: key_overlap_days(key["key_id"]) for key in ring["keys"]
        },
        "downgrade_check": _downgrade_report(),
        "note": (
            "rotation is scheduled in data (KEY_ROTATION_SCHEDULE) and strength is "
            "a total order (KEY_STRENGTH_RANKS), so a downgrade is a reported fact; "
            "retirement requires the overlap window to have elapsed"
        ),
    }


def _downgrade_report() -> dict[str, Any]:
    """Does any verifiable key rank below the active one?

    True is a reportable condition, not an error: an overlap that keeps a
    weaker key verifiable is legitimate, it just has to be a decision.
    """
    keys = SIGNER_KEY_RING.catalog()["keys"]
    active = next((key for key in keys if key["status"] == "active"), None)
    if active is None:
        return {"checked": 0, "downgraded": False, "weaker_key_ids": []}
    active_rank = rank_key(active["algorithm"])
    weaker = [
        key["key_id"]
        for key in keys
        if key["status"] != "retired" and rank_key(key["algorithm"]) < active_rank
    ]
    return {
        "checked": len(keys),
        "active_key_id": active["key_id"],
        "active_algorithm": active["algorithm"],
        "active_rank": active_rank,
        "weaker_key_ids": weaker,
        "downgraded": bool(weaker),
    }


class HsmSigner(Protocol):
    """Minimal HSM signing protocol every backend implements."""

    key_id: str
    algorithm: str

    def sign(self, payload: bytes) -> bytes: ...

    def verify(self, payload: bytes, signature: bytes) -> bool: ...


class HmacHsmSigner:
    """Deterministic software signer: HMAC-SHA256 over the payload."""

    algorithm = "HSM-HS256"

    def __init__(self, key: bytes | None = None, key_id: str = HSM_KEY_ID):
        self.key = key if key is not None else HSM_SIGNER_KEY.encode("utf-8")
        self.key_id = key_id

    def sign(self, payload: bytes) -> bytes:
        return hmac.new(self.key, payload, hashlib.sha256).digest()

    def verify(self, payload: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(signature, self.sign(payload))


class Ed25519HsmSigner:
    """Ed25519 signer backed by ``cryptography`` (ephemeral or PEM-persisted)."""

    algorithm = "HSM-ED25519"

    def __init__(self, key_path: str | None = None, key_id: str = HSM_KEY_ID):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self.key_id = key_id
        path = key_path or HSM_SIGNER_KEY_PATH or None
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                self._key = serialization.load_pem_private_key(fh.read(), password=None)
        else:
            self._key = Ed25519PrivateKey.generate()
            if path:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                pem = self._key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                with open(path, "wb") as fh:
                    fh.write(pem)
        if not hasattr(self._key, "sign"):  # pragma: no cover - defensive
            raise TypeError("Ed25519 private key required")
        self._public = self._key.public_key()

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload)

    def verify(self, payload: bytes, signature: bytes) -> bool:
        try:
            self._public.verify(signature, payload)
            return True
        except Exception:
            return False


class MockHsmSigner:
    """Deterministic signer for tests/contracts (never for production)."""

    algorithm = "HSM-MOCK"

    def __init__(self, key_id: str = HSM_KEY_ID):
        self.key_id = key_id

    def sign(self, payload: bytes) -> bytes:
        return b"mock-signature:" + hashlib.sha256(payload).digest()[:16]

    def verify(self, payload: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(signature, self.sign(payload))


_signer_cache: dict[str, HsmSigner] = {}

# Backend name -> zero-arg factory. A PKCS#11 or cloud-KMS adapter registers
# itself here, which is what keeps business code off a device library.
SIGNER_BACKENDS: dict[str, Callable[[], HsmSigner]] = {
    "hmac": HmacHsmSigner,
    "ed25519": Ed25519HsmSigner,
    "mock": MockHsmSigner,
}


def register_signer_backend(
    name: str, factory: Callable[[], HsmSigner], *, replace: bool = False
) -> None:
    """Register (or replace) a signer backend factory."""
    key = str(name).strip().lower()
    if not key:
        raise ValueError("backend name must be a non-empty string")
    if key in SIGNER_BACKENDS and not replace:
        raise ValueError(
            f"backend {key!r} is already registered; pass replace=True to override"
        )
    SIGNER_BACKENDS[key] = factory
    _signer_cache.pop(key, None)


def registered_backends() -> list[str]:
    return sorted(SIGNER_BACKENDS)


def get_signer(backend: str | None = None) -> HsmSigner:
    """Return the configured (cached) signer for a backend.

    An unrecognized backend still falls back to the software HMAC signer, so a
    typo in ``HSM_BACKEND`` degrades rather than taking the process down.
    """
    backend = backend or HSM_BACKEND
    if backend not in _signer_cache:
        factory = SIGNER_BACKENDS.get(backend)
        _signer_cache[backend] = factory() if factory else HmacHsmSigner()
    return _signer_cache[backend]


def reset_signer_cache() -> None:
    _signer_cache.clear()


# --- key identification --------------------------------------------------------


def public_key_pem(signer: HsmSigner) -> str:
    """Export the public half of ``signer`` as PEM (empty for symmetric keys)."""
    key = getattr(signer, "_public", None)
    if key is None:
        return ""
    try:
        from cryptography.hazmat.primitives import serialization

        return key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
    except Exception:  # pragma: no cover - defensive
        return ""


def key_fingerprint(signer: HsmSigner) -> str:
    """Stable, non-secret identifier for a signing key.

    Prefers the public key, falls back to a digest of the symmetric secret, and
    is prefixed with the algorithm so an id is self-describing.
    """
    material = public_key_pem(signer) or repr(getattr(signer, "key", ""))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{signer.algorithm}:{digest[:32]}"


def signer_info(signer: HsmSigner) -> dict[str, Any]:
    """Non-secret description of a key, safe to expose through metadata."""
    pem = public_key_pem(signer)
    return {
        "key_id": signer.key_id,
        "algorithm": signer.algorithm,
        "fingerprint": key_fingerprint(signer),
        "asymmetric": bool(pem),
        "public_key_pem": pem,
        "signer_class": type(signer).__name__,
    }


# --- key rotation --------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    """One key in the ring, with its lifecycle status."""

    key_id: str
    signer: HsmSigner
    status: str = "active"
    created_at: str = ""

    def to_summary(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "status": self.status,
            "algorithm": self.signer.algorithm,
            "fingerprint": key_fingerprint(self.signer),
            "created_at": self.created_at,
        }


class SigningKeyRing:
    """Several verifiable key ids, exactly one of which signs.

    Rotation is a two-step data change: ``add`` the incoming key, then
    ``activate`` it. The outgoing key stays ``previous`` so envelopes signed
    before the cutover still verify, and is only ``retired`` once its overlap
    window has passed.
    """

    def __init__(self) -> None:
        self._keys: "OrderedDict[str, SigningKey]" = OrderedDict()

    def add(self, key_id: str, signer: HsmSigner, *, status: str = "active") -> SigningKey:
        if status not in SIGNATURE_KEY_STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(SIGNATURE_KEY_STATUSES)}"
            )
        if status == "active":
            self._demote_active()
        key = SigningKey(
            key_id=key_id,
            signer=signer,
            status=status,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._keys[key_id] = key
        return key

    def _demote_active(self) -> None:
        for existing in self._keys.values():
            if existing.status == "active":
                self._keys[existing.key_id] = SigningKey(
                    key_id=existing.key_id,
                    signer=existing.signer,
                    status="previous",
                    created_at=existing.created_at,
                )

    def activate(self, key_id: str) -> SigningKey:
        """Promote ``key_id`` to active; the previous active key is demoted."""
        key = self.get(key_id)
        if key is None:
            raise KeyError(f"unknown key id {key_id!r}")
        self._demote_active()
        promoted = SigningKey(
            key_id=key.key_id,
            signer=key.signer,
            status="active",
            created_at=key.created_at,
        )
        self._keys[key_id] = promoted
        return promoted

    def retire(self, key_id: str) -> SigningKey:
        """Stop accepting signatures from ``key_id`` but keep it inspectable."""
        key = self.get(key_id)
        if key is None:
            raise KeyError(f"unknown key id {key_id!r}")
        if key.status == "active":
            raise ValueError("cannot retire the active key; activate another first")
        retired = SigningKey(
            key_id=key.key_id, signer=key.signer, status="retired", created_at=key.created_at
        )
        self._keys[key_id] = retired
        return retired

    def retire_guarded(
        self, key_id: str, *, now: datetime | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Retire a key only if :func:`can_retire_key` allows it.

        :meth:`retire` keeps its historical rule (never the active key) so every
        existing call site behaves identically. This one adds the governance
        rules: the overlap window must have elapsed, so retiring a key cannot
        orphan an envelope that was signed while it was still current. A refusal
        is reported rather than raised, because "why not" is the answer an
        operator needs.
        """
        verdict = can_retire_key(key_id, now)
        if not verdict["retirable"] and not force:
            return {
                "key_id": key_id,
                "retired": False,
                "forced": False,
                **{k: v for k, v in verdict.items() if k != "key_id"},
            }
        if verdict["reason"] == "unknown_key":
            raise KeyError(f"unknown key id {key_id!r}")
        retired = self.retire(key_id)
        return {
            "key_id": key_id,
            "retired": True,
            "forced": bool(force),
            "reason": "ok",
            "status": retired.status,
        }

    def get(self, key_id: str | None) -> SigningKey | None:
        if key_id is None:
            return self.active()
        return self._keys.get(key_id)

    def active(self) -> SigningKey | None:
        for key in self._keys.values():
            if key.status == "active":
                return key
        return None

    def previous_key_ids(self) -> list[str]:
        return [k.key_id for k in self._keys.values() if k.status == "previous"]

    def verifiable_key_ids(self) -> list[str]:
        return [k.key_id for k in self._keys.values() if k.status != "retired"]

    def key_ids(self) -> list[str]:
        return list(self._keys)

    def signer_for(self, key_id: str | None = None) -> HsmSigner | None:
        """Signer for ``key_id``, or the active one when unspecified."""
        key = self.get(key_id)
        return key.signer if key else None

    def verify(self, payload: bytes, signature: bytes, *, key_id: str | None = None) -> str | None:
        """Verify against the named key, else every still-verifiable key.

        Returns the key id that validated the signature, or ``None``.
        """
        candidates = (
            [self._keys[key_id]]
            if key_id and key_id in self._keys
            else [k for k in self._keys.values() if k.status != "retired"]
        )
        for key in candidates:
            if key.signer.verify(payload, signature):
                return key.key_id
        return None

    def catalog(self) -> dict[str, Any]:
        active = self.active()
        return {
            "size": len(self._keys),
            "active_key_id": active.key_id if active else None,
            "previous_key_ids": self.previous_key_ids(),
            "verifiable_key_ids": self.verifiable_key_ids(),
            "keys": [k.to_summary() for k in self._keys.values()],
        }

    def reset(self) -> None:
        self._keys.clear()


# Opt-in: verification falls back to ``get_signer()`` unless a ring is passed
# explicitly, so rotation never changes legacy behaviour by accident.
SIGNER_KEY_RING = SigningKeyRing()


class ReplayGuard:
    """Bounded nonce memory for anti-replay on signed envelopes.

    Deliberately in-process and LRU-bounded: it is a fast local filter, not a
    distributed replay store.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self.capacity = max(1, int(capacity))
        self._seen: "OrderedDict[str, None]" = OrderedDict()

    def check_and_remember(self, nonce: str) -> bool:
        """``True`` when ``nonce`` is fresh (and now remembered), else ``False``."""
        if not nonce:
            return False
        if nonce in self._seen:
            self._seen.move_to_end(nonce)
            return False
        self._seen[nonce] = None
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return True

    def forget(self, nonce: str) -> None:
        self._seen.pop(nonce, None)

    def size(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        self._seen.clear()


REPLAY_GUARD = ReplayGuard()


# --- signatures ----------------------------------------------------------------


@dataclass(frozen=True)
class HsmSignature:
    key_id: str
    algorithm: str
    signature: bytes
    payload_sha256: str
    signed_at: str
    nonce: str = ""
    expires_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "signature": base64.b64encode(self.signature).decode("ascii"),
            "payload_sha256": self.payload_sha256,
            "signed_at": self.signed_at,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
        }


def sign_bytes(
    payload: bytes,
    signer: HsmSigner | None = None,
    *,
    nonce: str | None = None,
    ttl_seconds: int | None = None,
) -> HsmSignature:
    """Sign ``payload``; optionally bind a nonce and an expiry into the result."""
    signer = signer or get_signer()
    signed_at = datetime.now(timezone.utc)
    expires_at = (
        (signed_at + timedelta(seconds=int(ttl_seconds))).isoformat()
        if ttl_seconds
        else None
    )
    return HsmSignature(
        key_id=signer.key_id,
        algorithm=signer.algorithm,
        signature=signer.sign(payload),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        signed_at=signed_at.isoformat(),
        nonce=nonce if nonce is not None else uuid.uuid4().hex,
        expires_at=expires_at,
    )


def verify_bytes(payload: bytes, signature: bytes, signer: HsmSigner | None = None) -> bool:
    return (signer or get_signer()).verify(payload, signature)


def _canonical_bytes(data: dict) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_envelope(
    data: dict,
    signer: HsmSigner | None = None,
    *,
    nonce: str | None = None,
    ttl_seconds: int | None = None,
    extra: dict | None = None,
) -> dict[str, Any]:
    """Sign arbitrary JSON and return a self-contained signed envelope.

    ``nonce``/``ttl_seconds`` opt the envelope into replay and freshness
    checking; both are omitted from the wire format when unset, so a plain
    ``sign_envelope(data)`` is byte-identical to the original contract.
    """
    signer = signer or get_signer()
    payload_bytes = _canonical_bytes(data)
    sig = sign_bytes(payload_bytes, signer, nonce=nonce, ttl_seconds=ttl_seconds)
    envelope = {
        "payload": base64.b64encode(payload_bytes).decode("ascii"),
        "key_id": sig.key_id,
        "algorithm": sig.algorithm,
        "signed_at": sig.signed_at,
        "payload_sha256": sig.payload_sha256,
        "signature": base64.b64encode(sig.signature).decode("ascii"),
    }
    if sig.expires_at:
        envelope["expires_at"] = sig.expires_at
    if nonce is not None:
        envelope["nonce"] = sig.nonce
    if extra:
        for key, value in extra.items():
            envelope.setdefault(key, value)
    return envelope


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def verify_envelope_detailed(
    envelope: dict,
    signer: HsmSigner | None = None,
    *,
    policy: dict | None = None,
    ring: SigningKeyRing | None = None,
    replay_guard: ReplayGuard | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify an envelope and report *why* it passed or failed.

    Returns ``{"valid", "reason", "key_id", "algorithm", "expires_at"}`` with
    ``reason`` drawn from :data:`VERIFY_REASONS`. The checks are layered: shape,
    payload digest, algorithm/key policy, freshness, replay, then signature.
    """
    rules = {**SIGNATURE_POLICY_DEFAULTS, **dict(policy or {})}
    result: dict[str, Any] = {
        "valid": False,
        "reason": "malformed_envelope",
        "key_id": (envelope or {}).get("key_id"),
        "algorithm": (envelope or {}).get("algorithm"),
        "expires_at": (envelope or {}).get("expires_at"),
    }
    try:
        payload_bytes = base64.b64decode(envelope["payload"].encode("ascii"))
        signature = base64.b64decode(envelope["signature"].encode("ascii"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return result

    digest = hashlib.sha256(payload_bytes).hexdigest()
    if envelope.get("payload_sha256") and digest != envelope["payload_sha256"]:
        result["reason"] = "payload_digest_mismatch"
        return result

    if rules.get("check_algorithm"):
        allowed = rules.get("allowed_algorithms") or ()
        if allowed and envelope.get("algorithm") not in allowed:
            result["reason"] = "unknown_algorithm"
            return result

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    expires_at = _parse_ts(envelope.get("expires_at"))
    if rules.get("require_expiry") and expires_at is None:
        result["reason"] = "malformed_envelope"
        return result
    tolerance = timedelta(seconds=int(rules.get("expiry_tolerance_seconds") or 0))
    if expires_at is not None and reference - tolerance > expires_at:
        result["reason"] = "expired"
        return result
    not_before = _parse_ts(envelope.get("not_before"))
    skew = timedelta(seconds=int(rules.get("not_before_skew_seconds") or 0))
    if not_before is not None and reference + skew < not_before:
        result["reason"] = "not_yet_valid"
        return result

    if replay_guard is not None and envelope.get("nonce"):
        if not replay_guard.check_and_remember(str(envelope["nonce"])):
            result["reason"] = "replayed"
            return result

    claimed_key_id = envelope.get("key_id")
    if signer is None and ring is not None:
        # Ring-backed verification: honour the key the envelope claims, then
        # fall back to the rest of the ring (an envelope signed just before a
        # rotation is the common case).
        if rules.get("check_key_id") and claimed_key_id not in ring.verifiable_key_ids():
            result["reason"] = "key_id_mismatch"
            return result
        candidate = ring.signer_for(claimed_key_id)
        if candidate is not None and candidate.verify(payload_bytes, signature):
            result.update({"valid": True, "reason": "ok"})
            return result
        matched = ring.verify(payload_bytes, signature)
        if matched:
            result.update({"valid": True, "reason": "ok", "key_id": matched})
            return result
        result["reason"] = "signature_invalid"
        return result

    # Legacy single-key path: the signature itself is the proof of key id, so
    # ``check_key_id`` only applies to ring-backed verification.
    resolved = signer or get_signer()
    if resolved.verify(payload_bytes, signature):
        result.update({"valid": True, "reason": "ok"})
    else:
        result["reason"] = "signature_invalid"
    return result


def verify_envelope(
    envelope: dict,
    signer: HsmSigner | None = None,
    *,
    policy: dict | None = None,
    ring: SigningKeyRing | None = None,
    replay_guard: ReplayGuard | None = None,
) -> bool:
    """Verify an envelope's integrity (payload unchanged + signature valid)."""
    return verify_envelope_detailed(
        envelope,
        signer,
        policy=policy,
        ring=ring,
        replay_guard=replay_guard,
    )["valid"]


def decode_envelope(envelope: dict) -> dict | None:
    """Return the original data of a signed envelope (without verifying)."""
    try:
        payload_bytes = base64.b64decode(envelope["payload"].encode("ascii"))
        return json.loads(payload_bytes.decode("utf-8"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


# --- multi-authority (N-of-M) co-signature ------------------------------------


@dataclass(frozen=True)
class CoSignature:
    """One authority's detached signature over a shared payload."""

    key_id: str
    algorithm: str
    signature: str  # base64
    signed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "signature": self.signature,
            "signed_at": self.signed_at,
        }


def sign_multi(
    payload: bytes, signers: list[HsmSigner] | None = None
) -> dict[str, Any]:
    """Collect one detached signature per signer into one co-signed document."""
    authorities = signers or [get_signer()]
    signatures = [
        CoSignature(
            key_id=signer.key_id,
            algorithm=signer.algorithm,
            signature=base64.b64encode(signer.sign(payload)).decode("ascii"),
            signed_at=datetime.now(timezone.utc).isoformat(),
        )
        for signer in authorities
    ]
    return {
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "authorities": len(authorities),
        "cosignatures": [sig.to_dict() for sig in signatures],
        "threshold": len(authorities),
    }


def verify_multi(
    payload: bytes,
    document: dict,
    *,
    threshold: int = 1,
    ring: SigningKeyRing | None = None,
    signers: dict[str, HsmSigner] | None = None,
) -> dict[str, Any]:
    """Check a co-signed document against an N-of-M threshold.

    Keys are resolved from ``ring`` first, then ``signers``, then the default
    signer — so a caller without a key source still validates the authorities
    that share the process's configured key.
    """
    result: dict[str, Any] = {
        "valid": False,
        "reason": "threshold_not_met",
        "threshold": int(threshold),
        "required": int(threshold),
        "offered": len(document.get("cosignatures") or []),
        "verified_key_ids": [],
    }
    if document.get("payload_sha256") and hashlib.sha256(payload).hexdigest() != document[
        "payload_sha256"
    ]:
        result["reason"] = "payload_digest_mismatch"
        return result

    def _resolve(key_id: Any) -> HsmSigner | None:
        if ring is not None:
            found = ring.signer_for(str(key_id) if key_id is not None else None)
            if found is not None:
                return found
        if signers is not None and key_id in signers:
            return signers[key_id]
        default = get_signer()
        return default if key_id in (None, default.key_id) else None

    verified: list[str] = []
    for record in document.get("cosignatures") or []:
        try:
            raw = base64.b64decode(str(record["signature"]).encode("ascii"))
        except (KeyError, TypeError, ValueError):
            continue
        key_id = record.get("key_id")
        signer = _resolve(key_id)
        if signer is not None and signer.verify(payload, raw):
            verified.append(str(key_id))
    result["verified_key_ids"] = verified
    if int(threshold) > 0 and len(verified) >= int(threshold):
        result.update({"valid": True, "reason": "ok"})
    return result


# --- health --------------------------------------------------------------------


def signer_self_test(signer: HsmSigner | None = None) -> dict[str, Any]:
    """Round-trip and tamper probe for one signer."""
    active = signer or get_signer()
    probe = b"cservice-hsm-self-test"
    signature = active.sign(probe)
    roundtrip = active.verify(probe, signature)
    tampered = active.verify(probe + b"!", signature)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "key_id": active.key_id,
        "algorithm": active.algorithm,
        "signer_class": type(active).__name__,
        "roundtrip": bool(roundtrip),
        "tamper_detected": not tampered,
        "healthy": bool(roundtrip) and not tampered,
    }


def build_signer_health(backends: list[str] | None = None) -> dict[str, Any]:
    """Self-test the active signer, or each named backend."""
    results = [
        signer_self_test(get_signer(backend))
        for backend in (backends if backends is not None else [HSM_BACKEND])
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checked": len(results),
        "healthy": all(row["healthy"] for row in results),
        "signers": results,
    }


def build_hsm_signer_catalog() -> dict[str, object]:
    signer = get_signer()
    self_test = signer_self_test(signer)
    return {
        "backend": HSM_BACKEND,
        "key_id": signer.key_id,
        "algorithm": signer.algorithm,
        "supported_backends": list(SUPPORTED_BACKENDS),
        "signature_scheme": "detached signature over canonical JSON payload",
        "hash": "sha256",
        "key_source": "env/persisted PEM" if HSM_SIGNER_KEY_PATH else "env secret (derived)",
        # --- expansion surface -------------------------------------------------
        "registered_backends": registered_backends(),
        "backend_registry": {
            name: getattr(factory, "__name__", repr(factory))
            for name, factory in sorted(SIGNER_BACKENDS.items())
        },
        "key": signer_info(signer),
        "key_statuses": list(SIGNATURE_KEY_STATUSES),
        "key_ring": SIGNER_KEY_RING.catalog(),
        "verify_reasons": list(VERIFY_REASONS),
        "policy_defaults": dict(SIGNATURE_POLICY_DEFAULTS),
        "replay_guard": {
            "capacity": REPLAY_GUARD.capacity,
            "remembered": REPLAY_GUARD.size(),
        },
        "self_test": self_test,
        # Governance lives in its own catalog because this key set is pinned.
        "governance": {
            "catalog": "build_signer_governance",
            "retirement_reasons": list(RETIREMENT_REASONS),
            "strength_ranks": dict(sorted(KEY_STRENGTH_RANKS.items())),
        },
        "note": (
            "backends register through SIGNER_BACKENDS; rotation is data (the key "
            "ring), and verification is opt-in via ring= so legacy single-key "
            "envelopes keep verifying unchanged"
        ),
    }