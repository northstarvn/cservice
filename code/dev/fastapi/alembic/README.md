# How this project's schema is actually managed

Read this before touching `alembic/versions/`.

## The short version

**`Base.metadata.create_all` owns the schema.** `app/main.py` calls it on every
startup, inside the lifespan handler:

```python
async with engine.begin() as conn:
    await conn.execute(text("SELECT 1"))
    await conn.run_sync(Base.metadata.create_all)   # main.py:116
```

So a database is always brought to whatever the models declare, regardless of
what the `alembic_version` table says. Alembic is not in the running path.

## The chain is now SQLite-clean end to end

`20260905_01_add_booking_events_and_admin_flag.py` contained **three**
PostgreSQL-only statements, so it could never have run on this project's SQLite
in either direction. All three are now routed through `batch_alter_table`, which
issues the native statement on PostgreSQL and takes the copy-and-move path on
SQLite:

| statement | SQLite said | now |
|---|---|---|
| `add_column("chat_history", … ForeignKey …)` | `NotImplementedError: No support for ALTER of constraints` | `batch_alter_table` — adding a column is already a rebuild trigger |
| `alter_column("users", "is_admin", server_default=None)` | `near "ALTER": syntax error` | `batch_alter_table(recreate="always")` — see below |
| `drop_column("chat_history", "user_id")` | `error in table chat_history after drop column: unknown column "user_id" in foreign key definition` | `batch_alter_table` — drops the constraint with the column |

Two of the fixes are less obvious than they look and are worth knowing before
anyone "simplifies" them:

- **`recreate="always"` on the `users` alter is required, not decorative.** Batch
  mode rebuilds a table when it adds or drops a column, but a bare
  `alter_column` that only changes a server default is *not* a rebuild trigger,
  so it still emits the native statement and SQLite raises
  `NotImplementedError` again. Forcing the rebuild is what makes SQLite take
  the copy-and-move path.
- **The `chat_history.user_id` foreign key had to be named.** The copy-and-move
  path raises `ValueError: Constraint must have a name`, and an auto-generated
  name is not reproducible. It is now
  `fk_chat_history_user_id_users`; the matching downgrade drops the column, and
  the constraint goes with it.

`tests/test_migration_chain.py` round-trips the entire chain — builds the
pre-`20260905` schema, runs every `upgrade()` in order, every `downgrade()` in
reverse, and asserts the database is back where it started *with its rows
intact*. It also asserts the chain is a single linear head with no template
placeholder left in it.

## Why the chain looks redundant — because it is

Every migration in `versions/` creates a table that `create_all` already
creates from the same models:

| migration | creates | also in `create_all`? |
|---|---|---|
| `20260905_01_add_booking_events_and_admin_flag` | `booking_events`, `retention_snapshots` | yes |
| `20260907_01_add_recovery_outcomes` | `recovery_outcomes` | yes |
| `20260908_01_add_booking_assignments` | `booking_assignments` | yes |
| `20260929_01_add_preference_consent_tables` | `user_preference_profiles`, `user_consent_events` | yes |
| `20260930_01_add_complaint_cases` | `complaint_cases`, `complaint_events`, `complaint_decisions` | yes |

Consequently `alembic upgrade head` against an **empty** database still fails,
and that is not a bug in any one migration:

```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: users
[SQL: ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT false NOT NULL]
```

`20260905_01` — the chain's base — issues `ALTER TABLE users ADD COLUMN
is_admin`, but `create_all` already created `users` *with* `is_admin`, and on an
empty database there is no `users` at all. That migration belongs to a world
where the schema pre-dated `create_all`. It is a leftover from the same
reorganisation that produced the archived orphan.

## The workflow that works

For a database created by the app (i.e. by `create_all`), the chain is already
satisfied, so the baseline is `head`:

```bash
# 1. let the app create the schema (or run create_all yourself)
python -c "import asyncio; from app.db import Base, engine; \
  asyncio.run(engine.begin().__aenter__())" # or just start the app once

# 2. record that the chain is satisfied
alembic stamp head

# 3. subsequent migrations apply normally
alembic upgrade head
```

Verified for the complaints migration specifically:

- `create_all` → `stamp head` → `upgrade head` is a **no-op** (the tables are
  already there and already match the models)
- `downgrade -1` **does** drop `complaint_decisions`, `complaint_events` and
  `complaint_cases` cleanly, so the migration is genuinely reversible

## Rules that follow

1. **A new table needs two things, not one.** The model in `app/models.py`
   (that is what `create_all` emits) *and* a migration in `versions/` (that is
   what a deployment already past the baseline applies). Adding only the model
   means `create_all` builds the table and the migration then fails on
   "table already exists" for anyone who stamps from base. Adding only the
   migration means the model and the chain disagree.
2. **A migration's `upgrade()` must be checked against a database built by
   `create_all`**, not only against an empty one. `tests/test_complaints_expansion.py::TestMigration`
   does the stronger version of this — it runs `upgrade()` on a bare SQLite
   database and diffs the result column-for-column and index-for-index against
   `Base.metadata`.
3. **Do not re-add the archived orphan to `versions/`.** It reintroduces an
   unresolvable `down_revision`, and one such file breaks *every* alembic
   command, not just the one being run. See `archived/README.md`.

## If alembic is ever made authoritative

That is a real piece of work and out of scope for now. It would mean: removing
the unconditional `create_all` from the lifespan, deciding a baseline for
databases already in production, rewriting `20260905_01` so it is a no-op
against a `create_all` schema, and making every migration idempotent. The
migrations as written are additive-only, which is a good foundation for it, but
the `ALTER TABLE users` base and the `create_all` bootstrap would have to be
reconciled first. Recorded in `../../BLOCKAGES.md`.
