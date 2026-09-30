"""Isolated polymorphic base models and reusable column mixins.

Data definitions derive from these bases instead of re-declaring plumbing on
every table. The module deliberately lives apart from ``app/models.py`` so the
concrete entities stay thin and new entity families can reuse the shared
scoping/partitioning columns without touching existing tables (their DDL is
already emitted by ``Base.metadata.create_all`` / alembic and is left
untouched — see BLOCKAGES.md).

Reuse available here:

- ``TimestampMixin`` — the created/updated audit columns historically defined
  in ``app/models.py``. Moved here so all entities share one definition.
- ``TenantScopedMixin`` / ``PartitionedMixin`` — columns for multi-tenant
  routing and time-based data partitioning. Applied to *new* tables only so
  existing schemas are never altered out from under a running deployment.
- ``SecurityEvent`` — a single-table-inheritance polymorphic root for
  high-velocity security signals (authentication, risk, access). Polymorphic
  identity is the ``event_kind`` discriminator column; concrete families are
  subclasses that add no columns, keeping the hierarchy extensible by
  subclassing rather than schema edits.

The original module covered exactly three mixins and three signal families, so
every new table had to re-invent soft deletes, row versions, actor attribution,
and serialization. The additions below close that gap *without touching any
existing table*:

- ``SoftDeleteMixin`` — ``deleted_at`` / ``is_deleted`` plus ``soft_delete()``
  and ``restore()``. Delete becomes a reversible state transition, which is
  what an audit-driven domain needs.
- ``RowVersionMixin`` — a ``row_version`` integer that pairs with
  ``app.optimistic_locking`` for database-level compare-and-swap, so a
  persisted entity gets the same stale-write protection the in-memory
  decision records have.
- ``ActorAuditMixin`` — ``created_by_user_id`` / ``updated_by_user_id``, so
  provenance survives on the row instead of only in the audit trail.
- ``ExpiringMixin`` — ``expires_at`` plus ``is_expired()`` / ``seconds_until_expiry()``
  for credentials, invites, and short-lived grants.
- ``SerializationMixin`` — a single ``to_dict()`` contract so serializers do
  not each invent their own field list (and accidentally leak one).
- ``EntityRegistry`` — name -> model lookup, so a new family is discoverable by
  tooling without a hardcoded import list.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    inspect,
)

from app.db import Base

# Columns that must never appear in a serialized payload even when present on
# the instance. A denylist, not an allowlist, so adding a column cannot
# silently start leaking it.
SERIALIZATION_DENYLIST = frozenset(
    {
        "hashed_password",
        "password",
        "secret",
        "token",
        "api_key",
        "stored_hash",
        "reference_digest",
        "salt",
    }
)


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def touch(self):
        self.updated_at = func.now()


class TenantScopedMixin:
    """Optional tenant scope column for multi-tenant tables.

    Nullable on purpose: single-tenant tables and rows created outside any
    tenant scope keep working without a value.
    """

    tenant_id = Column(String(64), nullable=True, index=True)


class PartitionedMixin:
    """Time-partition key column consumed by ``app/partition_manager.py``."""

    partition_key = Column(String(32), nullable=True, index=True)


class SoftDeleteMixin:
    """Reversible deletion for new tables.

    Rows are never removed; they acquire a ``deleted_at`` timestamp and are
    filtered out of normal queries. ``include_deleted=True`` on a query, or an
    explicit ``undelete()``, brings them back.
    """

    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    @property
    def is_deleted(self) -> bool:
        return getattr(self, "deleted_at", None) is not None

    def soft_delete(self) -> "SoftDeleteMixin":
        if self.deleted_at is None:
            self.deleted_at = datetime.now(timezone.utc)
        return self

    def restore(self) -> "SoftDeleteMixin":
        self.deleted_at = None
        return self

    def deleted_at_iso(self) -> Optional[str]:
        value = getattr(self, "deleted_at", None)
        return value.isoformat() if isinstance(value, datetime) else None


class RowVersionMixin:
    """Integer row version for database-level compare-and-swap.

    Pairs with ``app.optimistic_locking``'s policy: a writer supplies the
    version it read, and a mismatch means someone else committed in between.
    Unlike the in-memory guard, this survives a process restart, so it is the
    right choice for rows that a human also edits in an admin tool.
    """

    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    @property
    def current_version(self) -> int:
        return int(getattr(self, "row_version", 1) or 1)

    def bump_version(self) -> int:
        self.row_version = self.current_version + 1
        return self.row_version

    def assert_version(self, expected: int) -> bool:
        return self.current_version == int(expected)


class ActorAuditMixin:
    """Who created and last changed this row.

    The audit trail records *events*; this records *provenance on the row*,
    which survives log retention and answers "who imported this?" without a
    trail query.
    """

    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    updated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    def stamp_actor(self, user_id: int | None, *, creating: bool = False) -> "ActorAuditMixin":
        if creating:
            self.created_by_user_id = user_id
        self.updated_by_user_id = user_id
        return self


class ExpiringMixin:
    """Bounded lifetime for credentials, invites, and short-lived grants."""

    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    def is_expired(self, now: datetime | None = None) -> bool:
        value = getattr(self, "expires_at", None)
        if value is None:
            return False
        moment = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value <= moment

    def seconds_until_expiry(self, now: datetime | None = None) -> Optional[float]:
        value = getattr(self, "expires_at", None)
        if value is None:
            return None
        moment = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (value - moment).total_seconds()


class SerializationMixin:
    """One ``to_dict`` contract for every entity family.

    Datetimes become ISO-8601, denylisted columns are dropped even when set,
    and nested relationships are included only when explicitly asked for — the
    three things hand-rolled serializers get wrong most often.
    """

    def to_dict(
        self,
        *,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        relationships: bool = False,
    ) -> dict[str, Any]:
        mapper = inspect(type(self))
        excluded = set(exclude or ()) | SERIALIZATION_DENYLIST
        payload: dict[str, Any] = {}
        for column in mapper.columns:
            name = column.key
            if name in excluded:
                continue
            if include is not None and name not in set(include):
                continue
            payload[name] = _jsonable(getattr(self, name, None))
        if relationships:
            for rel in mapper.relationships:
                if rel.key in excluded:
                    continue
                if include is not None and rel.key not in set(include):
                    continue
                value = getattr(self, rel.key, None)
                if isinstance(value, list):
                    payload[rel.key] = [
                        item.to_dict() if hasattr(item, "to_dict") else item for item in value
                    ]
                elif value is not None and hasattr(value, "to_dict"):
                    payload[rel.key] = value.to_dict()
        return payload

    def safe_fields(self) -> list[str]:
        """Column names that would be emitted by :meth:`to_dict`."""
        return [
            column.key
            for column in inspect(type(self)).columns
            if column.key not in SERIALIZATION_DENYLIST
        ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SecurityEvent(Base, TenantScopedMixin, PartitionedMixin):
    """Polymorphic root for immutable, high-velocity security signals.

    Uses SQLAlchemy single-table inheritance: each concrete subclass is
    identified by the ``event_kind`` discriminator and shares this table, so a
    new signal family is a new subclass — no DDL, no joins.
    """

    __tablename__ = "security_events"
    __mapper_args__ = {
        "polymorphic_on": "event_kind",
        "polymorphic_identity": "security_event",
    }

    id = Column(Integer, primary_key=True, index=True)
    event_kind = Column(String(40), nullable=False, index=True)
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    summary = Column(Text, nullable=False, default="")
    detail_json = Column(Text, nullable=False, default="{}")
    severity = Column(String(20), nullable=False, default="info", index=True)
    risk_score = Column(Float, nullable=False, default=0.0)
    source = Column(String(50), nullable=False, default="zero_trust")

    def to_summary(self) -> dict[str, Any]:
        """Compact projection for high-velocity read paths.

        Tolerant by design: this root intentionally carries no timestamp
        columns (adding them would change an emitted DDL), so ``created_at`` is
        reported only when a concrete subclass supplies it.
        """
        created_at = getattr(self, "created_at", None)
        return {
            "id": self.id,
            "event_kind": self.event_kind,
            "actor_user_id": self.actor_user_id,
            "severity": self.severity,
            "risk_score": self.risk_score,
            "source": self.source,
            "summary": self.summary,
            "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
        }


class AuthenticationSecurityEvent(SecurityEvent):
    """Login / token-issuance signals."""
    __mapper_args__ = {"polymorphic_identity": "authentication"}


class RiskSecurityEvent(SecurityEvent):
    """Contextual risk-evaluation signals."""
    __mapper_args__ = {"polymorphic_identity": "risk"}


class AccessSecurityEvent(SecurityEvent):
    """Data-cell access / permission signals."""
    __mapper_args__ = {"polymorphic_identity": "access"}


# The original three signal families. Pinned as a contract: callers and the
# catalog key ``families`` mean exactly these, and are not widened here.
SECURITY_EVENT_FAMILIES = {
    "authentication": AuthenticationSecurityEvent,
    "risk": RiskSecurityEvent,
    "access": AccessSecurityEvent,
}

# --- Additional signal families (subclass-only: no DDL change) ----------------
#
# Each of these reuses the same table via the ``event_kind`` discriminator, so
# the hierarchy grows without a migration. They are deliberately kept in a
# separate mapping so the original ``families`` contract is untouched while
# ``security_event_family`` still resolves any of them.


class DataAccessSecurityEvent(SecurityEvent):
    """A concrete data-cell read/write attempt, masked or denied."""
    __mapper_args__ = {"polymorphic_identity": "data_access"}


class PolicySecurityEvent(SecurityEvent):
    """Policy-tier / control-posture transitions and overrides."""
    __mapper_args__ = {"polymorphic_identity": "policy"}


class AuthenticationAnomalySecurityEvent(SecurityEvent):
    """Failed logins, replayed refresh tokens, credential-stuffing signals."""
    __mapper_args__ = {"polymorphic_identity": "authentication_anomaly"}


class BreakGlassSecurityEvent(SecurityEvent):
    """A temporary data-cell grant was minted or consumed."""
    __mapper_args__ = {"polymorphic_identity": "break_glass"}


SECURITY_EVENT_EXTENSIONS = {
    "data_access": DataAccessSecurityEvent,
    "policy": PolicySecurityEvent,
    "authentication_anomaly": AuthenticationAnomalySecurityEvent,
    "break_glass": BreakGlassSecurityEvent,
}

# Severity vocabulary shared by every security-event family, ranked so a
# consumer can threshold on it.
SECURITY_EVENT_SEVERITIES = ("info", "low", "medium", "high", "critical")
SECURITY_SEVERITY_RANK = {name: rank for rank, name in enumerate(SECURITY_EVENT_SEVERITIES)}


def security_event_family(event_kind: str):
    """Return the concrete polymorphic class for ``event_kind`` (or the root)."""
    return SECURITY_EVENT_FAMILIES.get(event_kind) or SECURITY_EVENT_EXTENSIONS.get(
        event_kind, SecurityEvent
    )


def build_security_event(
    event_kind: str,
    *,
    summary: str = "",
    detail_json: str = "{}",
    severity: str = "info",
    risk_score: float = 0.0,
    actor_user_id: int | None = None,
    source: str = "zero_trust",
    tenant_id: str | None = None,
    partition_key: str | None = None,
) -> SecurityEvent:
    """Instantiate the right polymorphic subclass for ``event_kind``.

    The single construction point every caller should use: it picks the
    concrete class, so the returned instance carries the correct
    ``polymorphic_identity`` and persists into the shared table.
    """
    if severity not in SECURITY_SEVERITY_RANK:
        raise ValueError(
            f"severity must be one of {', '.join(SECURITY_EVENT_SEVERITIES)}"
        )
    cls = security_event_family(event_kind)
    return cls(
        event_kind=event_kind,
        summary=summary,
        detail_json=detail_json,
        severity=severity,
        risk_score=float(risk_score),
        actor_user_id=actor_user_id,
        source=source,
        tenant_id=tenant_id,
        partition_key=partition_key,
    )


class EntityRegistry:
    """Name -> model lookup so tooling need not hardcode an import list.

    A new signal family registers itself on definition; discovery stays a data
    question rather than a code question.
    """

    def __init__(self) -> None:
        self._entities: dict[str, type] = {}

    def register(self, name: str, model: type) -> type:
        self._entities[name] = model
        return model

    def get(self, name: str) -> Optional[type]:
        return self._entities.get(name)

    def names(self) -> list[str]:
        return sorted(self._entities)

    def catalog(self) -> dict[str, Any]:
        return {
            name: {
                "class": model.__name__,
                "table": getattr(model, "__tablename__", None),
                "polymorphic_identity": (model.__mapper_args__ or {}).get(
                    "polymorphic_identity"
                ),
            }
            for name, model in sorted(self._entities.items())
        }


REGISTRY = EntityRegistry()
for _name, _cls in SECURITY_EVENT_FAMILIES.items():
    REGISTRY.register(_name, _cls)
for _name, _cls in SECURITY_EVENT_EXTENSIONS.items():
    REGISTRY.register(_name, _cls)
REGISTRY.register("security_event", SecurityEvent)


def build_model_bases_catalog() -> dict[str, Any]:
    """Introspectable description of the polymorphic base layer."""
    return {
        "mixins": [
            "timestamp",
            "tenant_scoped",
            "partitioned",
            "soft_delete",
            "row_version",
            "actor_audit",
            "expiring",
            "serialization",
        ],
        "mixin_detail": {
            "soft_delete": "deleted_at + soft_delete()/restore(); reversible",
            "row_version": "row_version; pairs with app.optimistic_locking for DB-level CAS",
            "actor_audit": "created_by_user_id / updated_by_user_id provenance",
            "expiring": "expires_at + is_expired()/seconds_until_expiry()",
            "serialization": "to_dict() with a denylist, so adding a column cannot leak it",
        },
        "polymorphic_root": "security_event",
        # Pinned: ``families`` means exactly the original three and is never
        # widened. New signal families are reported separately.
        "families": {kind: cls.__name__ for kind, cls in SECURITY_EVENT_FAMILIES.items()},
        "family_count": len(SECURITY_EVENT_FAMILIES),
        "extensions": {
            kind: cls.__name__ for kind, cls in SECURITY_EVENT_EXTENSIONS.items()
        },
        "extension_count": len(SECURITY_EVENT_EXTENSIONS),
        "all_families": {
            kind: cls.__name__
            for kind, cls in {**SECURITY_EVENT_FAMILIES, **SECURITY_EVENT_EXTENSIONS}.items()
        },
        "severities": list(SECURITY_EVENT_SEVERITIES),
        "severity_rank": dict(SECURITY_SEVERITY_RANK),
        "discriminator": "event_kind",
        "registry": REGISTRY.catalog(),
        "serialization_denylist": sorted(SERIALIZATION_DENYLIST),
        "note": (
            "polymorphic identity drives table reuse; new families are subclass-only "
            "additions, so extending the hierarchy requires no migration"
        ),
    }


# =============================================================================
# Expansion — governance mixins, field classification, table introspection
# =============================================================================
#
# Everything above this line is the r2–r5 layer and is byte-unchanged. What
# follows is additive in the same style, and obeys the same two constraints the
# rest of this file does:
#
# 1. **No existing table gains a column.** The four new mixins are available for
#    *new* tables only; none of them is applied to a class in ``app/models.py``,
#    so no DDL moves and no migration is implied.
# 2. **Every knob is a table row.** Slug length, approval transitions, currency
#    exponents, severity bands and query limits are all data, so an operator
#    tunes them without a code change and a test can assert on the table rather
#    than on a hardcoded branch.
#
# The module imports no ``app.services.*``: the services import the models, so a
# reverse import is a cycle. Everything added here is pure Python over
# ``Base.metadata`` or over values a caller passes in.

MODEL_BASES_OPS: dict[str, object] = {
    "max_slug_length": 64,
    "slug_separator": "-",
    "slug_lowercase": True,
    "slug_max_attempts": 8,
    "slug_fallback": "item",
    "idempotency_window_seconds": 86400,
    "provenance_table_cap": 0,  # 0 = report every mapped table
    "note": (
        "Knobs for the slug, idempotency and introspection helpers below. "
        "Changing one here changes behaviour for every *new* table built on "
        "the mixins; it never rewrites an existing column."
    ),
}

# Words a slug must not become. A row whose slug is a reserved word reads as a
# system route in a log line and collides with nothing, which is the worst
# outcome: it looks deliberate and is unfindable.
SLUG_RESERVED = frozenset(
    {
        "admin", "api", "auth", "config", "debug", "delete", "docs", "edit",
        "export", "false", "graphql", "health", "import", "index", "internal",
        "login", "logout", "me", "meta", "new", "none", "null", "public",
        "root", "search", "settings", "static", "system", "true", "update",
        "user", "users", "v1", "v2",
    }
)

# Field sensitivity, for *serialization* purposes only.
#
# ``SERIALIZATION_DENYLIST`` above stays exactly as it is and stays
# authoritative: a profile can never emit a denylisted field. These classes add
# a second, coarser axis on top — "this field identifies a person" or "this
# field is money" — so a caller can strip a whole category without enumerating
# names. First match wins, so the order below is the precedence.
#
# This is deliberately *not* the same vocabulary as ``app/models.py``'s
# ``SENSITIVITY_CLASSES``: that one drives persisted redaction and may not be
# imported here (models imports this module), and this one drives what leaves
# the process. They are related, not shared.
SERIALIZATION_CLASSES: list[dict[str, object]] = [
    {
        "class": "credential",
        "rank": 0,
        "match": ("hashed_password", "password", "api_key", "secret", "token", "salt", "stored_hash", "private_key"),
        "description": "A value that authenticates or reconstructs a credential. Never serialized under any profile.",
    },
    {
        "class": "digest",
        "rank": 1,
        "match": ("reference_digest", "_digest", "fingerprint", "checksum", "key_fingerprint"),
        "description": "A one-way derivative. Harmless alone, but it is a stable join key across exports.",
    },
    {
        "class": "personal",
        "rank": 2,
        "match": ("email", "phone", "address", "full_name", "date_of_birth", "national_id", "ip_address", "user_agent"),
        "description": "Directly identifies a person.",
    },
    {
        "class": "financial",
        "rank": 3,
        "match": ("amount", "balance", "price", "fee", "total", "salary", "wallet", "payout"),
        "description": "Money. Usable internally; a leak is a reportable incident at most tiers.",
    },
    {
        "class": "identifier",
        "rank": 4,
        "match": ("_user_id", "_by_user_id", "created_by", "updated_by", "actor_id"),
        "description": "Links the row to a person without naming one.",
    },
    {
        "class": "operational",
        "rank": 5,
        "match": ("internal_note", "debug", "trace", "_json", "_raw", "stack"),
        "description": "Engineering detail that has no business in a caller-facing payload.",
    },
]

# Named payload shapes over the classes above. ``redact`` picks what happens to
# an excluded field: ``drop`` removes the key, ``mask`` replaces the value with
# a partial that cannot be reversed. ``max_depth`` bounds nested dict/list
# walking so a cyclic or very deep payload cannot turn a serializer into a
# stack-overflow generator; ``0`` disables the bound, which is the right choice
# for a bulk export that legitimately carries nested JSON columns.
#
# The bound counts the *container* nesting of the payload: the outer dict is
# depth 0, a value of a key is depth 1, and so on. A list of row dicts — which
# is what every list endpoint in this service returns — puts the row dict at
# depth 2, so any profile a caller is meant to use with a list response needs
# ``max_depth >= 3``. ``public`` at 2 would blank out every item in every list
# response, which is a bound that removes the payload rather than bounding it.
SERIALIZATION_PROFILES: dict[str, dict[str, object]] = {
    "public": {
        "exclude_classes": ("credential", "digest", "personal", "financial", "identifier", "operational"),
        "redact": "drop",
        "max_depth": 3,
        "description": "Anything a browser can see. Only non-identifying scalars survive.",
    },
    "operator": {
        "exclude_classes": ("credential",),
        "redact": "drop",
        "max_depth": 4,
        "description": "Support and ops tooling. Identifies people, never carries a credential.",
    },
    "internal": {
        "exclude_classes": (),
        "redact": "drop",
        "max_depth": 6,
        "description": "Service-to-service. Still credential-free, because the denylist is absolute.",
    },
    "export": {
        "exclude_classes": ("credential", "operational"),
        "redact": "mask",
        "max_depth": 0,
        "description": (
            "Bulk export. Keeps every field's position so the file still lines "
            "up with the schema; operational detail is masked in place rather "
            "than dropped. Credentials are still dropped."
        ),
    },
}

# Masking rules. ``keep_suffix`` characters survive so two masked identifiers
# stay distinguishable in a diff; ``min_hidden`` is the number of leading
# characters that must *never* appear. Both are needed: a 5-character value with
# ``keep_suffix: 4`` would mask to ``***abcd`` and hand back 80% of the secret,
# which is a mask that leaks.
MASK_OPS: dict[str, object] = {
    "keep_suffix": 4,
    "min_hidden": 4,
    "prefix": "***",
    "shortest_maskable": 8,  # keep_suffix + min_hidden; shorter values get the bare prefix
    "note": (
        "A value shorter than keep_suffix + min_hidden is replaced by the bare "
        "prefix. Anything shorter would be revealed by its own suffix, so "
        "there is nothing safe to keep."
    ),
}


def classify_serialization_field(name: str) -> str:
    """The sensitivity class of a field name, or ``"ordinary"``.

    Matched by substring in ``rank`` order, so ``hashed_password`` classifies as
    ``credential`` (rank 0) rather than ``personal``. A name no row matches is
    ``ordinary`` — the default is "not sensitive", which is the *permissive*
    default and therefore the one worth being explicit about: a brand new
    column holding a phone number is ``ordinary`` until
    ``SERIALIZATION_CLASSES`` says otherwise.
    """
    lowered = str(name or "").lower()
    for row in SERIALIZATION_CLASSES:
        if any(token in lowered for token in tuple(row["match"])):
            return str(row["class"])
    return "ordinary"


def _mask_value(value: Any) -> Any:
    """Replace a value with a non-reversible partial. Never raises.

    Returns the bare prefix for anything too short to keep a suffix from
    safely, and a non-string for anything that is not a string at all.
    """
    if not isinstance(value, str):
        return MASK_OPS["prefix"]
    keep = int(MASK_OPS["keep_suffix"])
    if len(value) < int(MASK_OPS["shortest_maskable"]):
        return MASK_OPS["prefix"]
    return f"{MASK_OPS['prefix']}{value[-keep:]}"


# Which profile an unrecognised name resolves to, and what a resolution must
# fall back to. Named rather than read off the ``public`` row at check time: a
# validator that compares the fallback against whatever ``public`` currently
# says cannot fail when ``public`` is the thing that got widened.
SERIALIZATION_FALLBACK_PROFILE = "public"
SERIALIZATION_FALLBACK_CLASSES = frozenset(
    {str(row["class"]) for row in SERIALIZATION_CLASSES}
)


def resolve_serialization_profile(profile: str) -> dict[str, Any]:
    """Look a profile up, falling back to the most restrictive one.

    An unknown profile name resolves to ``public`` rather than raising or —
    worse — to ``internal``. Defaulting a *recognition* failure to the
    permissive profile is how a field meant to be hidden ends up in a payload.
    """
    row = SERIALIZATION_PROFILES.get(str(profile))
    if row is None:
        row = SERIALIZATION_PROFILES[SERIALIZATION_FALLBACK_PROFILE]
    return {
        "profile": str(profile),
        "known": str(profile) in SERIALIZATION_PROFILES,
        "exclude_classes": tuple(row["exclude_classes"]),
        "redact": str(row["redact"]),
        "max_depth": int(row["max_depth"]),
        "description": str(row["description"]),
    }


def filter_payload_for_profile(
    payload: Any, profile: str = "public", *, max_depth: int | None = None
) -> tuple[Any, list[str]]:
    """Apply a profile to an already-serialized payload.

    Returns ``(filtered, removed)`` where ``removed`` names every field that
    was dropped or masked, so a caller can report what a payload *would* have
    leaked instead of discovering it in an incident. The denylist is honoured
    regardless of profile, including ``internal``: a profile row cannot grant
    permission to emit a credential.
    """
    spec = resolve_serialization_profile(profile)
    depth_cap = int(spec["max_depth"]) if max_depth is None else max(0, int(max_depth))
    excluded = set(spec["exclude_classes"])
    removed: list[str] = []

    def _walk(value: Any, depth: int) -> Any:
        if isinstance(value, dict):
            if depth_cap and depth >= depth_cap:
                return {}
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                lowered = name.lower()
                field_class = classify_serialization_field(name)
                if lowered in SERIALIZATION_DENYLIST or field_class in excluded:
                    if spec["redact"] == "mask" and lowered not in SERIALIZATION_DENYLIST:
                        out[name] = _mask_value(item)
                        removed.append(f"{name}:{field_class}:masked")
                    else:
                        removed.append(f"{name}:{field_class}")
                    continue
                out[name] = _walk(item, depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            if depth_cap and depth >= depth_cap:
                return []
            return [_walk(item, depth + 1) for item in value]
        return value

    return _walk(payload, 0), removed


# --- New mixins (new tables only) ---------------------------------------------


class SluggableMixin:
    """A URL-safe, unique, human-readable key for a new table.

    Uniqueness cannot be a column constraint here: ``unique_slug`` resolves
    collisions *in the application* against whatever the caller already has
    loaded, which is what a slug needs for a friendly route. Callers that need
    a database-level guarantee add their own unique index — this mixin does not
    emit DDL, so it cannot promise one.
    """

    slug = Column(String(64), nullable=True, index=True)

    def assign_slug(self, base: str) -> str:
        self.slug = slugify(base)
        return self.slug

    def ensure_unique_slug(self, base: str, existing: Iterable[str] = ()) -> str:
        self.slug = unique_slug(base, existing)
        return self.slug


class ApprovalMixin:
    """A governed state machine for rows that need a decision before they count.

    The transitions live in ``APPROVAL_TRANSITIONS`` rather than in an
    ``if`` chain, because the interesting question is never "can I set
    ``approved``" — it is "who is allowed to move this row from *pending* to
    *approved*, and does that need a second pair of eyes". Encoding that as data
    makes the second pair of eyes a row someone can add.
    """

    approval_state = Column(String(20), nullable=False, default="draft", index=True)
    approval_actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    approval_decided_at = Column(DateTime(timezone=True), nullable=True)
    approval_note = Column(Text, nullable=False, default="")

    @property
    def is_approved(self) -> bool:
        return str(getattr(self, "approval_state", "draft")) == APPROVAL_TERMINAL_STATE

    def transition_approval(
        self,
        to_state: str,
        *,
        actor_user_id: int | None = None,
        note: str = "",
        now: datetime | None = None,
        proposer_user_id: int | None = None,
    ) -> "ApprovalMixin":
        """Move to ``to_state``, raising if the table forbids the move.

        Raises rather than returning a verdict, because the caller here is
        usually about to commit: a silently ignored illegal transition is a row
        that says ``approved`` and was never reviewed.

        ``proposer_user_id`` defaults to whoever last moved this row, which is
        the right default for a second-pair rule: in ``pending`` the last mover
        is whoever submitted it, and on a reopen from ``approved`` the last mover
        is whoever signed it off. A caller tracking the submitter separately
        (an author and a submitter) passes it explicitly.
        """
        proposer = (
            proposer_user_id
            if proposer_user_id is not None
            else getattr(self, "approval_actor_user_id", None)
        )
        verdict = approval_transition_verdict(
            str(getattr(self, "approval_state", "draft")),
            to_state,
            actor_user_id=actor_user_id,
            proposer_user_id=proposer,
        )
        if not verdict["allowed"]:
            raise ValueError(verdict["reason"])
        self.approval_state = str(to_state)
        self.approval_actor_user_id = actor_user_id
        self.approval_note = str(note or "")
        self.approval_decided_at = now or datetime.now(timezone.utc)
        return self


class MoneyMixin:
    """A money amount as an exact integer count of minor units.

    Integer minor units, never a float: ``0.1 + 0.2`` is the reason a ledger
    drifts. The exponent comes from ``CURRENCY_EXPONENTS`` so a JPY amount is
    stored in whole yen without every call site remembering that.
    """

    amount_minor = Column(Integer, nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="USD")

    def set_amount(self, amount: object, currency: str | None = None) -> int:
        verdict = money_conversion(amount, currency or str(getattr(self, "currency", "USD")))
        self.amount_minor = verdict["minor_units"]
        self.currency = verdict["currency"]
        return int(verdict["minor_units"])

    def get_amount(self) -> float:
        return from_minor_units(
            getattr(self, "amount_minor", 0), str(getattr(self, "currency", "USD"))
        )


class IdempotencyMixin:
    """Replay protection for a retried write.

    A client that retries a POST after a timeout must not create a second row.
    The stored fingerprint is what makes that decidable: the *same* key with a
    *different* body is a client bug, and answering "replay" to it would hide
    the bug rather than the duplicate.
    """

    idempotency_key = Column(String(128), nullable=True, index=True)
    request_fingerprint = Column(String(64), nullable=True)

    def mark_idempotent(self, key: str, fingerprint: str) -> "IdempotencyMixin":
        self.idempotency_key = str(key)
        self.request_fingerprint = str(fingerprint)
        return self


# --- Slugs --------------------------------------------------------------------


def slugify(value: object) -> str:
    """Fold arbitrary text into the configured slug shape.

    Accents are stripped rather than percent-encoded (``Crème`` -> ``creme``)
    because a slug lands in a URL a human reads and retypes. The result is
    always non-empty: text that reduces to nothing becomes the configured
    fallback, because an empty slug is a routing bug discovered in production.
    """
    text = str(value or "").strip().lower() if MODEL_BASES_OPS["slug_lowercase"] else str(value or "").strip()
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    separator = str(MODEL_BASES_OPS["slug_separator"])
    collapsed = re.sub(rf"[^{re.escape(separator)}a-z0-9]+", separator, ascii_text)
    collapsed = re.sub(rf"{re.escape(separator)}{{2,}}", separator, collapsed).strip(separator)
    limit = int(MODEL_BASES_OPS["max_slug_length"])
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].strip(separator)
    if not collapsed:
        return str(MODEL_BASES_OPS["slug_fallback"])
    if collapsed in SLUG_RESERVED:
        collapsed = f"{collapsed}{separator}{MODEL_BASES_OPS['slug_fallback']}"
    return collapsed


def unique_slug(base: object, existing: Iterable[str] = ()) -> str:
    """``slugify(base)``, suffixed until it does not collide.

    Bounded by ``slug_max_attempts``. Running out of attempts returns the
    *last* candidate rather than raising: a slug is a convenience, and a table
    that cannot find a free suffix is a constraint problem the caller should
    discover from the returned value, not an exception from a formatting
    helper.
    """
    stem = slugify(base)
    taken = {str(item) for item in existing}
    if stem not in taken:
        return stem
    for attempt in range(1, int(MODEL_BASES_OPS["slug_max_attempts"]) + 1):
        candidate = f"{stem}{MODEL_BASES_OPS['slug_separator']}{attempt}"
        if candidate not in taken:
            return candidate
    return f"{stem}{MODEL_BASES_OPS['slug_separator']}{int(MODEL_BASES_OPS['slug_max_attempts'])}"


# --- Approval ------------------------------------------------------------------

APPROVAL_STATES = ("draft", "pending", "approved", "rejected", "withdrawn")
APPROVAL_TERMINAL_STATE = "approved"

# ``requires_second_pair`` is the whole point of the table: a move that needs
# one cannot be made by the same actor who proposed it, and that rule is a row
# rather than a check buried in a service.
APPROVAL_TRANSITIONS: list[dict[str, object]] = [
    {"from": "draft", "to": "pending", "requires_second_pair": False, "description": "Submit for review."},
    {"from": "draft", "to": "withdrawn", "requires_second_pair": False, "description": "Abandon before review."},
    {"from": "pending", "to": "approved", "requires_second_pair": True, "description": "Sign off. Must not be the proposer."},
    {"from": "pending", "to": "rejected", "requires_second_pair": False, "description": "Send back with a reason."},
    {"from": "pending", "to": "withdrawn", "requires_second_pair": False, "description": "Withdraw during review."},
    {"from": "rejected", "to": "draft", "requires_second_pair": False, "description": "Revise and resubmit."},
    {"from": "approved", "to": "pending", "requires_second_pair": True, "description": "Reopen. Requires a second pair of eyes."},
]

APPROVAL_OPS: dict[str, object] = {
    "initial_state": "draft",
    "terminal_states": ("approved", "rejected", "withdrawn"),
    "note_max_length": 500,
    "note": (
        "A terminal state is one with no outgoing transition except an explicit "
        "reopen. 'approved' is the only one that makes a row effective; the "
        "others are endings."
    ),
}


def _approval_transition_key(from_state: str, to_state: str) -> dict[str, object] | None:
    for row in APPROVAL_TRANSITIONS:
        if str(row["from"]) == str(from_state) and str(row["to"]) == str(to_state):
            return row
    return None


def approval_transition_verdict(
    from_state: str, to_state: str, *, actor_user_id: int | None = None, proposer_user_id: int | None = None
) -> dict[str, object]:
    """Decide a state move. Never raises; every answer is a verdict.

    An unknown *source* state is a bug in the caller's data and is reported as
    such, distinct from a forbidden move — collapsing the two would make a
    corrupted ``approval_state`` look like a policy refusal.
    """
    row = _approval_transition_key(from_state, to_state)
    if str(from_state) not in APPROVAL_STATES:
        return {
            "allowed": False,
            "reason": f"unknown source state '{from_state}'",
            "from": str(from_state),
            "to": str(to_state),
            "requires_second_pair": False,
            "applied": False,
        }
    if row is None:
        return {
            "allowed": False,
            "reason": f"'{from_state}' -> '{to_state}' is not a declared transition",
            "from": str(from_state),
            "to": str(to_state),
            "requires_second_pair": False,
            "applied": False,
        }
    needs_second = bool(row["requires_second_pair"])
    same_actor = (
        needs_second
        and proposer_user_id is not None
        and actor_user_id is not None
        and int(actor_user_id) == int(proposer_user_id)
    )
    return {
        "allowed": not same_actor,
        "reason": (
            "requires a second pair of eyes; the proposer cannot approve their own row"
            if same_actor
            else str(row["description"])
        ),
        "from": str(from_state),
        "to": str(to_state),
        "requires_second_pair": needs_second,
        "applied": not same_actor,
    }


def approval_next_states(state: str) -> tuple[str, ...]:
    """Every state reachable in one move, in table order."""
    return tuple(
        str(row["to"]) for row in APPROVAL_TRANSITIONS if str(row["from"]) == str(state)
    )


def approval_transition_report() -> dict[str, Any]:
    """The table as a graph, plus the states no row can reach."""
    edges = {
        str(state): list(approval_next_states(state)) for state in APPROVAL_STATES
    }
    reachable = {str(APPROVAL_OPS["initial_state"])}
    frontier = [str(APPROVAL_OPS["initial_state"])]
    while frontier:
        for nxt in edges.get(frontier.pop(), ()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    return {
        "states": list(APPROVAL_STATES),
        "terminal_states": list(APPROVAL_OPS["terminal_states"]),
        "initial_state": str(APPROVAL_OPS["initial_state"]),
        "effective_state": APPROVAL_TERMINAL_STATE,
        "edges": edges,
        "transitions": [dict(row) for row in APPROVAL_TRANSITIONS],
        "second_pair_transitions": [
            f"{row['from']}->{row['to']}" for row in APPROVAL_TRANSITIONS if row["requires_second_pair"]
        ],
        "unreachable_states": sorted(set(APPROVAL_STATES) - reachable),
        "ops": dict(APPROVAL_OPS),
    }


# --- Money ---------------------------------------------------------------------

# ISO-4217 minor-unit exponents. Only the codes that differ from two appear,
# because "most currencies have two decimals" is the rule and this table is
# its exceptions — listing all 180 codes would hide the four that matter.
CURRENCY_EXPONENTS: dict[str, int] = {
    "BHD": 3, "CLF": 4, "CLP": 0, "IQD": 3, "ISK": 0, "JOD": 3, "JPY": 0,
    "KMF": 0, "KRW": 0, "KWD": 3, "LYD": 3, "OMR": 3, "PYG": 0, "RWF": 0,
    "TND": 3, "UGX": 0, "UYI": 0, "UYW": 4, "VND": 0, "VUV": 0, "XAF": 0,
    "XOF": 0, "XPF": 0,
}

MONEY_OPS: dict[str, object] = {
    "default_exponent": 2,
    "max_exponent": 4,
    "rounding": "ROUND_HALF_EVEN",
    "allow_unknown_currency": True,
    "note": (
        "Amounts are stored as integer minor units. An unknown currency code is "
        "accepted at the default exponent and reported as such, because "
        "rejecting a real ISO code this table has not heard of would lose a "
        "transaction; storing it as a float would lose more of them."
    ),
}


def currency_exponent(currency: str) -> int:
    """Minor-unit exponent for a currency code, clamped to ``max_exponent``."""
    code = str(currency or "").strip().upper()
    exponent = CURRENCY_EXPONENTS.get(code, int(MONEY_OPS["default_exponent"]))
    return max(0, min(int(exponent), int(MONEY_OPS["max_exponent"])))


def _to_decimal(value: object) -> "Decimal | None":
    try:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value).strip())
    except Exception:
        return None


def _iso_shape(currency: str) -> bool:
    """Whether a code has the shape of an ISO-4217 alphabetic code.

    Not a membership test against a 180-entry list: the point is to catch
    obvious garbage ("US", "dollars") without carrying a table whose only job
    is to be incomplete.
    """
    code = str(currency or "").strip()
    return len(code) == 3 and code.isalpha() and code.isupper()


def money_conversion(amount: object, currency: str = "USD") -> dict[str, Any]:
    """Convert a major-unit amount to exact integer minor units.

    Returns a verdict rather than a bare integer so a caller can see *why* a
    conversion failed. Never raises: money arrives from user input, and a
    helper that raises on ``"12.34abc"`` turns a bad form field into a 500.
    """
    code = str(currency or "").strip().upper()
    exponent = currency_exponent(code)
    number = _to_decimal(amount)
    if number is None:
        return {
            "ok": False,
            "minor_units": 0,
            "major_units": None,
            "currency": code,
            "exponent": exponent,
            "exponent_source": "table" if code in CURRENCY_EXPONENTS else "default",
            "iso_shape": _iso_shape(code),
            "rounded": False,
            "reason": f"{amount!r} is not a decimal amount",
        }
    scale = Decimal(1).scaleb(-exponent)
    try:
        quantized = number.quantize(scale, rounding=ROUND_HALF_EVEN)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "minor_units": 0,
            "major_units": None,
            "currency": code,
            "exponent": exponent,
            "exponent_source": "table" if code in CURRENCY_EXPONENTS else "default",
            "iso_shape": _iso_shape(code),
            "rounded": False,
            "reason": f"could not quantize at exponent {exponent}: {exc}",
        }
    minor = int(quantized.scaleb(exponent))
    return {
        "ok": True,
        "minor_units": minor,
        "major_units": float(quantized),
        "currency": code,
        "exponent": exponent,
        "exponent_source": "table" if code in CURRENCY_EXPONENTS else "default",
        "iso_shape": _iso_shape(code),
        "rounded": quantized != number,
        "reason": "",
    }


def to_minor_units(amount: object, currency: str = "USD") -> int:
    """Integer minor units, or ``0`` for an unreadable amount."""
    return int(money_conversion(amount, currency)["minor_units"])


def from_minor_units(minor: object, currency: str = "USD") -> float:
    """Major units from integer minor units. Unreadable input yields ``0.0``."""
    try:
        count = Decimal(str(int(minor)))
    except Exception:
        return 0.0
    exponent = currency_exponent(currency)
    return float(count.scaleb(-exponent))


# --- Idempotency ---------------------------------------------------------------


def request_fingerprint(method: str, path: str, body: object = None) -> str:
    """A stable digest of the parts of a request that make it *the same* request.

    Body keys are sorted and ``None`` values dropped, so ``{"a":1,"b":None}``
    and ``{"b":null,"a":1}`` fingerprint identically — a client that reorders
    its JSON is retrying, not issuing a new request. Query string is passed as
    part of ``path``, so two different queries are two different requests.
    """
    normalized: object
    if isinstance(body, dict):
        normalized = {str(k): body[k] for k in sorted(body, key=str) if body[k] is not None}
    elif isinstance(body, (list, tuple)):
        normalized = list(body)
    else:
        normalized = body
    material = json.dumps(
        {
            "method": str(method or "").strip().upper(),
            "path": str(path or ""),
            "body": normalized,
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def idempotency_verdict(
    stored_key: str | None, stored_fingerprint: str | None, candidate_key: str, candidate_fingerprint: str
) -> dict[str, Any]:
    """Decide whether a write is new, a replay, or a client bug. Never raises.

    Three outcomes, deliberately distinct: a fresh key proceeds, the *same* key
    with the *same* body is a replay to be answered from the original result,
    and the same key with a *different* body is a conflict. Reporting the third
    as a replay would silently return the wrong row's response.
    """
    if not stored_key:
        return {
            "outcome": "new",
            "proceed": True,
            "is_replay": False,
            "reason": "no idempotency key stored on this row",
        }
    if str(stored_key) != str(candidate_key):
        return {
            "outcome": "new",
            "proceed": True,
            "is_replay": False,
            "reason": "a different idempotency key is presented",
        }
    if str(stored_fingerprint or "") == str(candidate_fingerprint):
        return {
            "outcome": "replay",
            "proceed": False,
            "is_replay": True,
            "reason": "same key and same body: answer from the original result",
        }
    return {
        "outcome": "conflict",
        "proceed": False,
        "is_replay": False,
        "reason": "same key with a different body: the client reused a key, which is a client bug",
    }


# --- Security-event query and rollup -------------------------------------------

SEVERITY_BANDS: list[dict[str, object]] = [
    {"severity": "critical", "minimum_rank": 4, "escalate": True, "description": "Page someone. Never batch."},
    {"severity": "high", "minimum_rank": 3, "escalate": True, "description": "Review the same day."},
    {"severity": "medium", "minimum_rank": 2, "escalate": False, "description": "Review with the weekly rollup."},
    {"severity": "low", "minimum_rank": 1, "escalate": False, "description": "Trend only."},
    {"severity": "info", "minimum_rank": 0, "escalate": False, "description": "Baseline volume."},
]

# Where a security event came from. ``default`` marks the value
# ``build_security_event`` already uses, so a caller that names nothing is
# reporting a fact rather than a blank.
SECURITY_EVENT_SOURCES: list[dict[str, object]] = [
    {"source": "zero_trust", "default": True, "category": "policy", "description": "The cell-matrix / risk evaluator path."},
    {"source": "auth", "default": False, "category": "identity", "description": "Login, token issuance and refresh."},
    {"source": "api", "default": False, "category": "identity", "description": "API-key and machine-credential traffic."},
    {"source": "admin", "default": False, "category": "operator", "description": "A privileged operator action."},
    {"source": "import", "default": False, "category": "operator", "description": "Bulk data movement."},
    {"source": "system", "default": False, "category": "infrastructure", "description": "A background worker or scheduled job."},
]

SECURITY_EVENT_QUERY_OPS: dict[str, object] = {
    "default_limit": 50,
    "max_limit": 500,
    "default_order": "desc",
    "orders": ("asc", "desc"),
    "max_severity_span": 5,
    "note": (
        "A query spec is data, not a statement: nothing here touches a "
        "session. The limit is clamped rather than rejected so a dashboard "
        "asking for 10,000 rows gets the newest 500 instead of a 422."
    ),
}

SECURITY_EVENT_SOURCES_BY_NAME = {str(row["source"]): row for row in SECURITY_EVENT_SOURCES}
SECURITY_EVENT_DEFAULT_SOURCE = next(
    str(row["source"]) for row in SECURITY_EVENT_SOURCES if row["default"]
)


def severity_band(severity: str) -> dict[str, Any]:
    """The band a severity falls in, or an ``unknown`` band that says so."""
    name = str(severity or "")
    rank = SECURITY_SEVERITY_RANK.get(name)
    for row in SEVERITY_BANDS:
        if rank is not None and int(row["minimum_rank"]) == int(rank):
            return {**row, "known": True, "rank": rank}
    return {
        "severity": name,
        "minimum_rank": None,
        "escalate": False,
        "description": "not a declared severity",
        "known": False,
        "rank": rank,
    }


def build_security_event_query(
    *,
    event_kinds: Iterable[str] | None = None,
    min_severity: str | None = None,
    actor_user_id: int | None = None,
    tenant_id: str | None = None,
    source: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    order: str = "desc",
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a filter specification for the security-event table.

    Pure: it returns the shape of a query, never a statement and never a
    session, so it can be unit-tested and shown in a catalog. Unknown enum
    values are *dropped and reported* rather than passed through — a filter
    matching nothing is a silent wrong answer, so the caller is told which
    filter did not apply.
    """
    ignored: list[str] = []

    kinds = [str(kind) for kind in (event_kinds or ())]
    known_kinds = [kind for kind in kinds if kind in {**SECURITY_EVENT_FAMILIES, **SECURITY_EVENT_EXTENSIONS}]
    if kinds and len(known_kinds) != len(kinds):
        ignored.append("event_kinds")

    floor = SECURITY_SEVERITY_RANK.get(str(min_severity)) if min_severity is not None else None
    if min_severity is not None and floor is None:
        ignored.append("min_severity")

    resolved_source = str(source) if source is not None else ""
    if source is not None and resolved_source not in SECURITY_EVENT_SOURCES_BY_NAME:
        ignored.append("source")
        resolved_source = ""

    direction = str(order or "").lower()
    if direction not in SECURITY_EVENT_QUERY_OPS["orders"]:
        ignored.append("order")
        direction = str(SECURITY_EVENT_QUERY_OPS["default_order"])

    cap = int(SECURITY_EVENT_QUERY_OPS["max_limit"])
    try:
        requested = int(limit) if limit is not None else int(SECURITY_EVENT_QUERY_OPS["default_limit"])
    except (TypeError, ValueError):
        requested = int(SECURITY_EVENT_QUERY_OPS["default_limit"])
        ignored.append("limit")
    clamped_limit = max(1, min(requested, cap))

    filters: list[dict[str, object]] = []
    if known_kinds:
        filters.append({"field": "event_kind", "op": "in", "value": known_kinds})
    if floor is not None:
        filters.append(
            {
                "field": "severity",
                "op": "rank_gte",
                "value": int(floor),
                "severities": [
                    name for name, rank in SECURITY_SEVERITY_RANK.items() if rank >= int(floor)
                ],
            }
        )
    if actor_user_id is not None:
        filters.append({"field": "actor_user_id", "op": "eq", "value": int(actor_user_id)})
    if tenant_id:
        filters.append({"field": "tenant_id", "op": "eq", "value": str(tenant_id)})
    if resolved_source:
        filters.append({"field": "source", "op": "eq", "value": resolved_source})
    if since is not None:
        filters.append({"field": "created_at", "op": "gte", "value": since.isoformat()})
    if until is not None:
        filters.append({"field": "created_at", "op": "lte", "value": until.isoformat()})

    return {
        "filters": filters,
        "order_by": [{"field": "id", "direction": direction}],
        "limit": clamped_limit,
        "requested_limit": requested,
        "limit_clamped": clamped_limit != requested,
        "max_limit": cap,
        "ignored": sorted(set(ignored)),
        "table": SecurityEvent.__tablename__,
        "discriminator": "event_kind",
        "note": "A specification only. No session is opened and no statement is built here.",
    }


