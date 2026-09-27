"""ORM models, plus a declarative catalog of what those models actually mean.

Two layers live here, and it is worth keeping them apart:

* **The DDL layer** (everything above the expansion marker) is the schema. It is
  authoritative, it is what Alembic migrates, and it is not config-driven --
  changing it means a migration.
* **The metadata layer** (everything below the marker) is *derived* from the
  schema at import time, plus a set of config tables that add the judgments the
  schema cannot express: which columns are sensitive, which string columns are
  *supposed* to hold an enum value, which tables are append-only, which
  quantities are legitimately signed.

The metadata layer exists because three recurring questions could not be
answered from the ORM alone:

1. *What may I serialise?* ``sensitivity_of`` / ``model_to_dict`` classify every
   column, so redaction is driven by a table rather than by each endpoint
   remembering which fields are private.
2. *Which string columns are quietly unenforced?* ``bookings.status`` is a
   native enum, but ``booking_events.to_status``, ``booking_events.from_status``
   and ``arrears_entries.service_type`` are plain ``VARCHAR`` columns that hold
   the same vocabulary with no constraint at all. ``enum_field_report`` names
   them instead of leaving them to be discovered by a bad ``filter()``.
3. *What does the schema not protect?* ``check_constraint_coverage`` and
   ``referential_integrity_gaps`` report float columns with no non-negative
   check and id-shaped columns with no foreign key.

Everything here is additive: no table, column, constraint or index is added or
changed, so no Alembic migration is required. The helpers are pure functions
over ``Base.metadata`` plus a handful of overrides, so they can be called from
routers, services and ``/meta`` without a database session.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, Integer, String, DateTime, Text, Float, ForeignKey, Enum as SAEnum, Boolean, func, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import relationship
import enum
from app.db import Base  # Import Base from db.py instead of creating new one
from app.model_bases import (  # isolated polymorphic base models
    AccessSecurityEvent,
    AuthenticationSecurityEvent,
    PartitionedMixin,
    RiskSecurityEvent,
    SecurityEvent,
    TenantScopedMixin,
    TimestampMixin,
)



class ChatHistory(Base, TimestampMixin):
    __tablename__ = "chat_history"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    message = Column(Text, nullable=False)
    response = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class InteractionSignal(Base, TimestampMixin):
    __tablename__ = "interaction_signals"
    __table_args__ = (
        CheckConstraint("score >= 0", name="ck_interaction_signals_score_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(50), nullable=False, default="chat")
    area = Column(String(100), nullable=False, index=True)
    priority = Column(String(20), nullable=False, default="medium")
    score = Column(Float, nullable=False, default=0.0)
    evidence = Column(Text, nullable=False, default="")
    recommendation = Column(Text, nullable=False)


class CustomerPolicyScore(Base, TimestampMixin):
    __tablename__ = "customer_policy_scores"
    __table_args__ = (
        CheckConstraint("system_score >= 0", name="ck_customer_policy_scores_system_score_non_negative"),
        CheckConstraint("customer_score >= 0", name="ck_customer_policy_scores_customer_score_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    system_score = Column(Float, nullable=False, default=0.0)
    customer_score = Column(Float, nullable=False, default=0.0)
    access_score = Column(Float, nullable=False, default=0.0)
    interest_score = Column(Float, nullable=False, default=0.0)
    closeness_score = Column(Float, nullable=False, default=0.0)
    community_closeness_score = Column(Float, nullable=False, default=0.0)
    policy_tier = Column(String(20), nullable=False, default="standard", index=True)
    control_posture = Column(String(32), nullable=False, default="constrained", index=True)
    source = Column(String(50), nullable=False, default="system_and_customer_metrics")
    summary = Column(Text, nullable=False, default="")


class RetentionSnapshot(Base, TimestampMixin):
    __tablename__ = "retention_snapshots"
    __table_args__ = (
        CheckConstraint("loyalty_score >= 0", name="ck_retention_snapshots_loyalty_score_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_type = Column(String(50), nullable=False, index=True)
    window_days = Column(Integer, nullable=False, default=30)
    loyalty_score = Column(Float, nullable=False, default=0.0)
    churn_risk = Column(String(20), nullable=False, default="low")
    lifecycle_stage = Column(String(50), nullable=False, default="new")
    summary_json = Column(Text, nullable=False, default="{}")


class RecoveryOutcome(Base, TimestampMixin):
    __tablename__ = "recovery_outcomes"
    __table_args__ = (
        CheckConstraint("dissatisfaction_score >= 0", name="ck_recovery_outcomes_dissatisfaction_score_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    recovery_readiness = Column(String(20), nullable=False)
    dissatisfaction_score = Column(Float, nullable=False, default=0.0)
    primary_risks_json = Column(Text, nullable=False, default="[]")
    recovery_signals_json = Column(Text, nullable=False, default="[]")
    follow_up_json = Column(Text, nullable=False, default="{}")
    escalation_path_json = Column(Text, nullable=False, default="[]")
    handoff_outcome_json = Column(Text, nullable=False, default="{}")
    action_plan = Column(Text, nullable=False, default="")
    acknowledged = Column(Boolean, nullable=False, default=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    follow_up_completed = Column(Boolean, nullable=False, default=False)
    follow_up_completed_at = Column(DateTime(timezone=True), nullable=True)
    source = Column(String(50), nullable=False, default="chat")


class RecoveryAction(Base, TimestampMixin):
    """Audit row for every automatically triggered recovery playbook action.

    The recovery-playbooks orchestrator executes config-driven playbooks
    (credit_points / escalate_ticket / adjust_policy_score) and records each
    action here, so automated recovery stays explainable and auditable.
    """
    __tablename__ = "recovery_actions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    playbook_id = Column(String(80), nullable=False, index=True)
    action = Column(String(40), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="executed", index=True)
    payload_json = Column(Text, nullable=False, default="{}")
    result_json = Column(Text, nullable=False, default="{}")
    reference = Column(String(120), nullable=False, default="", index=True)
    failure_reason = Column(Text, nullable=False, default="")


class TopicSelection(Base, TimestampMixin):
    __tablename__ = "topic_selections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    topic = Column(String(120), nullable=False, index=True)
    source = Column(String(50), nullable=False, default="chat")
    rationale = Column(Text, nullable=False, default="")
    confidence = Column(Float, nullable=False, default=0.0)
    is_current = Column(Boolean, nullable=False, default=True, index=True)


class BookingEvent(Base, TimestampMixin):
    __tablename__ = "booking_events"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)
    from_status = Column(String(20), nullable=True)
    to_status = Column(String(20), nullable=True)
    note = Column(Text, nullable=False, default="")


class BookingAssignment(Base, TimestampMixin):
    __tablename__ = "booking_assignments"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    room_id = Column(Integer, nullable=False, index=True)
    match_reason = Column(Text, nullable=False)
    state = Column(String(20), nullable=False, default="suggested")
    source = Column(String(50), nullable=False, default="booking-service")
    explanation = Column(Text, nullable=False, default="")
    is_current = Column(Boolean, nullable=False, default=True, index=True)

    booking = relationship("Booking", backref="assignments")
    assigned_user = relationship("User")

class BookingStatus(enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"

class ServiceType(enum.Enum):
    consultation = "consultation"
    delivery = "delivery"
    meeting = "meeting"
    project = "project"

class AuditLogEntry(Base, TimestampMixin):
    """Immutable trail of operator and system actions.

    Mirrors the explainability guardrails: every important decision (override,
    assignment, policy change, settlement) can be recorded here so the backend
    can later answer *who did what, on which entity, and why*.
    """
    __tablename__ = "audit_log_entries"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info','warning','critical')",
            name="ck_audit_log_entries_severity_valid",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(50), nullable=False, default="system", index=True)
    entity_id = Column(String(120), nullable=False, default="", index=True)
    summary = Column(Text, nullable=False, default="")
    detail_json = Column(Text, nullable=False, default="{}")
    severity = Column(String(20), nullable=False, default="info", index=True)
    source = Column(String(50), nullable=False, default="api", index=True)


class UserCommunicationOverride(Base, TimestampMixin):
    """Admin-selected communication profile for a user.

    Highest-precedence criterion in the communication-strategy resolver
    (admin_select layer). At most one row per user (unique user_id).
    """
    __tablename__ = "communication_overrides"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    profile_id = Column(String(50), nullable=False, index=True)
    set_by_admin_id = Column(Integer, nullable=False, default=0)
    note = Column(Text, nullable=False, default="")


class ArrearsEntry(Base, TimestampMixin):
    """A deferred payment (pay-in-arrears) with policy-selected interest terms.

    The interest policy is chosen at open time by the config-driven
    `ARREARS_INTEREST_POLICIES` engine and snapshotted onto the row so later
    admin changes to the catalog never rewrite already-open agreements.
    """
    __tablename__ = "arrears_entries"
    __table_args__ = (
        CheckConstraint("principal >= 0", name="ck_arrears_entries_principal_non_negative"),
        CheckConstraint("annual_rate >= 0", name="ck_arrears_entries_annual_rate_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    booking_id = Column(Integer, nullable=True, index=True)
    reference = Column(String(120), nullable=False, default="")
    service_type = Column(String(50), nullable=False, default="consultation", index=True)
    principal = Column(Float, nullable=False, default=0.0)
    currency = Column(String(8), nullable=False, default="USD")
    policy_id = Column(String(50), nullable=False, index=True)
    annual_rate = Column(Float, nullable=False, default=0.0)
    grace_days = Column(Integer, nullable=False, default=0)
    compounding = Column(String(20), nullable=False, default="simple")
    interest_cap_pct = Column(Float, nullable=False, default=100.0)
    defer_days = Column(Integer, nullable=False, default=30)
    interest_accrued = Column(Float, nullable=False, default=0.0)
    status = Column(String(20), nullable=False, default="open", index=True)
    opened_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    due_at = Column(DateTime(timezone=True), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    settled_interest = Column(Float, nullable=False, default=0.0)
    total_settled = Column(Float, nullable=False, default=0.0)
    interest_waived = Column(Boolean, nullable=False, default=False)
    waived_interest = Column(Float, nullable=False, default=0.0)
    # Late-fee terms are snapshotted from the matching policy at open time
    # (flat amount and/or percentage of principal), like the interest terms.
    # `late_fee_charged` marks a settlement that accrued the fee; `fees_waived`
    # / `waived_fees` record a policy-score-governed admin fee waiver.
    late_fee_amount = Column(Float, nullable=False, default=0.0)
    late_fee_pct = Column(Float, nullable=False, default=0.0)
    late_fee_charged = Column(Boolean, nullable=False, default=False)
    fees_waived = Column(Boolean, nullable=False, default=False)
    waived_fees = Column(Float, nullable=False, default=0.0)
    note = Column(Text, nullable=False, default="")


class PointsWallet(Base, TimestampMixin):
    """Per-user balance for a single point type (one row per user/type)."""
    __tablename__ = "points_wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_points_wallets_balance_non_negative"),
        UniqueConstraint("user_id", "point_type", name="uq_points_wallets_user_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    point_type = Column(String(50), nullable=False, index=True)
    balance = Column(Float, nullable=False, default=0.0)


class PointsTransaction(Base, TimestampMixin):
    """Ledger row for every points movement (earn, redeem, purchase, adjust)."""
    __tablename__ = "points_transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    point_type = Column(String(50), nullable=False, index=True)
    kind = Column(String(30), nullable=False, index=True)
    points_delta = Column(Float, nullable=False, default=0.0)
    currency = Column(String(8), nullable=False, default="USD")
    currency_amount = Column(Float, nullable=False, default=0.0)
    rate = Column(Float, nullable=False, default=0.0)
    fee = Column(Float, nullable=False, default=0.0)
    reference = Column(String(120), nullable=False, default="")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    full_name = Column(String(100))
    hashed_password = Column(String(255), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    bookings = relationship("Booking", back_populates="user", cascade="all, delete-orphan")
    interaction_signals = relationship(
        "InteractionSignal",
        backref="user",
        cascade="all, delete-orphan",
    )
    chat_history = relationship(
        "ChatHistory",
        passive_deletes=True,
        backref="user",
    )
    retention_snapshots = relationship(
        "RetentionSnapshot",
        backref="user",
        cascade="all, delete-orphan",
    )
    recovery_outcomes = relationship(
        "RecoveryOutcome",
        backref="user",
        cascade="all, delete-orphan",
    )
    recovery_actions = relationship(
        "RecoveryAction",
        backref="user",
        cascade="all, delete-orphan",
    )
    booking_events = relationship(
        "BookingEvent",
        backref="user",
        cascade="all, delete-orphan",
    )
    policy_score = relationship(
        "CustomerPolicyScore",
        uselist=False,
        backref="user",
        cascade="all, delete-orphan",
    )

class Booking(Base, TimestampMixin):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("scheduled_date IS NOT NULL", name="ck_bookings_scheduled_date_required"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    service_type = Column(SAEnum(ServiceType, name="servicetype", native_enum=True), nullable=False)
    title = Column(String(255), nullable=True)
    details = Column(Text, nullable=False, default="")
    scheduled_date = Column(DateTime, nullable=False)

    status = Column(
        SAEnum(BookingStatus, name="bookingstatus", native_enum=True),
        nullable=False,
        default=BookingStatus.pending
    )

    user = relationship("User", back_populates="bookings")
    events = relationship(
        "BookingEvent",
        backref="booking",
        cascade="all, delete-orphan",
    )
# =============================================================================
# Metadata layer: what the schema above means, and what it does not protect
# =============================================================================
#
# Everything from here down is additive. No table, column, constraint or index
# is added or altered, so the emitted DDL is unchanged and no migration is
# needed. The helpers read `Base.metadata` at call time, which is what keeps
# them from drifting away from the schema they describe.
#
# The imports below are scoped to this layer on purpose: the DDL layer above
# keeps exactly the imports it needs to declare the schema, so a reader can
# tell which imports exist for the schema and which for the catalog. Nothing
# here imports `app.services.*` -- the services import the models, so a
# reverse import would be a cycle.

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import RelationshipProperty

# --- Sensitivity vocabulary ---------------------------------------------------
#
# `rank` orders the classes by how much damage leaking one causes. The
# `default_projection` says what happens to the column when no explicit audience
# is named: `include` passes it through, `redact` replaces the value with a
# placeholder, `omit` drops the key entirely. `bulk_export` is the harder rule
# -- whether the column may appear in a whole-table export at all.

SENSITIVITY_CLASSES: dict[str, dict[str, Any]] = {
    "credential": {
        "rank": 100,
        "default_projection": "omit",
        "bulk_export": False,
        "note": "auth material; a value here is a breach, so it is never serialised",
    },
    "content": {
        "rank": 80,
        "default_projection": "redact",
        "bulk_export": False,
        "note": "free text a person wrote or said; it carries whatever they put in it",
    },
    "financial": {
        "rank": 60,
        "default_projection": "redact",
        "bulk_export": False,
        "note": "money amounts and rates; disclosure is a compliance question, not a bug",
    },
    "behavioral": {
        "rank": 40,
        "default_projection": "include",
        "bulk_export": True,
        "note": "scores and signals derived from a person's activity",
    },
    "identifier": {
        "rank": 20,
        "default_projection": "include",
        "bulk_export": True,
        "note": "keys and foreign keys; identifying once paired with any other class",
    },
    "internal": {
        "rank": 10,
        "default_projection": "include",
        "bulk_export": True,
        "note": "operational detail with no personal content",
    },
    "public": {
        "rank": 0,
        "default_projection": "include",
        "bulk_export": True,
        "note": "safe for any audience",
    },
}
SENSITIVITY_RANK: dict[str, int] = {
    name: int(spec["rank"]) for name, spec in SENSITIVITY_CLASSES.items()
}
SENSITIVITY_PLACEHOLDER = "***redacted***"

#: Column-name -> class. Global by column name, because the same column name
#: means the same thing everywhere in this schema (`hashed_password` is a
#: credential in any table that has one). Overrides live in the table below.
FIELD_SENSITIVITY: dict[str, str] = {
    # credentials
    "hashed_password": "credential",
    # free text a person wrote or a model produced for them
    "message": "content",
    "response": "content",
    "full_name": "content",
    "rationale": "content",
    "evidence": "content",
    "recommendation": "content",
    "action_plan": "content",
    "match_reason": "content",
    "explanation": "content",
    "failure_reason": "content",
    "note": "content",
    "notes": "content",
    "details": "content",
    "title": "content",
    "summary": "content",
    "detail_json": "content",
    "payload_json": "content",
    "result_json": "content",
    "summary_json": "content",
    "primary_risks_json": "content",
    "recovery_signals_json": "content",
    "follow_up_json": "content",
    "escalation_path_json": "content",
    "handoff_outcome_json": "content",
    # money
    "principal": "financial",
    "annual_rate": "financial",
    "interest_accrued": "financial",
    "interest_cap_pct": "financial",
    "settled_interest": "financial",
    "total_settled": "financial",
    "waived_interest": "financial",
    "late_fee_amount": "financial",
    "late_fee_pct": "financial",
    "waived_fees": "financial",
    "balance": "financial",
    "points_delta": "financial",
    "currency_amount": "financial",
    "rate": "financial",
    "fee": "financial",
    # derived from activity
    "system_score": "behavioral",
    "customer_score": "behavioral",
    "access_score": "behavioral",
    "interest_score": "behavioral",
    "closeness_score": "behavioral",
    "community_closeness_score": "behavioral",
    "loyalty_score": "behavioral",
    "dissatisfaction_score": "behavioral",
    "score": "behavioral",
    "risk_score": "behavioral",
    "confidence": "behavioral",
    "is_admin": "internal",
}

#: Per-table overrides, keyed ``"table.column"``. Used where a shared column
#: name means different things in different tables.
TABLE_FIELD_SENSITIVITY: dict[str, str] = {
    # an operator-written reason, not user content -- but it still names people
    "recovery_actions.failure_reason": "content",
    # a policy score summary is machine-generated narrative about one person
    "customer_policy_scores.summary": "behavioral",
    # the audit trail's own summary is written about an entity, not by a user
    "audit_log_entries.summary": "content",
    # a security event summary is machine-generated from a signal
    "security_events.summary": "behavioral",
    # a username is an identifier, not free text, but it is still a login name
    "users.username": "identifier",
}

# --- JSON payload columns ----------------------------------------------------
#
# A `*_json` column is a serialised structure living in a TEXT column. The
# container it is expected to hold is read off the column default ("{}" means
# object, "[]" means array), so the convention needs no per-column declaration.
JSON_COLUMN_SUFFIX = "_json"
JSON_DECODE_FAILURES: dict[str, str] = {
    "fallback": "the column default",
    "note": "a malformed payload never raises out of a read path; it degrades to the empty container",
}


def _json_default_container(column: Any) -> str:
    """Infer ``object``/``array`` from a JSON column's declared default."""
    default = getattr(column, "default", None)
    raw = getattr(default, "arg", None)
    if isinstance(raw, str) and raw.strip().startswith("["):
        return "array"
    return "object"


