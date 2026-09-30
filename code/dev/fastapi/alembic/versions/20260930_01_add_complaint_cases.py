"""add complaint cases, events and decisions

Revision ID: 0006_complaints
Revises: 0005_preference_consent
Create Date: 2026-09-30 00:00:00.000000

The complaint surface had no spine. ``RecoveryOutcome`` carried
``escalation_path_json`` and a hardcoded ``{"status": "open", "owner":
"support"}`` handoff blob, both rewritten on every read, and the only
escalation identifier in the codebase was ``ESC-{user_id}-{sequence:03d}``
built from an in-process counter -- so the first escalation for a user after
a restart produced a reference identical to every earlier one. Three tables
replace that with something that has an identity, a history, and a memory:

``complaint_cases``
    The live state machine. ``reference`` is unique and durable; it is what a
    customer quotes and an agent searches. The SLA columns
    (``response_due_at`` / ``resolution_due_at``) are the clock that
    auto-escalation reads, so they are indexed -- nothing else in the schema
    is polled on a schedule.

``complaint_events``
    Append-only timeline. The case row answers "where is it"; this answers
    "how did it get there and who touched it". ``user_id`` is denormalised on
    purpose: every queue, SLA and workload query filters by customer, and the
    join back to the case is the join this table exists to avoid.

``complaint_decisions``
    Append-only operator decisions with the reasoning that produced them, and
    ``outcome_observed`` recording what the case actually did afterwards. That
    last column is what turns a decision log into institutional memory: a
    decision whose case was later reopened is evidence the precedent was bad,
    and one that held is evidence it was good. Nothing else in this schema can
    express that distinction.

Two deliberate non-constraints, both declared in ``UNCONSTRAINED_REFERENCE_COLUMNS``:

* ``owner_user_id`` / ``decided_by_id`` / ``actor_user_id`` are not foreign
  keys. A complaint may be owned by an external or already-deleted operator,
  and a foreign key would make the case unassignable to them. This is the same
  reasoning already applied to
  ``communication_overrides.set_by_admin_id``.
* The vocabulary columns (``category``, ``severity``, ``status``, ``tier``,
  ``event_type``, ``decision``, ``outcome``) carry no CHECK. Each is
  config-driven from ``app/services/complaints.py``, so a membership
  constraint hard-coded to today's list would reject a value the config
  already accepts -- the failure mode already documented on
  ``user_consent_events.purpose``.

``satisfaction_score`` is the one genuinely bounded column: it is a 0-10 scale
with a real domain, so it gets both ends of a CHECK.

Revision ID: 0006_complaints
Revises: 0005_preference_consent
Create Date: 2026-09-30 00:00:00.000000

The revision id is deliberately short and opaque. `alembic_version.version_num`
is `VARCHAR(32)`, and a descriptive id like
`20260905_01_add_booking_events_and_admin_flag` (45 chars) cannot be written
there at all:

    asyncpg.exceptions.StringDataRightTruncationError:
      value too long for type character varying(32)

so `alembic upgrade` failed the moment it tried to record the revision. The
date and the description live in the filename and this docstring, where they
cost nothing; the id stays inside the column that has to hold it.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0006_complaints"
down_revision = "0005_preference_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "complaint_cases",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reference", sa.String(length=40), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False, server_default="service_quality"),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="open"),
        sa.Column("tier", sa.String(length=30), nullable=False, server_default="tier_1"),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("owner_team", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation_trigger_id", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("auto_escalated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("regulatory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolution_code", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("resolution_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("satisfaction_score", sa.Integer(), nullable=True),
        sa.Column("reopened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("factors_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="chat"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("reopened_count >= 0", name="ck_complaint_cases_reopened_non_negative"),
        sa.CheckConstraint(
            "satisfaction_score IS NULL OR (satisfaction_score >= 0 AND satisfaction_score <= 10)",
            name="ck_complaint_cases_satisfaction_in_range",
        ),
        sa.UniqueConstraint("reference", name="uq_complaint_cases_reference"),
    )
    op.create_index(op.f("ix_complaint_cases_id"), "complaint_cases", ["id"], unique=False)
    op.create_index("ix_complaint_cases_user_id", "complaint_cases", ["user_id"])
    op.create_index("ix_complaint_cases_reference", "complaint_cases", ["reference"])
    op.create_index("ix_complaint_cases_category", "complaint_cases", ["category"])
    op.create_index("ix_complaint_cases_severity", "complaint_cases", ["severity"])
    op.create_index("ix_complaint_cases_status", "complaint_cases", ["status"])
    op.create_index("ix_complaint_cases_tier", "complaint_cases", ["tier"])
    op.create_index("ix_complaint_cases_owner_user_id", "complaint_cases", ["owner_user_id"])
    op.create_index("ix_complaint_cases_owner_team", "complaint_cases", ["owner_team"])
    op.create_index("ix_complaint_cases_response_due_at", "complaint_cases", ["response_due_at"])
    op.create_index("ix_complaint_cases_resolution_due_at", "complaint_cases", ["resolution_due_at"])
    op.create_index("ix_complaint_cases_escalation_trigger_id", "complaint_cases", ["escalation_trigger_id"])
    op.create_index("ix_complaint_cases_auto_escalated", "complaint_cases", ["auto_escalated"])
    op.create_index("ix_complaint_cases_regulatory", "complaint_cases", ["regulatory"])
    op.create_index("ix_complaint_cases_resolution_code", "complaint_cases", ["resolution_code"])
    op.create_index("ix_complaint_cases_source", "complaint_cases", ["source"])

    op.create_table(
        "complaint_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "complaint_id",
            sa.Integer(),
            sa.ForeignKey("complaint_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=50), nullable=False, server_default="opened"),
        sa.Column("from_status", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("to_status", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("from_tier", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("to_tier", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_role", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_complaint_events_complaint_id", "complaint_events", ["complaint_id"])
    op.create_index(op.f("ix_complaint_events_id"), "complaint_events", ["id"], unique=False)
    op.create_index("ix_complaint_events_user_id", "complaint_events", ["user_id"])
    op.create_index("ix_complaint_events_event_type", "complaint_events", ["event_type"])
    op.create_index("ix_complaint_events_actor_user_id", "complaint_events", ["actor_user_id"])

    op.create_table(
        "complaint_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "complaint_id",
            sa.Integer(),
            sa.ForeignKey("complaint_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=60), nullable=False, server_default="escalate"),
        sa.Column("outcome", sa.String(length=30), nullable=False, server_default="proposed"),
        sa.Column("from_tier", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("to_tier", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_by_role", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("step_up_level", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("factors_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("precedent_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("auto_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "superseded_by_id",
            sa.Integer(),
            sa.ForeignKey("complaint_decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("outcome_observed", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("outcome_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_complaint_decisions_complaint_id", "complaint_decisions", ["complaint_id"])
    op.create_index(op.f("ix_complaint_decisions_id"), "complaint_decisions", ["id"], unique=False)
    op.create_index("ix_complaint_decisions_user_id", "complaint_decisions", ["user_id"])
    op.create_index("ix_complaint_decisions_decision", "complaint_decisions", ["decision"])
    op.create_index("ix_complaint_decisions_outcome", "complaint_decisions", ["outcome"])
    op.create_index("ix_complaint_decisions_decided_by_id", "complaint_decisions", ["decided_by_id"])
    op.create_index("ix_complaint_decisions_superseded_by_id", "complaint_decisions", ["superseded_by_id"])
    op.create_index("ix_complaint_decisions_outcome_observed", "complaint_decisions", ["outcome_observed"])


def downgrade() -> None:
    op.drop_table("complaint_decisions")
    op.drop_table("complaint_events")
    op.drop_table("complaint_cases")
