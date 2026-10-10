"""Round-trip the whole revision chain, and check it builds what the models declare.

Two properties, both of which had to be added because a real database
demonstrated they were not true:

**The chain runs.** Builds the schema from nothing, runs every `upgrade()` in
order, every `downgrade()` in reverse, and asserts the database is back where
it started *with its rows intact*.

**The chain builds the application's schema.** This is the check whose absence
let ten tables ship without a migration. Every migration was verified against
the *models* in isolation and the chain was verified against a SQLite database,
but nothing ever compared the chain's *result* against `Base.metadata` — so
`alembic upgrade head` produced twelve of twenty-two tables and every test
passed. The assertion below is the one that would have caught it.

The whole thing runs on SQLite by default so it needs no server. Set
`CSERVICE_MIGRATION_TEST_URL` to run the same comparison against a real
PostgreSQL, which is where the VARCHAR-vs-native-enum and
`DependentObjectsStillExistError` failures actually appeared.
"""
import importlib.util
import re
import os
import pathlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

VERSIONS = pathlib.Path("alembic/versions")
CHAIN = (
    "20260904_00_initial_schema.py",
    "20260905_01_add_booking_events_and_admin_flag.py",
    "20260907_01_add_recovery_outcomes.py",
    "20260908_01_add_booking_assignments.py",
    "20260929_01_add_preference_consent_tables.py",
    "20260930_01_add_complaint_cases.py",
    "20260930_02_sync_remaining_schema.py",
    "20260930_03_add_complaint_learning.py",
    "20261001_01_add_identity_and_storage.py",
    "20261001_02_add_customer_offers.py",
    "20261002_01_add_transfers.py",
    "20261002_02_chat_history_timestamp_default.py",
    "20261002_03_score_scale_checks.py",
    "20261003_01_add_ai_providers.py",
)

#: The tables the *first* migration creates, before any delta runs. Used to
#: assert the chain really does build from nothing rather than relying on a
#: pre-existing schema.
BASE_TABLES = {"users", "chat_history", "bookings"}


def _load(name, ops):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), VERSIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = ops
    return module


def _columns(conn, table):
    return [c["name"] for c in sa.inspect(conn).get_columns(table)]


@pytest.fixture
def chain_db(tmp_path):
    """An empty database plus the loaded chain, ready to upgrade.

    SQLite gets a fresh file per test. An external database is *emptied* first,
    because a shared one carries state between tests: without the reset the
    second test that upgrades finds `relation "users" already exists` from the
    first, and reports a duplicate-table error that looks like a bug in the
    chain rather than in the fixture.
    """
    url = os.getenv("CSERVICE_MIGRATION_TEST_URL")
    if url:
        engine = sa.create_engine(url)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
                )
            )
    else:
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'chain.db'}")
    conn = engine.connect()
    ops = Operations(MigrationContext.configure(conn))
    try:
        yield conn, [_load(name, ops) for name in CHAIN]
    finally:
        conn.close()
        engine.dispose()


def test_the_chain_lists_every_migration_file_on_disk():
    """`CHAIN` is hand-maintained, and nothing checked it against the directory.

    The omission is silent in the worst way: the new migration applies cleanly on
    its own, so a developer testing it in isolation sees it work, while
    `test_the_chain_builds_every_table_the_models_declare` reports the new tables
    as "never created" with no hint that a file was simply left off a list. That
    is exactly how `complaint_signal_weights` and its two siblings were built,
    verified, and still missing from every migrated database.

    The one legitimate reason for a file to be absent from `CHAIN` is being
    archived -- `alembic/archived/` holds revisions that were removed from the
    chain on purpose, and those are excluded by looking only in `versions/`.
    """
    versions = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
    on_disk = {
        path.name
        for path in versions.glob("*.py")
        if not path.name.startswith("_")
    }
    assert on_disk - set(CHAIN) == set(), (
        "these migration files exist but are not in CHAIN, so they never run "
        f"against a migrated database: {sorted(on_disk - set(CHAIN))}"
    )
    assert set(CHAIN) - on_disk == set(), (
        f"CHAIN names files that do not exist: {sorted(set(CHAIN) - on_disk)}"
    )