# --- Enum vocabulary for columns the schema does not enforce -----------------
#
# A native `SAEnum` is enforced by the database. Several string columns hold the
# same vocabulary with no enforcement at all, which means a typo becomes a row
# that no `filter()` ever finds again. Declaring them here gives them a
# vocabulary, a normaliser, and a report -- without a migration.
#
# `enforced` says whether the *database* enforces it, so the report can separate
# "protected" from "declared". `values` is the vocabulary as the writing code
# actually uses it. `aliases` are accepted spellings that normalise to a value.
# `vocabulary` names a shared alias set in `ENUM_VOCABULARY_ALIASES`, so a column
# that mirrors another column's vocabulary (booking_events.to_status mirrors
# bookings.status) accepts the same alternate spellings.

#: Alternate spellings keyed by vocabulary, applied to every column that names
#: that vocabulary. Sharing them is the point: a reader who learns that
#: "cancelled_by_user" is accepted for a booking status should not have to
#: rediscover it for the event row that records the same transition.
ENUM_VOCABULARY_ALIASES: dict[str, dict[str, str]] = {
    "booking_status": {
        "cancelled_by_user": "cancelled",
        "canceled": "cancelled",
        "complete": "completed",
        "confirm": "confirmed",
    },
    "service_type": {
        "consult": "consultation",
        "consulting": "consultation",
        "delivery_": "delivery",
    },
}

