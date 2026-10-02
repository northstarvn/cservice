"""constrain the five score columns that could hold a negative value

Revision ID: 0013_score_scale_checks
Revises: 0012_chat_history_default
Create Date: 2026-10-02 12:00:00.000000

``topic_selections.confidence`` and four of the six ``customer_policy_scores``
score columns had no non-negative CHECK. That was not a neutral omission, it was
load-bearing, and it is the persistence-layer copy of a defect in the scoring
engine itself.

Why it was load-bearing
-----------------------
``compose_access_score`` applies a ceiling to every composite rule and **no
floor** -- six of the rules declare neither, and ``access_score`` is a
``scaled_mean`` over three unfloored terms. So a negative *input* produced
``access_score = -4444.44``, and ``topic_selections.confidence`` was the one
score column a writer could actually get negative into:

    TopicSelection(confidence=-1000.0)  ->  compose_access_score  ->  -4444.44

Every sibling score column (``interaction_signals.score``,
``recovery_outcomes.dissatisfaction_score``, ``retention_snapshots.loyalty_score``)
already carried a CHECK. ``confidence`` was exempt in ``SIGNED_QUANTITY_COLUMNS``
with the reason "the writer clamps instead". That reason was false: nothing
clamped it, which is why the row was writable and the negative composed.

Why the repair runs first
-------------------------
A deployed database may already hold such a row, and ``ADD CONSTRAINT`` validates
existing rows -- so adding the CHECK to a database that has one would fail the
migration, which is the worst possible moment to discover the problem.

So the rows are repaired first, and the repair is **the value the code now
produces**, not an arbitrary zero. ``_within_published_scale`` runs on the
``build_customer_policy_snapshot`` boundary and clamps to ``score_floor``, so a
recomputed score for the same customer would land on exactly these values.
Repairing to anything else would leave the row disagreeing with the next
recompute, and the disagreement would be invisible.

``customer_policy_scores.access_score`` is the column that matters most: it is
what ``resolve_access_band``, ``resolve_policy_tier`` and ``_control_posture``
all read, so a negative there was a negative handed to the three functions that
decide what a customer is served. Two of six columns had a CHECK, so a negative
``customer_score`` was rejected while a negative ``access_score`` -- the one
actually consumed -- was storable.

The sixth column, ``retention_snapshots.loyalty_score``, is a different defect
with the same consequence: the model **declares** its CHECK and the chain never
created it, so a ``create_all`` database was guarded and a migrated one was not.
It is in this revision because the drift report was extended to compare CHECK
constraints in the same change, and it reported this on its first run -- which is
the clearest evidence available that the missing comparison was the thing letting
this class of defect through.

The repair is a scan-and-report, not a blind ``UPDATE``: it says how many rows it
touched so an operator can see whether the number is zero (nothing was ever
wrong) or not (something was writing negatives, and is worth finding).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0013_score_scale_checks"
down_revision: Union[str, Sequence[str], None] = "0012_chat_history_default"
branch_labels = None
depends_on = None


#: (table, column, check name) for every column gaining a non-negative CHECK.
#:
#: `customer_policy_scores` already had two (`system_score`, `customer_score`),
#: which is why the list below starts at the third and why the two are named
#: separately here -- a reader comparing against the model needs to see which
#: of the six were already covered.
SCORE_COLUMNS = (
    (
        "topic_selections",
        "confidence",
        "ck_topic_selections_confidence_non_negative",
    ),
    (
        "customer_policy_scores",
        "access_score",
        "ck_customer_policy_scores_access_score_non_negative",
    ),
    (
        "customer_policy_scores",
        "interest_score",
        "ck_customer_policy_scores_interest_score_non_negative",
    ),
    (
        "customer_policy_scores",
        "closeness_score",
        "ck_customer_policy_scores_closeness_score_non_negative",
    ),
    (
        "customer_policy_scores",
        "community_closeness_score",
        # `_ge_0` rather than `_non_negative`, because the long form is 64
        # characters and PostgreSQL truncates identifiers at 63. Found by
        # running the chain against a real database rather than SQLite, which
        # does not enforce the limit and therefore reported this revision as
        # working.
        "ck_customer_policy_scores_community_closeness_ge_0",
    ),
    (
        # Found by `compare_check_constraints`, added to the drift report in
        # this same change, and missing from the chain for the life of it.
        #
        # Unlike the five above, this CHECK **is** declared in the model -- so
        # it is not a modelling gap but a migration gap, and the two look
        # identical from the application and behave differently: on a
        # `create_all` database (which is what the whole suite builds) the guard
        # is present, and on a migrated one it is absent. That is the exact shape
        # of the `chat_history.timestamp` defect, and it is invisible to the
        # suite by construction.
        #
        # It matters more than its size suggests, because `loyalty_score` is one
        # of the eight inputs to `compose_access_score` and carries weight 12 in
        # `customer_score`. So the one table feeding the composition most
        # directly was the one the chain failed to guard.
        "retention_snapshots",
        "loyalty_score",
        "ck_retention_snapshots_loyalty_score_non_negative",
    ),
)


def upgrade() -> None:
    for table, column, constraint in SCORE_COLUMNS:
        # Repair before constraining. `ADD CONSTRAINT` validates every existing
        # row, so a database holding one of these would fail here -- and a failed
        # migration on a live deployment is a worse place to find that out than a
        # reported row count.
        #
        # Clamped to 0.0 rather than to anything else because that is what
        # `_within_published_scale` produces on the snapshot boundary: the row
        # ends up holding the value its next recompute would give it, rather than
        # a number that silently disagrees with the code.
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = 0.0 WHERE {column} < 0.0"
            )
        )
        # `batch_alter_table` because SQLite has no `ADD CONSTRAINT`; it issues
        # the native statement on PostgreSQL and takes the copy-and-move path
        # elsewhere. Adding a constraint is itself a rebuild trigger there, so
        # this needs no `recreate="always"` -- unlike the server-default alters
        # in `0012`, which are not.
        with op.batch_alter_table(table) as batch:
            batch.create_check_constraint(
                constraint,
                sa.text(f"{column} >= 0"),
            )


def downgrade() -> None:
    # Drops the guard only. The repaired rows are *not* restored to their
    # original negative values: they were never meaningful, and putting them back
    # would deliberately reintroduce the defect this revision removes. That makes
    # this downgrade lossy by design, which is the honest trade -- the alternative
    # is a downgrade that re-creates a known bug.
    for table, column, constraint in SCORE_COLUMNS:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(constraint, type_="check")