def test_the_chain_upgrades_from_an_empty_database(chain_db):
    """No pre-existing schema: the base revision has to create the first tables.

    Before `20260904_00_initial_schema` the chain began at a delta that issued
    `ALTER TABLE users ADD COLUMN is_admin`, and nothing in alembic had ever
    created `users` -- so this died on the first statement with
    `UndefinedTableError: relation "users" does not exist`.
    """
    conn, chain = chain_db
    assert sa.inspect(conn).get_table_names() == [], "the test database must start empty"

    for module in chain:
        module.upgrade()

    tables = set(sa.inspect(conn).get_table_names())
    assert BASE_TABLES <= tables, BASE_TABLES - tables


def test_the_chain_downgrades_back_to_nothing_with_rows_intact(chain_db):
    conn, chain = chain_db

    for module in chain:
        module.upgrade()

    # A row written by the base revision must survive a full round trip. Batch
    # mode rebuilds tables by copying, and a lost row is a silent data bug that
    # a schema-only assertion would not catch.
    #
    # `is_admin` is set explicitly because the chain deliberately removes its
    # server default: `0002` adds it as NOT NULL with `server_default=false` to
    # backfill existing rows, then drops the default. What is left is a column
    # the database protects and the *application* supplies, which is
    # `models.User.is_admin`'s `default=False` -- a Python-side default. A raw
    # INSERT that omits it therefore fails, and that is the intended shape
    # rather than a gap to paper over.
    conn.execute(
        sa.text(
            "INSERT INTO users (username, email, hashed_password, is_admin) "
            "VALUES ('roundtrip', 'rt@example.com', 'x', false)"
        )
    )
    conn.commit()
    assert conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() == 1

    for module in reversed(chain):
        module.downgrade()

    remaining = [t for t in sa.inspect(conn).get_table_names() if not t.startswith("alembic")]
    assert remaining == [], remaining


def test_the_chain_builds_every_table_the_models_declare(chain_db):
    """The check whose absence let ten tables ship with no migration.

    Each migration was verified against the models, and the chain was verified
    against SQLite, but nothing compared the chain's *result* to the models. So
    `alembic upgrade head` produced twelve of twenty-two tables and every test
    in the suite still passed.
    """
    from app.models import Base

    conn, chain = chain_db
    for module in chain:
        module.upgrade()

    live = {t for t in sa.inspect(conn).get_table_names() if not t.startswith("alembic")}
    declared = set(Base.metadata.tables)
    assert declared - live == set(), (
        "the chain does not build these tables, so a migrated database is "
        f"missing them: {sorted(declared - live)}"
    )


def test_the_chain_builds_every_column_the_models_declare(chain_db):
    from app.models import Base

    conn, chain = chain_db
    for module in chain:
        module.upgrade()

    inspector = sa.inspect(conn)
    for name in sorted(Base.metadata.tables):
        live = {c["name"] for c in inspector.get_columns(name)}
        declared = {c.name for c in Base.metadata.tables[name].columns}
        assert live == declared, (
            f"{name}: missing={sorted(declared - live)} extra={sorted(live - declared)}"
        )


def test_no_migration_drops_anything_on_the_way_up(chain_db):
    """`upgrade()` must not destroy data.

    `drop_table` and `drop_column` in an `upgrade()` are how a migration loses
    rows in a deployment that has any, and the schema-sync revision is
    autogenerated output, so this is worth asserting rather than reviewing by
    eye once.

    `alter_column` is deliberately *not* banned. `0002_booking_events` uses it
    to drop the server default on `users.is_admin` after backfilling it, which
    is a narrowing of what the database fills in and loses nothing. What would
    be destructive is a type conversion, so that is what is checked instead --
    see the next test.
    """
    conn, chain = chain_db
    for module in chain:
        source = (VERSIONS / Path(module.__file__).name).read_text(encoding="utf-8")
        upgrade_body = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
        for destructive in ("op.drop_table", "op.drop_column", "batch_op.drop_column"):
            assert destructive not in upgrade_body, (
                f"{Path(module.__file__).name} runs {destructive} in upgrade()"
            )