ENUM_FIELD_SPECS: dict[str, dict[str, Any]] = {
    "bookings.status": {
        "enum": BookingStatus,
        "values": tuple(status.value for status in BookingStatus),
        "enforced": True,
        "enforced_by": "native enum type 'bookingstatus'",
        "vocabulary": "booking_status",
        "aliases": {},
        "note": "the reference implementation of a properly enforced column",
    },
    "bookings.service_type": {
        "enum": ServiceType,
        "values": tuple(service.value for service in ServiceType),
        "enforced": True,
        "enforced_by": "native enum type 'servicetype'",
        "vocabulary": "service_type",
        "aliases": {},
        "note": "same vocabulary as arrears_entries.service_type, which is not enforced",
    },
    "booking_events.to_status": {
        "enum": BookingStatus,
        "values": tuple(status.value for status in BookingStatus),
        "enforced": False,
        "enforced_by": None,
        "vocabulary": "booking_status",
        "aliases": {},
        "note": (
            "mirrors bookings.status with no constraint; a bad value here makes a "
            "booking look like it never left 'pending'"
        ),
    },
    "booking_events.from_status": {
        "enum": BookingStatus,
        "values": tuple(status.value for status in BookingStatus),
        "enforced": False,
        "enforced_by": None,
        "vocabulary": "booking_status",
        "aliases": {},
        "note": "nullable by design: the first transition has no prior status",
    },
    "arrears_entries.service_type": {
        "enum": ServiceType,
        "values": tuple(service.value for service in ServiceType),
        "enforced": False,
        "enforced_by": None,
        "vocabulary": "service_type",
        "aliases": {},
        "note": (
            "VARCHAR(50) where bookings uses a native enum, so the two tables can "
            "disagree about what a 'service type' is"
        ),
    },
    "booking_assignments.state": {
        "enum": None,
        "values": ("suggested", "offered", "accepted", "declined", "expired", "withdrawn"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"proposed": "suggested", "accepted_by_user": "accepted"},
        "note": (
            "only 'suggested' is ever written today; the rest are the states "
            "normalize_assignment_state() has to tolerate"
        ),
    },
    "recovery_actions.status": {
        "enum": None,
        "values": ("executed", "would_execute", "failed", "skipped"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"dry_run": "would_execute", "error": "failed"},
        "note": "'would_execute' is the dry-run marker; conflating it with 'executed' overstates automation",
    },
    "points_transactions.kind": {
        "enum": None,
        "values": ("purchase_points", "redeem_points", "recovery_credit", "earn"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"purchase": "purchase_points", "redeem": "redeem_points"},
        "note": (
            "'earn' is documented on the model but no code path writes it, so a "
            "statement that only ever shows three kinds is not a bug"
        ),
    },
    "interaction_signals.priority": {
        "enum": None,
        "values": ("low", "medium", "high"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"med": "medium", "normal": "medium"},
        "note": "only 'medium' and 'high' are produced by the current rules",
    },
    "customer_policy_scores.policy_tier": {
        "enum": None,
        "values": ("system-premium", "customer-premium", "standard"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"premium": "system-premium", "default": "standard"},
        "note": "the vocabulary is POLICY_TIER_RULES in services/policy_scoring.py",
    },
    "customer_policy_scores.control_posture": {
        "enum": None,
        "values": ("high_trust", "customer_trusted", "observed", "constrained"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"trusted": "high_trust", "monitored": "observed"},
        "note": (
            "resolve_control_posture() is total over this set, so a value outside "
            "it means the resolver was bypassed, not that it is a new posture"
        ),
    },
    "retention_snapshots.churn_risk": {
        "enum": None,
        "values": ("low", "medium", "high", "critical"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"med": "medium", "severe": "critical"},
        "note": "services/retention.py silently scores anything unknown as 0.0 (lowest)",
    },
    "retention_snapshots.lifecycle_stage": {
        "enum": None,
        "values": ("new", "engaged", "loyal", "recovering", "at_risk"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"atrisk": "at_risk", "at-risk": "at_risk", "risk": "at_risk"},
        "note": "produced by classify_lifecycle_stage() in services/chat_analytics.py",
    },
    "recovery_outcomes.recovery_readiness": {
        "enum": None,
        "values": ("ready", "partial", "blocked", "exhausted"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"unready": "blocked", "maxed": "exhausted"},
        "note": "no default and no constraint, so a blank value is representable",
    },
}
# `interaction_signals.source` is deliberately *absent* from the table above even
# though it is a non-nullable String like every entry in it. All seven `source`
# columns in this schema are open channel labels ('api', 'booking-service',
# 'zero_trust', 'system_and_customer_metrics', ...), so declaring one of them as
# a closed vocabulary makes it the odd one out and would have `validate_enum_field`
# reject values the other six accept. It is covered by *name* in
# OPEN_TEXT_COLUMNS instead, which is the honest description.

# --- Table lifecycle ----------------------------------------------------------
#
# What kind of table this is, which decides how it may be written. `write_mode`
# is `append_only` (rows are added, never updated or deleted), `snapshot` (the
# current row is replaced wholesale and history is kept by writing a new one),
# `ledger` (append-only *and* the balance is derived by summing rows), or
# `mutable` (ordinary CRUD). `retention_days` is advisory: nothing here prunes
# anything, it is the number an operator needs to write a retention policy.

APPEND_ONLY: str = "append_only"
SNAPSHOT: str = "snapshot"
LEDGER: str = "ledger"
MUTABLE: str = "mutable"
WRITE_MODES: tuple[str, ...] = (APPEND_ONLY, SNAPSHOT, LEDGER, MUTABLE)

TABLE_LIFECYCLE: dict[str, dict[str, Any]] = {
    "audit_log_entries": {
        "write_mode": APPEND_ONLY,
        "retention_days": 365,
        "owner": "services/audit_log.py",
        "note": (
            "the seal chain assumes append-only; an update invalidates every seal "
            "after it. 365 days is the info window -- the real one is severity-"
            "dependent (730 warning, 2555 critical)"
        ),
    },
    "security_events": {
        "write_mode": APPEND_ONLY,
        "retention_days": 90,
        "owner": "app/cell_matrix.py",
        "note": "single-table-inheritance; event_kind discriminates the subclass",
    },
    "recovery_actions": {
        "write_mode": APPEND_ONLY,
        "retention_days": 1095,
        "owner": "services/recovery_playbooks.py",
        "note": "one row per automated action; a correction is a new row, not an edit",
    },
    "booking_events": {
        "write_mode": APPEND_ONLY,
        "retention_days": 1095,
        "owner": "services/bookings.py",
        "note": "the booking state machine's own history",
    },
    "points_transactions": {
        "write_mode": LEDGER,
        "retention_days": 2555,
        "owner": "services/points_exchange.py",
        "note": (
            "points_delta is deliberately signed -- a ledger cannot express a "
            "spend without a negative -- so it has no non-negative check and must not gain one"
        ),
    },
    "retention_snapshots": {
        "write_mode": SNAPSHOT,
        "retention_days": 730,
        "owner": "services/retention.py",
        "note": "window_days is part of the key in practice; pruning is by window, not by row",
    },
    "recovery_outcomes": {
        "write_mode": SNAPSHOT,
        "retention_days": 1095,
        "owner": "services/recovery_playbooks.py",
        "note": "one live outcome per user; the JSON columns carry the history within it",
    },
    "topic_selections": {
        "write_mode": SNAPSHOT,
        "retention_days": 730,
        "owner": "routers/topics.py",
        "note": (
            "is_current marks the live row, but nothing in the database enforces "
            "at most one current row per user; the router filters on it"
        ),
    },
    "booking_assignments": {
        "write_mode": SNAPSHOT,
        "retention_days": 1095,
        "owner": "services/bookings.py",
        "note": "is_current marks the live row; same unenforced-at-most-one as above",
    },
    "chat_history": {
        "write_mode": APPEND_ONLY,
        "retention_days": 365,
        "owner": "services/chat_analytics.py",
        "note": (
            "user_id is ON DELETE SET NULL, so deleting a user leaves the transcript "
            "behind rather than destroying it"
        ),
    },
    "users": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "routers/users.py",
        "note": "the cascade root: most child rows go with the user",
    },
    "bookings": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "routers/bookings.py",
        "note": "scheduled_date is additionally constrained NOT NULL by a named check",
    },
    "customer_policy_scores": {
        "write_mode": SNAPSHOT,
        "retention_days": 1095,
        "owner": "services/policy_scoring.py",
        "note": "unique on user_id, so the database does enforce at most one row",
    },
    "communication_overrides": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "services/communication_strategy.py",
        "note": "unique on user_id; set_by_admin_id is not a foreign key (see referential gaps)",
    },
    "points_wallets": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "services/points_exchange.py",
        "note": "unique on (user_id, point_type); balance is the ledger's running total",
    },
    "interaction_signals": {
        "write_mode": APPEND_ONLY,
        "retention_days": 730,
        "owner": "services/chat_analytics.py",
        "note": "one row per derived signal; score is non-negative by check constraint",
    },
    "arrears_entries": {
        "write_mode": MUTABLE,
        "retention_days": 2555,
        "owner": "services/arrears_payments.py",
        "note": (
            "interest terms are snapshotted at open time, so a later catalog change "
            "never rewrites an open agreement"
        ),
    },
}

