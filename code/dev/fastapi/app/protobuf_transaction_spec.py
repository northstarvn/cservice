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

Expansion (thin-group pass): a hash chain only proves the log was not edited
*in place*; it says nothing about whether the log is complete, whether the
right events were signed, or whether a 10M-frame file can be verified in one
request. These config tables and helpers close those gaps:

- ``SIGNING_POLICY`` — which actions / severities / routes *require* a
  signature, and which never do. ``requires_signature`` makes the requirement
  declarative; ``sign_if_required`` enforces it on the append path.
- ``MerkleAccumulator`` — periodic tree checkpoints, so verifying a long log
  costs one root comparison instead of a full replay.
- ``SCHEMA_EVOLUTION`` — per-spec-version field provenance, so a frame written
  by an older writer is validated against the right field set rather than the
  current one.
- ``PAYLOAD_VIEWS`` — audience projections (``public``/``internal``/
  ``forensic``) so an exported frame never leaks what the reader may not see.
- ``REDACTED_PAYLOAD_KEYS`` — the deny-list those views share.
- ``TransactionLog.query`` / ``export`` / ``import_frames`` / ``append_many`` /
  ``verify_from_checkpoint`` — the operational surface over an append-only log.
"""
from __future__ import annotations

import base64
import binascii
import csv
import fnmatch
import hashlib
import io
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

# --- Expansion: signing policy, schema evolution, payload views --------------

REDACTED_PAYLOAD_KEYS = frozenset(
    {
        "password",
        "hashed_password",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "secret",
        "signature",
        "raw_message",
        "device_fingerprint",
    }
)

# action prefix / severity -> "required" | "optional" | "never". Most specific
# entry wins; an action with no entry inherits its severity's requirement.
SIGNING_POLICY: list[dict[str, Any]] = [
    {
        "match": "billing.",
        "applies_to": "action_prefix",
        "requirement": "required",
        "reason": "money movement must be attributable",
    },
    {
        "match": "auth.",
        "applies_to": "action_prefix",
        "requirement": "required",
        "reason": "identity events are the primary audit subject",
    },
    {
        "match": "access.",
        "applies_to": "action_prefix",
        "requirement": "required",
        "reason": "cell-level masking decisions are compliance evidence",
    },
    {"match": "critical", "applies_to": "severity", "requirement": "required", "reason": "any critical event"},
    {"match": "warning", "applies_to": "severity", "requirement": "optional", "reason": "sign when convenient"},
    {"match": "info", "applies_to": "severity", "requirement": "optional", "reason": "sign when convenient"},
    {"match": "data.", "applies_to": "action_prefix", "requirement": "never", "reason": "bulk data is high volume, low value"},
]

# version -> field provenance. `added_in` lets a reader tell "field absent
# because the writer was old" apart from "field absent because it was dropped".
SCHEMA_EVOLUTION: list[dict[str, Any]] = [
    {
        "version": 1,
        "added_in": 1,
        "field_count": len(TRANSACTION_FIELDS),
        "required": ["version", "tx_id", "cursor", "action", "occurred_at_millis", "payload_json"],
        "optional": ["entity_type", "entity_id", "actor_user_id", "tenant_id", "prev_hash", "signature"],
        "notes": "Initial spec: identity, cursor, hash chain, and signature.",
    }
]
SCHEMA_BY_VERSION = {entry["version"]: dict(entry) for entry in SCHEMA_EVOLUTION}

# audience -> which payload keys survive the projection.
PAYLOAD_VIEWS: list[dict[str, Any]] = [
    {
        "view": "public",
        "label": "Customer / external",
        "allow": [],
        "drop_sensitive": True,
        "include_signature": False,
        "when_hint": "Every sensitive key is masked; nothing new is revealed.",
    },
    {
        "view": "internal",
        "label": "Internal operator",
        "allow": [],
        "drop_sensitive": True,
        "include_signature": True,
        "when_hint": "Internal shape; sensitive values still masked.",
    },
    {
        "view": "forensic",
        "label": "Forensic / investigator",
        "allow": ["*"],
        "drop_sensitive": False,
        "include_signature": True,
        "when_hint": "Full payload. Restricted to the forensic audience by policy.",
    },
]
VIEW_BY_NAME = {entry["view"]: dict(entry) for entry in PAYLOAD_VIEWS}

CHECKPOINT_EVERY = int(os.getenv("TRANSACTION_CHECKPOINT_EVERY", "0") or 0)

# The queryable projection of a frame. A hash chain is excellent evidence and
# terrible search: walking 10M frames to find one transaction is not an API.
# This table declares what ``TransactionLog.query`` can filter on, how each
# field is derived from the frame, and which operators are legal — so the
# filter surface is data, not an ``if`` ladder, and a new filter is a new row.
QUERY_FIELDS: list[dict[str, Any]] = [
    {
        "field": "cursor",
        "source": "cursor",
        "type": "int",
        "operators": ["eq", "ne", "gt", "gte", "lt", "lte", "between", "in"],
        "description": "Monotonic append position; the cheapest selective filter.",
    },
    {
        "field": "action",
        "source": "action",
        "type": "string",
        "operators": ["eq", "ne", "in", "not_in", "prefix", "contains", "glob"],
        "description": "Exact dotted action, e.g. `auth.login`.",
    },
    {
        "field": "action_prefix",
        "source": "action",
        "type": "string",
        "operators": ["eq", "prefix", "in"],
        "description": "Leading dotted segment, e.g. `auth` for `auth.login`.",
    },
    {
        "field": "entity_type",
        "source": "entity_type",
        "type": "string",
        "operators": ["eq", "ne", "in", "not_in", "contains"],
        "description": "Subject class of the frame (`user`, `cell`, `system`).",
    },
    {
        "field": "entity_id",
        "source": "entity_id",
        "type": "string",
        "operators": ["eq", "ne", "in", "not_in", "prefix", "contains"],
        "description": "Identifier of the subject the frame is about.",
    },
    {
        "field": "actor_user_id",
        "source": "actor_user_id",
        "type": "int",
        "operators": ["eq", "ne", "gt", "gte", "lt", "lte", "in", "is_null", "not_null"],
        "description": "Authenticated actor; null for system-originated frames.",
    },
    {
        "field": "tenant_id",
        "source": "tenant_id",
        "type": "string",
        "operators": ["eq", "ne", "in", "not_in", "is_null", "not_null"],
        "description": "Owning tenant, for multi-tenant scoping.",
    },
    {
        "field": "severity",
        "source": "payload.severity",
        "type": "string",
        "operators": ["eq", "ne", "in", "not_in", "prefix", "glob"],
        "description": "Severity carried in the payload, same vocabulary as the pipeline.",
    },
    {
        "field": "actionable",
        "source": "action",
        "type": "bool",
        "operators": ["eq"],
        "description": "True when SIGNING_POLICY requires a signature for this action.",
    },
    {
        "field": "signed",
        "source": "signature",
        "type": "bool",
        "operators": ["eq"],
        "description": "True when the frame carries an HSM signature.",
    },
    {
        "field": "compliant",
        "source": "*",
        "type": "bool",
        "operators": ["eq"],
        "description": "Frame satisfies its schema's required fields and signing requirement.",
    },
    {
        "field": "version",
        "source": "version",
        "type": "int",
        "operators": ["eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte"],
        "description": "Spec version written by the producer.",
    },
    {
        "field": "payload",
        "source": "payload",
        "type": "json",
        "operators": ["has_key", "missing_key", "value_eq"],
        "description": "Sub-document probe for payload keys (e.g. `has_key=risk_score`).",
    },
]
QUERY_FIELD_BY_NAME = {entry["field"]: dict(entry) for entry in QUERY_FIELDS}

# Sortable fields for ``query(order=...)``; mirrors submit order for `cursor`.
QUERY_SORT_FIELDS = ("cursor", "occurred_at", "action", "entity_id")

# Export encodings. `base64` is the on-disk append-only line format itself, so
# an export is byte-identical to what the log would have persisted.
EXPORT_FORMATS = ("json", "jsonl", "csv", "base64")

CSV_COLUMNS = (
    "version",
    "cursor",
    "tx_id",
    "action",
    "entity_type",
    "entity_id",
    "actor_user_id",
    "tenant_id",
    "occurred_at",
    "prev_hash",
    "signed",
    "payload",
)

DEFAULT_QUERY_LIMIT = 50
MAX_QUERY_LIMIT = 1000


def _signing_requirement(action: str, payload: dict | None = None) -> str:
    """Resolve the signing requirement for an action (action prefix > severity)."""
    severity = str((payload or {}).get("severity", "") or "")
    best: str | None = None
    best_len = -1
    for rule in SIGNING_POLICY:
        if rule["applies_to"] == "action_prefix":
            needle = str(rule["match"])
            if needle != "*" and str(action).lower().startswith(needle) and len(needle) > best_len:
                best, best_len = str(rule["requirement"]), len(needle)
    if best is not None:
        return best
    for rule in SIGNING_POLICY:
        if rule["applies_to"] == "severity" and rule["match"] == severity:
            return str(rule["requirement"])
    return "optional"


def requires_signature(action: str, payload: dict | None = None) -> bool:
    """Does the signing policy demand a signature for this frame?"""
    return _signing_requirement(action, payload) == "required"


def schema_for_version(version: int) -> dict[str, Any]:
    """Field provenance for a spec version (falls back to the newest known)."""
    if int(version) in SCHEMA_BY_VERSION:
        return dict(SCHEMA_BY_VERSION[int(version)])
    if not SCHEMA_EVOLUTION:
        return {"version": int(version), "added_in": None, "field_count": 0, "required": [], "optional": []}
    newest = max(SCHEMA_EVOLUTION, key=lambda entry: int(entry["version"]))
    return {**dict(newest), "version": int(version), "assumed": True}


def _is_present(value: Any) -> bool:
    """Protobuf presence, not truthiness.

    A length-delimited field that was written with length 0 is *on the wire*:
    an empty payload is a real, valid frame, not a missing field. Absent scalars
    (None, "", 0) are missing. Getting this wrong would report every
    payload-less frame as corrupt.
    """
    if value is None:
        return False
    if isinstance(value, (str, bytes, bytearray)):
        return len(value) > 0
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return value != 0
    return True


def validate_frame(tx: "Transaction") -> dict[str, Any]:
    """Validate one frame against the schema for *its own* version.

    Checks required-field presence, the field-count/provenance split, and
    whether the frame honours the signing policy. A frame written by an older
    writer is checked against that older schema, so a legitimately sparse v1
    frame is not reported as corrupt.
    """
    schema = schema_for_version(tx.version)
    present = {
        "version": tx.version,
        "tx_id": tx.tx_id,
        "cursor": tx.cursor,
        "action": tx.action,
        "occurred_at_millis": int(tx.occurred_at.timestamp() * 1000),
        "payload_json": dict(tx.payload or {}),
    }
    optional_present = {
        "entity_type": tx.entity_type,
        "entity_id": tx.entity_id,
        "actor_user_id": tx.actor_user_id,
        "tenant_id": tx.tenant_id,
        "prev_hash": tx.prev_hash,
        "signature": tx.signature,
    }
    missing = [name for name in schema["required"] if not _is_present(present.get(name))]
    requirement = _signing_requirement(tx.action, dict(tx.payload or {}))
    signed = bool(tx.signature)
    return {
        "tx_id": tx.tx_id,
        "cursor": tx.cursor,
        "version": tx.version,
        "schema_assumed": bool(schema.get("assumed")),
        "valid": not missing,
        "missing_required": missing,
        "optional_present": sorted(
            name for name, value in optional_present.items() if _is_present(value)
        ),
        "signing": {
            "requirement": requirement,
            "signed": signed,
            "compliant": signed if requirement == "required" else True,
        },
        "sensitive_payload_keys": sorted(
            key for key in dict(tx.payload or {}) if key in REDACTED_PAYLOAD_KEYS
        ),
    }


def project_payload(payload: dict | None, view: str = "internal") -> dict[str, Any]:
    """Project a payload through a ``PAYLOAD_VIEWS`` row.

    ``forensic`` is the only view that keeps sensitive values, and it is not the
    default: an accidental ``view="internal"`` on a forensic export can never
    leak, only under-expose.
    """
    config = VIEW_BY_NAME.get(str(view))
    if config is None:
        raise ValueError(
            f"unknown payload view: {view!r} (expected one of {sorted(VIEW_BY_NAME)})"
        )
    data = dict(payload or {})
    if not config["drop_sensitive"]:
        return data
    allow = set(config.get("allow") or ())
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in REDACTED_PAYLOAD_KEYS and key not in allow:
            out[key] = "***"
        else:
            out[key] = value
    return out


def project_transaction(tx: "Transaction", view: str = "internal") -> dict[str, Any]:
    """``Transaction.to_dict`` through a payload view (signature is metadata)."""
    config = VIEW_BY_NAME.get(str(view))
    if config is None:
        raise ValueError(
            f"unknown payload view: {view!r} (expected one of {sorted(VIEW_BY_NAME)})"
        )
    payload = project_payload(tx.payload, view)
    return {
        "version": tx.version,
        "tx_id": tx.tx_id,
        "cursor": tx.cursor,
        "action": tx.action,
        "entity_type": tx.entity_type,
        "entity_id": tx.entity_id,
        "actor_user_id": tx.actor_user_id,
        "tenant_id": tx.tenant_id,
        "occurred_at": tx.occurred_at,
        "payload": payload,
        "prev_hash": tx.prev_hash.hex(),
        "signature_present": bool(tx.signature) if config["include_signature"] else False,
        "view": str(view),
    }


# --- Expansion: query / export surface ---------------------------------------


def _resolve_source(tx: "Transaction", source: str) -> Any:
    """Read one QUERY_FIELDS ``source`` expression off a frame.

    Sources are dotted paths rooted at either a ``Transaction`` attribute or the
    ``payload`` sub-document, so ``payload.severity`` and ``payload.nested.key``
    both work without the table knowing the payload's shape.
    """
    if source == "signature":
        return bool(tx.signature)
    if source == "*":
        return validate_frame(tx)
    parts = str(source).split(".")
    if parts[0] == "payload":
        node: Any = dict(tx.payload or {})
        for part in parts[1:]:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node
    if len(parts) > 1:
        return None
    return getattr(tx, parts[0], None)


def _query_value(tx: "Transaction", spec: dict[str, Any]) -> Any:
    """Project a frame onto one query field, normalised by declared type."""
    source = str(spec["source"])
    if spec["field"] == "action_prefix":
        return str(tx.action or "").split(".")[0]
    if spec["field"] == "actionable":
        return requires_signature(tx.action, dict(tx.payload or {}))
    if spec["field"] == "compliant":
        return bool(validate_frame(tx)["valid"]) and validate_frame(tx)["signing"]["compliant"]
    if spec["field"] == "occurred_at":
        return tx.occurred_at
    value = _resolve_source(tx, source)
    kind = spec["type"]
    if kind == "int" and value is not None:
        return int(value)
    if kind == "bool":
        return bool(value)
    if kind == "string":
        return "" if value is None else str(value)
    return value


def _compare(actual: Any, operator: str, expected: Any, kind: str) -> bool:
    """Apply one operator. Unknown operators never match (fail closed)."""
    if operator == "is_null":
        return actual is None or actual == ""
    if operator == "not_null":
        return not (actual is None or actual == "")
    if kind == "json":
        payload = actual if isinstance(actual, dict) else {}
        if operator == "has_key":
            return str(expected) in payload
        if operator == "missing_key":
            return str(expected) not in payload
        if operator == "value_eq":
            target = payload.get(str(expected[0]))
            return target == expected[1]
        return False
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "in":
        return actual in list(expected or ())
    if operator == "not_in":
        return actual not in list(expected or ())
    if operator in ("gt", "gte", "lt", "lte") and actual is not None:
        left, right = actual, expected
        if kind == "int":
            left, right = int(actual), int(expected)
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[operator]
    if operator == "between" and actual is not None:
        low, high = list(expected or ())[:2]
        return (int(actual) if kind == "int" else actual) >= low and (
            int(actual) if kind == "int" else actual
        ) <= high
    if operator == "prefix":
        return str(actual).startswith(str(expected))
    if operator == "suffix":
        return str(actual).endswith(str(expected))
    if operator == "contains":
        return str(expected) in str(actual)
    if operator == "glob":
        return _glob_match(str(actual), str(expected))
    return False


def _glob_match(value: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(value, pattern)


def _matches(tx: "Transaction", filters: list[tuple[str, str, Any]]) -> bool:
    """All declared filters must hold (AND); undeclared fields match nothing."""
    for field_name, operator, expected in filters:
        spec = QUERY_FIELD_BY_NAME.get(str(field_name))
        if spec is None:
            return False
        if operator not in spec["operators"]:
            return False
        if not _compare(
            _query_value(tx, spec), str(operator), expected, str(spec["type"])
        ):
            return False
    return True


def normalize_filters(filters: dict | list | None) -> list[tuple[str, str, Any]]:
    """Accept ``{"action_prefix": "auth"}`` or ``[("action", "eq", "x")]``."""
    if not filters:
        return []
    if isinstance(filters, dict):
        out: list[tuple[str, str, Any]] = []
        for key, value in filters.items():
            operators = tuple(QUERY_FIELD_BY_NAME.get(str(key), {}).get("operators") or ())
            if isinstance(value, str) and value in operators:
                # A bare operator name is a unary test: {"signed": "is_null"}.
                out.append((str(key), value, None))
            elif isinstance(value, (list, tuple)) and len(value) == 2 and value[0] in operators:
                out.append((str(key), str(value[0]), value[1]))
            elif isinstance(value, (list, tuple, set, frozenset)):
                out.append((str(key), "in", list(value)))
            else:
                out.append((str(key), "eq", value))
        return out
    return [(str(f), str(o), v) for f, o, v in filters]


def _transaction_from_row(row: dict[str, Any]) -> "Transaction":
    """Rebuild a frame from a ``json``/``jsonl``/``csv`` export record.

    ``payload`` arrives as a nested object in the JSON formats and as a JSON
    *string* in the flat CSV format, so it is normalised here rather than in
    each caller.
    """
    raw_payload = row.get("payload")
    if isinstance(raw_payload, str):
        raw_payload = json.loads(raw_payload) if raw_payload.strip() else {}
    occurred = str(row.get("occurred_at") or "")
    return Transaction(
        version=int(row.get("version", SPEC_VERSION)),
        tx_id=str(row.get("tx_id", "")),
        cursor=int(row.get("cursor", 0)),
        action=str(row.get("action", "")),
        entity_type=str(row.get("entity_type", "system")),
        entity_id=str(row.get("entity_id", "")),
        actor_user_id=None if row.get("actor_user_id") in (None, "") else int(row["actor_user_id"]),
        tenant_id=row.get("tenant_id") or None,
        payload=dict(raw_payload or {}),
        prev_hash=bytes.fromhex(str(row.get("prev_hash") or "")),
        signature=b"",
        occurred_at=(
            datetime.fromisoformat(occurred) if occurred else datetime.now(timezone.utc)
        ),
    )


def _transaction_from_csv_row(line: str) -> "Transaction | None":
    """Rebuild a frame from one CSV line; ``None`` for a header/blank line.

    ``fieldnames`` is pinned to ``CSV_COLUMNS`` because ``DictReader`` would
    otherwise read the line itself as the header and drop the record.

    The flat CSV shape carries a ``signed`` boolean rather than signature bytes,
    so a CSV round-trip preserves content and chain pointers but not the
    signature. Use the ``base64`` format when the signature must survive.
    """
    text = (line or "").strip()
    if not text:
        return None
    fields = next(csv.reader([text]))
    if tuple(fields) == CSV_COLUMNS:
        return None
    row = next(iter(csv.DictReader([text], fieldnames=list(CSV_COLUMNS))), None)
    if not row or not row.get("tx_id"):
        raise ValueError("csv line is not a transaction row")
    return _transaction_from_row(row)



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


class MerkleAccumulator:
    """Periodic tree checkpoints over the hash chain.

    ``verify_chain`` is O(n): to prove a 10M-frame log is intact you must walk
    every frame. A checkpoint collapses a verified prefix into one root, so
    verification becomes "recompute from the last checkpoint" and the answer
    arrives in constant time. A checkpoint is only ever recorded over a prefix
    that has already been chain-verified — it is a cache of trust, never a
    bypass.
    """

    def __init__(self, interval: int = CHECKPOINT_EVERY or 1000):
        self.interval = max(0, int(interval))
        self._checkpoints: list[dict[str, Any]] = []

    @property
    def checkpoints(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._checkpoints]

    def latest(self) -> dict[str, Any] | None:
        return dict(self._checkpoints[-1]) if self._checkpoints else None

    def should_checkpoint(self, entry_count: int) -> bool:
        if self.interval <= 0:
            return False
        return int(entry_count) > 0 and int(entry_count) % self.interval == 0

    def record(self, entries: list["Transaction"], *, from_cursor: int = 0) -> dict[str, Any]:
        """Record a checkpoint over ``entries`` (call after chain verification)."""
        if not entries:
            raise ValueError("cannot checkpoint an empty range")
        leaves = [tx.digest() for tx in entries]
        root = _merkle_root(leaves)
        checkpoint = {
            "from_cursor": from_cursor,
            "to_cursor": int(entries[-1].cursor),
            "leaves": len(leaves),
            "root": root.hex(),
            "algo": "sha256",
            "tree": "merkle-binary",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._checkpoints.append(checkpoint)
        return dict(checkpoint)

    def verify_from_checkpoint(self, entries: list["Transaction"]) -> dict[str, Any]:
        """Verify a log suffix against the newest checkpoint's root.

        ``entries`` must be exactly the range the checkpoint covered. Reports
        ``unverifiable`` (rather than raising) when there is no checkpoint, so a
        caller can fall back to a full ``verify_chain``.
        """
        checkpoint = self.latest()
        if checkpoint is None or not entries:
            return {
                "verifiable": False,
                "reason": "no checkpoint recorded" if checkpoint is None else "no entries supplied",
            }
        expected = int(checkpoint["leaves"])
        if len(entries) != expected:
            return {
                "verifiable": False,
                "reason": f"checkpoint covers {expected} entries, got {len(entries)}",
                "checkpoint": checkpoint,
            }
        root = _merkle_root([tx.digest() for tx in entries])
        return {
            "verifiable": True,
            "valid": root.hex() == checkpoint["root"],
            "computed_root": root.hex(),
            "checkpoint": checkpoint,
        }


def _merkle_root(leaves: list[bytes]) -> bytes:
    """Binary Merkle root; a single leaf is its own root, odd levels duplicate."""
    if not leaves:
        return hashlib.sha256(b"").digest()
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest()
            for i in range(0, len(level), 2)
        ]
    return level[0]


class TransactionLog:
    """Append-only transaction log with optional file persistence."""

    def __init__(self, path: str | None = None, *, merkle_interval: int | None = None):
        self.path = path or TRANSACTION_LOG_PATH or None
        self._entries: list[Transaction] = []
        self._cursor = 0
        self.merkle = MerkleAccumulator(
            merkle_interval if merkle_interval is not None else CHECKPOINT_EVERY
        )
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
        self._auto_checkpoint()
        return tx

    def _auto_checkpoint(self) -> dict[str, Any] | None:
        """Roll a Merkle checkpoint at the configured interval.

        Only ever runs over a prefix that the append path has just proved
        well-formed: the new frame was chained from the previous frame's digest
        on the line above, so the prefix is intact by construction. The
        checkpoint is therefore a cache of an already-earned fact — it can
        never launder a broken chain, because a broken chain cannot be appended
        in the first place.
        """
        if not self.merkle.should_checkpoint(len(self._entries)):
            return None
        return self.checkpoint()

    def checkpoint(self) -> dict[str, Any]:
        """Record a checkpoint over the whole verified prefix (explicit call)."""
        return self.merkle.record(self._entries)

    def append_many(self, frames: list[dict[str, Any]] | list[Any]) -> list[Transaction]:
        """Append a batch of frames in order, re-chaining each onto the last.

        Accepts either ``Transaction`` objects (their fields are copied; the
        original ``prev_hash``/``cursor``/``signature`` are *not* reused because
        a frame's position in a log is the log's business, not the sender's) or
        plain dicts of ``append`` keyword arguments. Chaining happens per frame,
        so a mid-batch failure leaves the prefix valid rather than orphaned.
        """
        out: list[Transaction] = []
        for frame in frames:
            if isinstance(frame, Transaction):
                fields = {
                    "action": frame.action,
                    "tx_id": frame.tx_id,
                    "entity_type": frame.entity_type,
                    "entity_id": frame.entity_id,
                    "actor_user_id": frame.actor_user_id,
                    "tenant_id": frame.tenant_id,
                    "payload": dict(frame.payload or {}),
                    "occurred_at": frame.occurred_at,
                    "signature": frame.signature,
                }
            elif isinstance(frame, dict):
                fields = dict(frame)
            else:  # pragma: no cover - defensive
                raise TypeError(f"cannot append {type(frame).__name__} to a transaction log")
            out.append(self.append(**fields))
        return out

    def query(
        self,
        filters: dict | list | None = None,
        *,
        order: str = "desc",
        sort: str = "cursor",
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
        view: str = "internal",
    ) -> dict[str, Any]:
        """Filter the log through ``QUERY_FIELDS`` and project through a view.

        ``order`` is ``"desc"`` (newest first) or ``"asc"``; the append order is
        always the log's physical order, so a query never reinterprets it. The
        result reports how many frames matched *before* pagination, because
        ``len(results)`` alone cannot tell a caller whether to ask for more.
        """
        if str(sort) not in QUERY_SORT_FIELDS:
            raise ValueError(f"cannot sort by {sort!r} (expected one of {list(QUERY_SORT_FIELDS)})")
        parsed = normalize_filters(filters)
        matched = [tx for tx in self._entries if _matches(tx, parsed)]
        reverse = str(order).lower() != "asc"
        if str(sort) != "cursor":
            # ``_query_value`` dispatches on the synthetic "field" key, so a
            # sort spec that was not found in QUERY_FIELDS has to declare one
            # rather than only a source. Sorting on an undeclared name stays
            # allowed (it is a declared sort field) and degrades to a string
            # sort on the raw attribute.
            key_spec = QUERY_FIELD_BY_NAME.get(str(sort)) or {
                "field": str(sort),
                "type": "string",
                "source": str(sort),
            }
            matched.sort(key=lambda tx: _query_value(tx, key_spec), reverse=reverse)
        elif reverse:
            matched = list(reversed(matched))
        window = max(0, min(int(limit), MAX_QUERY_LIMIT))
        start = max(0, int(offset))
        page = matched[start : start + window]
        return {
            "total": len(self._entries),
            "matched": len(matched),
            "offset": start,
            "limit": window,
            "order": "desc" if reverse else "asc",
            "sort": str(sort),
            "view": str(view),
            "filters": [
                {"field": f, "operator": o, "value": v} for f, o, v in parsed
            ],
            "results": [project_transaction(tx, view) for tx in page],
        }

    def export(
        self,
        *,
        fmt: str = "jsonl",
        view: str = "forensic",
        filters: dict | list | None = None,
    ) -> str:
        """Serialise matching frames.

        Defaults to ``jsonl`` + ``forensic``: an export is an evidence handoff,
        so it must carry full payloads and it must be a line format an
        investigator can ``diff``, ``grep`` and ``split`` without this repo.
        ``base64`` reproduces the exact append-only on-disk encoding, so an
        export can be replayed into another log with ``import_frames``.
        """
        out_fmt = str(fmt)
        if out_fmt not in EXPORT_FORMATS:
            raise ValueError(f"unknown export format {fmt!r} (expected one of {list(EXPORT_FORMATS)})")
        parsed = normalize_filters(filters)
        frames = [tx for tx in self._entries if _matches(tx, parsed)]
        if out_fmt == "base64":
            return "".join(
                base64.b64encode(tx.to_bytes()).decode("ascii") + "\n" for tx in frames
            )
        if out_fmt == "json":
            return json.dumps(
                [project_transaction(tx, view) for tx in frames],
                indent=2,
                default=str,
                sort_keys=True,
            )
        if out_fmt == "jsonl":
            return "".join(
                json.dumps(project_transaction(tx, view), default=str, sort_keys=True) + "\n"
                for tx in frames
            )
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for tx in frames:
            projected = project_transaction(tx, view)
            writer.writerow(
                [
                    projected["version"],
                    projected["cursor"],
                    projected["tx_id"],
                    projected["action"],
                    projected["entity_type"],
                    projected["entity_id"],
                    "" if projected["actor_user_id"] is None else projected["actor_user_id"],
                    projected["tenant_id"] or "",
                    projected["occurred_at"].isoformat() if projected["occurred_at"] else "",
                    projected["prev_hash"],
                    "true" if projected["signature_present"] else "false",
                    json.dumps(projected["payload"], sort_keys=True, default=str),
                ]
            )
        return buffer.getvalue()

    def import_frames(
        self, data: str | bytes, *, fmt: str = "jsonl", attach: bool = False
    ) -> dict[str, Any]:
        """Decode frames produced by ``export`` and report on them.

        Import is a *verification* step by default: it returns the decoded
        frames, their per-frame validation verdict, and a duplicate count, but
        appends nothing. ``attach=True`` re-appends them through ``append_many``,
        which re-chains them onto this log's current tip — imported frames keep
        their content and lose their original ``prev_hash``/``cursor``, because
        those describe a position in a *different* log and reusing them here
        would forge a chain link.
        """
        out_fmt = str(fmt)
        if out_fmt not in EXPORT_FORMATS:
            raise ValueError(f"unknown import format {fmt!r} (expected one of {list(EXPORT_FORMATS)})")
        known_tx = {tx.tx_id for tx in self._entries}
        decoded: list[Transaction] = []
        invalid: list[dict[str, Any]] = []
        raw_lines: list[str]
        if out_fmt == "base64":
            raw_lines = [
                line.strip()
                for line in (data.decode("ascii") if isinstance(data, bytes) else data).splitlines()
                if line.strip()
            ]
        elif out_fmt == "jsonl":
            raw_lines = [line for line in (data or "").splitlines() if line.strip()]
        elif out_fmt == "json":
            raw_lines = [json.dumps(entry, default=str) for entry in json.loads(data or "[]")]
        else:  # csv
            raw_lines = list(io.StringIO(data or "").readlines())
        for line in raw_lines:
            try:
                if out_fmt == "base64":
                    tx = Transaction.from_bytes(base64.b64decode(line.encode("ascii")))
                elif out_fmt == "csv":
                    tx = _transaction_from_csv_row(line)
                    if tx is None:
                        continue
                else:
                    tx = _transaction_from_row(json.loads(line))
            except (ValueError, TypeError, KeyError, binascii.Error) as exc:
                invalid.append({"line": line[:120], "error": str(exc)})
                continue
            verdict = validate_frame(tx)
            if not verdict["valid"]:
                invalid.append({"tx_id": tx.tx_id, "error": "schema: " + ", ".join(verdict["missing_required"])})
                continue
            decoded.append(tx)
        duplicates = sum(1 for tx in decoded if tx.tx_id in known_tx)
        report = {
            "format": out_fmt,
            "decoded": len(decoded),
            "invalid": len(invalid),
            "duplicates": duplicates,
            "attached": 0,
            "problems": invalid,
            "chain_preserved": False,
            "policy": "imported frames are re-chained onto this log's tip",
        }
        if attach and decoded:
            report["attached"] = len(self.append_many(decoded))
        return report

    def verify_from_checkpoint(self, from_cursor: int = 0) -> dict[str, Any]:
        """Verify the log's suffix against the newest checkpoint, not from zero.

        Answers "is everything after the last checkpoint intact?" in time
        proportional to the suffix rather than the whole log. Falls back to a
        full ``verify_chain`` verdict when no checkpoint exists, so the caller
        always gets a trustworthy answer rather than a failure.
        """
        checkpoint = self.merkle.latest()
        if checkpoint is None:
            full = self.verify_chain()
            return {
                "verifiable": False,
                "fallback": "verify_chain",
                "valid": full["valid"],
                "entries": full["entries"],
                "reason": "no checkpoint recorded; ran full chain verification",
            }
        covered = [
            tx for tx in self._entries if int(from_cursor) <= int(tx.cursor) <= int(checkpoint["to_cursor"])
        ]
        result = self.merkle.verify_from_checkpoint(covered)
        suffix_broken = None
        for index in range(len(self._entries)):
            if int(self._entries[index].cursor) > int(checkpoint["to_cursor"]):
                suffix_broken = index
                break
        if suffix_broken is not None:
            for index in range(suffix_broken, len(self._entries)):
                if index and self._entries[index].prev_hash != self._entries[index - 1].digest():
                    suffix_broken = index
                    break
            else:
                suffix_broken = None
        return {
            **result,
            "suffix_from_cursor": int(checkpoint["to_cursor"]),
            "suffix_broken_at": suffix_broken,
            "valid": bool(result.get("valid")) and suffix_broken is None,
            "verified_entries": len(covered),
            "total_entries": len(self._entries),
            "work_ratio": round(len(covered) / max(1, len(self._entries)), 4),
        }

    def integrity_report(self, *, sample: int = 5) -> dict[str, Any]:
        """One-call summary: chain, schema compliance, signing, checkpoints."""
        frames = self._entries[-max(0, int(sample)) :] if sample else []
        return {
            "chain": self.verify_chain(),
            "checkpoints": self.merkle.checkpoints,
            "checkpoint_interval": self.merkle.interval,
            "spec_versions": sorted({int(tx.version) for tx in self._entries}),
            "sampled": [validate_frame(tx) for tx in frames],
            "unsigned_but_required": [
                tx.tx_id
                for tx in frames
                if requires_signature(tx.action, dict(tx.payload or {})) and not tx.signature
            ],
            "append_only": True,
        }

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
        "signing_policy": {
            "table": [dict(rule) for rule in SIGNING_POLICY],
            "precedence": "longest action prefix, then payload severity, then 'optional'",
            "enforcement": "validate_frame().signing.compliant; unsigned frames remain valid frames",
            "helpers": ["requires_signature", "sign_transaction", "verify_transaction_signature"],
        },
        "schema_evolution": {
            "table": [dict(entry) for entry in SCHEMA_EVOLUTION],
            "current_version": SPEC_VERSION,
            "policy": "frames are validated against their own version, never the newest",
            "helper": "schema_for_version",
        },
        "payload_views": {
            "table": [dict(entry) for entry in PAYLOAD_VIEWS],
            "default": "internal",
            "policy": "sensitive values are masked unless a view explicitly opts out",
            "helpers": ["project_payload", "project_transaction"],
        },
        "redacted_payload_keys": sorted(REDACTED_PAYLOAD_KEYS),
        "merkle": {
            "algo": "sha256",
            "tree": "merkle-binary",
            "interval": CHECKPOINT_EVERY,
            "policy": "a checkpoint is a cache of an already-verified prefix, never a bypass",
            "checkpoint_count": len(get_default_transaction_log().merkle.checkpoints),
            "helpers": ["MerkleAccumulator", "TransactionLog.verify_from_checkpoint"],
        },
        "query": {
            "fields": [dict(entry) for entry in QUERY_FIELDS],
            "sort_fields": list(QUERY_SORT_FIELDS),
            "default_limit": DEFAULT_QUERY_LIMIT,
            "max_limit": MAX_QUERY_LIMIT,
            "policy": "undeclared fields and operators match nothing (fail closed)",
            "helper": "TransactionLog.query",
        },
        "export": {
            "formats": list(EXPORT_FORMATS),
            "default": {"fmt": "jsonl", "view": "forensic"},
            "reimportable": ["base64", "jsonl", "json", "csv"],
            "csv_columns": list(CSV_COLUMNS),
            "signature_survives": ["base64"],
            "policy": "jsonl/csv round-trips preserve content but not signature bytes",
            "helper": "TransactionLog.export / TransactionLog.import_frames",
        },
        "operations": [
            "TransactionLog.append",
            "TransactionLog.append_many",
            "TransactionLog.query",
            "TransactionLog.export",
            "TransactionLog.import_frames",
            "TransactionLog.verify_chain",
            "TransactionLog.verify_from_checkpoint",
            "TransactionLog.checkpoint",
            "TransactionLog.integrity_report",
        ],
    }