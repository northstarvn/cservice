"""add customer-to-customer transfers: credit, and third-party debt settlement

Revision ID: 0011_transfers
Revises: 0010_customer_offers
Create Date: 2026-10-02 09:00:00.000000

Two tables for an asymmetric pair of operations that share only the word
"transfer".

``points_transfers``
    A credit moving between two customers. Safe by construction: the total is
    conserved, and the two ledger legs live in ``points_transactions`` as
    ``transfer_out`` / ``transfer_in`` under one shared reference, so a movement
    is auditable from either customer's history alone rather than only from a
    joining table nobody reads.

    ``idempotency_key`` is UNIQUE and is the load-bearing constraint. A balance is
    spent twice by a retry, and a client that timed out cannot tell whether its
    first attempt landed. Without this key the only safe client behaviour is to
    refuse to retry -- which turns every network blip into a lost transfer and
    teaches people not to use the feature.

    ``points > 0`` with the direction in the column names rather than in the sign
    means no arithmetic has to know which way round a movement is.

``arrears_settlements``
    Someone paying a debt they do not owe. Not a transfer of the liability: the
    entry's named debtor was and remains the person who owes it, and
    ``debt_transferred`` is a column defaulting false so the question "did anyone
    move this debt?" is answerable from the data rather than from an absence.

    Two CHECK constraints carry the design rather than the application:

    * ``payout_credit_points = 0``. Awarding loyalty credit for settling
      someone's arrears is a closed laundering loop -- settle a friend's debt,
      collect the credit, send the credit back, and the debt has become spendable
      value from nowhere. Application code asserting it awards nothing is worth
      far less than a database that cannot represent the reward, and this makes a
      future edit that tries to make it configurable fail here.
    * ``amount > 0``, because a zero-amount "settlement" row is indistinguishable
      from a real one when someone later asks who paid what.

    The split across principal / interest / late_fee is snapshotted rather than
    derived, because the entry's waiver flags can change afterwards and what the
    payer was told they were paying has to stay recoverable.

No dependency on the offers revision other than the chain itself: these tables
reference ``users`` and ``arrears_entries`` only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0011_transfers"
# Chained after `0010_customer_offers` rather than branching from `0009`.
# Another thread landed that revision off the same parent, and two revisions
# claiming one parent is a branch -- which the chain test asserts against, and
# which means `alembic upgrade head` has no single answer. Appending keeps one
# linear head.
down_revision: Union[str, Sequence[str], None] = "0010_customer_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "points_transfers",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("from_user_id", sa.Integer(), nullable=False),
        sa.Column("to_user_id", sa.Integer(), nullable=False),
        sa.Column("point_type", sa.String(length=50), nullable=False, server_default="loyalty"),
        # Positive on both sides; direction is in the column names.
        sa.Column("points", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="completed"),
        sa.Column("reason", sa.String(length=40), nullable=False, server_default="points_gift"),
        sa.Column("note", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("sender_balance_after", sa.Float(), nullable=False, server_default="0"),
        sa.Column("receiver_balance_after", sa.Float(), nullable=False, server_default="0"),
        # Defaults false: a transfer that costs the sender nothing and cannot be
        # undone should not be recorded as freely given.
        sa.Column(
            "consent_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_reason", sa.String(length=60), nullable=False, server_default=""),
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
        sa.CheckConstraint("points > 0", name="ck_points_transfers_positive"),
        # Two *different* parties. Both columns are NOT NULL, so this is the
        # remaining property -- a self-transfer creates a ledger pair with no
        # counterparty.
        sa.CheckConstraint(
            "from_user_id <> to_user_id",
            name="ck_points_transfers_two_distinct_parties",
        ),
        sa.ForeignKeyConstraint(["from_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_points_transfers_idempotency"),
    )
    op.create_index("ix_points_transfers_id", "points_transfers", ["id"])
    op.create_index("ix_points_transfers_idempotency_key", "points_transfers", ["idempotency_key"])
    op.create_index("ix_points_transfers_from_user_id", "points_transfers", ["from_user_id"])
    op.create_index("ix_points_transfers_to_user_id", "points_transfers", ["to_user_id"])
    op.create_index("ix_points_transfers_point_type", "points_transfers", ["point_type"])
    op.create_index("ix_points_transfers_status", "points_transfers", ["status"])
    op.create_index("ix_points_transfers_reason", "points_transfers", ["reason"])

    op.create_table(
        "arrears_settlements",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("debtor_user_id", sa.Integer(), nullable=False),
        sa.Column("payer_user_id", sa.Integer(), nullable=False),
        sa.Column("arrears_entry_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("principal_covered", sa.Float(), nullable=False, server_default="0"),
        sa.Column("interest_covered", sa.Float(), nullable=False, server_default="0"),
        sa.Column("late_fee_covered", sa.Float(), nullable=False, server_default="0"),
        sa.Column("debt_transferred", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payout_credit_points", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_third_party", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "payer_consent_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="settled"),
        sa.Column("note", sa.String(length=200), nullable=False, server_default=""),
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
        sa.CheckConstraint("amount > 0", name="ck_arrears_settlements_positive"),
        # The laundering guard, in the schema rather than in Python. Settling
        # someone else's arrears for loyalty credit is a closed loop that turns a
        # debt into spendable points.
        sa.CheckConstraint(
            "payout_credit_points = 0", name="ck_arrears_settlements_no_payout"
        ),
        sa.ForeignKeyConstraint(["debtor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payer_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["arrears_entry_id"], ["arrears_entries.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_arrears_settlements_idempotency"),
    )
    op.create_index("ix_arrears_settlements_id", "arrears_settlements", ["id"])
    op.create_index("ix_arrears_settlements_idempotency_key", "arrears_settlements", ["idempotency_key"])
    op.create_index("ix_arrears_settlements_debtor_user_id", "arrears_settlements", ["debtor_user_id"])
    op.create_index("ix_arrears_settlements_payer_user_id", "arrears_settlements", ["payer_user_id"])
    op.create_index(
        "ix_arrears_settlements_arrears_entry_id", "arrears_settlements", ["arrears_entry_id"]
    )
    op.create_index("ix_arrears_settlements_is_third_party", "arrears_settlements", ["is_third_party"])
    op.create_index("ix_arrears_settlements_status", "arrears_settlements", ["status"])


def downgrade() -> None:
    op.drop_table("arrears_settlements")
    op.drop_table("points_transfers")
