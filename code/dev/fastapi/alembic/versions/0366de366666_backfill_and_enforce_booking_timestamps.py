"""Backfill NULL booking timestamps and enforce NOT NULL + defaults.

Revision ID: 7a2c41b9d810
Revises: <PUT_PREVIOUS_REVISION_ID_HERE>
Create Date: 2025-08-11 09:15:00 UTC
"""
from alembic import op


# Revision identifiers.
revision = "7a2c41b9d810"
down_revision = "<PUT_PREVIOUS_REVISION_ID_HERE>"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Backfill any NULL values.
    op.execute("""
        UPDATE bookings
        SET created_at = COALESCE(created_at, NOW()),
            updated_at = COALESCE(updated_at, NOW())
        WHERE created_at IS NULL OR updated_at IS NULL
    """)
    # 2. Ensure defaults at DB level.
    op.execute("ALTER TABLE bookings ALTER COLUMN created_at SET DEFAULT NOW()")
    op.execute("ALTER TABLE bookings ALTER COLUMN updated_at SET DEFAULT NOW()")
    # 3. Enforce NOT NULL.
    op.execute("ALTER TABLE bookings ALTER COLUMN created_at SET NOT NULL")
    op.execute("ALTER TABLE bookings ALTER COLUMN updated_at SET NOT NULL")


def downgrade():
    # Relax constraints & remove defaults (cannot undo the backfill).
    op.execute("ALTER TABLE bookings ALTER COLUMN created_at DROP NOT NULL")
    op.execute("ALTER TABLE bookings ALTER COLUMN updated_at DROP NOT NULL")
    op.execute("ALTER TABLE bookings ALTER COLUMN created_at DROP DEFAULT")
    op.execute("ALTER TABLE bookings ALTER COLUMN updated_at DROP DEFAULT")