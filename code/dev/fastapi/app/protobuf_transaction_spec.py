"""Immutable, append-only protocol-buffer transaction format.

Standardizes the wire format for every high-velocity audit/security
transaction so downstream integrity checks never depend on JSON-shaped rows.
The encoder/decoder here implements the protobuf wire format directly (field
tags, varints, length-delimited values), so any standard protobuf toolchain
can consume the emitted bytes given the schema in ``TRANSACTION_FIELDS`` — no
``protoc`` step and no generated-code dependency in this repo.

Guarantees:

- **Deterministic bytes** — fields are emitted in ascending field-number
  order with canonical JSON in ``payload_json``; equal transactions produce
  identical bytes (diffable, hashable).
- **Append-only** — ``TransactionLog`` only ever appends (optionally persisted
  to an append-only base64 line file at ``TRANSACTION_LOG_PATH``); there is no
  update/delete surface.
- **Hash-chained** — every frame carries the SHA-256 of the previous frame in
  ``prev_hash`` (field 11), so tampering breaks ``verify_chain``.
- **Signable** — ``sign_transaction`` attaches an HSM signature over the
  unsigned canonical bytes; re-encoding never changes the signed payload.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.hsm_signer import HsmSigner, get_signer

SPEC_VERSION = 1
TRANSACTION_LOG_PATH = os.getenv("TRANSACTION_LOG_PATH", "")

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH_DELIMITED = 2
WIRE_32BIT = 5

# The protobuf schema. field numbers are part of the public contract — never
# renumber; append new fields at the end.
TRANSACTION_FIELDS: list[dict[str, Any]] = [
    {"field": 1, "name": "version", "type": "uint32", "wire": WIRE_VARINT, "required": True},
    {"field": 2, "name": "tx_id", "type": "string", "wire": WIRE_LENGTH_DELIMITED, "required": True},
    {"field": 3, "name": "cursor", "type": "uint64", "wire": WIRE_VARINT, "required": True},
    {"field": 4, "name": "action", "type": "string", "wire": WIRE_LENGTH_DELIMITED, "required": True},
    {"field": 5, "name": "entity_type", "type": "string", "wire": WIRE_LENGTH_DELIMITED, "default": "system"},
    {"field": 6, "name": "entity_id", "type": "string", "wire": WIRE_LENGTH_DELIMITED, "default": ""},
    {"field": 7, "name": "actor_user_id", "type": "uint64", "wire": WIRE_VARINT, "optional": True},
    {"field": 8, "name": "tenant_id", "type": "string", "wire": WIRE_LENGTH_DELIMITED, "optional": True},
    {"field": 9, "name": "occurred_at_millis", "type": "uint64", "wire": WIRE_VARINT, "required": True},
    {"field": 10, "name": "payload_json", "type": "bytes", "wire": WIRE_LENGTH_DELIMITED, "required": True},
    {"field": 11, "name": "prev_hash", "type": "bytes", "wire": WIRE_LENGTH_DELIMITED, "default_hex": ""},
    {"field": 12, "name": "signature", "type": "bytes", "wire": WIRE_LENGTH_DELIMITED, "optional": True},
]
FIELD_BY_NUMBER = {entry["field"]: entry for entry in TRANSACTION_FIELDS}


def _encode_varint(value: int) -> bytes:
    value = int(value)
    if value < 0:
        raise ValueError("negative values cannot be varint-encoded")
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint overflow")


def _tag(field_number: int, wire_type: int) -> int:
    return (field_number << 3) | wire_type


def _key_bytes(field_number: int, wire_type: int) -> bytes:
    return _encode_varint(_tag(field_number, wire_type))


def _len_delimited(value: bytes) -> bytes:
    return _encode_varint(len(value)) + value


def _canonical_json(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Transaction:
    """One immutable transaction frame in the protobuf wire format."""

    version: int = SPEC_VERSION
    tx_id: str = ""
    cursor: int = 0
    action: str = ""
    entity_type: str = "system"
    entity_id: str = ""
    actor_user_id: Optional[int] = None
    tenant_id: Optional[str] = None
    payload: dict = field(default_factory=dict)
    prev_hash: bytes = b""
    signature: bytes = b""
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_bytes(self, *, include_signature: bool = True) -> bytes:
        chunks = [
            _key_bytes(1, WIRE_VARINT) + _encode_varint(self.version),
            _key_bytes(2, WIRE_LENGTH_DELIMITED) + _len_delimited(self.tx_id.encode("utf-8")),
            _key_bytes(3, WIRE_VARINT) + _encode_varint(self.cursor),
            _key_bytes(4, WIRE_LENGTH_DELIMITED) + _len_delimited(self.action.encode("utf-8")),
            _key_bytes(5, WIRE_LENGTH_DELIMITED) + _len_delimited(self.entity_type.encode("utf-8")),
            _key_bytes(6, WIRE_LENGTH_DELIMITED) + _len_delimited(str(self.entity_id or "").encode("utf-8")),
        ]
        if self.actor_user_id is not None:
            chunks.append(_key_bytes(7, WIRE_VARINT) + _encode_varint(self.actor_user_id))
        if self.tenant_id:
            chunks.append(_key_bytes(8, WIRE_LENGTH_DELIMITED) + _len_delimited(self.tenant_id.encode("utf-8")))
        chunks.append(
            _key_bytes(9, WIRE_VARINT)
            + _encode_varint(int(self.occurred_at.timestamp() * 1000))
        )
        chunks.append(
            _key_bytes(10, WIRE_LENGTH_DELIMITED) + _len_delimited(_canonical_json(self.payload))
        )
        chunks.append(_key_bytes(11, WIRE_LENGTH_DELIMITED) + _len_delimited(self.prev_hash))
        if include_signature and self.signature:
            chunks.append(_key_bytes(12, WIRE_LENGTH_DELIMITED) + _len_delimited(self.signature))
        return b"".join(chunks)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Transaction":
        values: dict[str, Any] = {}
        offset = 0
        while offset < len(data):
            key, offset = _decode_varint(data, offset)
            field_number = key >> 3
            wire_type = key & 0x07
            entry = FIELD_BY_NUMBER.get(field_number)
            if wire_type == WIRE_VARINT:
                value, offset = _decode_varint(data, offset)
                if entry:
                    values[entry["name"]] = value
            elif wire_type == WIRE_LENGTH_DELIMITED:
                length, offset = _decode_varint(data, offset)
                value = data[offset : offset + length]
                offset += length
                if entry:
                    values[entry["name"]] = value
            elif wire_type in (WIRE_64BIT,):
                offset += 8
            elif wire_type == WIRE_32BIT:
                offset += 4
            else:  # pragma: no cover - defensive
                raise ValueError(f"unsupported wire type {wire_type}")
        return cls(
            version=int(values.get("version", SPEC_VERSION)),
            tx_id=_as_text(values.get("tx_id", b"")),
            cursor=int(values.get("cursor", 0)),
            action=_as_text(values.get("action", b"")),
            entity_type=_as_text(values.get("entity_type", b"system")),
            entity_id=_as_text(values.get("entity_id", b"")),
            actor_user_id=int(values["actor_user_id"])
            if values.get("actor_user_id") is not None
            else None,
            tenant_id=_as_text(values["tenant_id"]) if values.get("tenant_id") else None,
            payload=_parse_payload(values.get("payload_json", b"{}")),
            prev_hash=values.get("prev_hash", b""),
            signature=values.get("signature", b""),
            occurred_at=datetime.fromtimestamp(
                int(values.get("occurred_at_millis", 0)) / 1000.0, tz=timezone.utc
            ),
        )

    def signed_bytes(self) -> bytes:
        """Canonical bytes excluding the signature field (what we sign)."""
        return self.to_bytes(include_signature=False)

    def digest(self) -> bytes:
        return hashlib.sha256(self.to_bytes(include_signature=True)).digest()

    def sha256_hex(self) -> str:
        return hashlib.sha256(self.to_bytes(include_signature=True)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tx_id": self.tx_id,
            "cursor": self.cursor,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "actor_user_id": self.actor_user_id,
            "tenant_id": self.tenant_id,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
            "prev_hash": self.prev_hash.hex(),
            "signature_present": bool(self.signature),
        }


def _as_text(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return raw


def _parse_payload(raw: bytes | str) -> dict:
    try:
        return json.loads(_as_text(raw) if isinstance(raw, bytes) else raw or "{}")
    except (ValueError, TypeError):
        return {}


def sign_transaction(tx: Transaction, signer: HsmSigner | None = None) -> Transaction:
    """Return a copy of ``tx`` with an HSM signature over the canonical bytes."""
    signer = signer or get_signer()
    return Transaction(
        version=tx.version,
        tx_id=tx.tx_id,
        cursor=tx.cursor,
        action=tx.action,
        entity_type=tx.entity_type,
        entity_id=tx.entity_id,
        actor_user_id=tx.actor_user_id,
        tenant_id=tx.tenant_id,
        payload=dict(tx.payload),
        prev_hash=tx.prev_hash,
        signature=signer.sign(tx.signed_bytes()),
        occurred_at=tx.occurred_at,
    )


def verify_transaction_signature(tx: Transaction, signer: HsmSigner | None = None) -> bool:
    """Verify a transaction's attached signature against its canonical bytes."""
    if not tx.signature:
        return True  # unsigned frames are permitted
    return (signer or get_signer()).verify(tx.signed_bytes(), tx.signature)


