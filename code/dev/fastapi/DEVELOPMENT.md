# Local development

## One-time setup

```bash
# 1. PostgreSQL (the app is async and its default URL names postgres)
sudo apt-get install -y postgresql postgresql-contrib
sudo pg_ctlcluster 16 main start          # or: sudo systemctl start postgresql
sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD 'postgres';"
sudo -u postgres psql -c "CREATE DATABASE cservice;"

# 2. Settings
cp .env.example .env                     # the defaults already match a local postgres

# 3. Schema + sample data
python3 scripts/seed_sample_data.py --reset
```

Sign in as any seeded user — `ana`, `bruno`, `chiara`, `dmitri`, or `root`
(the admin) — with password `SamplePassw0rd!`.

## Run it

```bash
uvicorn app.main:app --reload --port 8000
```

## The sample data is deliberately varied

A seed of three identical happy customers cannot tell you whether the churn,
recovery or dormancy paths work, so the population spans the states the engines
branch on:

| user | state | why |
|------|-------|-----|
| `ana` | loyal, 2 completed bookings, points balance, an open arrears deferral, a stated preference | the healthy path, plus a payment and a preference to read |
| `bruno` | 1 pending booking, repeated "any update?" | the abandonment / follow-up-gap path |
| `chiara` | 3 cancelled bookings, negative messages, signals, snapshots, a consent grant | the recovery and at-risk path |
| `dmitri` | 1 cancellation, last active 45 days ago | the dormancy / win-back path |
| `root` | admin | the admin surfaces |

## Checks worth running

```bash
# the unit suite (fakes the database; ~2 000 tests, ~2 min)
pytest tests/ -q

# does the migration chain build what the models declare?
alembic upgrade head && python3 scripts/schema_drift_report.py
#   -> "in sync: the database matches Base.metadata"

# the migration chain itself, round-tripped, against a real postgres
sudo -u postgres psql -c "CREATE DATABASE cservice_chaintest;"
CSERVICE_MIGRATION_TEST_URL=postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/cservice_chaintest \
  pytest tests/test_migration_chain.py -q

# every route, over HTTP, against the real database
uvicorn app.main:app --port 8123 --log-level warning &
CSERVICE_SMOKE_URL=http://127.0.0.1:8123 python3 scripts/smoke_live_api.py
```

`schema_drift_report.py` and `smoke_live_api.py` are not decoration. Every
defect in the "found by running it against a real database" list below was
invisible to the unit suite, because the suite's fakes do not enforce a column
length, do not lazy-load a relationship, and do not run a `create_table`.

## What running it against a real database actually found

Every one of these passed the full suite before it was found:

- **`GET /users/me` returned 500 on every request.** `current_control_posture`
  reads `user.policy_score`, a lazy relationship, from synchronous code:
  `MissingGreenlet: greenlet_spawn has not been called`. The test suite's user
  is a stand-in object with no lazy loading. Fixed by eager-loading
  `policy_score` in the three user loaders in `app/deps.py`.
- **`GET /chat/history` returned 500.** `ChatHistoryOut.timestamp` was
  annotated `str` while the column is `DATETIME`; pydantic v2 will not coerce
  a `datetime` into a `str`. The unit test constructed the model from a string.
- **The preference centre could not save a preference.**
  `user_preference_profiles.consent_version` was `VARCHAR(20)` and
  `PREFERENCE_CATALOG_VERSION` is 21 characters:
  `StringDataRightTruncationError`. `user_consent_events.version` had the same
  defect, so every consent event failed to insert too.
- **`GET /chat/customer-360` returned 422 on every request.** The section
  orchestrator keyed its lookup on `"_profile"` while the wanted-set held
  `"profile"`, so every section was skipped without being attempted and the
  error reported "section not built" — which is why the cause was not visible.
- **The migration chain could not build a database at all.** No base revision;
  revision ids longer than `alembic_version.version_num`; `recreate="always"`
  forcing a table rebuild on PostgreSQL; and ten tables with no migration at
  all, because nothing compared the chain's result against `Base.metadata`.
- **Every `alembic` command failed** with
  `Can't load plugin: sqlalchemy.dialects:driver`, because `alembic/env.py`
  read the placeholder URL from `alembic.ini` and used a synchronous engine
  with an async driver.

## Workers are off by default

`CSERVICE_PARTITION_WORKER`, `CSERVICE_AUTO_RECOVERY`,
`CSERVICE_PIPELINE_AUTOSTART` and `CSERVICE_CANARY_AUTOPROMOTE` all default to
`0` so a local boot has no background thread mutating data while you are
looking at it. Enable one deliberately.

`ENRICHMENT_ENABLED` is also off: it calls real third-party HTTP APIs.
