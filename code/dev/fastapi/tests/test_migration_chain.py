"""Round-trip the whole revision chain against a real SQLite database.

Builds the schema as it looked *before* the chain's base revision, runs every
`upgrade()` in order, then every `downgrade()` in reverse, and checks the
database is back where it started. This is the test that would have caught the
three PostgreSQL-only statements in `20260905_01`, none of which could ever have
run on this project's SQLite.
"""
import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

VERSIONS = pathlib.Path("alembic/versions")
CHAIN = (
    "20260905_01_add_booking_events_and_admin_flag.py",
    "20260907_01_add_recovery_outcomes.py",
    "20260908_01_add_booking_assignments.py",
    "20260929_01_add_preference_consent_tables.py",
    "20260930_01_add_complaint_cases.py",
)


def _pre_chain_schema(conn):
    """The tables `20260905_01` expects to already exist, and nothing more.

    One `MetaData` so the foreign keys resolve during batch-mode reflection.
    Deliberately *without* `users.is_admin` or `chat_history.user_id` -- the
    base revision adds both, so pre-creating them is what made an earlier
    attempt at this harness fail with "duplicate column name".
    """
    md = sa.MetaData()
    sa.Table(
        "users", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("email", sa.String(100), nullable=False),
        sa.Column("full_name", sa.String(100)),
        sa.Column("hashed_password", sa.String(255), nullable=False),
    )
    sa.Table(
        "chat_history", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
    )
    sa.Table(
        "bookings", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_date", sa.DateTime, nullable=False),
    )
    md.create_all(conn)
    conn.execute(
        sa.text("INSERT INTO users (id, username, email, hashed_password) VALUES (1, 'u', 'u@e.com', 'x')")
    )
    conn.execute(
        sa.text("INSERT INTO chat_history (id, message, timestamp) VALUES (1, 'hi', '2026-01-01 00:00:00')")
    )
    conn.commit()


def _load(name, ops):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), VERSIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = ops
    return module


def _columns(conn, table):
    return [c["name"] for c in sa.inspect(conn).get_columns(table)]


def test_the_whole_chain_upgrades_and_downgrades(tmp_path):
    db = tmp_path / "chain.db"
    engine = sa.create_engine(f"sqlite:///{db}")
    conn = engine.connect()
    ops = Operations(MigrationContext.configure(conn))
    try:
        _pre_chain_schema(conn)
        before_tables = sorted(sa.inspect(conn).get_table_names())
        before_users = _columns(conn, "users")
        before_chat = _columns(conn, "chat_history")

        chain = [_load(name, ops) for name in CHAIN]

        for module in chain:
            module.upgrade()

        inspector = sa.inspect(conn)
        assert "is_admin" in _columns(conn, "users")
        assert "user_id" in _columns(conn, "chat_history")
        assert sorted(t for t in inspector.get_table_names() if "complaint" in t) == [
            "complaint_cases", "complaint_decisions", "complaint_events",
        ]

        for module in reversed(chain):
            module.downgrade()

        inspector = sa.inspect(conn)
        assert sorted(inspector.get_table_names()) == before_tables
        assert _columns(conn, "users") == before_users
        assert _columns(conn, "chat_history") == before_chat
        # Batch-mode rebuilds copy rows; losing them would be a silent data bug.
        assert conn.execute(sa.text("SELECT COUNT(*) FROM chat_history")).scalar() == 1
        assert conn.execute(sa.text("SELECT username FROM users")).scalar() == "u"
    finally:
        conn.close()
        engine.dispose()


def test_the_chain_is_a_single_linear_head():
    """One `down_revision` per revision, exactly one with no parent."""
    from alembic.script import ScriptDirectory
    from alembic.config import Config

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
