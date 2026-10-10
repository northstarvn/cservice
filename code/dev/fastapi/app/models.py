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
        # The other four scores, which had no check while these two did.
        #
        # That asymmetry is the persistence-layer copy of the same defect
        # `compose_access_score` has: a ceiling on every composite rule and no
        # floor. Here it is two of six columns guarded, so a negative
        # `access_score` was storable even though a negative `customer_score`
        # was not -- and `access_score` is the one every consumer reads, being
        # what `resolve_access_band`, `resolve_policy_tier` and
        # `_control_posture` all take.
        #
        # Added after `_within_published_scale` began clamping on the snapshot
        # boundary, so the boundary and the schema now agree on the same
        # invariant at two levels. Belt and braces is the right posture when the
        # failing write is one that silently changes what a customer is served.
        CheckConstraint("access_score >= 0", name="ck_customer_policy_scores_access_score_non_negative"),
        CheckConstraint("interest_score >= 0", name="ck_customer_policy_scores_interest_score_non_negative"),
        CheckConstraint("closeness_score >= 0", name="ck_customer_policy_scores_closeness_score_non_negative"),
        # The one constraint name that does not spell out `non_negative`, and
        # the reason is a hard limit rather than taste: this one reaches 64
        # characters and PostgreSQL truncates identifiers at 63, so
        # `20261002_03_score_scale_checks` failed at runtime with
        # `IdentifierError: Identifier ... exceeds maximum length of 63
        # characters`. `ge_0` says the same thing in 5 characters fewer and is
        # the only such abbreviation in the file.
        CheckConstraint(
            "community_closeness_score >= 0",
            name="ck_customer_policy_scores_community_closeness_ge_0",
        ),
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
    __table_args__ = (
        # Non-negative, which it did not used to be.
        #
        # `SIGNED_QUANTITY_COLUMNS` exempted this column with the reason "the
        # writer clamps instead, so a check here would only disagree with the
        # code that produced the value". That reason was false: nothing clamped
        # it. A row with `confidence = -1000.0` was writable, `max(...)` carried
        # it into `compose_access_score`, and that produced
        # `access_score = -4444.44` -- which the snapshot boundary now absorbs
        # but which was reaching the tier, posture and band resolvers.
        #
        # So the check was added rather than the declaration kept, and the
        # exemption removed. A documented exception whose stated justification is
        # demonstrably untrue is worse than no exemption: it reads as a decision
        # and stops anyone looking.
        CheckConstraint(
            "confidence >= 0", name="ck_topic_selections_confidence_non_negative"
        ),
    )

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


class UserPreferenceProfile(Base, TimestampMixin):
    """User-owned communication preferences and consent grants (preference centre).

    One row per user (``unique user_id``), holding both the preference
    key/value pairs and the consent grants as JSON blobs. A single row keeps
    the preference centre a mutable singleton (like ``communication_overrides``
    and ``customer_policy_scores``) rather than an append-only ledger, because
    a customer editing a preference is a state change, not an event.

    Both columns are TEXT holding a JSON object. That is deliberate: the
    preference key space is config-driven (``PREFERENCE_CATALOG`` in
    ``services/preferences.py``) and adding a key must not be a migration.
    """
    __tablename__ = "user_preference_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    preferences_json = Column(Text, nullable=False, default="{}")
    consents_json = Column(Text, nullable=False, default="{}")
    # VARCHAR(20) could not hold `PREFERENCE_CATALOG_VERSION`
    # ("preference_catalog_v1", 21 chars), so every write of a preference row
    # failed with a truncation error -- the centre could not save a single
    # preference. Sized generously rather than fitted to today's value: the
    # column holds a version *identifier*, and truncating one silently is the
    # worst outcome for the field that records which catalog a consent was
    # given against.
    consent_version = Column(String(50), nullable=False, default="1.0")


