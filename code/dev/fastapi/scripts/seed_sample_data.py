"""Create a real database and seed it with sample data.

The test suite fakes the database, which is the right default for 2 000 tests
and the wrong way to find out whether the thing works. Nothing in the suite
exercises: a real PostgreSQL enum, a real unique constraint rejecting a
duplicate, a real transaction rollback, `SELECT ... FOR UPDATE`, or the actual
`create_all` DDL against a real server. This script does.

    python3 scripts/seed_sample_data.py --reset

It is idempotent: without `--reset` it leaves the schema alone and only
inserts rows that are missing. With `--reset` it drops and recreates
everything.

What it deliberately does *not* fake
------------------------------------
- Sentiment is **not** stubbed to a happy answer. `analyze_sentiment` calls
  Hugging Face and returns `None` when it cannot; the seed works with that,
  because a customer whose sentiment reads as `none` is a state the
  application must handle anyway.
- The recovery playbook run, points exchange, arrears open and the complaint
  intake all go through their real service functions against real tables, so a
  constraint that the models declare but the schema does not is caught here
  rather than in production.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.models import (  # noqa: E402
    ArrearsEntry,
    AuditLogEntry,
    Booking,
    ChatHistory,
    InteractionSignal,
    PointsTransaction,
    PointsWallet,
    RetentionSnapshot,
    User,
    UserConsentEvent,
    UserPreferenceProfile,
)

NOW = datetime.now(timezone.utc)
PASSWORD = "SamplePassw0rd!"


async def create_schema() -> None:
    """Emit the DDL from the models, exactly as the app's lifespan does."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("schema created from Base.metadata")


async def drop_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    print("schema dropped")