#: Float columns with no non-negative check *on purpose*. Listed so that
#: `check_constraint_coverage()` can tell a documented exception from an
#: oversight instead of reporting both.
SIGNED_QUANTITY_COLUMNS: dict[str, str] = {
    "points_transactions.points_delta": "a ledger needs negative rows for spends",
    "topic_selections.confidence": (
        "a confidence in [0,1] would be nicer, but adding a CHECK would change the "
        "emitted DDL; the writer clamps instead, so a check here would only "
        "disagree with the code that produced the value"
    ),
    "security_events.risk_score": (
        "a risk score is not a magnitude -- 0.0 means 'no risk' and is the most "
        "harmless value, so a non-negative check would be the wrong guard"
    ),
}

#: Vocabulary-bearing string columns that are deliberately *not* in
#: `ENUM_FIELD_SPECS`, so the two tables do not overlap. `values` is the set the
#: writing code actually produces. `enforced` records whether a database
#: constraint protects the column -- the `audit_log_entries.severity` /
#: `security_events.severity` pair is listed together precisely because one is
#: protected and the other is not.
OPEN_VOCABULARY_COLUMNS: dict[str, dict[str, Any]] = {
    "audit_log_entries.severity": {
        "values": ("info", "warning", "critical"),
        "enforced": True,
        "enforced_by": "check constraint ck_audit_log_entries_severity_valid",
        "note": "the only severity column the database protects",
    },
    "security_events.severity": {
        "values": ("info", "warning", "critical"),
        "enforced": False,
        "enforced_by": None,
        "note": (
            "identical vocabulary to audit_log_entries.severity with no constraint, "
            "so the same query filter works on one table and silently misses on the other"
        ),
    },
    "security_events.event_kind": {
        "values": ("security_event", "authentication", "risk", "access"),
        "enforced": False,
        "enforced_by": None,
        "note": (
            "single-table-inheritance discriminator; the value is the mapper "
            "polymorphic_identity, not application data"
        ),
    },
    "arrears_entries.status": {
        "values": ("open", "settled", "waived"),
        "enforced": False,
        "enforced_by": None,
        "note": "the list the arrears catalog reports; a fourth state would not be rejected",
    },
    "arrears_entries.compounding": {
        "values": ("simple", "daily"),
        "enforced": False,
        "enforced_by": None,
        "note": "interest_terms() branches on exactly these two and defaults anything else to simple",
    },
    "audit_log_entries.entity_type": {
        "values": ("system",),
        "enforced": False,
        "enforced_by": None,
        "note": "a free label, but the single default value is worth knowing before a filter assumes more",
    },
}

#: String columns that are *meant* to be open text, listed so that
#: `unbacked_enum_like_columns()` does not report them as a missing vocabulary.
#: A name shared by several tables is the signal that triggered this table, so
#: an entry is an admission: "yes, this repeats, and yes, it is open on purpose".
OPEN_TEXT_COLUMNS: dict[str, str] = {
    "source": "a subsystem or channel label, added to freely (chat, api, admin, import, booking-service, ...)",
    "currency": "an ISO 4217 code; a lookup table, not a fixed vocabulary",
    "event_type": "booking lifecycle events, extended by the state machine",
    "action": "recovery playbook actions come from PLAYBOOKS, which is config-driven",
    "area": "policy areas come from the area catalog, which is config-driven",
    "snapshot_type": "retention snapshot kinds, extended by services/retention.py",
    "point_type": "derived from the exchange-rule config table, so it is open by construction",
    "profile_id": "communication profiles are config-driven",
    "playbook_id": "recovery playbooks are config-driven",
    "reference": "an external identifier, arbitrary text by nature",
    "policy_id": "arrears interest policies are config-driven",
    "note": "unbounded TEXT prose written by a human or an operator; no set of values is conceivable",
    "summary": "unbounded TEXT prose, same reasoning as `note`",
}

#: Columns that look like a reference (named ``*_id``, integer, indexed) but
#: carry no foreign key. Nothing in the database stops them pointing at a row
#: that does not exist; adding one would be new DDL, so it is reported instead.
#:
#: `severity` is declared per entry rather than derived, because two different
#: situations end up here and only one of them is a missing constraint:
#: `out_of_band` means the column names something that is genuinely not a
#: foreign key in this schema, while `dangling_possible` means the target table
#: exists and the constraint is simply absent.
UNCONSTRAINED_REFERENCE_COLUMNS: dict[str, dict[str, str]] = {
    "booking_assignments.room_id": {
        "severity": "out_of_band",
        "reason": (
            "an assignment can name a room that was never allocated; there is no "
            "rooms table in this schema to point at, so a foreign key is not the "
            "missing piece"
        ),
    },
    "communication_overrides.set_by_admin_id": {
        "severity": "out_of_band",
        "reason": (
            "an override can credit an admin id that is not a user, and nothing "
            "distinguishes it from one that was deleted -- pointing this at "
            "users.id would reject a legitimate external admin"
        ),
    },
    "arrears_entries.booking_id": {
        "severity": "dangling_possible",
        "reason": (
            "an arrears row can reference a booking that no longer exists, and the "
            "column is nullable so 'no booking' and 'dangling booking' are "
            "indistinguishable without a join; bookings is a real table here, so "
            "this is a missing constraint rather than a non-reference"
        ),
    },
    "arrears_entries.policy_id": {
        "severity": "out_of_band",
        "reason": (
            "a reference into the ARREARS_INTEREST_POLICIES config table, not a "
            "row; a policy removed from the config leaves a dangling id that is "
            "still meaningful as a record of what applied at open time"
        ),
    },
    "recovery_actions.playbook_id": {
        "severity": "out_of_band",
        "reason": "a reference into the recovery-playbook config table, not a row",
    },
    "communication_overrides.profile_id": {
        "severity": "out_of_band",
        "reason": "a reference into the communication-profile config table, not a row",
    },
    "audit_log_entries.entity_id": {
        "severity": "out_of_band",
        "reason": (
            "VARCHAR, not an integer: the audit trail points at whatever the audited "
            "entity is, so the *_id suffix is a naming convention here rather than a "
            "row reference. A filter that assumes an integer entity_id is wrong"
        ),
    },
    "security_events.tenant_id": {
        "severity": "out_of_band",
        "reason": (
            "TenantScopedMixin is applied to a table that predates multi-tenancy, "
            "and no tenants table exists in this schema, so the column is an opaque "
            "scope label rather than a foreign key"
        ),
    },
}


# --- Redaction presets --------------------------------------------------------
#
# A preset answers "who is this payload for". `min_rank` is the gate: a column
# is subject to its class's `default_projection` only when its class ranks at or
# above the gate, so a low gate does not automatically mean more redaction -- it
# only means more columns get their class's opinion applied. `overrides` lets a
# preset disagree with a class, which is the only way to express "an operator
# needs the notes, a public export does not". `bulk_export` applies the class's
# own `bulk_export` flag regardless of the gate: a class that may never leave in
# a whole-table dump is not rescued by an audience that is allowed to see it.


REDACTION_PRESETS: dict[str, dict[str, Any]] = {
    "internal": {
        "min_rank": None,
        "bulk_export": False,
        "overrides": {},
        "note": "nothing is redacted; for server-side use only",
    },
    "operator": {
        "min_rank": 60,
        "bulk_export": False,
        "overrides": {"content": "include"},
        "note": (
            "credential material is dropped, free text is passed through because an "
            "operator has to read the note, money is redacted"
        ),
    },
    "public": {
        "min_rank": 60,
        "bulk_export": False,
        "overrides": {},
        "note": "credential dropped, content and money redacted",
    },
    "export": {
        "min_rank": 0,
        "bulk_export": True,
        "overrides": {},
        "note": (
            "every class the schema marks bulk-unsafe is redacted regardless of the "
            "gate; identifiers and behavioural scores pass through"
        ),
    },
}


