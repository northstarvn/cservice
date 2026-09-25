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