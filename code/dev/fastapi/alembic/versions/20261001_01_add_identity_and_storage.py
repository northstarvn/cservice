"""add identity, sign-in method and connected storage tables

Revision ID: 0009_identity_storage
Revises: 0008_complaint_learning
Create Date: 2026-10-01 09:00:00.000000

Four tables behind the sign-in surface. None of them stores a credential, a raw
signal, or a plaintext token -- that is the property worth stating before the
column list, because it is what makes a dump of this section insufficient to
impersonate anybody or read anybody's files.

``auth_devices``
    A device observed for a user, with its recognition signals stored as
    peppered HMAC digests rather than the values behind them. ``usual_hours`` is
    a JSON *set* of hours, not a histogram: "how often did you sign in at 03:00"
    is a signal an attacker can manufacture by signing in repeatedly, so
    frequency is not recorded. The CHECK ties ``trusted_at`` and ``token_hash``
    together -- a row that claims trust without holding a credential is exactly
    the row that would make recognition authoritative rather than advisory, so
    the schema refuses it rather than trusting the router.

``auth_challenges``
    Outstanding one-time codes. A table rather than a column on ``users``
    because the state that makes a code safe is the *attempt counter*, and that
    has to survive a process restart -- a cache entry would reset it. The unique
    constraint on ``(user_id, purpose, consumed_at)`` is what makes requesting a
    new code *replace* the old one, so repeated requests cannot widen the set of
    codes in circulation; they only invalidate codes the caller already held.

``user_identities``
    Other identifiers an account can be reached by. Added unverified, and the
    CHECK ``is_primary <= is_verified`` encodes that an unproven identifier can
    neither be primary nor used to sign in. The unique constraint on
    ``(provider, identifier_hash)`` is the guarantee that matters: without it the
    same address could be claimed by two accounts and a login by that address
    would have to pick one. ``(provider, external_id)`` covers external identity
    providers, where NULLs do not collide and so correctly constrain nothing for
    the email/phone case.

``storage_connections``
    A user's own cloud storage, connected under their own authority. Tokens are
    stored as ciphertext with the scheme recorded in the value itself
    (``enc:fernet:`` / ``enc:stream:``) so a change of available libraries
    cannot make stored rows undecryptable. ``requested_scopes_json`` and
    ``scopes_json`` are both present because they are different claims: what was
    asked for, and what was actually granted. A user can approve less than was
    requested, and the connection has to record the grant.

Ordering note: this revision has no dependency on ``0008`` other than the chain
itself, so the four tables could be applied independently of the complaint
learning work that happens to precede it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0009_identity_storage"
down_revision: Union[str, Sequence[str], None] = "0008_complaint_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_devices",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("device_digest", sa.String(length=64), nullable=False),
        sa.Column("network_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("agent_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("language_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("timezone_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("usual_hours", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trusted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trust_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_hash", sa.String(length=255), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=40), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(trusted_at IS NULL) = (token_hash IS NULL)",
            name="ck_auth_devices_trusted_requires_token",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "device_digest", name="uq_auth_devices_user_device"),
    )
    op.create_index("ix_auth_devices_id", "auth_devices", ["id"])
    op.create_index("ix_auth_devices_user_id", "auth_devices", ["user_id"])
    op.create_index("ix_auth_devices_label", "auth_devices", ["label"])
    op.create_index("ix_auth_devices_device_digest", "auth_devices", ["device_digest"])
    op.create_index("ix_auth_devices_network_digest", "auth_devices", ["network_digest"])
    op.create_index("ix_auth_devices_revoked_reason", "auth_devices", ["revoked_reason"])

    op.create_table(
        "auth_challenges",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=40), nullable=False, server_default="login"),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False, server_default="email"),
        sa.Column("device_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "purpose", "consumed_at", name="uq_auth_challenges_live"
        ),
    )
    op.create_index("ix_auth_challenges_id", "auth_challenges", ["id"])
    op.create_index("ix_auth_challenges_user_id", "auth_challenges", ["user_id"])
    op.create_index("ix_auth_challenges_purpose", "auth_challenges", ["purpose"])

    op.create_table(
        "user_identities",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="email"),
        sa.Column("identifier_hash", sa.String(length=64), nullable=False),
        sa.Column("identifier_hint", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        # False rather than 0. Postgres will not coerce an integer default onto a
        # boolean column ("column is of type boolean but default expression is of
        # type integer"), so a `0` here is a migration that only works on SQLite.
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_method", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contact_consent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "source", sa.String(length=40), nullable=False, server_default="self_service"
        ),
        sa.CheckConstraint(
            "(is_primary) <= (is_verified)",
            name="ck_user_identities_primary_requires_verification",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "identifier_hash",
            name="uq_user_identities_provider_identifier",
        ),
        sa.UniqueConstraint(
            "provider", "external_id", name="uq_user_identities_provider_subject"
        ),
        sa.UniqueConstraint(
            "user_id", "identifier_hash", name="uq_user_identities_user_identifier"
        ),
    )
    op.create_index("ix_user_identities_id", "user_identities", ["id"])
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_index("ix_user_identities_provider", "user_identities", ["provider"])
    op.create_index("ix_user_identities_external_id", "user_identities", ["external_id"])
    op.create_index("ix_user_identities_is_primary", "user_identities", ["is_primary"])
    op.create_index("ix_user_identities_is_verified", "user_identities", ["is_verified"])
    op.create_index("ix_user_identities_source", "user_identities", ["source"])

    op.create_table(
        "storage_connections",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("requested_scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "broad_scope_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("code_verifier_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            # 'pending' is the value an OAuth attempt is inserted with, before
            # the customer has reached the provider's consent screen. Leaving it
            # out of the vocabulary makes the constraint reject the first write
            # the connect flow performs.
            "status IN ('pending', 'active', 'revoked', 'expired', 'error')",
            name="ck_storage_connections_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "provider", name="uq_storage_connections_user_provider"
        ),
    )
    op.create_index("ix_storage_connections_id", "storage_connections", ["id"])
    op.create_index("ix_storage_connections_user_id", "storage_connections", ["user_id"])
    op.create_index("ix_storage_connections_provider", "storage_connections", ["provider"])
    op.create_index("ix_storage_connections_status", "storage_connections", ["status"])
    op.create_index("ix_storage_connections_state_hash", "storage_connections", ["state_hash"])
    op.create_index(
        "ix_storage_connections_revoked_reason", "storage_connections", ["revoked_reason"]
    )


def downgrade() -> None:
    # Reverse order of creation, so the foreign keys and the drop sequence stay
    # consistent even though none of these four reference each other.
    op.drop_table("storage_connections")
    op.drop_table("user_identities")
    op.drop_table("auth_challenges")
    op.drop_table("auth_devices")
