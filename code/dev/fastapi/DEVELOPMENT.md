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

## Simulating every real-life flow

`app/real_life_flows.py` runs 12 flows across 41 subflows against 5 personas —
loyal-with-points, abandoned-pending, repeatedly-cancelled, dormant-45-days,
and the admin console — and reports what did not hold. It reads every
expectation from the engines' own config, so it survives a rename, and it is
deliberately useless as a detector of an intentional edit to the table it
validates; that limit is stated in the module and covered from the other side
by `tests/test_kaizen_shadow_release.py::TestPinningOracle`, which spells the
posture deltas out as literals.

```bash
# run the simulation and print what it found
python3 scripts/run_flow_simulation.py

# append the findings to BLOCKAGES.md (refuses a duplicate section unless
# --allow-repeat; a log nobody reads is indistinguishable from no log)
python3 scripts/run_flow_simulation.py --append

# non-zero exit on any blocker, for CI
python3 scripts/run_flow_simulation.py --strict
```

Findings carry a **conclusion** and a **suggestion**, and the suggestion is
text. The module reports; it never edits the codebase. Applying a suggestion is
a separate, promoted candidate.

## The shadow environment, and why it is one-way

`app/shadow_env.py` answers one question: can this process affect live? Six
checks decide it, and every one **fails closed** when unconfigured — an
unobserved check is a failed check, because a guard that passes because nobody
answered it is the "published number is not the enforced one" defect this repo
has now found three times.

Isolation is proven by comparing **database identities**
`(scheme, host, port, database)`, not by reading a config value. A shadow
pointed at live's database with the variable set is exactly the case worth
catching. The username is deliberately excluded from the identity, so a
read-only replication role does not read as a different database.

```bash
# six checks, each with what it asserts and what to do about it
python3 scripts/run_shadow_env.py

# non-zero exit when the shadow is not isolated, for CI
python3 scripts/run_shadow_env.py --strict
```

To actually stand one up, see the `shadow environment` block in
`.env.example`. The asymmetry that matters: `CSERVICE_SHADOW_DATABASE_URL` has
**no fallback** to `DATABASE_URL`.

## The maturity ladder, and the five-event rollback

`app/release_ladder.py` moves a change along
`l0_draft → l1_verified → l2_shadow → l3_canary → l4_live`. Each rung has entry
gates evaluated against the **destination**, not the rung already occupied — an
earlier version read `required_gates_for(candidate.level)`, which checked the
gates it had already passed and never consulted the next rung's, so `l4_live`
was reachable with `canary_error_rate` never measured.

- **Code and data version as one pair.** A rollback that restores the code and
  keeps the data is not a rollback.
- **A gate nobody measured fails.** `unmeasured` folds to `fail`, so there is no
  way to advance by supplying fewer measurements.
- **A refused advance is a 200, not a 409.** An admin asking why a candidate is
  not moving is asking a question, and a 409 would throw the answer away.
- **Rollback appends.** `kind: "rollback"` with `restores: <event_id>`, so
  7 → 8 → 7 is three events and the history stays honest. The window is the
  last **5** events, not 5 versions.
- **Deployment is not an HTTP verb.** Promoting a candidate and changing what is
  live are two acts; only the first is exposed, so the gates are read before
  anyone can act on them.

```bash
# the whole surface, admin-gated under /kaizen/admin* (NOT /meta*, which is
# public by intent and this contains three mutating endpoints)
curl -H "Authorization: Bearer $TOKEN" localhost:8000/kaizen/admin/catalog
curl -H "Authorization: Bearer $TOKEN" localhost:8000/kaizen/admin/safe-levels
curl -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/kaizen/admin/candidates \
  -H 'content-type: application/json' \
  -d '{"candidate_id":"c1","commit":"abc123def456","revision":"0007_x"}'
curl -X POST -H "Authorization: Bearer $TOKEN" \
  localhost:8000/kaizen/admin/candidates/c1/measure \
  -H 'content-type: application/json' \
  -d '{"measured":{"tests_failed":0},"from_app":["authz","flows"]}'
```