class TransactionLog:
    """Append-only transaction log with optional file persistence."""

    def __init__(self, path: str | None = None):
        self.path = path or TRANSACTION_LOG_PATH or None
        self._entries: list[Transaction] = []
        self._cursor = 0
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    tx = Transaction.from_bytes(base64.b64decode(line.encode("ascii")))
                except (ValueError, binascii.Error):
                    continue
                self._entries.append(tx)
                self._cursor = max(self._cursor, int(tx.cursor or 0))

    def _persist(self, tx: Transaction) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(base64.b64encode(tx.to_bytes()).decode("ascii") + "\n")

    def append(
        self,
        *,
        action: str,
        tx_id: str | None = None,
        entity_type: str = "system",
        entity_id: str = "",
        actor_user_id: int | None = None,
        tenant_id: str | None = None,
        payload: dict | None = None,
        occurred_at: datetime | None = None,
        signature: bytes = b"",
    ) -> Transaction:
        """Append one immutable frame; chains to the previous frame's hash."""
        self._cursor += 1
        prev_hash = self._entries[-1].digest() if self._entries else b""
        tx = Transaction(
            version=SPEC_VERSION,
            tx_id=tx_id or uuid.uuid4().hex,
            cursor=self._cursor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id or ""),
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            payload=dict(payload or {}),
            prev_hash=prev_hash,
            signature=signature,
            occurred_at=occurred_at or datetime.now(timezone.utc),
        )
        self._entries.append(tx)
        self._persist(tx)
        return tx

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        return [tx.to_dict() for tx in self._entries[-n:]]

    def all(self) -> list[Transaction]:
        return list(self._entries)

    def total(self) -> int:
        return len(self._entries)

    def verify_chain(self) -> dict[str, Any]:
        broken_at = None
        for index in range(1, len(self._entries)):
            if self._entries[index].prev_hash != self._entries[index - 1].digest():
                broken_at = index
                break
        return {
            "valid": broken_at is None,
            "entries": len(self._entries),
            "broken_at": broken_at,
            "algo": "sha256",
            "policy": "append-only immutable frames",
        }


DEFAULT_TRANSACTION_LOG = TransactionLog(path=TRANSACTION_LOG_PATH)


def get_default_transaction_log() -> TransactionLog:
    return DEFAULT_TRANSACTION_LOG


def set_default_transaction_log(log: TransactionLog) -> None:
    global DEFAULT_TRANSACTION_LOG
    DEFAULT_TRANSACTION_LOG = log


def build_transaction_spec_catalog() -> dict[str, object]:
    return {
        "version": SPEC_VERSION,
        "encoding": "protobuf wire format (varint + length-delimited)",
        "fields": [dict(entry) for entry in TRANSACTION_FIELDS],
        "wire_types": {WIRE_VARINT: "varint", WIRE_64BIT: "64-bit", WIRE_LENGTH_DELIMITED: "length-delimited", WIRE_32BIT: "32-bit"},
        "hash_chain": {
            "algo": "sha256",
            "prev_hash_field": 11,
            "policy": "append-only immutable frames",
        },
        "log": {
            "path": TRANSACTION_LOG_PATH or None,
            "persistence": "append-only base64 lines" if TRANSACTION_LOG_PATH else "in-memory default",
            "entries": get_default_transaction_log().total(),
        },
    }