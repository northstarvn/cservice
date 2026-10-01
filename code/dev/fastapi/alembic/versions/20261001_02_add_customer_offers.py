"""add customer offers: the mutable row and its append-only trail

Revision ID: 0010_customer_offers
Revises: 0009_identity_storage
Create Date: 2026-10-01 00:00:00.000000

Stage B. ``services/recovery_playbooks.py`` has always known what to offer --
``RECOVERY_SAVE_INCENTIVES`` holds three tiers and
``resolve_recovery_incentive`` composes the right one -- but it says so itself:
the preview carries ``"issued": False`` and the registered action is described as
*issues nothing*. That boundary was correct. Composing an offer and handing it
to a customer are different acts, and the second one needs somewhere to live.

``customer_offers``
    One offer, and where it stands. Mutable, because the customer reads it as
    current state -- "this is still open for you" -- and because a status a
    customer cannot see is not a status.

    The three kinds deliberately do not share one value column. A waiver is
    worth nothing in points and a goodwill credit is worth nothing as a waiver,
    so a single ``value`` field would make ``points > 0`` mean "this offer has
    value" on rows where it means nothing at all. Hence ``points`` /
    ``discount_percent`` for goodwill, ``waiver_type`` / ``arrears_entry_id`` for
    a waiver, and ``priority`` for queue urgency.

    ``generosity_scale`` records how far the investment signal scaled the
    amount, *on the row*, because a scale recomputed at fulfilment time can move
    an amount the customer has already accepted. The same rule must produce the
    same number for as long as the offer is open.

    ``reference`` is rendered from the primary key after the first flush rather
    than generated from a per-process counter. This schema already had that bug
    once: the complaint escalation id was ``ESC-{user_id}-{sequence:03d}`` built
    from a module-level counter, so it reissued its own values after every
    redeploy and two customers ended up holding the same reference. A rendered id
    is stable across restarts and unique by construction.

``customer_offer_events``
    Append-only. The mutable row answers "where is it"; this answers "how did it
    get there and who did what". A goodwill credit that was offered, declined
    and re-offered has to stay provable, and overwriting the offer row to record
    the latest status would erase exactly that.

    ``kind`` is the verb rather than the resulting status. ``accepted`` and
    ``fulfilled`` are two events that a status-only vocabulary would call the
    same thing, and "was this fulfilled or only accepted" is the question an
    operator actually asks when a customer is chasing.

    ``user_id`` is denormalised for the same reason ``complaint_events`` carries
    it: every inbox, backlog and cohort query filters by customer, and the join
    back to the offer is the join this table exists to avoid.

Two deliberate non-constraints, both declared in
``UNCONSTRAINED_REFERENCE_COLUMNS``:

* ``issued_by_id`` is not a foreign key. Most offers are issued by the recovery
  sweep rather than by a person, and the sweep is identified by name in the
  event's ``actor``. Pointing this at ``users.id`` would reject every automated
  issuance, which is the majority of them.
* ``arrears_entry_id`` is not a foreign key either. A waiver can be offered for
  a charge that has not been written yet -- a fee about to be charged, or a
  quote that pre-dates the row. The offer records what was *offered*, not a
  settlement, and requiring the row to exist would refuse the offer at exactly
  the moment it is most useful.

``follow_up_due_at`` is nullable rather than defaulted, and that is a real
distinction rather than tidiness: a priority offer has no fulfilment step, and a
timestamp meaning "never" written as a date would be a lie the scheduler would
eventually act on.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_customer_offers"
down_revision = "0009_identity_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("offer_kind", sa.String(length=30), nullable=False),
        sa.Column("source_offer_id", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="offered"),
        sa.Column("headline", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("points", sa.Float(), nullable=False, server_default="0"),
        sa.Column("discount_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("waiver_type", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("arrears_entry_id", sa.Integer(), nullable=True),
        sa.Column("priority", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("generosity_scale", sa.Float(), nullable=False, server_default="1"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("justification", sa.Text(), nullable=False, server_default=""),
        sa.Column("issued_by_id", sa.Integer(), nullable=True),
        sa.Column("follow_up_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "discount_percent >= 0 AND discount_percent <= 100",
            name="ck_customer_offers_discount_percent_range",
        ),
        sa.CheckConstraint(
            "generosity_scale > 0", name="ck_customer_offers_generosity_positive"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_offers_id", "customer_offers", ["id"])
    op.create_index("ix_customer_offers_user_id", "customer_offers", ["user_id"])
    op.create_index("ix_customer_offers_reference", "customer_offers", ["reference"])
    op.create_index("ix_customer_offers_offer_kind", "customer_offers", ["offer_kind"])
    op.create_index("ix_customer_offers_source_offer_id", "customer_offers", ["source_offer_id"])
    op.create_index("ix_customer_offers_status", "customer_offers", ["status"])
    op.create_index("ix_customer_offers_waiver_type", "customer_offers", ["waiver_type"])
    op.create_index("ix_customer_offers_arrears_entry_id", "customer_offers", ["arrears_entry_id"])
    op.create_index("ix_customer_offers_priority", "customer_offers", ["priority"])
    op.create_index("ix_customer_offers_expires_at", "customer_offers", ["expires_at"])
    op.create_index("ix_customer_offers_issued_by_id", "customer_offers", ["issued_by_id"])
    op.create_index("ix_customer_offers_follow_up_due_at", "customer_offers", ["follow_up_due_at"])

    op.create_table(
        "customer_offer_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("to_status", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["offer_id"], ["customer_offers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_offer_events_id", "customer_offer_events", ["id"])
    op.create_index("ix_customer_offer_events_offer_id", "customer_offer_events", ["offer_id"])
    op.create_index("ix_customer_offer_events_user_id", "customer_offer_events", ["user_id"])
    op.create_index("ix_customer_offer_events_kind", "customer_offer_events", ["kind"])
    op.create_index("ix_customer_offer_events_actor", "customer_offer_events", ["actor"])


def downgrade() -> None:
    op.drop_index("ix_customer_offer_events_actor", table_name="customer_offer_events")
    op.drop_index("ix_customer_offer_events_kind", table_name="customer_offer_events")
    op.drop_index("ix_customer_offer_events_user_id", table_name="customer_offer_events")
    op.drop_index("ix_customer_offer_events_offer_id", table_name="customer_offer_events")
    op.drop_index("ix_customer_offer_events_id", table_name="customer_offer_events")
    op.drop_table("customer_offer_events")

    for name in (
        "follow_up_due_at",
        "issued_by_id",
        "expires_at",
        "priority",
        "arrears_entry_id",
        "waiver_type",
        "source_offer_id",
        "offer_kind",
        "status",
        "reference",
        "user_id",
        "id",
    ):
        op.drop_index(f"ix_customer_offers_{name}", table_name="customer_offers")
    op.drop_table("customer_offers")