def projection_for(
    sensitivity: str,
    *,
    preset: str | None = None,
    min_rank: int | None = None,
    bulk_export: bool = False,
) -> str:
    """What happens to a column of class ``sensitivity``: include/redact/omit.

    Resolution order: the preset's ``overrides`` win, then the gate decides
    whether the class's own ``default_projection`` applies, then
    ``bulk_export`` downgrades anything the schema marks as unsafe to dump. An
    unknown preset is reported as ``internal`` rather than raising, because a
    redaction helper that throws is a redaction helper that gets bypassed.
    """
    spec = REDACTION_PRESETS.get(str(preset)) if preset else None
    if spec is None:
        spec = REDACTION_PRESETS["internal"]
    rank = SENSITIVITY_RANK.get(sensitivity, 0)
    gate = spec.get("min_rank") if min_rank is None else min_rank
    if gate is None:
        projection = "include"
    elif rank >= int(gate):
        projection = str(SENSITIVITY_CLASSES.get(sensitivity, {}).get("default_projection", "include"))
    else:
        projection = "include"
    if spec.get("bulk_export") or bulk_export:
        if not SENSITIVITY_CLASSES.get(sensitivity, {}).get("bulk_export", True):
            projection = str(SENSITIVITY_CLASSES[sensitivity]["default_projection"])
    return str(spec.get("overrides", {}).get(sensitivity, projection))


# --- Schema introspection -----------------------------------------------------


def all_table_names() -> list[str]:
    """Every table name in the metadata, sorted."""
    return sorted(Base.metadata.tables)


def get_table(name: str) -> Any:
    """The ``Table`` for ``name``, or ``None`` if there is no such table."""
    return Base.metadata.tables.get(str(name))


def table_columns(name: str) -> list[str]:
    """Column names for one table, in declaration order."""
    table = get_table(name)
    if table is None:
        return []
    return [column.name for column in table.columns]


def _python_default(column: Any) -> Any:
    """A column's default as a plain value, or ``None`` if it has no literal one."""
    default = getattr(column, "default", None)
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    # A callable default (e.g. uuid4) is not a value; report it as its name.
    if callable(arg):
        return f"<callable {getattr(arg, '__name__', 'anonymous')}>"
    # An enum member is a value, but not one JSON can carry; the catalog payload
    # is served as JSON, so report the stored form.
    if isinstance(arg, enum.Enum):
        return arg.value
    return arg


def _enum_values(column: Any) -> list[str] | None:
    """The value list of a native ``SAEnum`` column, or ``None``."""
    column_type = column.type
    if not isinstance(column_type, SAEnum):
        return None
    enums = getattr(column_type, "enums", None)
    return [str(value) for value in enums] if enums else None


def _foreign_keys(column: Any) -> list[str]:
    return [
        f"{key.target_fullname} (ondelete={key.ondelete or 'NO ACTION'})"
        for key in sorted(column.foreign_keys, key=lambda k: k.target_fullname)
    ]


def column_spec(table_name: str, column_name: str) -> dict[str, Any]:
    """Describe one column, merging the DDL facts with the configured judgment.

    The ``ddl`` sub-dict is read straight off SQLAlchemy and can never be
    wrong about the schema. Everything beside it is judgment: the sensitivity
    class, whether the column is treated as a JSON payload, and whether it is
    meant to hold an enum value.
    """
    table = get_table(table_name)
    column = None if table is None else table.columns.get(column_name)
    if column is None:
        raise KeyError(f"{table_name}.{column_name} is not a column in this schema")
    sensitivity = sensitivity_of(table_name, column_name)
    return {
        "table": table_name,
        "column": column_name,
        "ddl": {
            "type": str(column.type),
            "python_type": type(column.type.python_type).__name__
            if hasattr(column.type, "python_type")
            else "unknown",
            "nullable": bool(column.nullable),
            "primary_key": bool(column.primary_key),
            "indexed": bool(column.index),
            "unique": bool(column.unique),
            "default": _python_default(column),
            "foreign_keys": _foreign_keys(column),
            "enum_values": _enum_values(column),
            "server_default": str(column.server_default.arg)
            if column.server_default is not None and column.server_default.arg is not None
            else None,
        },
        "sensitivity": sensitivity,
        "sensitivity_rank": SENSITIVITY_RANK.get(sensitivity, 0),
        "is_json": is_json_column(column_name),
        "json_container": _json_default_container(column) if is_json_column(column_name) else None,
        "enum_field": enum_field_id(table_name, column_name),
        "referential": bool(column.foreign_keys) or column_name.endswith("_id"),
    }


def build_column_catalog() -> dict[str, Any]:
    """Every column of every table, described by :func:`column_spec`."""
    catalog: dict[str, Any] = {}
    for table_name in all_table_names():
        catalog[table_name] = {
            column_name: column_spec(table_name, column_name)
            for column_name in table_columns(table_name)
        }
    return catalog


def sensitivity_of(table_name: str, column_name: str) -> str:
    """The sensitivity class for one column.

    A per-table override wins over the global column-name map, and an unknown
    column falls back to ``internal`` -- never to ``public``, so a column added
    later without a classification is included rather than leaked, but a caller
    that wants a guarantee can ask for ``public`` explicitly.
    """
    key = f"{table_name}.{column_name}"
    if key in TABLE_FIELD_SENSITIVITY:
        return TABLE_FIELD_SENSITIVITY[key]
    if column_name in FIELD_SENSITIVITY:
        return FIELD_SENSITIVITY[column_name]
    if column_name in {"id", "created_at", "updated_at"}:
        return "identifier" if column_name == "id" else "internal"
    if column_name.endswith("_id"):
        return "identifier"
    if column_name.endswith("_at") or column_name in {"timestamp"}:
        return "internal"
    if column_name in {"is_admin", "is_current"}:
        return "internal"
    return "internal"


def sensitive_columns(
    table_name: str | None = None,
    *,
    min_rank: int = 0,
) -> list[dict[str, str]]:
    """Columns at or above ``min_rank``, optionally limited to one table."""
    tables = [table_name] if table_name else all_table_names()
    found: list[dict[str, str]] = []
    for name in tables:
        if get_table(name) is None:
            continue
        for column_name in table_columns(name):
            sensitivity = sensitivity_of(name, column_name)
            if SENSITIVITY_RANK.get(sensitivity, 0) >= int(min_rank):
                found.append(
                    {"table": name, "column": column_name, "sensitivity": sensitivity}
                )
    return sorted(found, key=lambda row: (-SENSITIVITY_RANK.get(row["sensitivity"], 0), row["table"], row["column"]))


def is_json_column(column_name: str) -> bool:
    """True when a column is a serialised structure in a TEXT column.

    By convention only: the name ends in ``_json``. ``security_events.detail_json``
    and ``audit_log_entries.detail_json`` both qualify, and nothing else does by
    accident.
    """
    return str(column_name).endswith(JSON_COLUMN_SUFFIX)


def json_container_for(column_name: str) -> str:
    """``"array"`` or ``"object"``: what a ``*_json`` column is expected to hold."""
    table = get_table_by_column(column_name)
    if table is not None:
        return _json_default_container(table.columns[column_name])
    return "object"


def get_table_by_column(column_name: str) -> Any | None:
    """The first table declaring ``column_name``, or ``None``."""
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is not None and column_name in table.columns:
            return table
    return None


def loads_json(raw: Any, container: str = "object") -> Any:
    """Parse a ``*_json`` column, degrading to the empty container on bad input.

    A malformed payload in a read path should not become a 500 for a dashboard;
    it should look like "there was nothing there".
    """
    fallback: Any = [] if container == "array" else {}
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    if container == "array" and not isinstance(parsed, list):
        return fallback
    if container == "object" and not isinstance(parsed, dict):
        return fallback
    return parsed


