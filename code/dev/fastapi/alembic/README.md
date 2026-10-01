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

## `downgrade base` leaves the database neither up nor down

`20260904_00_initial_schema.py` created two native enums — `servicetype` and
`bookingstatus` — and its `downgrade()` dropped the tables that used them but not
the *types*. `DROP TABLE` takes the columns; it does not take the type.

So the sequence `alembic downgrade base && alembic upgrade head` failed on the
second command with `type "servicetype" already exists`, while reporting success
on the first. Worse than a broken round trip, because it left a database that was
neither up nor down — tables gone, types not — and surfaced the cause one command
later than the mistake that caused it.

Both types are now dropped in `0001`'s downgrade, with `checkfirst=True` because
SQLite has no standalone enum type to drop (the values live inline in the column
definition) and asking it to drop one raises rather than no-ops.

Verified: `upgrade head` → 29 tables, drift in sync → `downgrade base` → **0
tables and 0 enum types** → `upgrade head` → drift in sync. On an empty database,
and on one that has been through it.

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

## The chain now builds the whole schema from nothing (2026-09-30)

This used to be untrue, and the previous version of this file documented the
failure rather than fixing it. Verified against a real PostgreSQL 16.15:

```
$ alembic upgrade head          # empty database
asyncpg.exceptions.UndefinedTableError: relation "users" does not exist
[SQL: ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT false NOT NULL]
```

Three separate problems, all of which had to be fixed for `upgrade head` to
work on a real database:

1. **No base revision.** The chain started at a *delta*
   (`20260905_01_add_booking_events_and_admin_flag`) against a schema nothing
   in alembic had ever created — only `main.py`'s `create_all` had.
   `20260904_00_initial_schema` now creates `users`, `chat_history` and
   `bookings` in the state that revision expects.
2. **Revision ids too long.** `alembic_version.version_num` is `VARCHAR(32)`
   and every id was a descriptive date-plus-slug string of 35-45 characters:

   ```
   asyncpg.exceptions.StringDataRightTruncationError:
     value too long for type character varying(32)
   ```

   Ids are now short and opaque (`0001_initial` ... `0008_complaint_learning`);
   the date and description live in the filename. `test_migration_chain.py`
   asserts every id fits.
3. **`recreate="always"` on PostgreSQL.** `20260905_01` forces a batch rebuild
   to work around a *SQLite* limitation, but `recreate="always"` applies to
   every dialect, and a rebuild is DROP + CREATE — which fails as soon as
   anything references the table:

   ```
   asyncpg.exceptions.DependentObjectsStillExistError:
     cannot drop constraint users_pkey on table users because other objects depend on it
   DETAIL: constraint bookings_user_id_fkey on table bookings depends on index users_pkey
   ```

   It is now applied on SQLite only.

`0007_sync_remaining_schema` adds the **ten tables that were never migrated at
all**. Every migration was verified against the models in isolation and the
chain was verified against SQLite, but nothing compared the chain's *result*
against `Base.metadata`, so `upgrade head` produced 12 of 22 tables and the
whole suite passed. `scripts/schema_drift_report.py` is that check, and
`test_migration_chain.py` now runs it inside the suite:

```
$ alembic upgrade head && python3 scripts/schema_drift_report.py
live tables    : 22
declared tables: 22

in sync: the database matches Base.metadata
```

### The chain

| revision | what it does |
|---|---|
| `0001_initial` | `users`, `chat_history`, `bookings` — the base that was missing |
| `0002_booking_events` | `is_admin`, `booking_events`, `retention_snapshots`, `chat_history.user_id` |
| `0003_recovery_outcomes` | `recovery_outcomes` |
| `0004_booking_assignments` | `booking_assignments` |
| `0005_preference_consent` | `user_preference_profiles`, `user_consent_events` |
| `0006_complaints` | `complaint_cases`, `complaint_events`, `complaint_decisions` |
| `0007_sync_remaining_schema` | the ten tables the hand-written chain never covered |
| `0008_complaint_learning` | `complaint_signal_weights`, `complaint_signal_observations`, `complaint_improvement_proposals` |

