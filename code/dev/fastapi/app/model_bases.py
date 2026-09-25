"""Isolated polymorphic base models.

Data definitions derive from these bases instead of re-declaring plumbing on
every table. The module deliberately lives apart from ``app/models.py`` so the
concrete entities stay thin and new entity families can reuse the shared
scoping/partitioning columns without touching existing tables (their DDL is
already emitted by ``Base.metadata.create_all`` / alembic and is left
untouched — see BLOCKAGES.md).

Three kinds of reuse live here:

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
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, func

from app.db import Base


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


class AuthenticationSecurityEvent(SecurityEvent):
    """Login / token-issuance signals."""
    __mapper_args__ = {"polymorphic_identity": "authentication"}


class RiskSecurityEvent(SecurityEvent):
    """Contextual risk-evaluation signals."""
    __mapper_args__ = {"polymorphic_identity": "risk"}


class AccessSecurityEvent(SecurityEvent):
    """Data-cell access / permission signals."""
    __mapper_args__ = {"polymorphic_identity": "access"}


# Concrete security-event families discoverable by tooling.
SECURITY_EVENT_FAMILIES = {
    "authentication": AuthenticationSecurityEvent,
    "risk": RiskSecurityEvent,
    "access": AccessSecurityEvent,
}


def security_event_family(event_kind: str):
    """Return the concrete polymorphic class for ``event_kind`` (or the root)."""
    return SECURITY_EVENT_FAMILIES.get(event_kind, SecurityEvent)


def build_model_bases_catalog() -> dict[str, object]:
    """Introspectable description of the polymorphic base layer."""
    return {
        "mixins": ["timestamp", "tenant_scoped", "partitioned"],
        "polymorphic_root": "security_event",
        "families": {kind: cls.__name__ for kind, cls in SECURITY_EVENT_FAMILIES.items()},
        "discriminator": "event_kind",
        "note": "polymorphic identity drives table reuse; new families are subclass-only additions",
    }