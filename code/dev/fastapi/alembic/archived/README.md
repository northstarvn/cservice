# Archived migrations

Files here are **not** part of the revision chain. Alembic loads every module
under `alembic/versions/` to build its revision map, so a single file in that
directory with an unresolvable `down_revision` breaks *every* command —
`upgrade`, `downgrade`, `history`, `current`, `heads`, `stamp` — not just the
one you were running. That is what happened here: see below.

## `0366de366666_backfill_and_enforce_booking_timestamps.py` (rev `7a2c41b9d810`)

Moved out of `versions/` on 2026-09-30. It shipped with

```python
down_revision = "<PUT_PREVIOUS_REVISION_ID_HERE>"
```

a literal template placeholder, which alembic cannot resolve. Because the file
lived in `versions/`, the whole revision map failed to build and **no** alembic
command could run against this project.

Three facts made moving it the right call rather than repairing it:

1. **Nothing referenced it.** No migration in `versions/` had
   `down_revision = "7a2c41b9d810"`, and nothing referenced whatever it was
   meant to revise. It was an unlinked head.
2. **It was never in the deployed chain.** The live chain starts at
   `20260905_01_add_booking_events_and_admin_flag`, which declares
   `down_revision = None` — i.e. it is a base, not a successor. The orphan came
   in with a repository-reorganisation commit (`b13906d`, "refolder") and has
   never been part of a linear sequence.
3. **It could not run here anyway.** Its `upgrade()` is PostgreSQL-only —
   `ALTER TABLE bookings ALTER COLUMN created_at SET NOT NULL` and
   `NOW()` defaults. This project runs on SQLite, where that syntax is invalid.
   Wiring it into the chain would have turned an inert file into a migration
   that fails on the first real deployment.

The file is kept rather than deleted so the intent stays recoverable. If the
booking-timestamp backfill is genuinely wanted, the correct shape is a **new**
migration in `versions/` whose `down_revision` is the current head, written in
SQLite-compatible SQL, and expressed as a table-rebuild rather than
`ALTER COLUMN`.

That would largely be a no-op, because the constraints it was trying to add
already exist in the models. `bookings.created_at` and `bookings.updated_at` are
both declared `nullable=False` with a `server_default`, via `TimestampMixin`:

```
bookings.created_at  -> nullable=False, server_default=func.now()
bookings.updated_at  -> nullable=False, server_default=func.now()
```

So `Base.metadata.create_all` already produces the NOT NULL + default the
orphan was reaching for with raw `ALTER COLUMN`. The backfill half
(`WHERE created_at IS NULL OR updated_at IS NULL`) is idempotent and harmless,
but it exists to clean up rows written before those columns were constrained;
with no deployed history in the revision chain to justify that, it is not
obviously needed.

Do not simply move this file back into `versions/`: that reintroduces the
placeholder, because nothing about the file changed.
