"""add booking events and admin flag

Revision ID: 20260905_01_add_booking_events_and_admin_flag
Revises: 
Create Date: 2026-09-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260905_01_add_booking_events_and_admin_flag"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.alter_column("users", "is_admin", server_default=None)

    op.create_table(
        "booking_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(op.f("ix_booking_events_booking_id"), "booking_events", ["booking_id"], unique=False)
    op.create_index(op.f("ix_booking_events_user_id"), "booking_events", ["user_id"], unique=False)
    op.create_index(op.f("ix_booking_events_event_type"), "booking_events", ["event_type"], unique=False)

    op.add_column(
        "chat_history",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index(op.f("ix_chat_history_user_id"), "chat_history", ["user_id"], unique=False)

    op.create_table(
        "retention_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_type", sa.String(length=50), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("loyalty_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("churn_risk", sa.String(length=20), nullable=False, server_default="low"),
        sa.Column("lifecycle_stage", sa.String(length=50), nullable=False, server_default="new"),
        sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(op.f("ix_retention_snapshots_user_id"), "retention_snapshots", ["user_id"], unique=False)
    op.create_index(op.f("ix_retention_snapshots_snapshot_type"), "retention_snapshots", ["snapshot_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_retention_snapshots_snapshot_type"), table_name="retention_snapshots")
    op.drop_index(op.f("ix_retention_snapshots_user_id"), table_name="retention_snapshots")
    op.drop_table("retention_snapshots")

    op.drop_index(op.f("ix_chat_history_user_id"), table_name="chat_history")
    op.drop_column("chat_history", "user_id")

    op.drop_index(op.f("ix_booking_events_event_type"), table_name="booking_events")
    op.drop_index(op.f("ix_booking_events_user_id"), table_name="booking_events")
    op.drop_index(op.f("ix_booking_events_booking_id"), table_name="booking_events")
    op.drop_table("booking_events")
    op.drop_column("users", "is_admin")