def dumps_json(value: Any) -> str:
    """Serialise a value for a ``*_json`` column, with a stable key order."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        # Already-serialised input passes through rather than being double-wrapped.
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


# --- Enum field access --------------------------------------------------------
#
# The two native enum columns are already protected; these helpers give the
# unenforced ones the same vocabulary, so a writer can normalise before the
# database refuses nothing and a reader can tell a typo from a real value.


def enum_field_id(table_name: str, column_name: str) -> str | None:
    """The ``ENUM_FIELD_SPECS`` key for a column, or ``None`` if it has no vocabulary."""
    key = f"{table_name}.{column_name}"
    if key in ENUM_FIELD_SPECS:
        return key
    if key in OPEN_VOCABULARY_COLUMNS:
        return key
    return None


def enum_field_spec(table_name: str, column_name: str) -> dict[str, Any] | None:
    """The merged vocabulary spec for a column, or ``None``.

    ``ENUM_FIELD_SPECS`` wins over ``OPEN_VOCABULARY_COLUMNS`` on the (impossible
    today) collision, so a column can be moved between the two tables without
    changing behaviour at the call sites. The returned ``aliases`` are the
    column's own aliases *plus* the shared set its ``vocabulary`` names, with
    the column's own winning a collision -- so a column can accept a spelling the
    rest of its vocabulary rejects.
    """
    key = f"{table_name}.{column_name}"
    spec = ENUM_FIELD_SPECS.get(key)
    if spec is None:
        spec = OPEN_VOCABULARY_COLUMNS.get(key)
    if spec is None:
        return None
    merged = dict(spec)
    shared = ENUM_VOCABULARY_ALIASES.get(str(spec.get("vocabulary") or ""), {})
    merged["aliases"] = {**shared, **(spec.get("aliases", {}) or {})}
    return merged


def enum_values_for(table_name: str, column_name: str) -> tuple[str, ...]:
    """The vocabulary for a column, falling back to the DDL for a native enum.

    A column with no declared vocabulary and no native enum returns an empty
    tuple -- which is what makes ``"x" in enum_values_for(...)`` a safe question
    rather than a crash.
    """
    spec = enum_field_spec(table_name, column_name)
    if spec is not None:
        return tuple(str(value) for value in spec.get("values", ()))
    table = get_table(table_name)
    if table is not None and column_name in table.columns:
        declared = _enum_values(table.columns[column_name])
        if declared:
            return tuple(declared)
    return ()


def normalize_enum_value(table_name: str, column_name: str, value: Any) -> Any:
    """Coerce a value into the column's vocabulary, passing unknown values through.

    Total on purpose: a caller that does not want an exception gets the input
    back unchanged, so normalisation never becomes an implicit filter that
    silently drops rows. A ``None`` stays ``None`` because most of these columns
    are nullable (``booking_events.from_status``) and a blank means "not
    applicable", not "invalid".
    """
    if value is None:
        return None
    spec = enum_field_spec(table_name, column_name)
    if spec is None:
        return value
    text = value.value if isinstance(value, enum.Enum) else value
    if not isinstance(text, str):
        return value
    values = [str(item) for item in spec.get("values", ())]
    if text in values:
        return text
    aliases = spec.get("aliases", {}) or {}
    aliased = aliases.get(text)
    if aliased is not None:
        return aliased
    lowered = text.strip().lower().replace(" ", "_").replace("-", "_")
    if lowered in values:
        return lowered
    for alias, target in aliases.items():
        if str(alias).strip().lower().replace(" ", "_").replace("-", "_") == lowered:
            return target
    return value


def validate_enum_field(table_name: str, column_name: str, value: Any) -> dict[str, Any]:
    """Check one value against a column's vocabulary and explain the verdict.

    The report separates two failures that look identical to a caller: a value
    that is *not in the vocabulary* (an error) and a value that is only
    *reached through an alias* (accepted, but recorded so the aliasing is
    visible). An undeclared column is not an error either -- it returns
    ``declared=False`` and ``ok=True``, because most columns in this schema
    genuinely are free text.
    """
    key = f"{table_name}.{column_name}"
    spec = enum_field_spec(table_name, column_name)
    report: dict[str, Any] = {
        "field": key,
        "declared": spec is not None,
        "enforced": bool(spec.get("enforced")) if spec else False,
        "enforced_by": spec.get("enforced_by") if spec else None,
        "value": value.value if isinstance(value, enum.Enum) else value,
        "ok": True,
        "normalized": value.value if isinstance(value, enum.Enum) else value,
        "via_alias": None,
        "reason": None,
    }
    if spec is None:
        report["reason"] = "no vocabulary declared for this column"
        return report
    values = [str(item) for item in spec.get("values", ())]
    aliases = spec.get("aliases", {}) or {}
    raw = report["value"]
    if raw is None:
        report["ok"] = bool(report["enforced"]) is False and _column_nullable(table_name, column_name)
        report["reason"] = "null value"
        if not report["ok"]:
            report["reason"] = "null value in a non-nullable column"
        return report
    if not isinstance(raw, str):
        report["ok"] = False
        report["reason"] = f"expected a string, got {type(raw).__name__}"
        return report
    if raw in values:
        return report
    if raw in aliases:
        report["normalized"] = aliases[raw]
        report["via_alias"] = raw
        return report
    normalized = normalize_enum_value(table_name, column_name, raw)
    if normalized in values:
        report["normalized"] = normalized
        report["via_alias"] = raw
        return report
    report["ok"] = False
    report["reason"] = (
        f"{raw!r} is not one of {', '.join(values)}"
        if values
        else f"{raw!r} is not in the declared vocabulary"
    )
    if not report["enforced"]:
        report["reason"] += " -- and no constraint will reject it"
    return report


def coerce_booking_status(value: Any) -> str | None:
    """A booking status value, or ``None`` when it is not one.

    Separate from :func:`normalize_enum_value` because this one is used as a
    guard: a caller that *asks whether* a value is valid needs a falsy answer,
    not the input passed through. It is the check behind the
    ``booking_events.to_status`` guard, where an unknown value would otherwise
    make a booking look like it never left ``pending``.
    """
    normalized = normalize_enum_value("bookings", "status", value)
    return normalized if normalized in booking_status_values() else None


def coerce_service_type(value: Any) -> str | None:
    """A service type value, or ``None`` when it is not one."""
    normalized = normalize_enum_value("bookings", "service_type", value)
    return normalized if normalized in service_type_values() else None


def booking_status_values() -> tuple[str, ...]:
    """The booking status vocabulary, read from the native enum at call time."""
    return tuple(status.value for status in BookingStatus)


def service_type_values() -> tuple[str, ...]:
    """The service type vocabulary, read from the native enum at call time."""
    return tuple(service.value for service in ServiceType)


def _column_nullable(table_name: str, column_name: str) -> bool:
    table = get_table(table_name)
    if table is None or column_name not in table.columns:
        return True
    return bool(table.columns[column_name].nullable)


def enum_field_report() -> dict[str, Any]:
    """Every declared vocabulary, split by whether the database enforces it.

    The split is the useful part: ``enforced`` fields can be trusted to reject a
    bad value, ``declared`` fields are a promise the application keeps, and
    ``declared`` is not a substitute for a constraint.
    """
    enforced: list[dict[str, Any]] = []
    declared: list[dict[str, Any]] = []
    keys = sorted(set(ENUM_FIELD_SPECS) | set(OPEN_VOCABULARY_COLUMNS))
    for key in keys:
        # Read through enum_field_spec so the reported aliases are the merged
        # set a caller would actually get from normalize_enum_value().
        spec = enum_field_spec(*key.split(".", 1)) or {}
        row = {
            "field": key,
            "values": [str(value) for value in spec.get("values", ())],
            "aliases": dict(spec.get("aliases", {}) or {}),
            "vocabulary": spec.get("vocabulary"),
            "enforced_by": spec.get("enforced_by"),
            "native_enum": spec.get("enum") is not None,
            "note": spec.get("note"),
        }
        (enforced if spec.get("enforced") else declared).append(row)
    return {
        "total": len(enforced) + len(declared),
        "enforced_count": len(enforced),
        "declared_count": len(declared),
        "enforced": enforced,
        "declared": declared,
        "vocabularies": {
            name: dict(aliases) for name, aliases in sorted(ENUM_VOCABULARY_ALIASES.items())
        },
        "alias_count": sum(
            len((enum_field_spec(*key.split(".", 1)) or {}).get("aliases", {}) or {})
            for key in keys
        ),
        "guard_helpers": [
            "normalize_enum_value(table, column, value)",
            "validate_enum_field(table, column, value)",
            "coerce_booking_status(value)",
            "coerce_service_type(value)",
        ],
        "note": (
            "a declared field is only as good as its writer: ENUM_FIELD_SPECS "
            "documents the vocabulary, it does not add a CHECK constraint"
        ),
    }


# --- Serialisation ------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Coerce a column value into something JSON can carry, losslessly enough."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        import base64

        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def model_to_dict(
    instance: Any,
    *,
    preset: str | None = None,
    parse_json: bool = True,
    include_relationships: bool = False,
    relationship_depth: int = 0,
    max_depth: int = 2,
    include: Any = None,
    omit: Any = None,
    skip_unloaded: bool = True,
    _seen: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """Serialise one ORM instance, applying a redaction preset to every column.

    Driven entirely by :func:`projection_for`, so adding a column cannot
    silently start leaking it: an unclassified column lands in ``internal``,
    which the ``public`` preset redacts like anything else, and a new class
    added to ``SENSITIVITY_CLASSES`` immediately has a defined behaviour in all
    four presets.

    Relationships are opt-in and depth-limited, because the default for a
    *redacting* serialiser has to be "nothing extra". A cycle guard means a
    bidirectional relationship produces one level of nesting and then a stop,
    not a recursion error.

    ``skip_unloaded`` only applies to persistent instances, where reading an
    unloaded attribute would emit a query. On a transient instance nothing has
    been written yet, so an "unset" column reads as ``None`` and *is* part of the
    payload -- dropping it would make a freshly built object look like a partial
    one.
    """
    if instance is None:
        return {}
    try:
        mapper = sa_inspect(type(instance))
    except Exception:
        # Not an ORM class at all (an int, a dict, a dataclass from another
        # layer). Report the value rather than raising: a serialiser that
        # throws on an unexpected row shape gets wrapped in a try/except by its
        # caller, and the row silently disappears instead.
        if isinstance(instance, dict):
            return {str(key): _json_safe(value) for key, value in instance.items()}
        return {"value": _json_safe(instance)}
    if not hasattr(mapper, "column_attrs"):
        return {"value": _json_safe(instance)}
    state = sa_inspect(instance)
    unloaded = getattr(state, "unloaded", frozenset()) if getattr(state, "persistent", False) else frozenset()
    included = {str(name) for name in include} if include else None
    omitted = {str(name) for name in omit} if omit else set()
    payload: dict[str, Any] = {}
    for attr in mapper.column_attrs:
        key = attr.key
        if included is not None and key not in included:
            continue
        if key in omitted:
            continue
        if skip_unloaded and key in unloaded:
            continue
        sensitivity = sensitivity_of(mapper.local_table.name, key)
        projection = projection_for(sensitivity, preset=preset)
        if projection == "omit":
            continue
        value = getattr(instance, key, None)
        if is_json_column(key) and parse_json:
            value = loads_json(value, _json_default_container(attr.columns[0]))
        if projection == "redact":
            payload[key] = SENSITIVITY_PLACEHOLDER
        else:
            payload[key] = _json_safe(value)
    if include_relationships and relationship_depth < max_depth:
        for name, prop in _relationships_of(mapper):
            if name in omitted or (included is not None and name not in included):
                continue
            if getattr(prop, "uselist", False):
                related = getattr(instance, name, None) or []
                payload[name] = [
                    model_to_dict(
                        item,
                        preset=preset,
                        parse_json=parse_json,
                        include_relationships=True,
                        relationship_depth=relationship_depth + 1,
                        max_depth=max_depth,
                        _seen=_seen | {id(instance)},
                    )
                    for item in related
                    if id(item) not in _seen
                ]
            else:
                related = getattr(instance, name, None)
                if related is not None and id(related) not in _seen:
                    payload[name] = model_to_dict(
                        related,
                        preset=preset,
                        parse_json=parse_json,
                        include_relationships=True,
                        relationship_depth=relationship_depth + 1,
                        max_depth=max_depth,
                        _seen=_seen | {id(instance)},
                    )
    return payload


def model_to_rows(instances: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """``model_to_dict`` over an iterable, skipping ``None`` entries."""
    return [model_to_dict(instance, **kwargs) for instance in instances or [] if instance is not None]


def redact_instance(instance: Any, *, preset: str = "operator", **kwargs: Any) -> dict[str, Any]:
    """:func:`model_to_dict` with a named redaction preset.

    Named separately because the intent ("this goes to a member of the public")
    is a decision, while ``preset=`` on the generic serialiser is a detail. A
    caller reaching for this function has chosen an audience.
    """
    return model_to_dict(instance, preset=preset, **kwargs)


def _relationships_of(mapper: Any) -> list[tuple[str, Any]]:
    """``(name, RelationshipProperty)`` for a mapper, sorted and JSON-safe."""
    found: list[tuple[str, Any]] = []
    for name, prop in sorted(mapper.relationships.items()):
        if isinstance(prop, RelationshipProperty):
            found.append((name, prop))
    return found


def _cascade_modes(prop: Any) -> list[str]:
    """The active ORM cascade modes for a relationship, as sorted names.

    Read off the ``CascadeOptions`` flags rather than the object's ``repr``,
    which differs between SQLAlchemy 1.4 and 2.x and is not a stable contract.
    ``delete-orphan`` is the one the catalog keys off, so it is guaranteed to be
    spelled that way regardless of version.
    """
    cascade = getattr(prop, "cascade", None)
    if cascade is None:
        return []
    if isinstance(cascade, str):
        return sorted(item.strip() for item in cascade.split(",") if item.strip())
    flags = {
        "save-update": "save_update",
        "delete": "delete",
        "merge": "merge",
        "refresh-expire": "refresh",
        "expunge": "expunge",
        "delete-orphan": "delete_orphan",
    }
    return sorted(name for name, flag in flags.items() if getattr(cascade, flag, False))


# --- What the schema does not protect -----------------------------------------


def unbacked_enum_like_columns() -> list[dict[str, Any]]:
    """String columns that look like a vocabulary but have no declaration.

    The signal is a column *name* that repeats across several tables: a name
    shared by three or more non-nullable ``String`` columns is a vocabulary the
    schema is quietly relying on. A name is excluded when it is declared in
    :data:`ENUM_FIELD_SPECS` / :data:`OPEN_VOCABULARY_COLUMNS` (a vocabulary
    exists) or listed in :data:`OPEN_TEXT_COLUMNS` (open on purpose).

    The output is the residual: a name that repeats, is not open by intent, and
    has no declared vocabulary. Each entry names the tables involved, because
    the cost of a missing vocabulary scales with how many tables share it.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is None:
            continue
        for column in table.columns:
            if not isinstance(column.type, String) or column.primary_key:
                continue
            if column.foreign_keys:
                continue
            entry = by_name.setdefault(column.name, {"column": column.name, "columns": [], "nullable": 0, "indexed": 0})
            entry["columns"].append(
                {
                    "table": table_name,
                    "nullable": bool(column.nullable),
                    "indexed": bool(column.index),
                }
            )
            entry["nullable"] += 1 if column.nullable else 0
            entry["indexed"] += 1 if column.index else 0
    findings: list[dict[str, Any]] = []
    for name, entry in sorted(by_name.items()):
        required = [row for row in entry["columns"] if not row["nullable"]]
        if len(required) < 3:
            continue
        # Two distinct admissions of "not a missing vocabulary", checked
        # separately because they answer different questions: a *declared*
        # vocabulary says the column has values we know, an entry in
        # OPEN_TEXT_COLUMNS says it deliberately has no fixed set at all.
        if name in OPEN_TEXT_COLUMNS:
            continue
        declared = [
            f"{row['table']}.{name}"
            for row in entry["columns"]
            if f"{row['table']}.{name}" in ENUM_FIELD_SPECS
            or f"{row['table']}.{name}" in OPEN_VOCABULARY_COLUMNS
        ]
        if declared:
            continue
        findings.append(
            {
                "column": name,
                "occurrences": len(required),
                "tables": sorted(row["table"] for row in required),
                "all_columns": sorted(f"{row['table']}.{name}" for row in entry["columns"]),
                "indexed_everywhere": all(row["indexed"] for row in required),
                "reason": (
                    f"{name!r} is a non-nullable String in {len(required)} tables and is "
                    "neither declared as a vocabulary nor listed as open text"
                ),
                "suggestion": (
                    f"add an entry to ENUM_FIELD_SPECS (values) or OPEN_TEXT_COLUMNS "
                    f"(why it is open) under the key '<table>.{name}'"
                ),
            }
        )
    return findings


