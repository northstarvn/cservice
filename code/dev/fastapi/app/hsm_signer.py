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
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.security import SECRET_KEY

HSM_BACKEND = os.getenv("HSM_BACKEND", "hmac")
HSM_KEY_ID = os.getenv("HSM_KEY_ID", "cservice-hsm-key-001")
HSM_SIGNER_KEY = os.getenv("HSM_SIGNER_KEY", "") or SECRET_KEY
HSM_SIGNER_KEY_PATH = os.getenv("HSM_SIGNER_KEY_PATH", "")

SUPPORTED_BACKENDS = ("hmac", "ed25519", "mock")


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


def get_signer(backend: str | None = None) -> HsmSigner:
    """Return the configured (cached) signer for a backend."""
    backend = backend or HSM_BACKEND
    if backend not in _signer_cache:
        if backend == "ed25519":
            _signer_cache[backend] = Ed25519HsmSigner()
        elif backend == "mock":
            _signer_cache[backend] = MockHsmSigner()
        else:
            _signer_cache[backend] = HmacHsmSigner()
    return _signer_cache[backend]


def reset_signer_cache() -> None:
    _signer_cache.clear()


@dataclass(frozen=True)
class HsmSignature:
    key_id: str
    algorithm: str
    signature: bytes
    payload_sha256: str
    signed_at: str


def sign_bytes(payload: bytes, signer: HsmSigner | None = None) -> HsmSignature:
    signer = signer or get_signer()
    return HsmSignature(
        key_id=signer.key_id,
        algorithm=signer.algorithm,
        signature=signer.sign(payload),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        signed_at=datetime.now(timezone.utc).isoformat(),
    )


def verify_bytes(payload: bytes, signature: bytes, signer: HsmSigner | None = None) -> bool:
    return (signer or get_signer()).verify(payload, signature)


def _canonical_bytes(data: dict) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_envelope(data: dict, signer: HsmSigner | None = None) -> dict[str, Any]:
    """Sign arbitrary JSON and return a self-contained signed envelope."""
    signer = signer or get_signer()
    payload_bytes = _canonical_bytes(data)
    sig = sign_bytes(payload_bytes, signer)
    return {
        "payload": base64.b64encode(payload_bytes).decode("ascii"),
        "key_id": sig.key_id,
        "algorithm": sig.algorithm,
        "signed_at": sig.signed_at,
        "payload_sha256": sig.payload_sha256,
        "signature": base64.b64encode(sig.signature).decode("ascii"),
    }


def verify_envelope(envelope: dict, signer: HsmSigner | None = None) -> bool:
    """Verify an envelope's integrity (payload unchanged + signature valid)."""
    try:
        payload_bytes = base64.b64decode(envelope["payload"].encode("ascii"))
        signature = base64.b64decode(envelope["signature"].encode("ascii"))
    except (KeyError, TypeError, ValueError):
        return False
    if envelope.get("payload_sha256") and hashlib.sha256(payload_bytes).hexdigest() != envelope["payload_sha256"]:
        return False
    return verify_bytes(payload_bytes, signature, signer)


def decode_envelope(envelope: dict) -> dict | None:
    """Return the original data of a signed envelope (without verifying)."""
    try:
        payload_bytes = base64.b64decode(envelope["payload"].encode("ascii"))
        return json.loads(payload_bytes.decode("utf-8"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_hsm_signer_catalog() -> dict[str, object]:
    signer = get_signer()
    return {
        "backend": HSM_BACKEND,
        "key_id": signer.key_id,
        "algorithm": signer.algorithm,
        "supported_backends": list(SUPPORTED_BACKENDS),
        "signature_scheme": "detached signature over canonical JSON payload",
        "hash": "sha256",
        "key_source": "env/persisted PEM" if HSM_SIGNER_KEY_PATH else "env secret (derived)",

    }