`from_app` names the measurements the server computes rather than accepts.
`authz` is the important one: it is a property of the live route table, so a
hand-typed `authz_in_sync: true` would be an assertion about the running system
rather than a measurement of it. App-computed values are merged last, so a
number in the request body cannot override one this process measured.

## One command, or one timer

Everything above is reachable from a single entry point, because three scripts
and an admin-only HTTP surface is the standard way a codebase ends up with a
check nobody runs.

```bash
# flows + completeness + shadow isolation + authorization + the promotion gates
python3 scripts/run_kaizen.py

# grade as if promoting to live, machine-readable
python3 scripts/run_kaizen.py --level l4_live --json

# record the findings in BLOCKAGES.md (a person's decision, never automatic)
python3 scripts/run_kaizen.py --append

# the same thing on a loop instead of once
python3 scripts/run_kaizen.py --daemon

# what the timer has been doing, and whether it is even on
python3 scripts/run_kaizen.py --status
```

**Three exit codes, because two of them must never be confused.**

| code | meaning |
|---|---|
| `0` | nothing measured is wrong |
| `1` | something measured is wrong: a flow failed, a part is a stub, a mutating public route is unlisted, the shadow can reach live, or a gate failed *with evidence* |
| `2` | the sweep could not complete, so the answer is unknown |

A gate that treats "no blockers" and "I could not tell" as the same number is a
gate that has stopped running. Relatedly: **a gate that was never measured is
not a failed gate.** On a checkout there is no canary serving traffic and no
deployment ledger, so most gates are unmeasured by definition — a sweep that
reported "no" every day would be a sweep people learned to ignore.

### Completeness: the other axis

`/flows` answers *does it behave*. The completeness report answers *is it there*,
which is the question that matters while the backend is immature.

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8000/kaizen/admin/completeness
```

Six graded states rather than a boolean:

| state | meaning |
|---|---|
| `absent` | nothing to test |
| `declared_only` | something promises it and nothing implements it |
| `stub` | present and callable, but not branching |
| `partial` | real implementation, wrong for at least one persona |
| `untested` | present and callable, and no probe exercises it |
| `complete` | present, non-stub, and every persona agreed |

Two properties make it worth reading:

- **Absence is evidence, never a caught exception.** A probe that raised is
  equally consistent with a typo, a renamed argument and a regression, so
  `unknown` never degrades to `absent` and a probe is deferred only when its
  target positively failed to resolve.
- **What is fatal depends on the rung.** A defect blocks everywhere; a gap blocks
  from `l3_canary`; an unexercised part blocks only at `l4_live`. An immature
  backend passes at `l0_draft` — "not built yet" is this project's starting state
  and must not block the first commit of every feature.

Two findings the report publishes that no probe could have found on its own:
**orphan probes** (registered, classified, and invoked by no flow — so the
registry overstates its own coverage) and **blind probes** (every persona is
asked the same question, so a constant engine would satisfy them).

### The automatic trigger

`CSERVICE_KAIZEN_AUTORUN=1` starts the sweep as a background task, on the same
idiom as `CSERVICE_PARTITION_WORKER`, `CSERVICE_AUTO_RECOVERY` and
`CSERVICE_PIPELINE_AUTOSTART`. It runs **immediately** on start rather than
after the first interval, so a deployment that enables it and then crashes still
leaves evidence it ever started. Read the result at
`GET /kaizen/admin/sweep`, which publishes the configuration beside the result —
because the useful question is not "did it pass" but "is it running", and a
worker that silently never started looks exactly like a healthy one.

The scheduled sweep **never writes to `BLOCKAGES.md`** unless
`CSERVICE_KAIZEN_APPEND=1`. An automatic writer fills the log with dated sections
nobody wrote, and the duplicate-section guard refuses every one of them, so it is
either a no-op or a flood. Automatic means *measured on a schedule and readable
at an endpoint*; appending stays an act a person performs.

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

## Signing in, and recognising a device

```bash
# what can be used to sign in, and what this build deliberately does not offer
curl -s localhost:8000/users/auth/methods | python3 -m json.tool

