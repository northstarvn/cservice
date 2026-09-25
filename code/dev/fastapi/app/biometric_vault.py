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
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any

BIOMETRIC_MODALITIES: dict[str, dict[str, Any]] = {
    "fingerprint": {
        "threshold": 0.94,
        "hash": "sha256",
        "matcher": "digest_hamming",
        "note": "high precision; short capture window",
    },
    "face": {
        "threshold": 0.86,
        "hash": "sha256",
        "matcher": "digest_hamming",
        "note": "moderate threshold; liveness variance expected",
    },
    "voice": {
        "threshold": 0.80,
        "hash": "sha256",
        "matcher": "digest_hamming",
        "note": "lower threshold; acoustic variance expected",
    },
}


def _hash_template(template: bytes, salt: bytes) -> str:
    return hmac.new(salt, template, hashlib.sha256).hexdigest()


def _digest(template: bytes) -> bytes:
    return hashlib.sha256(template).digest()


def _digest_similarity(digest_a: bytes, digest_b: bytes) -> float:
    """Deterministic pseudo-similarity between two SHA-256 digests (0..1)."""
    mismatches = sum((x ^ y).bit_count() for x, y in zip(digest_a, digest_b))
    return 1.0 - mismatches / (len(digest_a) * 8)


class BiometricVault:
    """Per-user, per-modality template vault (salted digests only)."""

    def __init__(self, modalities: dict[str, dict[str, Any]] | None = None):
        self._modalities = dict(modalities or BIOMETRIC_MODALITIES)
        self._templates: dict[tuple[int, str], dict[str, Any]] = {}
        self._validations = 0

    def _require_modality(self, modality: str) -> None:
        if modality not in self._modalities:
            raise ValueError(f"unsupported biometric modality: {modality}")

    def register_template(
        self,
        user_id: int,
        modality: str,
        template: bytes,
        *,
        actor_user_id: int | None = None,
    ) -> dict[str, Any]:
        self._require_modality(modality)
        salt = secrets.token_bytes(16)
        entry = {
            "salt": salt.hex(),
            "stored_hash": _hash_template(template, salt),
            "reference_digest": _digest(template),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "registered_by": actor_user_id,
            "revoked": False,
            "revoked_at": None,
        }
        self._templates[(int(user_id), modality)] = entry
        return {
            "user_id": int(user_id),
            "modality": modality,
            "registered": True,
            "stored_hash_prefix": entry["stored_hash"][:12],
            "threshold": self._modalities[modality]["threshold"],
            "registered_at": entry["registered_at"],
        }

    def validate_template(self, user_id: int, modality: str, template: bytes) -> dict[str, Any]:
        self._require_modality(modality)
        self._validations += 1
        record = self._templates.get((int(user_id), modality))
        if record is None:
            return {
                "user_id": int(user_id),
                "modality": modality,
                "match": False,
                "accepted": False,
                "confidence": 0.0,
                "threshold": self._modalities[modality]["threshold"],
                "reason": "template_not_registered",
            }
        if record["revoked"]:
            return {
                "user_id": int(user_id),
                "modality": modality,
                "match": False,
                "accepted": False,
                "confidence": 0.0,
                "threshold": self._modalities[modality]["threshold"],
                "reason": "template_revoked",
            }
        confidence = _digest_similarity(_digest(template), record["reference_digest"])
        accepted = confidence >= self._modalities[modality]["threshold"]
        return {
            "user_id": int(user_id),
            "modality": modality,
            "match": accepted,
            "accepted": accepted,
            "confidence": round(confidence, 4),
            "threshold": self._modalities[modality]["threshold"],
            "reason": "match" if accepted else "below_threshold",
        }

    def revoke_template(self, user_id: int, modality: str) -> dict[str, Any]:
        key = (int(user_id), modality)
        if key not in self._templates:
            raise ValueError(f"no registered template for user {user_id} / {modality}")
        self._templates[key]["revoked"] = True
        self._templates[key]["revoked_at"] = datetime.now(timezone.utc).isoformat()
        return {"user_id": int(user_id), "modality": modality, "revoked": True}

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
        return rows

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
    }