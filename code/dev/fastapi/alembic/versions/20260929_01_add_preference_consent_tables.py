"""add preference & consent centre tables

Revision ID: 0005_preference_consent
Revises: 0004_booking_assignments
Create Date: 2026-09-29 00:00:00.000000

Two tables, deliberately split rather than combined:

``user_preference_profiles``
    A mutable singleton per user holding the *current* state -- both value
    columns are JSON blobs, because the preference key space is config-driven
    (``PREFERENCE_CATALOG`` in ``app/services/preferences.py``) and adding a key
    must not be a migration.

``user_consent_events``
    Append-only. A profile row is overwritten on every edit, so it can show the
    current state but not *when* consent was first given or later withdrawn.
    That question is what a consent trail exists to answer, so the history lives
    here as rows that are only ever added.

``purpose`` carries a non-empty CHECK but deliberately not a membership one:
the vocabulary is ``CONSENT_PURPOSES``, and a constraint hard-coded to today's
list would reject a purpose the config already accepts.

Revision ID: 0005_preference_consent
Revises: 0004_booking_assignments
Create Date: 2026-09-29 00:00:00.000000

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
revision = "0005_preference_consent"
down_revision = "0004_booking_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_preference_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("preferences_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("consents_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "consent_version", sa.String(length=50), nullable=False, server_default="1.0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_user_preference_profiles_id"), "user_preference_profiles", ["id"], unique=False
    )
    # At most one profile per user, enforced by the database rather than by a
    # filter in the service.
    op.create_index(
        op.f("ix_user_preference_profiles_user_id"),
        "user_preference_profiles",
        ["user_id"],
        unique=True,
    )

    op.create_table(
        "user_consent_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.String(length=50), nullable=False, server_default="1.0"),
        sa.Column(
            "lawful_basis",
            sa.String(length=30),
            nullable=False,
            server_default="legitimate_interest",
        ),
        # Nullable and deliberately not a foreign key: a self-service consent
        # change records the user as both subject and actor.
        sa.Column("recorded_by_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("purpose <> ''", name="ck_user_consent_events_purpose_required"),
    )
    op.create_index(
        op.f("ix_user_consent_events_id"), "user_consent_events", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_user_consent_events_user_id"),
        "user_consent_events",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_consent_events_purpose"),
        "user_consent_events",
        ["purpose"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_consent_events_recorded_by_id"),
        "user_consent_events",
        ["recorded_by_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_user_consent_events_recorded_by_id"), table_name="user_consent_events"
    )
    op.drop_index(op.f("ix_user_consent_events_purpose"), table_name="user_consent_events")
    op.drop_index(op.f("ix_user_consent_events_user_id"), table_name="user_consent_events")
    op.drop_index(op.f("ix_user_consent_events_id"), table_name="user_consent_events")
    op.drop_table("user_consent_events")

    op.drop_index(
        op.f("ix_user_preference_profiles_user_id"), table_name="user_preference_profiles"
    )
    op.drop_index(op.f("ix_user_preference_profiles_id"), table_name="user_preference_profiles")
    op.drop_table("user_preference_profiles")