# the bootstrap second factor: needs no enrolment, which is why it is the
# fallback a locked-out account can still use
curl -s -X POST localhost:8000/users/auth/email-otp/request \
  -H 'Content-Type: application/json' -d '{"username":"ana","device_id":"my-laptop"}'
```

There is **no mail sender in this deployment**. The code is generated, hashed
and stored; nothing delivers it. That is the honest failure mode — an
integration has to send it, and a deployment that has not wired a sender will
see a correct-shaped response and no inbox.

**Device recognition does not sign anyone in.** It scores a login from the
device digest, a network /24, user-agent, language, a declared UTC offset and
your usual hours, and the score only decides *which credential is asked for* —
never whether the request is admitted. The lowest band it can select is `loa2`,
reached through a real second factor.

Set `CSERVICE_RECOGNITION_MODE=enforced` to have the verdict actually choose the
credential. Two things about that mode are load-bearing:

- It is **off by default**, because a feature that changes who can sign in
  should not become stricter the moment it is deployed.
- A challenge the account has **not enrolled is downgraded, not applied**. A
  password-only customer on a new device is the most legitimate `unfamiliar`
  login there is, and telling them to produce a second factor they never
  enrolled means the only way forward is to be logged in — which they are not.

To see the whole decision, `GET /users/me/recognition?device_id=…` reports the
score, the band, and every signal's contribution. It shows contributions and
never the values behind them, so the endpoint that makes the weights arguable is
not also an endpoint that locates you. It is post-authentication only: the same
question before login, for a supplied username, would answer "does this account
exist and have it seen this device before".

Trusted devices are revocable at `DELETE /users/me/devices/{id}`, which clears
the token hash rather than flagging the row — a revoked device stays visible so
you can see the revocation, but holds nothing that works.

### The pieces of the request that were refused

`GET /users/auth/methods` returns a `refused` list beside the catalogue, and
each entry says why: PIN-only, trusted-network, security questions, automatic
cookie sign-in, and sign-in-by-recognition. Those are data in
`auth_methods.REFUSED_METHOD_PATTERNS` rather than omissions, so "why can I not
sign in from my network" is answerable from the running service, and adding one
is a deliberate act somebody has to argue for.

The reasoning is worth reading in full before changing any of it:
`app/services/device_recognition.py`, module docstring.

### Connecting your own storage

`GET /users/me/storage/providers` lists each provider with the consequence of
every scope, and `configured: false` unless the environment supplies
credentials. `POST /users/me/storage/connect` returns an authorisation URL;
the broadest scope grants read/write/delete over the whole account, is never the
default, and needs `confirm_broad_scope: true` plus a message stating so.

**The token exchange is not implemented here.** A connection stays `pending`
with no tokens stored and says so, rather than being reported live. Setting
`CSERVICE_STORAGE_REDIRECT_URI` is required — without it the connect endpoint
answers 503 instead of sending someone to a URL that cannot match what the
provider has registered.

Revoking overwrites the stored ciphertext and keeps the row, so the record of
what was once granted survives for an investigation while the credential does
not. The response says plainly that the provider-side grant is unaffected.

## Workers are off by default

`CSERVICE_PARTITION_WORKER`, `CSERVICE_AUTO_RECOVERY`,
`CSERVICE_PIPELINE_AUTOSTART` and `CSERVICE_CANARY_AUTOPROMOTE` all default to
`0` so a local boot has no background thread mutating data while you are
looking at it. Enable one deliberately.

`ENRICHMENT_ENABLED` is also off: it calls real third-party HTTP APIs.
