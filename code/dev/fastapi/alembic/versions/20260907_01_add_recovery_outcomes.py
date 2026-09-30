"""add recovery outcomes table

Revision ID: 0003_recovery_outcomes
Revises: 0002_booking_events
Create Date: 2026-09-07 00:00:00.000000

Revision ID: 0003_recovery_outcomes
Revises: 0002_booking_events
Create Date: 2026-09-07 00:00:00.000000

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
revision = "0003_recovery_outcomes"
down_revision = "0002_booking_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recovery_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recovery_readiness", sa.String(length=20), nullable=False),
        sa.Column("dissatisfaction_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("primary_risks_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("recovery_signals_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("escalation_path_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("handoff_outcome_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("follow_up_completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("follow_up_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_plan", sa.Text(), nullable=False, server_default=""),
        sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="chat"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("dissatisfaction_score >= 0", name="ck_recovery_outcomes_dissatisfaction_score_non_negative"),
    )
    op.create_index(op.f("ix_recovery_outcomes_id"), "recovery_outcomes", ["id"], unique=False)
    op.create_index(op.f("ix_recovery_outcomes_user_id"), "recovery_outcomes", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_recovery_outcomes_user_id"), table_name="recovery_outcomes")
    op.drop_index(op.f("ix_recovery_outcomes_id"), table_name="recovery_outcomes")
    op.drop_table("recovery_outcomes")