def check_constraint_coverage() -> dict[str, Any]:
    """Which numeric columns a CHECK constraint guards, and which it does not.

    Every ``Float`` column is reported, not just the unguarded ones: a column
    that is guarded is a claim the schema makes, and a reader wants to see the
    claim. ``expectation`` separates the three cases the report can distinguish
    -- guarded, deliberately signed, or unguarded.

    The reported gap is *advisory only*. Adding a CHECK constraint is new DDL and
    would need a migration, so this function describes the situation rather than
    promising that nothing here is wrong.
    """
    checks_by_table: dict[str, list[str]] = {}
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is None:
            continue
        checks_by_table[table_name] = [
            str(constraint.sqltext) for constraint in table.constraints if constraint.__class__.__name__ == "CheckConstraint"
        ]
    rows: list[dict[str, Any]] = []
    unguarded: list[str] = []
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is None:
            continue
        checks = checks_by_table.get(table_name, [])
        for column in table.columns:
            if not isinstance(column.type, Float):
                continue
            key = f"{table_name}.{column.name}"
            matched = [check for check in checks if column.name in check]
            if matched:
                expectation = "guarded"
            elif key in SIGNED_QUANTITY_COLUMNS:
                expectation = "signed_by_design"
            else:
                expectation = "unguarded"
                unguarded.append(key)
            rows.append(
                {
                    "field": key,
                    "nullable": bool(column.nullable),
                    "default": _python_default(column),
                    "expectation": expectation,
                    "checks": matched,
                    "reason": SIGNED_QUANTITY_COLUMNS.get(key) if expectation != "guarded" else None,
                }
            )
    return {
        "float_columns": len(rows),
        "guarded": [row["field"] for row in rows if row["expectation"] == "guarded"],
        "signed_by_design": [row["field"] for row in rows if row["expectation"] == "signed_by_design"],
        "unguarded": unguarded,
        "columns": rows,
        "all_checks": {name: checks_by_table[name] for name in sorted(checks_by_table) if checks_by_table[name]},
        "severity": "advisory",
        "note": (
            "adding a CHECK constraint changes the emitted DDL and needs a migration; "
            "this report is descriptive, and the unguarded list is a place to start "
            "when one is warranted"
        ),
    }