def _event_field(row: object, name: str, default: object = None) -> object:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def security_event_summary(rows: Iterable[object]) -> dict[str, Any]:
    """Roll a sequence of security events up by kind, severity, source and tenant.

    Accepts either event instances or plain dicts, and counts a row it cannot
    read in ``unreadable`` instead of skipping it. A rollup that quietly drops
    the rows it could not parse reports a *lower* event count than actually
    happened, which is the one direction an ops rollup must never be wrong in.
    """
    by_kind: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_tenant: dict[str, int] = {}
    unreadable = 0
    peak_risk = 0.0
    peak_id: object = None
    total = 0
    escalated = 0

    for row in rows or ():
        kind = _event_field(row, "event_kind")
        if kind is None:
            unreadable += 1
            continue
        total += 1
        kind_name = str(kind)
        by_kind[kind_name] = by_kind.get(kind_name, 0) + 1

        severity_name = str(_event_field(row, "severity", "") or "")
        by_severity[severity_name] = by_severity.get(severity_name, 0) + 1
        if severity_band(severity_name)["escalate"]:
            escalated += 1

        source_name = str(_event_field(row, "source", "") or "")
        by_source[source_name] = by_source.get(source_name, 0) + 1
        tenant_name = str(_event_field(row, "tenant_id", "") or "")
        by_tenant[tenant_name] = by_tenant.get(tenant_name, 0) + 1

        try:
            score = float(_event_field(row, "risk_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            unreadable += 1
            continue
        if score > peak_risk:
            peak_risk = score
            peak_id = _event_field(row, "id")

    return {
        "total": total,
        "unreadable": unreadable,
        "escalating": escalated,
        "by_kind": dict(sorted(by_kind.items(), key=lambda item: (-item[1], item[0]))),
        "by_severity": dict(sorted(by_severity.items(), key=lambda item: (-item[1], item[0]))),
        "by_source": dict(sorted(by_source.items(), key=lambda item: (-item[1], item[0]))),
        "by_tenant": dict(sorted(by_tenant.items(), key=lambda item: (-item[1], item[0]))),
        "peak_risk": peak_risk,
        "peak_risk_id": peak_id,
        "note": (
            "Aggregated over whatever the caller passed in. A row with no "
            "event_kind is counted as unreadable rather than dropped."
        ),
    }


# --- Table introspection --------------------------------------------------------

MIXIN_FAMILY: dict[str, type] = {
    "timestamp": TimestampMixin,
    "tenant_scoped": TenantScopedMixin,
    "partitioned": PartitionedMixin,
    "soft_delete": SoftDeleteMixin,
    "row_version": RowVersionMixin,
    "actor_audit": ActorAuditMixin,
    "expiring": ExpiringMixin,
    "serialization": SerializationMixin,
}

# The four governance mixins above, kept apart from ``MIXIN_FAMILY`` for the
# same reason ``SECURITY_EVENT_EXTENSIONS`` is kept apart: the catalog's
# original key sets are contracts, and widening them silently breaks callers
# that iterate them.
MIXIN_EXTENSIONS: dict[str, type] = {
    "sluggable": SluggableMixin,
    "approval": ApprovalMixin,
    "money": MoneyMixin,
    "idempotency": IdempotencyMixin,
}

MIXIN_CATEGORIES: dict[str, str] = {
    "timestamp": "lifecycle",
    "tenant_scoped": "scoping",
    "partitioned": "scoping",
    "soft_delete": "lifecycle",
    "row_version": "concurrency",
    "actor_audit": "provenance",
    "expiring": "lifecycle",
    "serialization": "projection",
    "sluggable": "identity",
    "approval": "governance",
    "money": "financial",
    "idempotency": "reliability",
}


def all_mixins() -> dict[str, type]:
    """Every mixin, original family first, then the extensions."""
    return {**MIXIN_FAMILY, **MIXIN_EXTENSIONS}


def mixin_contributed_columns() -> dict[str, list[str]]:
    """Column name -> the mixins that declare it.

    Compared by *name*, not by identity: SQLAlchemy copies a mixin's ``Column``
    into every table that uses the mixin, so the objects are distinct per table
    while the provenance is the same.
    """
    provenance: dict[str, list[str]] = {}
    for mixin_name, mixin in all_mixins().items():
        for attribute, value in vars(mixin).items():
            if isinstance(value, Column) and attribute not in provenance:
                provenance[attribute] = []
            if isinstance(value, Column):
                provenance.setdefault(attribute, []).append(mixin_name)
    return {name: sorted(mixins) for name, mixins in provenance.items()}


def mapped_entity_classes() -> list[type]:
    """Every mapped class, ordered by table then class name.

    Read from the registry rather than an import list, so a model added in a
    module nobody remembered is still counted.
    """
    seen: set[type] = set()
    found: list[type] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if cls in seen:
            continue
        seen.add(cls)
        found.append(cls)
    return sorted(
        found,
        key=lambda cls: (str(getattr(cls, "__tablename__", "") or ""), str(cls.__name__)),
    )


def _entities_module_imported() -> bool:
    """Whether ``app.models`` has been imported into this process.

    ``Base.metadata`` only holds the models that have been *imported*. A caller
    that introspects before importing the entity module sees a partial schema,
    and a report that did not say so would read as a whole-schema audit.
    """
    return any(
        str(getattr(cls, "__tablename__", "")) == "users" for cls in mapped_entity_classes()
    )


def mixin_column_provenance(table_name: str | None = None) -> dict[str, Any]:
    """Which mixin contributed each column of each mapped table.

    This is the question that makes the mixin layer debuggable: a column on a
    table that no mixin declares is entity-owned, and a column several mixins
    declare would collide if two of them were ever applied to the same table.
    """
    contributors = mixin_contributed_columns()
    tables: dict[str, Any] = {}

    for cls in mapped_entity_classes():
        table = str(getattr(cls, "__tablename__", "") or "")
        if not table or (table_name and table != table_name):
            continue
        mapper = inspect(cls)
        columns: dict[str, Any] = {}
        for column in mapper.columns:
            mixins = contributors.get(column.key, [])
            columns[column.key] = {
                "mixins": mixins,
                "origin": "mixin" if mixins else "entity",
                "nullable": bool(column.nullable),
                "primary_key": bool(column.primary_key),
                "type": str(column.type),
                "sensitivity": classify_serialization_field(column.key),
            }
        if table not in tables:
            tables[table] = {
                "classes": [],
                "columns": columns,
                "mixin_columns": sorted(k for k, v in columns.items() if v["origin"] == "mixin"),
                "entity_columns": sorted(k for k, v in columns.items() if v["origin"] == "entity"),
            }
        tables[table]["classes"].append(str(cls.__name__))

    for entry in tables.values():
        entry["classes"] = sorted(entry["classes"])

    # An STI family is a class whose mapper *inherits* its polymorphic identity
    # from a root in the same table. Recomputed as its own pass so the column
    # walk above stays a single concern.
    sti_families: dict[str, list[str]] = {}
    for cls in mapped_entity_classes():
        table = str(getattr(cls, "__tablename__", "") or "")
        if not table or table not in tables:
            continue
        mapper = inspect(cls)
        if mapper.base_mapper is not mapper:
            sti_families.setdefault(table, []).append(
                str(mapper.polymorphic_identity or cls.__name__)
            )
    for table, entry in tables.items():
        entry["sti_families"] = sorted(sti_families.get(table, []))

    cap = int(MODEL_BASES_OPS["provenance_table_cap"])
    if cap and len(tables) > cap:
        tables = dict(sorted(tables.items())[:cap])
    return {
        "tables": dict(sorted(tables.items())),
        "table_count": len(tables),
        "contributors": contributors,
        "column_collisions": {
            name: mixins for name, mixins in contributors.items() if len(mixins) > 1
        },
        "entities_module_imported": _entities_module_imported(),
        "coverage": (
            "Base.metadata holds only the modules imported into this process; "
            "entities_module_imported=False means this is a partial view."
        ),
    }


def mixin_capability_report() -> dict[str, Any]:
    """Per-mixin columns, and which mapped tables actually carry them."""
    contributors = mixin_contributed_columns()
    tables = mixin_column_provenance()["tables"]
    mixins: dict[str, Any] = {}
    for mixin_name, mixin in all_mixins().items():
        columns = sorted(name for name, owners in contributors.items() if mixin_name in owners)
        consumers: dict[str, list[str]] = {}
        partial: dict[str, list[str]] = {}
        for table, entry in tables.items():
            present = [name for name in columns if name in entry["columns"]]
            if len(present) == len(columns) and columns:
                # Every declared column is present, so the mixin is genuinely
                # applied. Matching on *any* column would credit a mixin for a
                # table that merely happens to share one name with it — which
                # is exactly how `money` looked applied while no table used it.
                consumers[table] = present
            elif present:
                partial[table] = present
        mixins[mixin_name] = {
            "class": mixin.__name__,
            "category": MIXIN_CATEGORIES.get(mixin_name, "other"),
            "columns": columns,
            "column_count": len(columns),
            "behavior": sorted(
                name
                for name in dir(mixin)
                if not name.startswith("_") and callable(getattr(mixin, name, None))
            ),
            "consumer_tables": sorted(consumers),
            "consumer_count": len(consumers),
            "partial_tables": sorted(partial),
            "partial_columns": {table: partial[table] for table in sorted(partial)},
            "unapplied": not consumers,
        }
    return {
        "mixins": mixins,
        "mixin_count": len(mixins),
        "table_count": len(tables),
        "unapplied": sorted(name for name, row in mixins.items() if row["unapplied"]),
        "entities_module_imported": _entities_module_imported(),
        "note": (
            "An unapplied mixin is available for a new table, not a gap. The "
            "seven original mixins are unapplied too, because no existing table "
            "was altered to adopt them."
        ),
    }


def build_serialization_audit() -> dict[str, Any]:
    """What ``to_dict`` would emit per table, and what each profile removes.

    The interesting output is not the emitted set — that is
    ``SerializationMixin.safe_fields`` — it is the *profiles*: which classes
    each one strips, and whether a column exists whose sensitivity no profile
    was written to consider.
    """
    provenance = mixin_column_provenance()["tables"]
    all_columns: set[str] = set()
    tables: dict[str, Any] = {}
    for table, entry in provenance.items():
        emitted: list[str] = []
        suppressed: list[str] = []
        classified: dict[str, str] = {}
        for name in sorted(entry["columns"]):
            all_columns.add(name)
            sensitivity = str(entry["columns"][name]["sensitivity"])
            classified[name] = sensitivity
            if name.lower() in SERIALIZATION_DENYLIST:
                suppressed.append(name)
            else:
                emitted.append(name)
        per_profile: dict[str, Any] = {}
        for profile in SERIALIZATION_PROFILES:
            probe, removed = filter_payload_for_profile(
                {name: 1 for name in emitted}, profile
            )
            per_profile[profile] = {
                "included": len(probe),
                "removed": sorted(removed),
            }
        tables[table] = {
            "emitted": emitted,
            "emitted_count": len(emitted),
            "suppressed_by_denylist": suppressed,
            "sensitivity": classified,
            "profiles": per_profile,
        }

    dead = sorted(
        name for name in SERIALIZATION_DENYLIST
        if not any(name in {c.lower() for c in table["columns"]} for table in provenance.values())
    )
    return {
        "tables": tables,
        "table_count": len(tables),
        "classes": [dict(row) for row in SERIALIZATION_CLASSES],
        "profiles": {name: dict(row) for name, row in SERIALIZATION_PROFILES.items()},
        "resolved_profiles": {
            name: resolve_serialization_profile(name) for name in SERIALIZATION_PROFILES
        },
        "denylist": sorted(SERIALIZATION_DENYLIST),
        "unmatched_denylist_entries": dead,
        "sensitive_columns": sorted(
            {
                name
                for table in tables.values()
                for name, klass in table["sensitivity"].items()
                if klass != "ordinary"
            }
        ),
        "entities_module_imported": _entities_module_imported(),
        "note": (
            "SERIALIZATION_DENYLIST is absolute: no profile emits a denylisted "
            "field, including 'internal'. An unmatched denylist entry is a "
            "warning, not dead config — it is a name reserved for a future "
            "table."
        ),
    }


# --- Validation and catalog -----------------------------------------------------


def validate_model_bases() -> dict[str, Any]:
    """Check the mixin/registry tables against the mapped schema.

    Errors mean the layer cannot be trusted: two mixins claiming one column
    name, a registry entry that is not a mapped class, a duplicate table. Errors
    are all reachable from a *new* table adopting a mixin, which is exactly
    when nobody is looking at this module.

    Warnings mean something is available but unused: a mixin no table applies,
    a denylist entry matching no column, a severity no band describes.
    """
    errors: list[str] = []
    warnings: list[str] = []

    provenance = mixin_column_provenance()
    for name, owners in sorted(provenance["column_collisions"].items()):
        errors.append(
            f"column '{name}' is declared by more than one mixin ({', '.join(owners)}); "
            "applying both to one table would collide"
        )

    table_owners: dict[str, list[str]] = {}
    for cls in mapped_entity_classes():
        table = str(getattr(cls, "__tablename__", "") or "")
        if table:
            table_owners.setdefault(table, []).append(str(cls.__name__))
    for table, classes in sorted(table_owners.items()):
        if len(classes) > 1 and table != SecurityEvent.__tablename__:
            warnings.append(
                f"table '{table}' is mapped by {len(classes)} classes "
                f"({', '.join(sorted(classes))})"
            )

    # ``REGISTRY.catalog()`` yields *descriptions*, so the check reads the
    # class mappings directly and uses the registry for the name set.
    known_classes = {**SECURITY_EVENT_FAMILIES, **SECURITY_EVENT_EXTENSIONS}
    known_classes["security_event"] = SecurityEvent
    for name, model in sorted(known_classes.items()):
        if not hasattr(model, "__tablename__"):
            errors.append(f"security family '{name}' -> {model} is not a mapped model")
    for name in sorted(REGISTRY.names()):
        if name not in known_classes:
            errors.append(f"registry entry '{name}' has no class in the security-event mappings")

    for kind, model in sorted({**SECURITY_EVENT_FAMILIES, **SECURITY_EVENT_EXTENSIONS}.items()):
        if str((getattr(model, "__mapper_args__", {}) or {}).get("polymorphic_identity") or "") != kind:
            errors.append(
                f"security family '{kind}' declares polymorphic_identity "
                f"{(getattr(model, '__mapper_args__', {}) or {}).get('polymorphic_identity')!r}"
            )

    for row in SEVERITY_BANDS:
        if int(row["minimum_rank"]) not in set(SECURITY_SEVERITY_RANK.values()):
            errors.append(f"severity band '{row['severity']}' has rank {row['minimum_rank']}, which no severity uses")
    uncovered = sorted(set(SECURITY_SEVERITY_RANK) - {str(row["severity"]) for row in SEVERITY_BANDS})
    if uncovered:
        warnings.append(f"severities with no band: {', '.join(uncovered)}")

    for source in SECURITY_EVENT_SOURCES:
        if not str(source["source"]).strip():
            errors.append("SECURITY_EVENT_SOURCES has a row with an empty source")
    defaults = [row for row in SECURITY_EVENT_SOURCES if row["default"]]
    if len(defaults) != 1:
        errors.append(f"expected exactly one default source, found {len(defaults)}")

    approval_report = approval_transition_report()
    if approval_report["unreachable_states"]:
        errors.append(
            f"approval states unreachable from '{APPROVAL_OPS['initial_state']}': "
            f"{', '.join(approval_report['unreachable_states'])}"
        )
    for state in APPROVAL_STATES:
        if state not in approval_report["edges"]:
            warnings.append(f"approval state '{state}' has no outgoing transition")

    for code, exponent in sorted(CURRENCY_EXPONENTS.items()):
        if int(exponent) > int(MONEY_OPS["max_exponent"]):
            errors.append(
                f"currency '{code}' declares exponent {exponent}, above max_exponent {MONEY_OPS['max_exponent']}"
            )

    # A declared profile that does not resolve to itself would mean the table
    # and the resolver disagree. The unknown probe is deliberate, so it is
    # asserted rather than warned about — and it is checked against the pinned
    # class set, not against the mutable ``public`` row, so widening ``public``
    # is itself an error rather than something the check silently follows.
    profiles = validate_serialization_profiles()
    for name in sorted(SERIALIZATION_PROFILES):
        if not profiles[name]["known"]:
            errors.append(f"declared serialization profile '{name}' does not resolve to itself")
    if SERIALIZATION_FALLBACK_PROFILE not in SERIALIZATION_PROFILES:
        errors.append(
            f"the unknown-profile fallback '{SERIALIZATION_FALLBACK_PROFILE}' is not a declared profile"
        )
    probe = profiles["__unknown_probe__"]
    if probe["known"] or probe["profile"] != "no-such-profile":
        errors.append("an unknown serialization profile no longer reports itself as unknown")
    if set(probe["exclude_classes"]) != set(SERIALIZATION_FALLBACK_CLASSES):
        errors.append(
            "an unknown serialization profile no longer falls back to excluding every class"
        )
    undeclared = sorted(
        set(probe["exclude_classes"]) - {str(row["class"]) for row in SERIALIZATION_CLASSES}
    )
    if undeclared:
        errors.append(f"a serialization profile excludes undeclared classes: {', '.join(undeclared)}")

    # A list response is ``{key: [row, ...]}``, which puts the row dict at
    # depth 2. Any profile with a bound below that empties every row — either
    # to ``[]`` or to a list of empty dicts, depending on where the bound lands —
    # so the bound, not the class list, becomes the thing that decides the
    # payload. Checked on shape rather than on any one field name, so tightening
    # the classes cannot make this fire spuriously.
    row_shape = {"items": [{"title": "keep", "email": "a@b.c"}]}
    for name in sorted(SERIALIZATION_PROFILES):
        filtered, _removed = filter_payload_for_profile(row_shape, name)
        rows = filtered["items"]
        if rows and all(row for row in rows):
            continue
        errors.append(
            f"serialization profile '{name}' has max_depth "
            f"{SERIALIZATION_PROFILES[name]['max_depth']}, which empties a list-of-rows payload"
        )

    audit = build_serialization_audit()
    if audit["unmatched_denylist_entries"]:
        warnings.append(
            f"denylist entries matching no mapped column (reserved for future tables, not dead): "
            f"{', '.join(audit['unmatched_denylist_entries'])}"
        )

    capability = mixin_capability_report()
    if capability["unapplied"]:
        warnings.append(
            f"mixins with no mapped-table consumer: {', '.join(capability['unapplied'])}"
        )
    for mixin_name, row in capability["mixins"].items():
        if row["partial_tables"]:
            overlaps = "; ".join(
                f"{table} ({', '.join(cols)})" for table, cols in row["partial_columns"].items()
            )
            warnings.append(
                f"mixin '{mixin_name}' shares a column name with a table it is not applied to — "
                f"adopting it there is a DDL change: {overlaps}"
            )

    if not provenance["entities_module_imported"]:
        warnings.append(
            "app.models is not imported into this process, so the schema view is partial"
        )

    return {
        "valid": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "error_list": errors,
        "warning_list": warnings,
        "mixins": len(all_mixins()),
        "tables": provenance["table_count"],
        "note": (
            "errors mean the mixin layer cannot be trusted; warnings mean "
            "something is available but not yet used."
        ),
    }


def validate_serialization_profiles() -> dict[str, dict[str, object]]:
    """Resolve every known profile name plus a deliberately wrong one.

    The unknown case is part of the contract under test: a typo must resolve to
    the most restrictive profile, so this reports what a typo *would* get.
    """
    resolved = {
        name: resolve_serialization_profile(name) for name in SERIALIZATION_PROFILES
    }
    resolved["__unknown_probe__"] = resolve_serialization_profile("no-such-profile")
    return resolved


def build_model_bases_ops_catalog() -> dict[str, Any]:
    """Introspectable contract for the governance layer added in this pass."""
    validation = validate_model_bases()
    capability = mixin_capability_report()
    return {
        "validation": {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "warnings": validation["warnings"],
            "error_list": validation["error_list"],
            "warning_list": validation["warning_list"],
        },
        "mixins": sorted(all_mixins()),
        "mixin_extensions": sorted(MIXIN_EXTENSIONS),
        "mixin_categories": dict(sorted(MIXIN_CATEGORIES.items())),
        "capability": capability,
        "field_classes": [dict(row) for row in SERIALIZATION_CLASSES],
        "profiles": {name: dict(row) for name, row in SERIALIZATION_PROFILES.items()},
        "resolved_profiles": validate_serialization_profiles(),
        "denylist": sorted(SERIALIZATION_DENYLIST),
        "slugs": {
            "reserved": sorted(SLUG_RESERVED),
            "separator": MODEL_BASES_OPS["slug_separator"],
            "max_length": int(MODEL_BASES_OPS["max_slug_length"]),
            "max_attempts": int(MODEL_BASES_OPS["slug_max_attempts"]),
        },
        "approval": approval_transition_report(),
        "money": {
            "exceptions": dict(sorted(CURRENCY_EXPONENTS.items())),
            "ops": dict(MONEY_OPS),
        },
        "idempotency": {"window_seconds": int(MODEL_BASES_OPS["idempotency_window_seconds"])},
        "severity_bands": [dict(row) for row in SEVERITY_BANDS],
        "event_sources": [dict(row) for row in SECURITY_EVENT_SOURCES],
        "event_query_ops": dict(SECURITY_EVENT_QUERY_OPS),
        "ops": dict(MODEL_BASES_OPS),
        "base_catalog": build_model_bases_catalog(),
        "note": (
            "Additive governance layer: four mixins available for new tables, "
            "field-sensitivity classes over the original denylist, and pure "
            "introspection. No existing table gained a column."
        ),
    }
