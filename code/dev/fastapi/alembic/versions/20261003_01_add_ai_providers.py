"""add AI provider credential and model-health tables

Revision ID: 0014_ai_providers
Revises: 0013_score_scale_checks
Create Date: 2026-10-03 09:00:00.000000

Two tables behind the external-brain failover, and the reason each exists is
different from the schema it holds.

``ai_provider_credentials``
    A credential an operator supplied at runtime, stored encrypted. The
    environment is how a deployment is configured; this is how a *browser
    session* -- short-lived by nature -- is rotated without a redeploy. The
    auth-mode and status CHECKs live in the database because a typo in either is
    a provider that silently never answers, and silence is the failure mode this
    whole feature exists to remove.

``ai_model_health``
    The cooldown state: which ``(provider, model)`` is out, why, and until when.
    Persisted so a quota that ended an hour ago does not have to be re-discovered
    on every restart or every pod of a rolling deploy. One row per model rather
    than per provider, because a quota is not always provider-wide.

Neither table stores a plaintext secret. ``secret_encrypted`` carries an
``enc:<scheme>:`` prefix so a change in which crypto library is installed cannot
make a stored row undecryptable -- the same scheme the storage connections use,
for the same reason.
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_ai_providers"
down_revision = "0013_score_scale_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("auth_mode", sa.String(length=20), nullable=False, server_default="api_key"),
        sa.Column("secret_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column(
            "credential_source", sa.String(length=20), nullable=False, server_default="database"
        ),
        sa.Column("last_error", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
            "auth_mode IN ('api_key', 'browser_session')",
            name="ck_ai_provider_credentials_auth_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_ai_provider_credentials_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", name="uq_ai_provider_credentials_provider"
        ),
    )
    op.create_index("ix_ai_provider_credentials_id", "ai_provider_credentials", ["id"])
    op.create_index(
        "ix_ai_provider_credentials_provider", "ai_provider_credentials", ["provider"]
    )
    op.create_index(
        "ix_ai_provider_credentials_status", "ai_provider_credentials", ["status"]
    )

    op.create_table(
        "ai_model_health",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("failure_kind", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
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
            "failure_count >= 0", name="ck_ai_model_health_failure_count_non_negative"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "model", name="uq_ai_model_health_provider_model"
        ),
    )
    op.create_index("ix_ai_model_health_id", "ai_model_health", ["id"])
    op.create_index("ix_ai_model_health_provider", "ai_model_health", ["provider"])
    op.create_index("ix_ai_model_health_model", "ai_model_health", ["model"])
    op.create_index("ix_ai_model_health_cooldown_until", "ai_model_health", ["cooldown_until"])


def downgrade() -> None:
    op.drop_table("ai_model_health")
    op.drop_table("ai_provider_credentials")