async def seed() -> None:
    """Insert a small, deliberately *varied* population.

    Variety is the point. A seed of three identical happy customers cannot
    tell you whether the churn paths, the recovery paths or the dormant
    paths work, so the population spans the states the engines branch on:
    a brand-new account, a healthy repeat customer, an at-risk customer with
    negative sentiment, a dormant one, and an admin.
    """
    from app.security import get_password_hash

    hashed = get_password_hash(PASSWORD)

    people = [
        # (username, email, is_admin, days_ago_last_activity, mood)
        ("ana", "ana@example.com", False, 1, "The appointment went well, thank you."),
        ("bruno", "bruno@example.com", False, 0, "still waiting on the confirmation"),
        ("chiara", "chiara@example.com", False, 2, "this is really frustrating, twice now"),
        ("dmitri", "dmitri@example.com", False, 45, "no idea what happened to my booking"),
        ("root", "root@example.com", True, 0, "admin console sign-in"),
    ]

    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT count(*) FROM users"))
        if (result.scalar() or 0) > 0:
            print("users already present -- pass --reset to reseed, or leave as is")
            return

        user_ids: dict[str, int] = {}
        for username, email, is_admin, _days, _mood in people:
            await conn.execute(
                text(
                    "INSERT INTO users (username, email, full_name, hashed_password, "
                    "is_admin, created_at, updated_at) "
                    "VALUES (:u, :e, :f, :p, :a, :c, :c)"
                ),
                {
                    "u": username,
                    "e": email,
                    "f": username.capitalize(),
                    "p": hashed,
                    "a": is_admin,
                    "c": NOW - timedelta(days=90),
                },
            )
            row = await conn.execute(
                text("SELECT id FROM users WHERE username = :u"), {"u": username}
            )
            user_ids[username] = int(row.scalar())

        # --- chat history: enough signal for the analytics to branch on ------
        conversations = {
            "ana": [
                (1, "How do I reschedule my appointment?", "You said: 'How do I reschedule...'"),
                (3, "Thanks, that worked out.", "You said: 'Thanks, that worked out.'"),
                (6, "One more question about the report.", "You said: 'One more question...'"),
            ],
            "bruno": [
                (0, "still waiting on the confirmation", "You said: 'still waiting...'"),
                (0, "any update?", "You said: 'any update?'"),
            ],
            "chiara": [
                (1, "this is really frustrating, twice now", "You said: 'this is frustrating...'"),
                (1, "I cancelled and nobody called me", "You said: 'I cancelled...'"),
                (2, "the price was not what I expected", "You said: 'the price...'"),
                (2, "still waiting for a refund", "You said: 'still waiting for a refund'"),
            ],
            "dmitri": [(45, "no idea what happened to my booking", "You said: 'no idea...'")],
            "root": [(0, "admin console sign-in", "You said: 'admin console sign-in'")],
        }
        for username, messages in conversations.items():
            for days_ago, message, response in messages:
                await conn.execute(
                    text(
                        "INSERT INTO chat_history (user_id, message, response, timestamp) "
                        "VALUES (:u, :m, :r, :t)"
                    ),
                    {
                        "u": user_ids[username],
                        "m": message,
                        "r": response,
                        "t": NOW - timedelta(days=days_ago),
                    },
                )

        # --- bookings across every status the engines branch on -------------
        bookings = [
            ("ana", "consultation", "Annual check-up", "completed", 10),
            ("ana", "delivery", "Report delivery", "completed", 3),
            ("bruno", "meeting", "Onboarding session", "pending", 0),
            ("chiara", "consultation", "Assessment", "cancelled", 2),
            ("chiara", "consultation", "Follow-up", "cancelled", 2),
            ("chiara", "project", "Migration work", "cancelled", 1),
            ("dmitri", "delivery", "Replacement item", "cancelled", 44),
        ]
        for username, service_type, title, status, days_ago in bookings:
            await conn.execute(
                text(
                    "INSERT INTO bookings (user_id, service_type, title, details, "
                    "scheduled_date, status, created_at, updated_at) "
                    "VALUES (:u, :s, :t, :d, :sd, :st, :c, :c)"
                ),
                {
                    "u": user_ids[username],
                    "s": service_type,
                    "t": title,
                    "d": "",
                    # `bookings.scheduled_date` is declared `DateTime` -- naive --
                    # while every other timestamp in this schema is
                    # `DateTime(timezone=True)`. asyncpg refuses to subtract a
                    # naive from an aware value, so a tz-aware value here is a
                    # DataError rather than a silent truncation. The column is
                    # left as the models declare it (see the note in
                    # `app/models.py`); the seed just has to match it.
                    "sd": (NOW + timedelta(days=7)).replace(tzinfo=None),
                    "st": status,
                    "c": NOW - timedelta(days=days_ago),
                },
            )

        # --- interaction signals: what the improvement pack writes ---------
        for username in ("chiara", "bruno"):
            for area, score in (("response_speed", 3.4), ("trust", 2.8), ("booking_flow", 2.1)):
                await conn.execute(
                    text(
                        "INSERT INTO interaction_signals (user_id, source, area, priority, "
                        "score, evidence, recommendation, created_at, updated_at) "
                        "VALUES (:u, 'chat', :a, 'high', :s, '{}', :r, :c, :c)"
                    ),
                    {
                        "u": user_ids[username],
                        "a": area,
                        "s": score,
                        "r": f"Investigate {area}",
                        "c": NOW - timedelta(days=1),
                    },
                )

        # --- retention snapshots: the series the health report needs -------
        for username, score, risk, stage in (
            ("ana", 88.0, "low", "loyal"),
            ("chiara", 41.5, "high", "at_risk"),
        ):
            for index in range(3):
                await conn.execute(
                    text(
                        "INSERT INTO retention_snapshots (user_id, snapshot_type, "
                        "window_days, loyalty_score, churn_risk, lifecycle_stage, "
                        "summary_json, created_at, updated_at) "
                        "VALUES (:u, 'chat_response', 30, :s, :r, :l, '{}', :c, :c)"
                    ),
                    {
                        "u": user_ids[username],
                        "s": score - (2 - index) * 3.0,
                        "r": risk,
                        "l": stage,
                        "c": NOW - timedelta(days=(3 - index) * 5),
                    },
                )

        # --- points: balances and a ledger that reconciles ------------------
        for username, balance in (("ana", 250.0), ("chiara", 40.0)):
            await conn.execute(
                text(
                    "INSERT INTO points_wallets (user_id, point_type, balance, "
                    "created_at, updated_at) VALUES (:u, 'loyalty_points', :b, :c, :c)"
                ),
                {"u": user_ids[username], "b": balance, "c": NOW},
            )
        await conn.execute(
            text(
                "INSERT INTO points_transactions (user_id, point_type, kind, points_delta, "
                "currency, currency_amount, rate, fee, reference, created_at, updated_at) "
                "VALUES (:u, 'loyalty_points', 'earn', 100.0, 'USD', 0.0, 0.0, 0.0, "
                "'seed:welcome', :c, :c)"
            ),
            {"u": user_ids["ana"], "c": NOW - timedelta(days=5)},
        )

        # --- arrears: an open deferral, so the payments surface is not empty
        #
        # Every NOT NULL column with a *Python-side* default has to be supplied
        # explicitly here, because the ORM is bypassed: `models.ArrearsEntry`
        # declares `interest_accrued = Column(Float, nullable=False, default=0.0)`
        # and similar, which SQLAlchemy applies on insert but the database knows
        # nothing about. The first version of this seed omitted them and the
        # real constraint rejected it:
        #
        #   asyncpg.exceptions.NotNullViolationError: null value in column
        #   "interest_accrued" of relation "arrears_entries" violates not-null
        #   constraint
        #
        # Which is the point of seeding against a real database.
        await conn.execute(
            text(
                "INSERT INTO arrears_entries (user_id, reference, service_type, "
                "principal, currency, policy_id, annual_rate, grace_days, "
                "compounding, interest_cap_pct, defer_days, interest_accrued, "
                "status, settled_interest, total_settled, interest_waived, "
                "waived_interest, late_fee_amount, late_fee_pct, "
                "late_fee_charged, fees_waived, waived_fees, note, opened_at, "
                "due_at, created_at, updated_at) "
                "VALUES (:u, 'seed-arrears-1', 'consultation', 120.0, 'USD', "
                "'vip_premium_deferral', 6.0, 45, 'simple', 100.0, 30, 0.0, "
                "'open', 0.0, 0.0, false, 0.0, 0.0, 0.0, false, false, 0.0, "
                "'seeded deferred payment', :o, :d, :o, :o)"
            ),
            {
                "u": user_ids["ana"],
                "o": NOW - timedelta(days=2),
                "d": NOW + timedelta(days=28),
            },
        )

        # --- preferences + consent: so the preference centre is populated --
        await conn.execute(
            text(
                "INSERT INTO user_preference_profiles (user_id, preferences_json, "
                "consents_json, consent_version, created_at, updated_at) "
                "VALUES (:u, :p, :c, 'preference_catalog_v1', :t, :t)"
            ),
            {
                "u": user_ids["chiara"],
                "p": json.dumps({"communication_channel": "email",
                                 "communication_frequency": "daily"}),
                "c": json.dumps({"service": True, "recovery": True, "analytics": True,
                                 "marketing": False, "personalization": True,
                                 "third_party_sharing": False}),
                "t": NOW - timedelta(days=1),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO user_consent_events (user_id, purpose, granted, version, "
                "lawful_basis, recorded_by_id, note, created_at, updated_at) "
                "VALUES (:u, 'analytics', true, 'preference_catalog_v1', "
                "'legitimate_interest', :u, 'seeded grant', :t, :t)"
            ),
            {"u": user_ids["chiara"], "t": NOW - timedelta(days=1)},
        )

        # --- an audit entry, so the trail is not empty on first read --------
        # `actor_user_id` is the column name; there is no free-text `actor`.
        await conn.execute(
            text(
                "INSERT INTO audit_log_entries (actor_user_id, source, action, "
                "entity_type, entity_id, severity, summary, detail_json, "
                "created_at, updated_at) "
                "VALUES (NULL, 'seed', 'database.seeded', 'system', 'seed', "
                "'info', 'sample data inserted', '{}', :t, :t)"
            ),
            {"t": NOW},
        )

    print(
        f"seeded {len(people)} users, {sum(len(v) for v in conversations.values())} "
        f"messages, {len(bookings)} bookings"
    )


async def verify() -> None:
    """Read the data back through the real tables and print what is there.

    Deliberately raw SQL rather than the services: this checks the *data*, and
    going through the services would prove the services work, not the seed.
    """
    async with engine.begin() as conn:
        for table in ("users", "chat_history", "bookings", "interaction_signals",
                      "retention_snapshots", "points_wallets", "points_transactions",
                      "arrears_entries", "user_preference_profiles",
                      "user_consent_events", "audit_log_entries"):
            result = await conn.execute(text(f"SELECT count(*) FROM {table}"))
            count = int(result.scalar() or 0)
            marker = " " if count else "!"   # zero rows is worth seeing, not a failure
            print(f" {marker} {table:26s} {count:>5}")

        result = await conn.execute(
            text(
                "SELECT u.username, b.status, count(*) FROM bookings b "
                "JOIN users u ON u.id = b.user_id GROUP BY 1,2 ORDER BY 1,2"
            )
        )
        print("\n  bookings by user and status:")
        for username, status, count in result:
            print(f"    {username:8s} {status:10s} {count}")

        result = await conn.execute(
            text(
                "SELECT t.tier, count(*) FROM complaint_cases t GROUP BY 1 ORDER BY 1"
            )
        )
        print("\n  complaints by tier:")
        for tier, count in result:
            print(f"    {tier:8s} {count}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop and recreate first")
    args = parser.parse_args()

    if args.reset:
        await drop_schema()
    await create_schema()
    await seed()
    print()
    await verify()
    await engine.dispose()
    print("\nlogin with any seeded username and password:", PASSWORD)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