class UserConsentEvent(Base, TimestampMixin):
    """Append-only history of every consent grant and revocation.

    Separate from ``UserPreferenceProfile`` on purpose: the profile row is the
    *current* state and is overwritten on every edit, but consent is a
    compliance claim and a revocation has to be provable after the fact. A
    mutable row cannot show that consent was granted on a date and withdrawn on
    another, so the trail lives here as append-only rows.

    ``granted`` carries the direction rather than a separate event-type column,
    so "the customer consented to marketing" and "the customer withdrew
    marketing consent" are the same row shape with a different boolean.
    """
    __tablename__ = "user_consent_events"
    __table_args__ = (
        CheckConstraint(
            "purpose <> ''", name="ck_user_consent_events_purpose_required"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose = Column(String(50), nullable=False, index=True)
    granted = Column(Boolean, nullable=False, default=False)
    # Same VARCHAR(20) overflow as `user_preference_profiles.consent_version`:
    # the service writes `PREFERENCE_CATALOG_VERSION` (21 chars) here, so every
    # consent event would have failed to insert.
    version = Column(String(50), nullable=False, default="1.0")
    lawful_basis = Column(String(30), nullable=False, default="legitimate_interest")
    recorded_by_id = Column(Integer, nullable=True, index=True)
    note = Column(Text, nullable=False, default="")


class CustomerOffer(Base, TimestampMixin):
    """One offer made to one customer, and where it currently stands (Stage B).

    Mutable, and deliberately separate from the append-only trail beside it, for
    the same reason ``UserPreferenceProfile`` and ``UserConsentEvent`` are: an
    offer has a current state a customer reads ("this is still open for you"),
    while *how it got there and who did what* is a compliance question that has
    to stay answerable after the fact. Collapsing them would mean overwriting the
    record that a goodwill credit was offered, declined, and re-offered.

    One row per offer, keyed by a rendered ``reference`` rather than by a
    per-process counter -- the complaints spine already had that bug and fixed it
    this way, because a counter reissues its own values after every redeploy and
    two customers then hold the same reference.

    The three kinds do not share a vocabulary with each other, and that is the
    point of the separate columns rather than one ``value`` column: a waiver is
    worth nothing in points and a goodwill credit is worth nothing as a waiver,
    so forcing both through one numeric field would make ``points > 0`` mean
    "this offer has value" on a row where it means nothing.
    """

    __tablename__ = "customer_offers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Rendered from `id` on first flush, so it is stable across restarts and
    # unique by construction rather than by a counter.
    reference = Column(String(32), nullable=False, default="", index=True)
    # goodwill | waiver | priority. Config-driven from
    # CUSTOMER_OFFER_KINDS in services/customer_offers.py, so it is not an enum.
    offer_kind = Column(String(30), nullable=False, index=True)
    # Which row of the upstream catalogue this came from -- a
    # RECOVERY_SAVE_INCENTIVES offer_id, or an ARREARS_WAIVER_POLICY waiver_type.
    # Kept so the offer says which policy produced it, which is the difference
    # between "we offered you this" and "the critical-save rule offered you this".
    source_offer_id = Column(String(50), nullable=False, default="", index=True)
    status = Column(String(20), nullable=False, default="offered", index=True)
    headline = Column(String(200), nullable=False, default="")
    # --- goodwill
    points = Column(Float, nullable=False, default=0.0)
    discount_percent = Column(Float, nullable=False, default=0.0)
    # --- waiver
    waiver_type = Column(String(40), nullable=False, default="", index=True)
    arrears_entry_id = Column(Integer, nullable=True, index=True)
    # --- priority
    priority = Column(String(20), nullable=False, default="", index=True)
    # How much the LTV/investment signal scaled this offer, 1.0 being unscaled.
    # Recorded per row because the same rule must apply the same scaling to the
    # same customer for as long as the offer is open -- re-deriving it at
    # fulfilment time would let the amount move under an accepted offer.
    generosity_scale = Column(Float, nullable=False, default=1.0)
    currency = Column(String(8), nullable=False, default="USD")
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    issued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    declined_at = Column(DateTime(timezone=True), nullable=True)
    fulfilled_at = Column(DateTime(timezone=True), nullable=True)
    decline_reason = Column(Text, nullable=False, default="")
    justification = Column(Text, nullable=False, default="")
    issued_by_id = Column(Integer, nullable=True, index=True)
    # When the system should check in about an accepted-but-unfulfilled offer.
    # Nullable rather than defaulted: an offer with no fulfilment step (priority)
    # has no follow-up, and a timestamp of "never" written as a date would be a
    # lie the scheduler would eventually act on.
    follow_up_due_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        CheckConstraint(
            "discount_percent >= 0 AND discount_percent <= 100",
            name="ck_customer_offers_discount_percent_range",
        ),
        CheckConstraint("generosity_scale > 0", name="ck_customer_offers_generosity_positive"),
    )


class CustomerOfferEvent(Base, TimestampMixin):
    """Append-only trail of every state change to an offer.

    ``kind`` is the verb, not the resulting status, so "issued", "accepted",
    "declined", "expired", "fulfilled" and "suppressed" are one vocabulary even
    though two of them (accepted and fulfilled) share a status. A record of what
    happened *and* the status it produced is what lets an operator answer "why
    was this offered twice" -- which a mutable status alone cannot answer.
    """

    __tablename__ = "customer_offer_events"

    id = Column(Integer, primary_key=True, index=True)
    offer_id = Column(Integer, ForeignKey("customer_offers.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(30), nullable=False, index=True)
    from_status = Column(String(20), nullable=False, default="")
    to_status = Column(String(20), nullable=False, default="")
    actor = Column(String(120), nullable=False, default="", index=True)
    note = Column(Text, nullable=False, default="")
    payload_json = Column(Text, nullable=False, default="{}")


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
    complaint_cases = relationship(
        "ComplaintCase",
        backref="user",
        cascade="all, delete-orphan",
    )
    complaint_events = relationship(
        "ComplaintEvent",
        passive_deletes=True,
        backref="user",
    )
    complaint_decisions = relationship(
        "ComplaintDecision",
        passive_deletes=True,
        backref="user",
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


# --- Complaints ---------------------------------------------------------------
#
# Three tables, deliberately split, because they answer three different
# questions and have three different lifetimes.
#
# `complaint_cases`
#     The one row that *is* the complaint: who, what, how bad, whose hands, and
#     which clock is running. Mutable, because a case is a live thing with a
#     state machine. `reference` is durable and unique -- the old recovery
#     path minted `ESC-{user}-{run_seq:03d}` from an in-process counter that
#     reset on restart, so the first escalation for a user after a redeploy
#     collided with every earlier one. The reference is now the row's identity.
#
# `complaint_events`
#     Append-only timeline. A case row shows current state and overwrites it on
#     every transition, so it can answer "where is it" but not "how did it get
#     there" or "who touched it". ISO 10002 asks for a process the complainant
#     can see, and an audit trail the organization can act on; both need the
#     transitions, not just the destination.
#
# `complaint_decisions`
#     Append-only record of what an operator decided, why, and what happened
#     next. This is the institutional memory: a *closed* decision that was
#     later reopened is evidence the precedent was wrong, and a decision that
#     held is evidence it was right. Nothing else in the schema can express
#     that, which is why it is a table and not a column on the case.
#
# Every vocabulary column here (category, severity, status, tier, event_type,
# decision, outcome, resolution_code) is config-driven from
# `app/services/complaints.py`, so it is declared in `ENUM_FIELD_SPECS` below
# and deliberately *not* hard-constrained to today's list.


class ComplaintCase(Base, TimestampMixin):
    """A complaint under active handling: the SLA clock and the owner live here."""

    __tablename__ = "complaint_cases"
    __table_args__ = (
        CheckConstraint("reopened_count >= 0", name="ck_complaint_cases_reopened_non_negative"),
        CheckConstraint(
            "satisfaction_score IS NULL OR (satisfaction_score >= 0 AND satisfaction_score <= 10)",
            name="ck_complaint_cases_satisfaction_in_range",
        ),
        UniqueConstraint("reference", name="uq_complaint_cases_reference"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Durable and unique. This is what a customer quotes on the phone and what
    # an agent searches; it must never depend on a process that can restart.
    reference = Column(String(40), nullable=False, index=True)
    category = Column(String(50), nullable=False, default="service_quality", index=True)
    severity = Column(String(20), nullable=False, default="medium", index=True)
    status = Column(String(30), nullable=False, default="open", index=True)
    tier = Column(String(30), nullable=False, default="tier_1", index=True)
    # Deliberately not a foreign key, for the reason given on
    # `communication_overrides.set_by_admin_id`: an owner may be an external or
    # already-deleted operator, and neither should make the row unwriteable.
    owner_user_id = Column(Integer, nullable=True, index=True)
    owner_team = Column(String(50), nullable=False, default="", index=True)

    opened_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    first_response_at = Column(DateTime(timezone=True), nullable=True)
    escalated_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    response_due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    resolution_due_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Which configured trigger fired, and whether the engine acted by itself.
    # The pair is the difference between "a human decided this" and "a clock
    # decided this", which is exactly what a regulator or a reviewer asks first.
    escalation_trigger_id = Column(String(60), nullable=False, default="", index=True)
    auto_escalated = Column(Boolean, nullable=False, default=False, index=True)
    regulatory = Column(Boolean, nullable=False, default=False, index=True)

    resolution_code = Column(String(50), nullable=False, default="", index=True)
    resolution_note = Column(Text, nullable=False, default="")
    satisfaction_score = Column(Integer, nullable=True)
    reopened_count = Column(Integer, nullable=False, default=0)
    # The factors the engine saw when the case opened. Snapshotted because the
    # live signals move; a later reviewer needs what was true then, not now.
    factors_json = Column(Text, nullable=False, default="{}")
    summary = Column(Text, nullable=False, default="")
    source = Column(String(50), nullable=False, default="chat", index=True)


class ComplaintEvent(Base, TimestampMixin):
    """Append-only transition log for a complaint case."""

    __tablename__ = "complaint_events"

    id = Column(Integer, primary_key=True, index=True)
    complaint_id = Column(Integer, ForeignKey("complaint_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    # Denormalised on purpose: every queue, SLA and workload query filters by
    # user, and joining back to the case for it would be the join this table
    # exists to avoid. It is kept consistent by the service, not by a trigger.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, default="opened", index=True)
    from_status = Column(String(30), nullable=False, default="")
    to_status = Column(String(30), nullable=False, default="")
    from_tier = Column(String(30), nullable=False, default="")
    to_tier = Column(String(30), nullable=False, default="")
    actor_user_id = Column(Integer, nullable=True, index=True)
    actor_role = Column(String(30), nullable=False, default="")
    detail_json = Column(Text, nullable=False, default="{}")
    note = Column(Text, nullable=False, default="")


class ComplaintDecision(Base, TimestampMixin):
    """Append-only record of an escalation decision and its eventual outcome.

    `factors_json` is the decision-support snapshot as the decider saw it, and
    `precedent_refs_json` is the set of past cases that were cited. Both are
    frozen at decision time on purpose: the live feeds keep moving, so a row
    that re-read them later would report a reasoning that was never actually
    given.
    """

    __tablename__ = "complaint_decisions"

    id = Column(Integer, primary_key=True, index=True)
    complaint_id = Column(Integer, ForeignKey("complaint_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    decision = Column(String(60), nullable=False, default="escalate", index=True)
    outcome = Column(String(30), nullable=False, default="proposed", index=True)
    from_tier = Column(String(30), nullable=False, default="")
    to_tier = Column(String(30), nullable=False, default="")
    # A decision may name an operator who is not a user record; same reasoning
    # as `complaint_cases.owner_user_id`.
    decided_by_id = Column(Integer, nullable=True, index=True)
    decided_by_role = Column(String(30), nullable=False, default="")
    # Which assurance level the decision was taken under. Recorded so that
    # "admin" and "admin who re-authenticated for this" stay distinguishable.
    step_up_level = Column(String(10), nullable=False, default="")
    rationale = Column(Text, nullable=False, default="")
    factors_json = Column(Text, nullable=False, default="{}")
    precedent_refs_json = Column(Text, nullable=False, default="[]")
    auto_applied = Column(Boolean, nullable=False, default=False)
    # A later decision that reverses this one. A real foreign key, unlike the
    # actor columns, because it does point at a row in this same table.
    superseded_by_id = Column(Integer, ForeignKey("complaint_decisions.id", ondelete="SET NULL"), nullable=True, index=True)
    # What the case actually did afterwards. This is the column that turns a
    # decision log into evidence: `reopened` and `escalated_further` are what
    # mark a precedent as bad.
    outcome_observed = Column(String(40), nullable=False, default="", index=True)
    outcome_recorded_at = Column(DateTime(timezone=True), nullable=True)


class ComplaintSignalWeight(Base, TimestampMixin):
    """The learned contribution weight for one complaint signal.

    One row per `signal_id` in `services/complaint_learning.LOYALTY_SIGNALS`.
    Persisted rather than held in module state on purpose: `risk_evaluator`
    keeps its adaptive weights in a process-local dict, which means a restart
    resets what the system learned from every complaint it has ever handled.
    A weight that has to survive a deploy to count is a weight nobody can audit,
    so this is a table with an explicit version and an observation count.

    `weight` is the current value; `prior_weight` is where the configured
    starting point sits, so decay has a destination and the "why is it this
    number" question always has an answer. `confidence` is observations scaled
    by agreement, and it is reported rather than acted on -- see
    `LEARNED_WEIGHT_AUTHORITY` for why a learned weight cannot move a tier.
    """

    __tablename__ = "complaint_signal_weights"
    __table_args__ = (
        CheckConstraint("weight >= 0", name="ck_complaint_signal_weights_weight_non_negative"),
        CheckConstraint("confidence >= 0 AND confidence <= 1",
                        name="ck_complaint_signal_weights_confidence_in_range"),
        CheckConstraint("observations >= 0", name="ck_complaint_signal_weights_observations_non_negative"),
        UniqueConstraint("signal_id", name="uq_complaint_signal_weights_signal"),
    )

    id = Column(Integer, primary_key=True, index=True)
    signal_id = Column(String(60), nullable=False, index=True)
    weight = Column(Float, nullable=False, default=1.0)
    prior_weight = Column(Float, nullable=False, default=1.0)
    confidence = Column(Float, nullable=False, default=0.0)
    observations = Column(Integer, nullable=False, default=0)
    agreements = Column(Integer, nullable=False, default=0)
    disagreements = Column(Integer, nullable=False, default=0)
    # Bumped on every learning pass so a stale cache is detectable rather than
    # silently wrong.
    version = Column(Integer, nullable=False, default=1)
    last_outcome_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=False, default="")


class ComplaintSignalObservation(Base, TimestampMixin):
    """Append-only: one signal contributing to one case, and what came of it.

    This is the learning set. Kept as rows rather than a running average on the
    weight table for two reasons: the training signal has to be re-derivable when
    the weighting rule changes, and a reviewer has to be able to answer "why
    does ``sentiment_cliff`` weigh 1.8" by reading rows rather than trusting an
    aggregate.

    ``loyalty_outcome`` is measured against the retention objective, not against
    "was the complaint closed". A resolution the customer then abandoned is a
    failure and is recorded as one.
    """

    __tablename__ = "complaint_signal_observations"
    __table_args__ = (
        CheckConstraint("observations_snapshot >= 0",
                        name="ck_complaint_signal_observations_count_non_negative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    complaint_id = Column(Integer, ForeignKey("complaint_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    signal_id = Column(String(60), nullable=False, index=True)
    # The weight at the moment the decision was taken, so a later re-weighting
    # cannot retroactively change what the operator was shown.
    weight_snapshot = Column(Float, nullable=False, default=1.0)
    confidence_snapshot = Column(Float, nullable=False, default=0.0)
    observations_snapshot = Column(Integer, nullable=False, default=0)
    loyalty_outcome = Column(String(40), nullable=False, default="", index=True)
    loyalty_delta = Column(Float, nullable=False, default=0.0)
    # The evidence that produced the outcome, frozen with it.
    evidence_json = Column(Text, nullable=False, default="{}")
    # Set when this observation is superseded by a later one for the same pair,
    # so a re-observation does not double-count.
    superseded = Column(Boolean, nullable=False, default=False, index=True)
    observed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ComplaintImprovementProposal(Base, TimestampMixin):
    """A durable improvement suggestion derived from stacked complaints.

    Durable because the detection is a pure function of complaint history: run
    it twice on the same data and it must produce the same proposal id, and a
    human who has already seen and dismissed one should not have it reappear on
    the next sweep. `proposal_id` is therefore a deterministic hash of the
    cluster's identity, and `status` records what happened to it.
    """

    __tablename__ = "complaint_improvement_proposals"
    __table_args__ = (
        # No invented check here. The cluster's case count, distinct-user count
        # and reopen rate are *evidence*, and they live in `evidence_json`
        # because they are computed at detection time and re-read verbatim;
        # a CHECK on a column the table does not have is worse than no check,
        # since it reads as a guarantee that was never enforced.
        UniqueConstraint("proposal_id", name="uq_complaint_improvement_proposals_proposal"),
    )

    id = Column(Integer, primary_key=True, index=True)
    proposal_id = Column(String(80), nullable=False, index=True)
    cluster_key = Column(String(120), nullable=False, index=True)
    title = Column(String(255), nullable=False, default="")
    component = Column(String(80), nullable=False, default="", index=True)
    owner_hint = Column(String(80), nullable=False, default="")
    priority = Column(String(20), nullable=False, default="medium", index=True)
    impact = Column(String(20), nullable=False, default="medium")
    effort = Column(String(20), nullable=False, default="medium")
    rationale_json = Column(Text, nullable=False, default="[]")
    recommended_action = Column(Text, nullable=False, default="")
    signals_json = Column(Text, nullable=False, default="[]")
    evidence_json = Column(Text, nullable=False, default="{}")
    status = Column(String(30), nullable=False, default="detected", index=True)
    # The release-ladder candidate this proposal was published as, if any.
    candidate_id = Column(String(80), nullable=False, default="", index=True)


# --- Moving value between customers ----------------------------------------
#
# Two tables, and they are separate because the two operations have nothing in
# common but the word "transfer".
#
# `points_transfers` moves a *credit*. The payer receives it, so this is
# ordinary. `arrears_settlements` records a *debt being paid by someone else*,
# where the payer gets nothing and the debtor stays liable -- because that is the
# only defensible reading of "transfer my arrears", and the one that is refused
# is written down rather than left as a missing feature.

class PointsTransfer(Base, TimestampMixin):
    """One paired movement of loyalty credit between two customers.

    The two ledger legs live in ``points_transactions`` as ``transfer_out`` and
    ``transfer_in``; this table holds what a pair of ledger rows cannot, which is
    that they belong to the same event.

    ``idempotency_key`` is the load-bearing column. Points are a balance and a
    balance is spent twice by a retry, and a client that times out mid-request
    has no way to tell whether the first attempt landed. Without a unique key,
    the only safe client behaviour is to refuse to retry -- which makes every
    network blip a lost transfer and trains people not to use the feature.

    It is a caller-supplied string rather than a server-generated id for the same
    reason: the caller is the only party that can make a second attempt carry
    the *same* key.
    """

    __tablename__ = "points_transfers"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_points_transfers_idempotency"
        ),
        CheckConstraint("points > 0", name="ck_points_transfers_positive"),
        # Two *different* parties. Both columns are already `nullable=False`, so
        # this is the only thing left to assert -- and it is the thing that
        # matters, because a self-transfer creates a ledger pair with no
        # counterparty.
        #
        # The XOR form `(a IS NULL) <> (b IS NULL)` was the first attempt and is
        # the inverse of what it looks like: it demands *exactly one* party be
        # NULL, so it rejected every real transfer and permitted only the broken
        # ones. It failed loudly on the first test, which is the argument for
        # having written it down rather than shipping it untested.
        CheckConstraint(
            "from_user_id <> to_user_id",
            name="ck_points_transfers_two_distinct_parties",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String(120), nullable=False, index=True)
    from_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    to_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    point_type = Column(String(50), nullable=False, default="loyalty", index=True)
    # Positive on both sides. The direction is in the column names, not in the
    # sign, so no arithmetic has to know which way round it is.
    points = Column(Float, nullable=False, default=0.0)
    status = Column(String(20), nullable=False, default="completed", index=True)
    # "points_gift", "points_refund", "points_goodwill". Config-driven, so a
    # deployment can recognise a reason the business uses without a migration.
    reason = Column(String(40), nullable=False, default="points_gift", index=True)
    # The note the sender wrote, and the requester's own words. Not free text
    # about a third party by default -- see the service, which bounds its length.
    note = Column(String(200), nullable=False, default="")
    sender_balance_after = Column(Float, nullable=False, default=0.0)
    receiver_balance_after = Column(Float, nullable=False, default=0.0)
    # 1.0 means the sender chose freely. Anything lower means the decision was
    # coerced or the recipient was not the intended one, which is exactly the
    # case an operator needs to find later.
    consent_confirmed = Column(Boolean, nullable=False, default=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    reversal_reason = Column(String(60), nullable=False, default="")


class ArrearsSettlement(Base, TimestampMixin):
    """Someone other than the debtor paying down an arrears entry.

    This is **not** a transfer of the debt. The debtor named on
    ``arrears_entries.user_id`` remains the person who owes it, before and after
    this row exists, and ``debtor_liability_unchanged`` records that as a fact
    rather than leaving it to be inferred.

    Why the distinction is the whole design, and not a caveat: moving a debt to
    another person is how people buy their way out of one. The service refuses it
    (see ``arrears_settlements.REFUSED_*`` in
    ``app/services/transfers.py``), and refusing it means this table only ever
    holds genuine third-party *payments* -- so it can be read as "who helped whom"
    without anyone inferring that a liability moved.

    ``payout_credit_points`` is fixed at zero by the service and exists as a
    column so that the reason it is zero is visible in the schema. Paying
    someone's debt for loyalty credit is a closed laundering loop: settle a
    friend's arrears, collect the credit, send the credit back. The loop is
    closed by never opening it.
    """

    __tablename__ = "arrears_settlements"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_arrears_settlements_idempotency"
        ),
        CheckConstraint("amount > 0", name="ck_arrears_settlements_positive"),
        CheckConstraint(
            "payout_credit_points = 0",
            name="ck_arrears_settlements_no_payout",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String(120), nullable=False, index=True)
    # The debtor. Unchanged by this row -- see the class docstring.
    debtor_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # The person who actually paid. May equal the debtor (a normal payment).
    payer_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    arrears_entry_id = Column(Integer, ForeignKey("arrears_entries.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Float, nullable=False, default=0.0)
    currency = Column(String(8), nullable=False, default="USD")
    # The split, snapshotted from the entry at payment time. Stored rather than
    # derived because the entry's terms may be waived afterwards, and what the
    # payer was told they were paying has to stay recoverable.
    principal_covered = Column(Float, nullable=False, default=0.0)
    interest_covered = Column(Float, nullable=False, default=0.0)
    late_fee_covered = Column(Float, nullable=False, default=0.0)
    # Always False. A column rather than a docstring so that a query asking
    # "did anyone move this debt?" is answerable from the data.
    debt_transferred = Column(Boolean, nullable=False, default=False)
    # Always False. See the class docstring on the laundering loop.
    payout_credit_points = Column(Float, nullable=False, default=0.0)
    is_third_party = Column(Boolean, nullable=False, default=False, index=True)
    payer_consent_confirmed = Column(Boolean, nullable=False, default=False)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), nullable=False, default="settled", index=True)
    note = Column(String(200), nullable=False, default="")


# --- Identity, sign-in methods and connected storage ------------------------
#
# These four tables exist because of two questions the ``users`` row cannot
# answer on its own: *which device is asking*, and *which other accounts does
# this person have*. Both are one-to-many off ``users``, so both are here.
#
# The design rule running through all four: **no row in this section stores a
# credential, a raw signal, or a plaintext token.** Device signals are stored
# as peppered digests, the trusted-device token is stored as a password hash,
# one-time codes are stored as digests, and OAuth refresh tokens are stored
# encrypted with the scheme recorded alongside them. A dump of this section
# should not be enough to impersonate anybody or to read anybody's files.

class AuthDevice(Base, TimestampMixin):
    """A device observed for a user, and whether it has been trusted.

    Every column is a digest or a flag. The recognition engine in
    ``app/services/device_recognition.py`` reads these to score a later login
    and writes ``last_seen_at`` / ``use_count`` as it goes, which is what makes
    "usual hour of activity" an observation about this person rather than an
    assumption about the population.

    ``trusted_at`` is set only after a second factor has been presented *on
    this device*. That ordering is the whole security property: recognition may
    skip a re-challenge for an already-trusted device, but it can never
    *create* the trust, so there is no path from "looks familiar" to "is
    trusted".
    """

    __tablename__ = "auth_devices"
    __table_args__ = (
        # A user's digests are unique per signal *pair*, so the same user cannot
        # accumulate two rows claiming the same device id -- which would let a
        # single device score twice and inflate its own recognition.
        UniqueConstraint(
            "user_id", "device_digest", name="uq_auth_devices_user_device"
        ),
        # A trusted device always has a token hash, and a token hash is always
        # on a trusted device. Enforced in the schema rather than in the router
        # because "trusted" without a credential is precisely the row that would
        # make recognition authoritative.
        CheckConstraint(
            "(trusted_at IS NULL) = (token_hash IS NULL)",
            name="ck_auth_devices_trusted_requires_token",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Human label the user gave this device ("Work laptop"), so the trust
    # decision is revocable by someone who recognises it in a list.
    label = Column(String(80), nullable=False, default="", index=True)

    # --- Recognition digests (never the raw signal) -------------------------
    device_digest = Column(String(64), nullable=False, index=True)
    network_digest = Column(String(64), nullable=False, default="", index=True)
    agent_digest = Column(String(64), nullable=False, default="")
    language_digest = Column(String(64), nullable=False, default="")
    timezone_digest = Column(String(64), nullable=False, default="")

    # The hours this user has signed in at, as a JSON array of ints 0..23.
    # A *set* of hours rather than a histogram on purpose: "how often" is a
    # signal an attacker can manufacture by logging in repeatedly, so it is
    # not recorded. See ``device_recognition.fold_hour``.
    usual_hours = Column(Text, nullable=False, default="[]")

    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    use_count = Column(Integer, nullable=False, default=0)

    # --- Trust --------------------------------------------------------------
    trusted_at = Column(DateTime(timezone=True), nullable=True)
    trust_expires_at = Column(DateTime(timezone=True), nullable=True)
    # Password hash of the opaque device token. The token itself is returned
    # once at creation and never stored.
    token_hash = Column(String(255), nullable=True)
    # Bounded so a leaked token stops being useful on its own.
    rotated_at = Column(DateTime(timezone=True), nullable=True)

    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String(40), nullable=False, default="", index=True)


class AuthChallenge(Base):
    """One outstanding one-time-code challenge.

    A separate table rather than a column on ``users`` because these are
    short-lived, high-volume, and the interesting state is the *attempt
    counter*: a code is only as safe as the number of guesses it tolerates, and
    that has to survive a process restart, which a cache entry would not.
    """

    __tablename__ = "auth_challenges"
    __table_args__ = (
        # One live challenge per (user, purpose). A second request replaces the
        # first rather than accumulating, so requesting a code repeatedly
        # cannot widen the attacker's guesses -- it only invalidates codes they
        # already had.
        UniqueConstraint("user_id", "purpose", "consumed_at", name="uq_auth_challenges_live"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # What the code is for ("login", "step_up", "email_change"), so a code
    # minted for one purpose cannot be replayed at another.
    purpose = Column(String(40), nullable=False, default="login", index=True)
    # Digest of the code. Not the code.
    code_hash = Column(String(255), nullable=False)
    # Which channel it went out by, for the audit trail.
    channel = Column(String(20), nullable=False, default="email")
    # Digest of the device that asked, so a code minted on a recognised device
    # is not accepted from an unrecognised one.
    device_digest = Column(String(64), nullable=False, default="")
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    issued_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    # Set when this challenge was replaced by a newer one, so "superseded" and
    # "never used" stay distinguishable.
    superseded_at = Column(DateTime(timezone=True), nullable=True)


class UserIdentity(Base):
    """Another account this person may sign in with, linked to a ``users`` row.

    This is the "opt to login" surface: a person can add an address, a phone
    number, or an external identity provider to the account they already have.
    Adding one is what makes an account *recoverable*; it is not a second
    account and it carries no separate credential.

    ``is_primary`` is what a login resolves to when several identifiers match.
    At most one row per user is primary, enforced below rather than trusted to
    application code, because two primaries means "which account did I just log
    into" has two answers.
    """

    __tablename__ = "user_identities"
    __table_args__ = (
        # One account per (provider, identifier). This is the constraint that
        # actually matters: without it, the same address could be linked to two
        # different users, and a login by that address would then have to pick
        # one -- which is the shape of an account-takeover bug. The per-user
        # variant below only stops one account listing the same identifier
        # twice.
        UniqueConstraint(
            "provider", "identifier_hash", name="uq_user_identities_provider_identifier"
        ),
        # For an external provider the subject is the provider's own stable id,
        # so this is what binds "the Google account that says who it is" to a
        # row here. Null for email/phone, where NULLs do not collide and so
        # correctly place no constraint at all.
        UniqueConstraint(
            "provider", "external_id", name="uq_user_identities_provider_subject"
        ),
        UniqueConstraint("user_id", "identifier_hash", name="uq_user_identities_user_identifier"),
        # A primary identifier is one the user has proven they control.
        CheckConstraint(
            "(is_primary) <= (is_verified)",
            name="ck_user_identities_primary_requires_verification",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Which kind of identifier this is: "email", "phone", or a named external
    # provider ("google", "okta").
    provider = Column(String(40), nullable=False, default="email", index=True)
    # Digest of the identifier. Stored hashed so a database read cannot be used
    # to enumerate who has an account here; ``identifier_hint`` below keeps it
    # displayable without keeping it retrievable.
    identifier_hash = Column(String(64), nullable=False)
    # A masked fragment ("a***@example.com") so a user can recognise an
    # identifier in a list without the list being a credential dump.
    identifier_hint = Column(String(120), nullable=False, default="")
    # Only meaningful for an external provider; null for email/phone.
    external_id = Column(String(255), nullable=True, index=True)
    is_primary = Column(Boolean, nullable=False, default=False, index=True)
    is_verified = Column(Boolean, nullable=False, default=False, index=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verification_method = Column(String(30), nullable=False, default="")
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    # Consent to be contacted at this identifier, recorded per identifier
    # rather than per user, because people use different addresses.
    contact_consent = Column(Boolean, nullable=False, default=False)
    # Free-form provenance ("added during migration", "self-service"). Kept as
    # a column rather than an enum because this vocabulary is operational and
    # grows without a migration being worth it.
    source = Column(String(40), nullable=False, default="self_service", index=True)


class StorageConnection(Base, TimestampMixin):
    """A user's own cloud storage, connected under their own authority.

    ``access_token_encrypted`` and ``refresh_token_encrypted`` hold ciphertext
    whose ``enc:<scheme>:`` prefix records how it was encrypted, so a change of
    available libraries cannot make stored rows undecryptable. Plaintext tokens
    are never written.

    ``scopes_json`` records what the user actually consented to, which is not
    necessarily what the provider would grant: a user can approve a narrower
    grant than the scope requested, and the connection must describe the grant
    rather than the request.

    Carries ``TimestampMixin`` even though the OAuth flow updates the same row
    several times over (pending -> active, or -> revoked) and one might think
    ``connected_at`` alone would do. The mixin is here because ``updated_at`` is
    what distinguishes "this connection was re-authorised yesterday" from "it has
    been sitting there since 2024 with a dead refresh token", and that is the
    question ``connection_health`` is asked. Migration ``0009`` created the
    columns, so this costs nothing at deploy time.
    """

    __tablename__ = "storage_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_storage_connections_user_provider"),
        # 'pending' is the value an OAuth attempt is inserted with, before the
        # customer has reached the provider's consent screen. Without it in the
        # vocabulary the constraint rejects the very first write the connect
        # flow performs -- a check that is not merely wrong but makes the
        # feature unreachable.
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'expired', 'error')",
            name="ck_storage_connections_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(40), nullable=False, index=True)
    label = Column(String(80), nullable=False, default="")
    status = Column(String(20), nullable=False, default="active", index=True)
    # What was asked for, what was granted, and whether the user had to confirm
    # the broad end of the ladder. All three, because "we requested full access"
    # and "the user granted full access" are different claims.
    requested_scopes_json = Column(Text, nullable=False, default="[]")
    scopes_json = Column(Text, nullable=False, default="[]")
    broad_scope_confirmed = Column(Boolean, nullable=False, default=False)
    access_token_encrypted = Column(Text, nullable=False, default="")
    refresh_token_encrypted = Column(Text, nullable=False, default="")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # Opaque, single-use, bound to one connect attempt. Present only between
    # the redirect being issued and the callback landing.
    state_hash = Column(String(64), nullable=False, default="", index=True)
    code_verifier_encrypted = Column(Text, nullable=False, default="")
    connected_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String(40), nullable=False, default="", index=True)
    last_error = Column(String(255), nullable=False, default="")


class AiProviderCredential(Base, TimestampMixin):
    """A credential an *operator* supplied for an AI provider, at rest encrypted.

    The environment is how a deployment is configured; this table is how a
    credential is rotated without a redeploy. It exists because the second kind
    of credential this service accepts -- a browser session -- is short-lived by
    nature, and asking a human to restart the process every time a web session
    refreshes is not a rotation story.

    ``secret_encrypted`` holds ciphertext with an ``enc:<scheme>:`` prefix, so a
    change in which crypto library is installed cannot make a stored row
    undecryptable. It is classified as a credential even though it is ciphertext:
    the ciphertext is what an attacker replays, so it is auth material by any
    reasonable reading.

    ``auth_mode`` and ``status`` are constrained to their vocabularies in the
    database rather than trusted from the application, because a typo in either
    is a provider that silently never answers.
    """

    __tablename__ = "ai_provider_credentials"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(40), nullable=False, index=True)
    label = Column(String(80), nullable=False, default="")
    auth_mode = Column(String(20), nullable=False, default="api_key")
    secret_encrypted = Column(Text, nullable=False, default="")
    status = Column(String(20), nullable=False, default="active", index=True)
    # Where the credential came from, so an operator can tell a value they set
    # from one the environment injected under the same provider name.
    credential_source = Column(String(20), nullable=False, default="database")
    last_error = Column(String(255), nullable=False, default="")
    # When it was last replaced (a rotation) and last used (an attempt charged
    # against it). Two different questions: "is this fresh" and "is it live".
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "auth_mode IN ('api_key', 'browser_session')",
            name="ck_ai_provider_credentials_auth_mode",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_ai_provider_credentials_status",
        ),
        UniqueConstraint(
            "provider", name="uq_ai_provider_credentials_provider"
        ),
    )


class AiModelHealth(Base, TimestampMixin):
    """The failover state: which provider/model is cooled down, and until when.

    This is the table that makes a quota ending survive a restart. Without it,
    every deploy would re-probe the provider that said "insufficient_quota" an
    hour ago, discover it again, and burn a request doing so -- and a rolling
    deploy would do it on every pod.

    One row per ``(provider, model)``, because a quota is not always
    provider-wide: an OpenAI deployment can exhaust ``gpt-4o`` and still answer
    from ``gpt-4o-mini``, and the cooldown has to be narrow enough to allow that.
    A rejected *credential* is the exception, and the pool cools every model of
    that provider rather than walking the ones that will also be rejected.

    ``failure_count`` is cumulative until the next success, so a flapping
    provider can be told apart from one that failed once.
    """

    __tablename__ = "ai_model_health"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(40), nullable=False, index=True)
    model = Column(String(120), nullable=False, index=True)
    failure_kind = Column(String(30), nullable=False, default="")
    failure_count = Column(Integer, nullable=False, default=0)
    cooldown_until = Column(DateTime(timezone=True), nullable=True, index=True)
    last_error = Column(String(255), nullable=False, default="")
    last_checked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "failure_count >= 0", name="ck_ai_model_health_failure_count_non_negative"
        ),
        UniqueConstraint(
            "provider", "model", name="uq_ai_model_health_provider_model"
        ),
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

# noqa: E402 -- deliberate. The metadata layer begins below the expansion
# marker and this import belongs to it; hoisting it above the DDL layer
# would erase the separation the module docstring describes.
from sqlalchemy.orm import RelationshipProperty  # noqa: E402

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
    # --- Identity and sign-in material ------------------------------------
    # Every one of these is either a credential or the encrypted form of one.
    # They are grouped together because they share one failure mode: a value in
    # any of them is enough to act as the user, so none of them is ever
    # serialised, projected, or included in a bulk export.
    #
    # `*_encrypted` is classified as a credential even though it is ciphertext.
    # The ciphertext is itself the thing an attacker needs -- it can be replayed
    # verbatim if the endpoint decrypts it on their behalf -- so it is auth
    # material by any reasonable reading.
    "token_hash": "credential",
    "code_hash": "credential",
    "code_verifier_encrypted": "credential",
    "access_token_encrypted": "credential",
    "refresh_token_encrypted": "credential",
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
    # A preference blob is the customer stating a channel, a language and a
    # cadence about themselves. It is not free text a person wrote, but it is
    # behavioural and it is theirs, so `content` (redact by default) is the
    # right default rather than `internal` (include by default).
    "preferences_json": "content",
    # The consent blob is a set of booleans over a fixed purpose vocabulary --
    # no free text, no identifiers. `behavioral` keeps it in bulk exports
    # because a consent state is exactly what an export needs and contains
    # nothing that identifies anybody.
    "consents_json": "behavioral",
    # A complaint is the most sensitive thing a customer can write about us.
    # The snapshot of the signals that opened it, the operator's written
    # reasoning, and the precedent set cited all describe a person mid-dispute.
    "factors_json": "content",
    "precedent_refs_json": "content",
    "resolution_note": "content",
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
    # Recognition digests. These are HMACs of signals, so they are not
    # reversible, but they *are* a behavioural profile: a set of them taken
    # together identifies a device and its usual hours, and correlating them
    # across tables is exactly what domain separation in
    # `device_recognition._DIGEST_DOMAINS` is meant to prevent. `behavioral`
    # rather than `identifier` because they describe a device rather than a
    # person -- except `identifier_hash`, which is a digest of *how the person
    # is named*, and stays content-adjacent.
    "device_digest": "behavioral",
    "network_digest": "behavioral",
    "agent_digest": "behavioral",
    "language_digest": "behavioral",
    "timezone_digest": "behavioral",
    "usual_hours": "behavioral",
    "state_hash": "credential",
    "identifier_hash": "content",
    "identifier_hint": "content",
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
    "lawful_basis": "internal",
}

#: Per-table overrides, keyed ``"table.column"``. Used where a shared column
#: name means different things in different tables.
TABLE_FIELD_SENSITIVITY: dict[str, str] = {
    # A provider credential is a credential in any table. `secret_encrypted` is
    # ciphertext, but ciphertext is what an attacker replays, exactly as with the
    # storage tokens -- so it is classified by hand rather than left to the
    # column-name default, which would only know the name `secret_encrypted`
    # because this table introduced it.
    "ai_provider_credentials.secret_encrypted": "credential",
    # an operator-written reason, not user content -- but it still names people
    "recovery_actions.failure_reason": "content",
    # a policy score summary is machine-generated narrative about one person
    "customer_policy_scores.summary": "behavioral",
    # A consent row is a legal record *about a person*, and its free-text note
    # is written by whoever recorded the change. Both override the name-based
    # classification above.
    # A stated reason for declining an offer is the most sensitive thing this
    # subsystem records: it is the customer telling us why they did not want what
    # we offered, which frequently means the underlying problem we failed to fix.
    "customer_offers.decline_reason": "content",
    "customer_offers.justification": "internal",
    "customer_offers.points": "financial",
    "customer_offers.discount_percent": "financial",
    "customer_offers.generosity_scale": "internal",
    "customer_offer_events.note": "content",
    "customer_offer_events.actor": "internal",
    "user_consent_events.purpose": "behavioral",
    "user_consent_events.lawful_basis": "internal",
    # the audit trail's own summary is written about an entity, not by a user
    "audit_log_entries.summary": "content",
    # a security event summary is machine-generated from a signal
    "security_events.summary": "behavioral",
    # a username is an identifier, not free text, but it is still a login name
    "users.username": "identifier",
    # A case reference is an identifier, not content, even though it is a
    # string: it is what support and the customer use to find the row, and
    # redacting it would make the complaint undiscussable.
    "complaint_cases.reference": "identifier",
    # Who held a complaint, and at what tier, is operational routing detail.
    # It is not the complainant's own data, so it stays in bulk exports.
    "complaint_cases.owner_user_id": "internal",
    "complaint_cases.owner_team": "internal",
    "complaint_cases.tier": "internal",
    "complaint_cases.status": "internal",
    # The operator's own role and assurance level on a decision is
    # authorisation metadata. It is what makes a decision auditable, so it must
    # not be redacted away from an auditor reading the decision history.
    "complaint_decisions.decided_by_role": "internal",
    "complaint_decisions.step_up_level": "internal",
    "complaint_events.actor_role": "internal",
    # How a customer rated the outcome is behavioural, and it is the one number
    # in this family that belongs in a bulk export.
    "complaint_cases.satisfaction_score": "behavioral",
    "complaint_cases.reopened_count": "behavioral",
    "complaint_signal_weights.signal_id": "internal",
    "complaint_signal_observations.signal_id": "internal",
    "complaint_improvement_proposals.recommended_action": "content",
    "complaint_improvement_proposals.cluster_key": "internal",
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
    # Complaint vocabularies. Each is declared here rather than inline so a
    # second complaint column added later inherits the same accepted spellings
    # for free -- which is the whole reason `vocabulary` exists.
    "complaint_status": {
        "new": "open",
        "reopened": "open",
        "done": "closed",
        "resolved_closed": "closed",
        "cancelled": "withdrawn",
        "canceled": "withdrawn",
    },
    "complaint_severity": {
        "med": "medium",
        "minor": "low",
        "major": "high",
        "severe": "critical",
        "sev1": "critical",
        "sev2": "high",
    },
    "complaint_tier": {
        "l1": "tier_1",
        "l2": "tier_2",
        "l3": "tier_3",
        "level1": "tier_1",
        "level2": "tier_2",
        "level3": "tier_3",
        "senior": "executive",
    },
    "complaint_category": {
        "quality": "service_quality",
        "billing_dispute": "billing",
        "data_protection": "privacy",
        "booking": "booking_failure",
        "misc": "other",
    },
    "complaint_decision": {
        "keep": "hold",
        "accept": "escalate",
        "reject": "decline",
        "close": "resolve",
        "assign_owner": "assign",
    },
    "complaint_decision_outcome": {
        "auto": "auto_applied",
        "rejected": "declined",
        "reversed": "superseded",
        "accepted": "applied",
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
    "user_consent_events.purpose": {
        "enum": None,
        "values": (
            "service",
            "recovery",
            "analytics",
            "marketing",
            "personalization",
            "third_party_sharing",
        ),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"promotions": "marketing", "third_party": "third_party_sharing"},
        "note": (
            "the vocabulary is CONSENT_PURPOSES in services/preferences.py; the "
            "column has a non-empty CHECK but not a membership one, so a purpose "
            "added to the config is writable before the DDL catches up"
        ),
    },
    # --- Complaints ------------------------------------------------------------
    # Every one of these is config-driven from `app/services/complaints.py`.
    # They are declared here for the same reason `user_consent_events.purpose`
    # is: a value that cannot be spelled correctly becomes a row no `filter()`
    # ever finds again. None of them is DB-enforced, so adding a row to the
    # config does not need a migration.
    "complaint_cases.status": {
        "enum": None,
        "values": (
            "open",
            "acknowledged",
            "in_progress",
            "escalated",
            "resolved",
            "closed",
            "withdrawn",
        ),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"new": "open", "reopened": "open", "done": "closed", "cancelled": "withdrawn"},
        "vocabulary": "complaint_status",
        "note": (
            "the vocabulary is COMPLAINT_STATUSES in services/complaints.py; "
            "'open' is reachable again by way of reopened_count, so a case that "
            "came back is open-with-a-history, not a distinct status"
        ),
    },
    "complaint_cases.severity": {
        "enum": None,
        "values": ("low", "medium", "high", "critical"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"med": "medium", "severe": "critical", "minor": "low", "major": "high"},
        "vocabulary": "complaint_severity",
        "note": (
            "the same four bands as retention_snapshots.churn_risk and "
            "model_bases.SEVERITY_BANDS, kept as its own vocabulary so a "
            "complaint can be severe while a customer is low churn risk"
        ),
    },
    "complaint_cases.tier": {
        "enum": None,
        "values": ("tier_1", "tier_2", "tier_3", "executive"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"l1": "tier_1", "l2": "tier_2", "l3": "tier_3", "senior": "executive"},
        "vocabulary": "complaint_tier",
        "note": "the vocabulary is ESCALATION_TIERS in services/complaints.py, which is also the routing table",
    },
    "complaint_cases.category": {
        "enum": None,
        "values": (
            "service_quality",
            "billing",
            "refund",
            "privacy",
            "booking_failure",
            "communication",
            "access_tier",
            "other",
        ),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"billing_dispute": "billing", "data_protection": "privacy", "quality": "service_quality"},
        "vocabulary": "complaint_category",
        "note": "the vocabulary is COMPLAINT_CATEGORIES in services/complaints.py",
    },
    "complaint_cases.resolution_code": {
        "enum": None,
        "values": (
            "refunded",
            "credited",
            "corrected",
            "explained",
            "apology_offered",
            "no_action_required",
            "withdrawn_by_complainant",
            "unresolved",
        ),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"refund": "refunded", "goodwill": "credited"},
        "note": (
            "blank while a case is open, which is the point: it is the "
            "categorical answer to 'what did we actually do', and it is the "
            "field a precedent is weighted on"
        ),
    },
    "complaint_events.event_type": {
        "enum": None,
        "values": (
            "opened",
            "acknowledged",
            "assigned",
            "escalated",
            "de_escalated",
            "note_added",
            "status_changed",
            "resolved",
            "closed",
            "reopened",
            "withdrawn",
            "sla_breached",
            "auto_escalated",
        ),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"comment": "note_added", "note": "note_added", "reopen": "reopened"},
        "note": (
            "note_added carries no status or tier change, which is what makes "
            "this a timeline rather than a second, sparser state machine"
        ),
    },
    "complaint_decisions.decision": {
        "enum": None,
        "values": (
            "escalate",
            "hold",
            "decline",
            "resolve",
            "assign",
            "reopen",
            "waive_guard",
            "offer_goodwill",
        ),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"keep": "hold", "accept": "escalate", "reject": "decline", "close": "resolve"},
        "vocabulary": "complaint_decision",
        "note": "the vocabulary is COMPLAINT_DECISIONS in services/complaints.py",
    },
    "complaint_decisions.outcome": {
        "enum": None,
        "values": ("proposed", "applied", "auto_applied", "overridden", "declined", "superseded"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"auto": "auto_applied", "rejected": "declined", "reversed": "superseded"},
        "vocabulary": "complaint_decision_outcome",
        "note": (
            "'proposed' is a recommendation nobody acted on and it is a real "
            "outcome, not a pending state -- the row is never updated to say so, "
            "because the next decision supersedes it instead"
        ),
    },
    "complaint_signal_observations.loyalty_outcome": {
        "enum": None,
        "values": ("", "retained", "re-engaged", "neutral", "dormant", "churned"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"stayed": "retained", "returned": "re-engaged", "left": "churned"},
        "note": (
            "the vocabulary is LOYALTY_OUTCOMES in services/complaint_learning.py. "
            "It is deliberately NOT complaint lifecycle vocabulary: 'resolved' is "
            "a complaint outcome, 'retained' is a loyalty one, and conflating them "
            "is how a system ends up confident about resolutions the customer "
            "walked away from"
        ),
    },
    "complaint_improvement_proposals.status": {
        "enum": None,
        "values": ("detected", "published", "acknowledged", "dismissed"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"new": "detected", "open": "detected", "rejected": "dismissed"},
        "note": (
            "a proposal is 'dismissed' by a human, and dismissal is recorded "
            "rather than achieved by the row disappearing, so the same cluster can "
            "be re-detected without the decision being silently reversed"
        ),
    },
    "complaint_decisions.outcome_observed": {
        "enum": None,
        "values": ("", "held", "resolved", "reopened", "escalated_further", "complainant_left", "no_contact"),
        "enforced": False,
        "enforced_by": None,
        "aliases": {"churned": "complainant_left", "left": "complainant_left"},
        "note": (
            "empty until the case reaches a terminal state; this is the column "
            "that decides whether a precedent is treated as sound, so it is "
            "indexed and never collapsed into the decision's own outcome"
        ),
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
    "customer_offers": {
        "write_mode": "mutable",
        "retention_days": 2555,
        "owner": "services/customer_offers.py",
        "note": (
            "mutable because an offer has a current state a customer reads "
            "('still open for you'), while the trail in customer_offer_events is "
            "append-only so how it got there stays answerable. NOT a ledger, so it "
            "unlike points_transactions carries no signed delta and takes a "
            "non-negative discount check"
        ),
    },
    "customer_offer_events": {
        "write_mode": "append_only",
        "retention_days": 2555,
        "owner": "services/customer_offers.py",
        "note": (
            "append-only: a goodwill credit that was offered, declined and "
            "re-offered has to be provable, and overwriting the offer row to "
            "record the latest status would erase exactly that. kind is the verb "
            "rather than the resulting status, so 'accepted' and 'fulfilled' -- "
            "two events, one status transition chain -- read the same way"
        ),
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
    "complaint_cases": {
        "write_mode": MUTABLE,
        "retention_days": 2555,
        "owner": "services/complaints.py",
        "note": (
            "the only complaint table that is updated in place, because a case is a "
            "live state machine. Its history is the two append-only tables below; "
            "the row itself is the current state and nothing more. 2555 days "
            "(seven years) is the retention a regulator may expect for a "
            "complaint record, not a number anything here enforces"
        ),
    },
    "complaint_events": {
        "write_mode": APPEND_ONLY,
        "retention_days": 2555,
        "owner": "services/complaints.py",
        "note": (
            "the case timeline. A correction is a new row, never an edit, so the "
            "sequence of what an operator did stays reconstructable"
        ),
    },
    "complaint_decisions": {
        "write_mode": APPEND_ONLY,
        "retention_days": 2555,
        "owner": "services/complaints.py",
        "note": (
            "the institutional memory. outcome_observed is the one column written "
            "after the fact, once the case reaches a terminal state -- an append "
            "only table can still learn something, it just cannot rewrite it"
        ),
    },
    "complaint_signal_weights": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "services/complaint_learning.py",
        "note": (
            "one row per learned signal weight. Mutable because a weight is a "
            "current belief, not a record of what was believed; the history of "
            "beliefs lives in complaint_signal_observations, which is why this "
            "table can be rewritten wholesale without losing anything"
        ),
    },
    "complaint_signal_observations": {
        "write_mode": APPEND_ONLY,
        "retention_days": 2555,
        "owner": "services/complaint_learning.py",
        "note": (
            "the learning set. weight_snapshot freezes what the operator saw, so "
            "a later re-weighting cannot rewrite history. superseded marks a row "
            "re-observed rather than deleted, which is how a re-observation is "
            "prevented from double-counting"
        ),
    },
    "complaint_improvement_proposals": {
        "write_mode": MUTABLE,
        "retention_days": 2555,
        "owner": "services/complaint_learning.py",
        "note": (
            "proposal_id is a deterministic hash of the cluster identity, so the "
            "same evidence always yields the same id and a dismissed suggestion "
            "does not reappear on the next sweep. Status and candidate_id change "
            "as it moves; the evidence does not"
        ),
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
    "user_preference_profiles": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "services/preferences.py",
        "note": (
            "unique on user_id, so the database does enforce one profile per user. "
            "Both value columns are TEXT holding a JSON object because the key space "
            "is config-driven (PREFERENCE_CATALOG), so adding a preference is a data "
            "edit rather than a migration"
        ),
    },
    "user_consent_events": {
        "write_mode": APPEND_ONLY,
        "retention_days": 2555,
        "owner": "services/preferences.py",
        "note": (
            "the consent trail is append-only while the profile row it mirrors is "
            "overwritten on every edit: consent has to be provable after the fact, "
            "so a grant and a later revocation are two rows rather than one current "
            "value. purpose is checked non-empty but its vocabulary is config-driven, "
            "so it is deliberately not in ENUM_FIELD_SPECS"
        ),
    },
    "auth_challenges": {
        "write_mode": MUTABLE,
        "retention_days": 30,
        "owner": "routers/users.py",
        "note": (
            "mutable because the attempt counter and consumed_at are the state that "
            "makes a one-time code safe: they have to survive a restart, so they "
            "cannot live in a cache. 30 days is well past the 10-minute TTL, so every "
            "row here is already expired when it is swept -- the retention window "
            "exists for incident review, not for the code to still work. purged by "
            "expires_at rather than by age"
        ),
    },
    "auth_devices": {
        "write_mode": MUTABLE,
        "retention_days": 400,
        "owner": "services/device_recognition.py",
        "note": (
            "mutable because last_seen_at/use_count/usual_hours are observations that "
            "accumulate, and trust is withdrawn by setting revoked_at rather than by "
            "deleting the row -- a revoked device that can still be counted is how a "
            "recogniser learns that it was right. 400 days is chosen so a dormant "
            "device falls out of the recognition set entirely rather than being "
            "remembered indefinitely"
        ),
    },
    "user_identities": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "routers/users.py",
        "note": (
            "no retention: an identifier is the means of recovery, so sweeping it on "
            "a timer would remove the only way back into an account. Revocation is a "
            "revoked_at, and that row is kept for the same reason. The unique "
            "constraint on (provider, identifier_hash) is the actual guarantee that "
            "one address cannot answer for two accounts -- not the lifecycle rule"
        ),
    },
    "storage_connections": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "services/storage_providers.py",
        "note": (
            "no retention, and no sweep of revoked rows: a revoked connection still "
            "records that the user once granted this provider this scope, and that "
            "record is what an operator needs when a leak is investigated. The "
            "ciphertext columns are overwritten with '' on revocation so the row is "
            "kept and the credential is not"
        ),
    },
    "ai_provider_credentials": {
        "write_mode": MUTABLE,
        "retention_days": None,
        "owner": "services/ai_providers.py",
        "note": (
            "one row per provider, replaced on rotation rather than versioned: the "
            "ciphertext is a live credential, not a record of authorisation, so there "
            "is nothing an old row could prove. no retention, because the row IS the "
            "credential -- an operator who wants it gone clears the secret, which is "
            "the revocation. The unique constraint on provider is the guarantee that "
            "two rotations cannot leave two live secrets for one service"
        ),
    },
    "ai_model_health": {
        "write_mode": MUTABLE,
        "retention_days": 30,
        "owner": "services/ai_providers.py",
        "note": (
            "the failover state, persisted so a quota that ended does not have to be "
            "discovered again on every restart or every pod of a rolling deploy. "
            "mutable because a cooldown is overwritten on the next failure and cleared "
            "on the next success. 30 days is longer than any cooldown the module "
            "assigns, so a row that survives to the sweep is a provider nobody has "
            "tried since -- which is itself worth keeping long enough to notice"
        ),
    },
}

#: Float columns with no non-negative check *on purpose*. Listed so that
#: `check_constraint_coverage()` can tell a documented exception from an
#: oversight instead of reporting both.
SIGNED_QUANTITY_COLUMNS: dict[str, str] = {
    "points_transactions.points_delta": "a ledger needs negative rows for spends",
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
    # Two vocabularies share this column name. `booking_events.event_type` holds
    # lifecycle events extended by the state machine; `complaint_events.event_type`
    # comes from COMPLAINT_EVENT_TYPES, which is config-driven. Neither is a
    # closed set, so neither is declared as an enum -- and because a dict has one
    # value per key, both descriptions have to live here or one of them is
    # silently lost. They used to be two separate entries, and the complaint one
    # won.
    "offer_kind": (
        "goodwill | waiver | priority, declared by CUSTOMER_OFFER_KINDS in "
        "services/customer_offers.py. Open rather than an enum so a fourth kind "
        "is a config row, matching how RECOVERY_SAVE_INCENTIVES and "
        "ARREARS_WAIVER_POLICY already extend without a migration"
    ),
    "status": (
        "several independent vocabularies share this column name: booking "
        "lifecycle, arrears settlement, and now offer status, declared by "
        "OFFER_STATUSES. All open; none is a database enum"
    ),
    "source_offer_id": (
        "names a row in whichever upstream catalogue produced the offer -- a "
        "RECOVERY_SAVE_INCENTIVES offer_id, an ARREARS_WAIVER_POLICY waiver_type, "
        "or a RECOVERY_REVIEW_RULES rule_id. Three vocabularies in one column, so "
        "it cannot be an enum and must be read with offer_kind"
    ),
    "decline_reason": (
        "free text by design: the customer is explaining themselves and a "
        "controlled vocabulary would lose the only informative part of it"
    ),
    "event_type": (
        "two vocabularies share this column name: booking lifecycle events "
        "(extended by the state machine) and complaint events, which come from "
        "COMPLAINT_EVENT_TYPES in services/complaints.py. Both are open, so "
        "neither is declared as an enum"
    ),
    "action": "recovery playbook actions come from PLAYBOOKS, which is config-driven",
    "area": "policy areas come from the area catalog, which is config-driven",
    "snapshot_type": "retention snapshot kinds, extended by services/retention.py",
    "point_type": "derived from the exchange-rule config table, so it is open by construction",
    "profile_id": "communication profiles are config-driven",
    "playbook_id": "recovery playbooks are config-driven",
    "preference_key": "preference keys come from PREFERENCE_CATALOG, which is config-driven",
    "purpose": "consent purposes come from CONSENT_PURPOSES, which is config-driven",
    "category": "complaint categories come from COMPLAINT_CATEGORIES, which is config-driven",
    "tier": "escalation tiers come from ESCALATION_TIERS, which is config-driven",
    "resolution_code": "complaint resolution codes come from COMPLAINT_RESOLUTION_CODES, which is config-driven",
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
    "customer_offers.issued_by_id": {
        "severity": "out_of_band",
        "reason": (
            "an offer can be issued by the recovery sweep or by an integration "
            "rather than by a person, and the sweep is identified by name in "
            "`actor` -- pointing this at users.id would reject every automated "
            "issuance, which is the majority of them"
        ),
    },
    "customer_offers.arrears_entry_id": {
        "severity": "out_of_band",
        "reason": (
            "a waiver offer can be made for a principal with no arrears entry "
            "written yet -- a fee that is about to be charged, or a quote that "
            "pre-dates the row. The offer records what was offered, not a "
            "settlement, so requiring the row to exist would refuse the offer at "
            "the moment it is most useful"
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
    "user_consent_events.purpose": {
        "severity": "out_of_band",
        "reason": (
            "a reference into the CONSENT_PURPOSES config table, not a row. A "
            "purpose retired from the config leaves a historical id that is still "
            "the correct record of what was consented to at the time"
        ),
    },
    "user_consent_events.recorded_by_id": {
        "severity": "out_of_band",
        "reason": (
            "nullable and deliberately not a foreign key: most consent changes are "
            "self-service and record the user as both subject and actor, and a "
            "foreign key to users.id would also have to tolerate the same row, "
            "which it would"
        ),
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
    "complaint_cases.owner_user_id": {
        "severity": "out_of_band",
        "reason": (
            "nullable, and the same reasoning as "
            "communication_overrides.set_by_admin_id: a complaint may be owned by an "
            "external or already-deleted operator, and a foreign key to users.id "
            "would make the case unassignable to them. A queue query that assumes "
            "every owner is a live user row is wrong"
        ),
    },
    "complaint_events.actor_user_id": {
        "severity": "out_of_band",
        "reason": (
            "nullable because the background escalation sweep and the engine's own "
            "auto-escalation write rows with no human actor; the pairing of "
            "actor_user_id with actor_role is what makes 'the clock did it' "
            "distinguishable from 'an admin did it'"
        ),
    },
    "complaint_decisions.decided_by_id": {
        "severity": "out_of_band",
        "reason": (
            "nullable and deliberately not a foreign key, for the same reason as "
            "complaint_cases.owner_user_id; decided_by_role and step_up_level carry "
            "the authority the id alone cannot prove"
        ),
    },
    "complaint_cases.category": {
        "severity": "out_of_band",
        "reason": "a reference into the COMPLAINT_CATEGORIES config table, not a row",
    },
    "complaint_signal_observations.signal_id": {
        "severity": "out_of_band",
        "reason": (
            "a reference into the LOYALTY_SIGNALS config table, not a row. A signal "
            "retired from the config leaves a historical id that is still the "
            "correct record of what was measured at the time"
        ),
    },
    "complaint_signal_weights.signal_id": {
        "severity": "out_of_band",
        "reason": "a reference into the LOYALTY_SIGNALS config table, not a row",
    },
    "complaint_cases.tier": {
        "severity": "out_of_band",
        "reason": (
            "a reference into the ESCALATION_TIERS config table, which is also the "
            "routing table; a tier retired from the config leaves a historical id "
            "that is still the correct record of where a case was handled"
        ),
    },
    "complaint_cases.escalation_trigger_id": {
        "severity": "out_of_band",
        "reason": (
            "a reference into the ESCALATION_TRIGGERS config table, not a row. The "
            "column being indexed and non-empty is deliberate: 'which configured "
            "rule fired' is the first question asked about any escalation"
        ),
    },
    "complaint_cases.resolution_code": {
        "severity": "out_of_band",
        "reason": "a reference into the COMPLAINT_RESOLUTION_CODES config table, not a row",
    },
    "complaint_events.event_type": {
        "severity": "out_of_band",
        "reason": "a reference into the COMPLAINT_EVENT_TYPES config table, not a row",
    },
    "complaint_decisions.decision": {
        "severity": "out_of_band",
        "reason": "a reference into the COMPLAINT_DECISIONS config table, not a row",
    },
    "complaint_decisions.outcome": {
        "severity": "out_of_band",
        "reason": "a reference into the COMPLAINT_DECISION_OUTCOMES config table, not a row",
    },
    "complaint_decisions.outcome_observed": {
        "severity": "out_of_band",
        "reason": (
            "a reference into the COMPLAINT_OUTCOMES_OBSERVED config table. It "
            "carries the empty string as a first-class value meaning 'not known "
            "yet', which is why it is not simply nullable"
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
            # `synchronize_pairs` yields (remote_column, local_column) 2-tuples.
            #
            # This block has been broken in two independent ways, and both were
            # hidden by a bare `except Exception`, so `join_columns` was an empty
            # list for every relationship in the catalog:
            #   1. it unpacked three names from a 2-tuple -> ValueError, always;
            #   2. it then referenced `parent`, which is never assigned in this
            #      function -> NameError.
            # No try/except now, deliberately: this is deterministic mapper
            # introspection, verified against all 27 relationships, so a future
            # breakage should be loud rather than silently reporting an empty
            # column list again.
            pairs = [
                {
                    "local": f"{table_name}.{local.name}",
                    "remote": f"{remote.table.name}.{remote.name}",
                }
                for remote, local in prop.synchronize_pairs
            ]
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


# --- Enum field report --------------------------------------------------------


def enum_field_id(table_name: str, column_name: str) -> str | None:
    """The key in ``ENUM_FIELD_SPECS`` for ``table.column``, or ``None``."""
    key = f"{table_name}.{column_name}"
    return key if key in ENUM_FIELD_SPECS else None


def normalise_enum_value(table_name: str, column_name: str, value: Any) -> Any:
    """Map ``value`` through the declared aliases for this enum field.

    If the field has no spec, or the value is not an alias, the original value
    is returned. A `None` value is returned unchanged.
    """
    if value is None:
        return None
    spec = ENUM_FIELD_SPECS.get(f"{table_name}.{column_name}")
    if spec is None:
        return value
    aliases = spec.get("aliases", {})
    return aliases.get(str(value), value)


def enum_field_report() -> dict[str, Any]:
    """All enum-ish string columns: which are enforced and which are not.

    ``enforced`` is the only boolean a downstream caller should gate on. The
    rest is context for a human deciding whether to add a native enum (migration)
    or to tolerate the drift.
    """
    enforced: list[dict[str, Any]] = []
    unenforced: list[dict[str, Any]] = []
    for key, spec in sorted(ENUM_FIELD_SPECS.items()):
        table_name, column_name = key.split(".", 1)
        table = get_table(table_name)
        column = None if table is None else table.columns.get(column_name)
        column_type = str(column.type) if column is not None else "unknown"
        row = {
            "table": table_name,
            "column": column_name,
            "type": column_type,
            "enforced": bool(spec["enforced"]),
            "enforced_by": spec.get("enforced_by"),
            "values": list(spec["values"]),
            "aliases": dict(spec.get("aliases", {})),
            "note": spec.get("note", ""),
        }
        if spec["enforced"]:
            enforced.append(row)
        else:
            unenforced.append(row)
    return {
        "enforced": enforced,
        "unenforced": unenforced,
        "enforced_count": len(enforced),
        "unenforced_count": len(unenforced),
        "note": (
            "enforced columns use a native SQLAlchemy SAEnum (database constraint); "
            "unenforced columns are VARCHAR with a declared vocabulary and aliases. "
            "A value outside the declared set is still accepted by the database."
        ),
    }


# --- Check constraint coverage ------------------------------------------------


def _is_numeric_column(column: Any) -> bool:
    column_type = column.type
    return isinstance(column_type, (Integer, Float))


def _has_nonneg_check(table: Any, column: Any) -> bool:
    """True when the table has a named CheckConstraint on this column with >= 0."""
    col_name = column.name
    for constraint in table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        sql = str(constraint.sqltext).lower()
        if col_name in sql and (">= 0" in sql or "> 0" in sql):
            return True
    return False


def check_constraint_coverage() -> dict[str, Any]:
    """Numeric columns and whether they have a non-negative check constraint.

    Float columns without a check are the ones that can silently go negative.
    ``SIGNED_QUANTITY_COLUMNS`` are documented exceptions.
    """
    covered: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is None:
            continue
        for column in table.columns:
            if not _is_numeric_column(column):
                continue
            key = f"{table_name}.{column.name}"
            has_check = _has_nonneg_check(table, column)
            row = {
                "table": table_name,
                "column": column.name,
                "type": str(column.type),
                "has_nonneg_check": has_check,
                "signed_quantity_exception": key in SIGNED_QUANTITY_COLUMNS,
            }
            if has_check or key in SIGNED_QUANTITY_COLUMNS:
                covered.append(row)
            else:
                uncovered.append(row)
    return {
        "covered": covered,
        "uncovered": uncovered,
        "covered_count": len(covered),
        "uncovered_count": len(uncovered),
        "signed_quantity_exceptions": dict(SIGNED_QUANTITY_COLUMNS),
        "note": (
            "uncovered numeric columns have no non-negative check in the schema; "
            "SIGNED_QUANTITY_COLUMNS are intentional exceptions (ledger deltas, etc.). "
            "Adding a check is new DDL and therefore a migration."
        ),
    }


# --- Referential integrity gaps -----------------------------------------------


def referential_integrity_gaps() -> dict[str, Any]:
    """Columns that look like a foreign key but carry no foreign key constraint.

    Criteria: integer column, indexed, name ends in ``_id``, no ``ForeignKey``
    attached. ``UNCONSTRAINED_REFERENCE_COLUMNS`` are documented exceptions.
    """
    gaps: list[dict[str, Any]] = []
    for table_name in all_table_names():
        table = get_table(table_name)
        if table is None:
            continue
        for column in table.columns:
            if not isinstance(column.type, Integer):
                continue
            if not column.index:
                continue
            if not str(column.name).endswith("_id"):
                continue
            if column.foreign_keys:
                continue
            key = f"{table_name}.{column.name}"
            gaps.append(
                {
                    "table": table_name,
                    "column": column.name,
                    "type": str(column.type),
                    "indexed": bool(column.index),
                    "documented_exception": key in UNCONSTRAINED_REFERENCE_COLUMNS,
                    "exception_note": UNCONSTRAINED_REFERENCE_COLUMNS.get(key, ""),
                }
            )
    return {
        "gaps": sorted(gaps, key=lambda g: (g["table"], g["column"])),
        "gap_count": len(gaps),
        "documented_exceptions": dict(UNCONSTRAINED_REFERENCE_COLUMNS),
        "note": (
            "these columns can point at rows that do not exist; adding a foreign key "
            "would be new DDL (migration). The documented exceptions are the ones "
            "the code already tolerates without a constraint."
        ),
    }


# --- Serialization / projection -----------------------------------------------


def _is_sensitive_for_projection(
    sensitivity: str,
    projection: str,
) -> tuple[bool, str]:
    """Whether a column of this sensitivity is affected by this projection mode.

    Returns ``(affected, mode)`` where mode is one of ``omit``, ``redact``,
    ``include``. ``projection`` is one of ``public``, ``admin``, ``internal``,
    ``forensic``.
    """
    cls = SENSITIVITY_CLASSES.get(sensitivity, SENSITIVITY_CLASSES["internal"])
    if projection == "public":
        # Only columns marked `public` survive a public projection; everything
        # else is at least redacted.
        if sensitivity == "public":
            return False, "include"
        return True, cls.get("default_projection", "redact")
    if projection == "admin":
        # Admin sees everything except credentials, which are always omitted.
        if sensitivity == "credential":
            return True, "omit"
        return False, "include"
    if projection == "internal":
        # Internal sees everything except credentials (omit) and content (redact).
        if sensitivity == "credential":
            return True, "omit"
        if sensitivity == "content":
            return True, "redact"
        return False, "include"
    # forensic: everything, even credentials (redacted, but present)
    if sensitivity == "credential":
        return True, "redact"
    return False, "include"


def model_to_dict(
    instance: Any,
    *,
    projection: str = "internal",
    include_relationships: bool = False,
    skip_null: bool = False,
) -> dict[str, Any]:
    """Serialise an ORM instance to a dict, with sensitivity-driven projection.

    ``projection`` modes:
    * ``public``  -- only ``public`` columns; credentials/financial/content omitted or redacted
    * ``admin``   -- everything except credentials (omitted)
    * ``internal`` -- credentials omitted, content redacted, everything else
    * ``forensic`` -- credentials redacted but present, everything else included

    ``include_relationships`` is ``False`` by default because following
    relationships triggers lazy loads and can easily blow up a response. When
    ``True``, one-to-many are included as lists of their primary keys, and
    many-to-one as the related object's primary key (or ``None``).
    """
    if instance is None:
        return {}
    table = instance.__table__
    out: dict[str, Any] = {}
    for column in table.columns:
        value = getattr(instance, column.name)
        if skip_null and value is None:
            continue
        if column.name.endswith(JSON_COLUMN_SUFFIX):
            container = _json_default_container(column)
            value = loads_json(value, container)
        sensitivity = sensitivity_of(table.name, column.name)
        affected, mode = _is_sensitive_for_projection(sensitivity, projection)
        if affected:
            if mode == "omit":
                continue
            if mode == "redact":
                value = SENSITIVITY_PLACEHOLDER
        out[column.name] = value
    if include_relationships:
        for rel in instance.__mapper__.relationships:
            if rel.direction.name == "MANYTOONE":
                related = getattr(instance, rel.key)
                out[rel.key] = related.id if related is not None else None
            elif rel.direction.name == "ONETOMANY":
                related = getattr(instance, rel.key)
                out[rel.key] = [getattr(obj, "id", None) for obj in related] if related else []
    return out


def project_row(
    row: dict[str, Any],
    table_name: str,
    *,
    projection: str = "internal",
) -> dict[str, Any]:
    """Project an already-materialised row (e.g. from a ``_json`` decode) with the same rules.

    ``row`` is a plain dict of column names to values. The table name is needed
    to look up the per-table sensitivity overrides.
    """
    out: dict[str, Any] = {}
    for column_name, value in row.items():
        sensitivity = sensitivity_of(table_name, column_name)
        affected, mode = _is_sensitive_for_projection(sensitivity, projection)
        if affected:
            if mode == "omit":
                continue
            if mode == "redact":
                value = SENSITIVITY_PLACEHOLDER
        out[column_name] = value
    return out


def redact_row(row: dict[str, Any], table_name: str) -> dict[str, Any]:
    """Convenience: redact everything that is not ``public`` or ``internal``."""
    return project_row(row, table_name, projection="public")


# --- Entity spec registry -----------------------------------------------------
#
# A stable, string-keyed descriptor for every ORM model in this module. The
# keys are the class names; the values are dicts that can be used to build
# forms, tables, validators and documentation without a database session. This
# is *not* the same as ``build_column_catalog()``: the catalog is column-wise;
# the registry is entity-wise and carries the judgments (lifecycle, enum
# fields, sensitivity summary) that a UI or code generator needs.

ENTITY_SPECS: dict[str, dict[str, Any]] = {}


def register_entity_spec(cls: type) -> type:
    """Decorator that records a spec for ``cls`` into ``ENTITY_SPECS``."""
    name = cls.__name__
    table = getattr(cls, "__table__", None)
    if table is None:
        raise TypeError(f"{name} has no __table__; not an ORM model")
    columns: list[dict[str, Any]] = []
    for column in table.columns:
        sensitivity = sensitivity_of(table.name, column.name)
        columns.append(
            {
                "name": column.name,
                "type": str(column.type),
                "nullable": bool(column.nullable),
                "primary_key": bool(column.primary_key),
                "indexed": bool(column.index),
                "unique": bool(column.unique),
                "default": _python_default(column),
                "sensitivity": sensitivity,
                "is_json": is_json_column(column.name),
                "enum_field": enum_field_id(table.name, column.name),
            }
        )
    lifecycle = TABLE_LIFECYCLE.get(table.name, {})
    enum_fields = {
        k.split(".", 1)[1]: v
        for k, v in ENUM_FIELD_SPECS.items()
        if k.startswith(f"{table.name}.")
    }
    spec = {
        "name": name,
        "table": table.name,
        "columns": columns,
        "column_count": len(columns),
        "primary_keys": [c["name"] for c in columns if c["primary_key"]],
        "indexed_columns": [c["name"] for c in columns if c["indexed"]],
        "unique_columns": [c["name"] for c in columns if c["unique"]],
        "json_columns": [c["name"] for c in columns if c["is_json"]],
        "enum_fields": enum_fields,
        "lifecycle": lifecycle,
        "sensitivity_summary": {
            level: sum(1 for c in columns if c["sensitivity"] == level)
            for level in SENSITIVITY_CLASSES
        },
    }
    ENTITY_SPECS[name] = spec
    return cls


# Apply the decorator to every model class defined above (import order means
# they are already bound, so we just iterate the classes in this module).
# noqa: E402 -- the decorator sweep below has to run after every model class
# in the module is bound, so it cannot live at the top.
import sys as _sys  # noqa: E402

_this_module = _sys.modules[__name__]
for _name, _obj in list(vars(_this_module).items()):
    if isinstance(_obj, type) and hasattr(_obj, "__table__") and _obj is not _this_module.Base:
        register_entity_spec(_obj)


def build_entity_registry() -> dict[str, Any]:
    """The full entity registry, plus cross-cutting reports.

    This is what ``/meta/entity-registry`` returns (if a router exposes it). It
    is a single dict so a consumer can fetch everything in one call.
    """
    return {
        "entities": dict(ENTITY_SPECS),
        "entity_count": len(ENTITY_SPECS),
        "sensitivity_classes": dict(SENSITIVITY_CLASSES),
        "sensitivity_placeholder": SENSITIVITY_PLACEHOLDER,
        "enum_field_report": enum_field_report(),
        "check_constraint_coverage": check_constraint_coverage(),
        "referential_integrity_gaps": referential_integrity_gaps(),
        "table_lifecycle": dict(TABLE_LIFECYCLE),
        "write_modes": list(WRITE_MODES),
        "column_catalog": build_column_catalog(),
        "projection_modes": ("public", "admin", "internal", "forensic"),
        "helpers": [
            "model_to_dict",
            "project_row",
            "redact_row",
            "column_spec",
            "sensitivity_of",
            "sensitive_columns",
            "normalise_enum_value",
            "build_column_catalog",
            "build_entity_registry",
        ],
    }