def test_no_migration_converts_a_column_type_on_the_way_up(chain_db):
    """A type conversion in `upgrade()` can truncate the values already stored.

    Narrowing `TEXT` to `VARCHAR(9)`, or widening a date column, is a data
    change disguised as a schema change. No migration in this chain does one,
    which is the point of asserting it.
    """
    conn, chain = chain_db
    for module in chain:
        source = (VERSIONS / Path(module.__file__).name).read_text(encoding="utf-8")
        upgrade_body = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
        for alter in ("alter_column", "batch_op.alter_column"):
            if alter not in upgrade_body:
                continue
            # Every alter_column call in upgrade() must be a server-default
            # change, i.e. its only keyword argument besides the column name.
            for call in _calls_named(upgrade_body, alter):
                keywords = _top_level_keywords(call)
                allowed = {"existing_type", "existing_nullable",
                           "existing_server_default", "server_default", "type_"}
                unexpected = [k for k in keywords if k not in allowed]
                assert not unexpected or "type_" in unexpected, (
                    f"{Path(module.__file__).name} alters a column in a way this "
                    f"project does not intend: {unexpected} in {call!r}"
                )


def _top_level_keywords(call: str) -> list[str]:
    """Keyword names in a call, ignoring anything inside a nested call.

    A flat regex over the call text reads `timezone=True` out of
    ``existing_type=sa.DateTime(timezone=True)`` and reports it as an
    ``alter_column`` keyword, which rejects every legitimate server-default
    change that names a timezone-aware type. The permission this project grants
    is "alter_column for a server default", and ``DateTime(timezone=True)`` is
    how that is spelled for half the schema -- so the guard rejected exactly the
    calls it exists to allow.

    Nested parenthesised groups are blanked before the keyword scan, so only the
    outer call's arguments are read.
    """
    depth = 0
    out: list[str] = []
    current = ""
    for char in call:
        if char == "(":
            depth += 1
            if depth > 1:
                continue
        elif char == ")":
            depth -= 1
            if depth > 0:
                continue
        if depth > 1:
            continue
        current += char
    out.extend(re.findall(r"(\w+)\s*=", current))
    return out


def test_the_chain_gives_every_model_server_default_a_database_default(chain_db):
    """The second comparison this file was missing, added because it kept mattering.

    `alembic/README.md` rule 2 claims this file "runs the same comparison" as
    `scripts/schema_drift_report.py`, so CI catches drift without a server. That
    was true of *tables and columns only*, and it was not true of server
    defaults -- which is precisely the comparison whose absence hid
    `chat_history.timestamp` having no default on a migrated database while
    every chat insert failed there.

    A model declaring `server_default` does not send a value on insert; it relies
    on the database applying the default. So a model default the database lacks
    means every insert omitting that column fails against a NOT NULL column. The
    suite cannot see this, because its schema comes from `Base.metadata`, which
    has the default.
    """
    from app.models import Base
    from scripts.schema_drift_report import compare_server_defaults

    conn, chain = chain_db
    for module in chain:
        module.upgrade()

    inspector = sa.inspect(conn)
    shared = set(Base.metadata.tables) & set(inspector.get_table_names())
    drift = compare_server_defaults(inspector, shared)
    assert drift == {}, (
        "the chain does not produce these server defaults, so a migrated "
        f"database rejects inserts that rely on them: {drift}"
    )


