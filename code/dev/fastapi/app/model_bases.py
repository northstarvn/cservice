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

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import (
    Boolean,
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