def referential_integrity_gaps() -> list[dict[str, Any]]:
    """``*_id`` integer columns that carry no foreign key.

    Three levels of severity, because they are not the same problem:

    * ``dangling_likely`` -- NOT NULL with no foreign key and no declared reason
      to be one. A row either points at a real parent or the database accepted a
      lie.
    * ``dangling_possible`` -- nullable, so "no parent" is representable and
      legitimate, but "deleted parent" is indistinguishable from it. Also used
      for a declared entry whose target table genuinely exists.
    * ``out_of_band`` -- declared in :data:`UNCONSTRAINED_REFERENCE_COLUMNS` as
      something that is deliberately not a foreign key (a room id with no rooms
      table, an admin id that may not be a user at all).

    Like :func:`check_constraint_coverage`, this is descriptive: adding a
    foreign key is new DDL, so the gap is reported and left alone.
    """
    findings: list[dict[str, Any]] = []
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is None:
            continue
        for column in table.columns:
            if not column.name.endswith("_id") or column.primary_key:
                continue
            if column.foreign_keys:
                continue
            key = f"{table_name}.{column.name}"
            declared = UNCONSTRAINED_REFERENCE_COLUMNS.get(key)
            if declared is not None:
                severity = str(declared.get("severity", "dangling_possible"))
            elif column.nullable:
                severity = "dangling_possible"
            else:
                severity = "dangling_likely"
            findings.append(
                {
                    "field": key,
                    "type": str(column.type),
                    "nullable": bool(column.nullable),
                    "indexed": bool(column.index),
                    "severity": severity,
                    "declared": declared is not None,
                    "reason": declared.get("reason") if declared else None,
                }
            )
    order = {"dangling_likely": 0, "dangling_possible": 1, "out_of_band": 2}
    return sorted(findings, key=lambda row: (order.get(row["severity"], 9), row["field"]))


def build_relationship_catalog() -> dict[str, Any]:
    """The ORM relationship graph: who points at whom, and what happens on delete.

    The ``ondelete`` on the foreign key and the ORM ``cascade`` are different
    mechanisms that answer the same question, and the report shows both because
    they disagree in places -- a ``CASCADE`` foreign key on a ``SET NULL`` ORM
    relationship is a real configuration, not a typo, and a reader debugging a
    vanished row needs to see which one won.
    """
    grouped: dict[str, list[Any]] = {}
    for mapper in Base.registry.mappers:
        grouped.setdefault(mapper.local_table.name, []).append(mapper)
    relationships: list[dict[str, Any]] = []
    polymorphic: list[dict[str, Any]] = []
    for table_name in sorted(grouped):
        mappers = grouped[table_name]
        representative = mappers[0]
        identities = sorted(
            {
                str(getattr(mapper, "polymorphic_identity", "") or "")
                for mapper in mappers
            }
            - {""}
        )
        if len(identities) > 1 or getattr(representative, "polymorphic_on", None) is not None:
            discriminator = getattr(representative, "polymorphic_on", None)
            polymorphic.append(
                {
                    "table": table_name,
                    "discriminator": getattr(discriminator, "key", None),
                    "identities": identities,
                    "note": "single-table inheritance; a new family is a new subclass, not a new table",
                }
            )
        for name, prop in _relationships_of(representative):
            target_table = prop.mapper.local_table.name
            cascade = _cascade_modes(prop)
            pairs = []
            try:
                pairs = [
                    {
                        "local": f"{parent.name}.{local.name}",
                        "remote": f"{remote_table.name}.{remote.name}",
                    }
                    for local, remote, remote_table in prop.synchronize_pairs
                ]
            except Exception:  # pragma: no cover - defensive: mapper introspection varies
                pairs = []
            relationships.append(
                {
                    "table": table_name,
                    "relationship": name,
                    "target": target_table,
                    "direction": str(getattr(getattr(prop, "direction", None), "name", "unknown")),
                    "uselist": bool(getattr(prop, "uselist", False)),
                    "back_populates": getattr(prop, "back_populates", None),
                    "backref": getattr(prop, "backref", None),
                    "cascade": cascade,
                    "deletes": "delete-orphan" in cascade,
                    "passive_deletes": bool(getattr(prop, "passive_deletes", False)),
                    "lazy": str(getattr(prop, "lazy", "select")),
                    "join_columns": pairs,
                }
            )
    by_target: dict[str, list[str]] = {}
    for row in relationships:
        by_target.setdefault(row["target"], []).append(f"{row['table']}.{row['relationship']}")
    return {
        "tables": len(grouped),
        "relationship_count": len(relationships),
        "relationships": relationships,
        "children_of": {name: sorted(names) for name, names in sorted(by_target.items())},
        "polymorphic": polymorphic,
        "delete_orphan_tables": sorted(
            row["table"] for row in relationships if row["deletes"]
        ),
        "passive_delete_tables": sorted(
            row["table"] for row in relationships if row["passive_deletes"]
        ),
    }


def build_table_lifecycle_catalog() -> dict[str, Any]:
    """Per-table write mode, retention advisory, and the schema facts behind it.

    A lifecycle declaration is only trustworthy next to the shape of the table
    it describes, so each entry carries its column count, foreign keys and
    checks. A table with no entry in :data:`TABLE_LIFECYCLE` is reported in
    ``unclassified`` rather than assumed to be ``mutable`` -- the default that
    matters is the one nobody wrote down.
    """
    entries: list[dict[str, Any]] = []
    unclassified: list[str] = []
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is None:
            continue
        declared = TABLE_LIFECYCLE.get(table_name)
        foreign_keys = sorted(
            {
                key.target_fullname
                for column in table.columns
                for key in column.foreign_keys
            }
        )
        checks = [
            str(constraint.sqltext)
            for constraint in table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        ]
        if declared is None:
            unclassified.append(table_name)
            entries.append(
                {
                    "table": table_name,
                    "write_mode": None,
                    "retention_days": None,
                    "owner": None,
                    "note": "not classified; nothing here prunes or protects it",
                    "columns": len(table.columns),
                    "foreign_keys": foreign_keys,
                    "checks": checks,
                }
            )
            continue
        entries.append(
            {
                "table": table_name,
                "write_mode": declared.get("write_mode"),
                "retention_days": declared.get("retention_days"),
                "owner": declared.get("owner"),
                "note": declared.get("note"),
                "columns": len(table.columns),
                "foreign_keys": foreign_keys,
                "checks": checks,
            }
        )
    return {
        "table_count": len(entries),
        "classified": len(entries) - len(unclassified),
        "unclassified": sorted(unclassified),
        "write_modes": list(WRITE_MODES),
        "by_write_mode": {
            mode: sorted(row["table"] for row in entries if row["write_mode"] == mode)
            for mode in WRITE_MODES
        },
        "tables": entries,
        "retention_advisory": (
            "retention_days is documentation for an operator writing a retention "
            "policy; nothing in this module deletes a row"
        ),
    }


def build_model_catalog() -> dict[str, Any]:
    """The whole metadata layer as one introspection payload for ``/meta``.

    Composed rather than duplicated: every section is the return value of the
    helper that owns it, so this can never disagree with the function a caller
    would otherwise use directly.
    """
    tables = all_table_names()
    column_total = sum(len(table_columns(name)) for name in tables)
    sensitivity_counts: dict[str, int] = {name: 0 for name in SENSITIVITY_CLASSES}
    for table_name in tables:
        for column_name in table_columns(table_name):
            class_name = sensitivity_of(table_name, column_name)
            sensitivity_counts[class_name] = sensitivity_counts.get(class_name, 0) + 1
    json_columns = sorted(
        f"{table_name}.{column_name}"
        for table_name in tables
        for column_name in table_columns(table_name)
        if is_json_column(column_name)
    )
    return {
        "catalog_version": "models_metadata_v1",
        "layer_note": (
            "derived from Base.metadata at call time; no table, column, constraint "
            "or index is added, so the DDL above needs no migration"
        ),
        "table_count": len(tables),
        "column_count": column_total,
        "tables": tables,
        "sensitivity": {
            "classes": SENSITIVITY_CLASSES,
            "placeholder": SENSITIVITY_PLACEHOLDER,
            "field_map_size": len(FIELD_SENSITIVITY),
            "table_overrides": len(TABLE_FIELD_SENSITIVITY),
            "column_counts": dict(sorted(sensitivity_counts.items(), key=lambda item: -SENSITIVITY_RANK.get(item[0], 0))),
            "redaction_presets": REDACTION_PRESETS,
            "helpers": [
                "sensitivity_of(table, column)",
                "sensitive_columns(table=None, min_rank=0)",
                "projection_for(sensitivity, preset=None)",
                "model_to_dict(instance, preset=None, ...)",
                "redact_instance(instance, preset='operator')",
                "model_to_rows(instances, ...)",
            ],
        },
        "json_columns": {
            "suffix": JSON_COLUMN_SUFFIX,
            "count": len(json_columns),
            "columns": json_columns,
            "decode_failures": JSON_DECODE_FAILURES,
            "helpers": ["loads_json(raw, container='object')", "dumps_json(value)", "is_json_column(column)"],
        },
        "enum_fields": enum_field_report(),
        "schema_gaps": {
            "severity": "advisory",
            "note": "reported, never fixed: closing any of these is new DDL",
            "unbacked_enum_like_columns": unbacked_enum_like_columns(),
            "check_constraint_coverage": check_constraint_coverage(),
            "referential_integrity_gaps": referential_integrity_gaps(),
            "open_text_columns": dict(sorted(OPEN_TEXT_COLUMNS.items())),
            "signed_quantity_columns": dict(sorted(SIGNED_QUANTITY_COLUMNS.items())),
            "unconstrained_reference_columns": {
                key: {"severity": spec["severity"], "reason": spec["reason"]}
                for key, spec in sorted(UNCONSTRAINED_REFERENCE_COLUMNS.items())
            },
        },
        "lifecycle": build_table_lifecycle_catalog(),
        "relationships": build_relationship_catalog(),
        "columns": build_column_catalog(),
    }