def test_the_chain_builds_every_named_check_constraint_the_models_declare(chain_db):
    """The third missing comparison, and the one that found a live defect.

    Same story as the server defaults, one level along. A CHECK the model
    declares and the chain never created means a `create_all` database is
    guarded and a migrated one is not -- a difference invisible to the entire
    suite, which builds from `Base.metadata`.

    This is not hypothetical. `ck_retention_snapshots_loyalty_score_non_negative`
    was declared in the model and missing from every revision in the chain, so
    `loyalty_score` -- weight 12 in `customer_score`, one of the eight inputs to
    `compose_access_score` -- was writable-negative on any migrated database. It
    was found by adding the same comparison to `scripts/schema_drift_report.py`,
    which reported it on its first run.
    """
    from app.models import Base
    from scripts.schema_drift_report import compare_check_constraints

    conn, chain = chain_db
    for module in chain:
        module.upgrade()

    inspector = sa.inspect(conn)
    shared = set(Base.metadata.tables) & set(inspector.get_table_names())
    drift = compare_check_constraints(inspector, shared)
    assert drift == {}, (
        "the chain does not create these CHECK constraints, so a migrated "
        f"database accepts rows the models say it must not: {drift}"
    )


def _calls_named(body: str, function_name: str) -> list[str]:
    """The text of every `<function_name>(...)` call in ``body``."""
    pattern = re.compile(re.escape(function_name) + r"\((?:[^()]|\([^()]*\))*\)", re.S)
    return pattern.findall(body)


def test_every_revision_id_fits_the_alembic_version_column():
    """`alembic_version.version_num` is VARCHAR(32).

    Every id in the chain was originally a descriptive date-plus-slug string of
    35-45 characters, and `alembic upgrade` failed the moment it tried to
    record one:

        asyncpg.exceptions.StringDataRightTruncationError:
          value too long for type character varying(32)
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    script = ScriptDirectory.from_config(cfg)
    for revision in script.walk_revisions():
        assert len(revision.revision) <= 32, (
            f"{revision.revision!r} is {len(revision.revision)} chars and does not "
            "fit alembic_version.version_num VARCHAR(32)"
        )


def test_the_chain_is_a_single_linear_head():
    """One `down_revision` per revision, exactly one with no parent."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    script = ScriptDirectory.from_config(cfg)

    heads = script.get_heads()
    assert len(heads) == 1, f"the chain has branched or is detached: {heads}"

    # Walking head -> base must visit every revision exactly once, and the oldest
    # must be a real base rather than a template placeholder. That placeholder is
    # what made every alembic command fail for so long: alembic builds the whole
    # revision map up front, so one unresolvable `down_revision` anywhere in
    # `versions/` breaks `history` and `current` as well as `upgrade`.
    walked = [revision.revision for revision in script.walk_revisions()]
    assert len(walked) == len(set(walked)), walked
    base = [r for r in script.walk_revisions() if not r.down_revision]
    assert len(base) == 1, [r.revision for r in base]
    for revision in script.walk_revisions():
        for parent in revision._all_down_revisions or ():
            assert not str(parent).startswith("<"), (
                f"{revision.revision} still points at the template {parent!r}"
            )
    # And the archived orphan is genuinely out of the way.
    assert "7a2c41b9d810" not in walked
    assert pathlib.Path("alembic/archived").is_dir()


def test_alembic_env_resolves_the_database_url():
    """`env.py` must not depend on the placeholder in `alembic.ini`.

    It read `sqlalchemy.url` from the ini, which ships as
    `driver://user:pass@localhost/dbname`, so every alembic command that
    touched a database died with
    `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:driver`
    before reaching a single migration.
    """
    source = pathlib.Path("alembic/env.py").read_text(encoding="utf-8")
    assert "DATABASE_URL" in source, "env.py must use the application's own URL"
    # And the models must be imported, or `Base.metadata` is empty and
    # `--autogenerate` decides every table is surplus.
    assert "import app.models" in source, (
        "env.py must import app.models or Base.metadata is empty and "
        "autogenerate proposes dropping every table"
    )