### A migration file that is not in `CHAIN` never runs

`tests/test_migration_chain.py` keeps a hand-maintained tuple of filenames called
`CHAIN`, and for a while nothing compared it to the directory. A migration omitted
from that tuple still applies perfectly in isolation — which is how it gets
verified — while `alembic upgrade head` never runs it, so the tables it creates
simply do not exist in a migrated database. `test_the_chain_builds_every_table_the
_models_declare` then reports the missing tables without any hint that a file was
left off a list.

`0008_complaint_learning` was written that way and caught by exactly that test.
`test_the_chain_lists_every_migration_file_on_disk` now fails if the two ever
disagree, so **adding a migration means adding it to `CHAIN` in the same commit**
— and the test will tell you if you forget.

### `alembic/env.py` also had to be fixed

It read `sqlalchemy.url` from `alembic.ini`, which ships as the placeholder
`driver://user:pass@localhost/dbname`, so **every** command that touched a
database failed before reaching a migration:

```
sqlalchemy.exc.NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:driver
```

It now reads `app.db.DATABASE_URL` (so migrations and the app cannot be pointed
at different databases by accident) and drives the migrations **asynchronously**,
because the URL carries `asyncpg` and the synchronous `engine_from_config` could
never load it. It also imports `app.models`, without which `Base.metadata` is
empty and `--autogenerate` decides every table in the database is surplus.

## The workflow

`alembic upgrade head` on an empty database now works, so use it:

```bash
alembic upgrade head
python3 scripts/schema_drift_report.py    # must say "in sync"
```

For a database the app has already `create_all`ed, stamp instead of upgrading,
because the tables already exist:

```bash
alembic stamp head
```

## Rules that follow

1. **A new table needs two things, not one.** The model in `app/models.py`
   (that is what `create_all` emits) *and* a migration in `versions/` (that is
   what a deployment past the baseline applies). Adding only the model means
   `create_all` builds the table and the migration then fails on "table
   already exists" for anyone who stamps from base; adding only the migration
   means the model and the chain disagree.
2. **Run `scripts/schema_drift_report.py` after `alembic upgrade head`.** It
   is the only check that compares the chain's *result* against
   `Base.metadata`, and its absence is why ten tables shipped without a
   migration while the suite stayed green. `test_migration_chain.py` runs the
   same comparison, so CI catches it without a server.
3. **A revision id must fit `alembic_version.version_num` (32 chars).** The
   date and the description belong in the filename.
4. **`upgrade()` must be additive.** No `drop_table`, no `drop_column`, no
   type conversion. `test_migration_chain.py` asserts the first two, and
   `alter_column` is allowed only for a server-default change.
5. **Keep a string column wide enough for what the application writes into
   it.** `user_preference_profiles.consent_version` was `VARCHAR(20)` and
   `PREFERENCE_CATALOG_VERSION` is 21 characters, so the preference centre
   could not save a single preference. Only a real database enforces a length.
6. **Do not re-add the archived orphan to `versions/`.** It reintroduces an
   unresolvable `down_revision`, and one such file breaks *every* alembic
   command, not just the one being run. See `archived/README.md`.

## The remaining duplication: `create_all` and the chain

`main.py`'s lifespan still calls `Base.metadata.create_all` unconditionally, so
a database the app boots is already at head and `alembic upgrade` is not in the
running path. Both routes now produce the *same* schema — that is what
`scripts/schema_drift_report.py` verifies — so this is redundancy, not a
conflict.

It is still worth removing, and the order matters: decide the baseline for any
database already deployed first, then drop `create_all` from the lifespan, then
let `alembic upgrade` be the only path. Doing it the other way round would
leave every existing deployment with a schema no migration recorded.
