"""add complaint learning: signal weights, observations, improvement proposals

Revision ID: 0008_complaint_learning
Revises: 20260930_01_add_complaint_cases
Create Date: 2026-09-30 13:00:00.000000

Complaints were a case lifecycle with a static escalation table. Nothing in the
schema could say *why* one signal mattered more than another, and nothing could
learn from what actually happened to the customer afterwards. Three tables:

``complaint_signal_weights``
    One row per signal in ``LOYALTY_SIGNALS``, carrying the current learned
    weight, the configured prior it decays toward, a confidence, and an
    observation count. Persisted rather than module state, unlike
    ``risk_evaluator``'s ``_WEIGHT_STATE``: a weight that resets on restart is a
    weight that has learned nothing across a deploy, and one nobody can audit.

``complaint_signal_observations``
    Append-only learning set: which signal contributed to which case, at what
    weight, and the loyalty outcome that followed. ``weight_snapshot`` freezes
    what the operator was shown, so a later re-weighting cannot retroactively
    rewrite the reasoning behind a decision. ``superseded`` marks a re-observed
    row rather than deleting it, which is what stops a second observation pass
    from double-counting the same outcome.

``complaint_improvement_proposals``
    Stacked-complaint suggestions, made durable with a deterministic
    ``proposal_id`` so re-running detection on unchanged evidence yields the same
    id and a dismissed suggestion does not reappear.

The loyalty vocabulary on the observation table is deliberately *not* complaint
lifecycle vocabulary. ``complaint_decisions.outcome_observed`` answers "was it
resolved"; ``complaint_signal_observations.loyalty_outcome`` answers "did the
customer stay". Conflating the two is how a system becomes confident about
resolutions the customer walked away from, which is the single failure this
whole learning path exists to catch.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008_complaint_learning"
down_revision: Union[str, Sequence[str], None] = "0007_sync_remaining_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "complaint_signal_weights",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("signal_id", sa.String(length=60), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("prior_weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("observations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agreements", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disagreements", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("weight >= 0", name="ck_complaint_signal_weights_weight_non_negative"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1",
                           name="ck_complaint_signal_weights_confidence_in_range"),
        sa.CheckConstraint("observations >= 0",
                           name="ck_complaint_signal_weights_observations_non_negative"),
        sa.UniqueConstraint("signal_id", name="uq_complaint_signal_weights_signal"),
    )
    op.create_index(op.f("ix_complaint_signal_weights_id"), "complaint_signal_weights", ["id"], unique=False)
    op.create_index("ix_complaint_signal_weights_signal_id", "complaint_signal_weights", ["signal_id"])

    op.create_table(
        "complaint_signal_observations",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("complaint_id", sa.Integer(), sa.ForeignKey("complaint_cases.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal_id", sa.String(length=60), nullable=False),
        sa.Column("weight_snapshot", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("confidence_snapshot", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("observations_snapshot", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loyalty_outcome", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("loyalty_delta", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("superseded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("observations_snapshot >= 0",
                           name="ck_complaint_signal_observations_count_non_negative"),
    )
    op.create_index(op.f("ix_complaint_signal_observations_id"), "complaint_signal_observations", ["id"], unique=False)
    op.create_index("ix_complaint_signal_observations_complaint_id",
                    "complaint_signal_observations", ["complaint_id"])
    op.create_index("ix_complaint_signal_observations_user_id",
                    "complaint_signal_observations", ["user_id"])
    op.create_index("ix_complaint_signal_observations_signal_id",
                    "complaint_signal_observations", ["signal_id"])
    op.create_index("ix_complaint_signal_observations_loyalty_outcome",
                    "complaint_signal_observations", ["loyalty_outcome"])
    op.create_index("ix_complaint_signal_observations_superseded",
                    "complaint_signal_observations", ["superseded"])
    # The learning pass reads "unobserved cases for this user, newest first";
    # without this it is a scan.
    op.create_index("ix_complaint_signal_observations_complaint_observed",
                    "complaint_signal_observations", ["complaint_id", "superseded"])

    op.create_table(
        "complaint_improvement_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("proposal_id", sa.String(length=80), nullable=False),
        sa.Column("cluster_key", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("component", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("owner_hint", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("priority", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("impact", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("effort", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("rationale_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("recommended_action", sa.Text(), nullable=False, server_default=""),
        sa.Column("signals_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="detected"),
        sa.Column("candidate_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("proposal_id", name="uq_complaint_improvement_proposals_proposal"),
    )
    op.create_index(op.f("ix_complaint_improvement_proposals_id"), "complaint_improvement_proposals", ["id"], unique=False)
    op.create_index("ix_complaint_improvement_proposals_proposal_id",
                    "complaint_improvement_proposals", ["proposal_id"])
    op.create_index("ix_complaint_improvement_proposals_cluster_key",
                    "complaint_improvement_proposals", ["cluster_key"])
    op.create_index("ix_complaint_improvement_proposals_component",
                    "complaint_improvement_proposals", ["component"])
    op.create_index("ix_complaint_improvement_proposals_priority",
                    "complaint_improvement_proposals", ["priority"])
    op.create_index("ix_complaint_improvement_proposals_status",
                    "complaint_improvement_proposals", ["status"])
    op.create_index("ix_complaint_improvement_proposals_candidate_id",
                    "complaint_improvement_proposals", ["candidate_id"])


def downgrade() -> None:
    op.drop_table("complaint_improvement_proposals")
    op.drop_table("complaint_signal_observations")
    op.drop_table("complaint_signal_weights")