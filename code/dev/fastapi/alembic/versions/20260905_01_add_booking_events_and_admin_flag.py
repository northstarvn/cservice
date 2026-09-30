"""add booking events and admin flag

Revision ID: 0002_booking_events
Revises: 0001_initial
Create Date: 2026-09-05 00:00:00.000000

Revision ID: 0002_booking_events
Revises: 0001_initial
Create Date: 2026-09-05 00:00:00.000000

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
revision = "0002_booking_events"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # Backfilling an existing `users` table and then dropping the server default
    # is a two-step dance, because a NOT NULL column has to be added with a
    # default. The second step is `ALTER TABLE ... ALTER COLUMN ... DROP
    # DEFAULT`, which is PostgreSQL syntax and is a syntax error on SQLite:
    #
    #   near "ALTER": syntax error
    #   [SQL: ALTER TABLE users ALTER COLUMN is_admin DROP DEFAULT]
    #
    # `recreate="always"` would fix that on SQLite, and it was used here on the
    # understanding that PostgreSQL would "still get a plain ALTER TABLE". That
    # is not what `recreate="always"` does: it forces a rebuild on *every*
    # dialect, and a rebuild is DROP + CREATE. On PostgreSQL that fails as soon
    # as anything references the table:
    #
    #   asyncpg.exceptions.DependentObjectsStillExistError:
    #     cannot drop constraint users_pkey on table users because other
    #     objects depend on it
    #   DETAIL: constraint bookings_user_id_fkey on table bookings depends on
    #           index users_pkey
    #
    # which is why `alembic upgrade head` against a real database died here. The
    # rebuild is a workaround for a *SQLite* limitation, so it is applied on
    # SQLite only and the native statement is used everywhere else.
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("users", schema=None, recreate=recreate) as batch_op:
        batch_op.alter_column("is_admin", server_default=None)

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

    # Adding a column that carries a REFERENCES clause needs batch mode on
    # SQLite, which cannot attach a foreign key to an existing table:
    #
    #   NotImplementedError: No support for ALTER of constraints in SQLite
    #   dialect
    #
    # `add_column` *is* a rebuild trigger for batch mode, so `recreate="auto"`
    # is enough here -- the copy-and-move path emits the constraint on the new
    # table. On PostgreSQL this stays a plain `ALTER TABLE ... ADD COLUMN`.
    with op.batch_alter_table("chat_history", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "user_id",
                sa.Integer(),
                # Named, because batch mode's copy-and-move path requires it
                # ("Constraint must have a name") and an auto-generated name is
                # not reproducible. Naming it is also what makes the constraint
                # addressable if this migration is ever amended. The matching
                # downgrade drops the column, and the constraint goes with it.
                sa.ForeignKey(
                    "users.id",
                    ondelete="SET NULL",
                    name="fk_chat_history_user_id_users",
                ),
                nullable=True,
            )
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
    # `chat_history.user_id` is part of a foreign-key definition on the table, and
    # SQLite refuses `ALTER TABLE ... DROP COLUMN` for a column that a
    # constraint refers to:
    #
    #   error in table chat_history after drop column: unknown column "user_id"
    #   in foreign key definition
    #
    # `batch_alter_table` is the dialect-aware fix. On SQLite it builds a
    # replacement table, copies the surviving columns, drops the original and
    # renames -- which removes the column together with the constraint that
    # referenced it. On PostgreSQL it degrades to a plain `ALTER TABLE ... DROP
    # COLUMN`, so this stays correct on both.
    with op.batch_alter_table("chat_history", schema=None) as batch_op:
        batch_op.drop_column("user_id")

    op.drop_index(op.f("ix_booking_events_event_type"), table_name="booking_events")
    op.drop_index(op.f("ix_booking_events_user_id"), table_name="booking_events")
    op.drop_index(op.f("ix_booking_events_booking_id"), table_name="booking_events")
    op.drop_table("booking_events")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("is_admin")
