"""add booking assignments table

Revision ID: 20260908_01_add_booking_assignments
Revises: 20260907_01_add_recovery_outcomes
Create Date: 2026-09-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260908_01_add_booking_assignments"
down_revision = "20260907_01_add_recovery_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("match_reason", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="suggested"),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="booking-service"),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(op.f("ix_booking_assignments_id"), "booking_assignments", ["id"], unique=False)
    op.create_index(op.f("ix_booking_assignments_booking_id"), "booking_assignments", ["booking_id"], unique=False)
    op.create_index(op.f("ix_booking_assignments_user_id"), "booking_assignments", ["user_id"], unique=False)
    op.create_index(op.f("ix_booking_assignments_room_id"), "booking_assignments", ["room_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_booking_assignments_room_id"), table_name="booking_assignments")
    op.drop_index(op.f("ix_booking_assignments_user_id"), table_name="booking_assignments")
    op.drop_index(op.f("ix_booking_assignments_booking_id"), table_name="booking_assignments")
    op.drop_index(op.f("ix_booking_assignments_id"), table_name="booking_assignments")
    op.drop_table("booking_assignments")