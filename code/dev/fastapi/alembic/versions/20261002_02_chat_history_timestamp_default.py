"""give chat_history.timestamp the server default the model has always declared

Revision ID: 0012_chat_history_default
Revises: 0011_transfers
Create Date: 2026-10-02 10:00:00.000000

``chat_history.timestamp`` was ``NOT NULL`` with **no** default in the migration
chain, while ``models.ChatHistory.timestamp`` has always declared
``server_default=func.now()``. Every INSERT that relies on the default -- which
is how the application writes chat rows -- therefore failed:

    null value in column "timestamp" of relation "chat_history"
    violates not-null constraint

This is invisible to the whole suite for two independent reasons, which is why
it survived:

* The suite builds its schema from ``Base.metadata``, which *does* carry the
  default. A ``create_all`` database is correct; only a **migrated** database is
  wrong. So the failure needs a real deployment path to appear at all.
* ``scripts/schema_drift_report.py`` compared table and column *names* and not
  server defaults, so the chain's result reported "in sync" against a model it
  did not actually match.

``0001_initial_schema`` is corrected too, so a database built from base is
right at the source rather than right only after this revision. This revision
exists because ``0001`` has already run everywhere: fixing the base fixes fresh
installs, and only a new revision repairs the ones already deployed. ``0001``'s
own ``downgrade()`` drops the default again so the round trip is symmetric.

``created_at``/``updated_at`` were already correct -- they come from
``TimestampMixin``, which was declared with a default. Only the one hand-written
``timestamp`` column drifted, which is what makes this a one-column fix rather
than a sweep.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012_chat_history_default"
down_revision: Union[str, Sequence[str], None] = "0011_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A server-default change is the one `alter_column` this project's rules
    # permit (alembic/README.md rule 4), and `batch_alter_table` is required so
    # the same revision runs on SQLite, which cannot ALTER in place.
    with op.batch_alter_table("chat_history") as batch:
        batch.alter_column(
            "timestamp",
            existing_type=sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Back to what the chain used to produce, so `downgrade base` leaves the
    # database in the state `0001` describes and the round trip still round-trips.
    with op.batch_alter_table("chat_history") as batch:
        batch.alter_column(
            "timestamp",
            existing_type=sa.DateTime(timezone=True),
            server_default=None,
            existing_nullable=False,
        )
