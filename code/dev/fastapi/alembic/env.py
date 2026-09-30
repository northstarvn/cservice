"""Alembic environment.

Two things this file has to get right, and both were wrong before.

**It must use the application's own database URL.** It used to read
``sqlalchemy.url`` from ``alembic.ini``, which ships as the placeholder
``driver://user:pass@localhost/dbname``. Every alembic command that touched a
database therefore died with ``Can't load plugin: sqlalchemy.dialects:driver``
before reaching a single migration -- ``upgrade``, ``current`` and ``history``
all broken, not just the one being run. The URL now comes from
``app.db.DATABASE_URL`` (i.e. ``DATABASE_URL``), falling back to the ini. That
also means migrations and the app cannot be pointed at different databases by
accident, which is the failure this closes.

**It must drive the migrations asynchronously.** The app is async end to end
and its URL carries an async driver (``postgresql+asyncpg://``), which the
synchronous ``engine_from_config`` cannot load at all. The online path therefore
uses ``async_engine_from_config`` inside ``asyncio.run`` and hands alembic a
*sync* facade over that connection via ``run_sync``, which is the documented way
to run async migrations.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.db import DATABASE_URL, Base

# `Base.metadata` is only populated once the model modules have been imported.
# Reading `Base` from `app.db` does not import them -- `app.db` declares the
# declarative base and nothing else -- so without this line `target_metadata`
# is an *empty* metadata object and `--autogenerate` concludes that every
# table in the database is surplus and should be dropped:
#
#     INFO [alembic.autogenerate.compare.tables] Detected removed table 'bookings'
#     INFO [alembic.autogenerate.compare.tables] Detected removed table 'users'
#
# The import is here for its side effect. `# noqa: F401` is not added because
# the import IS the point, and linters flag it precisely when it is removed.
import app.models  # noqa: F401  (imported for Base.metadata side effects)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_url() -> str:
    """The database to migrate.

    Application settings win over the ini. The ini is only a fallback for a
    caller that deliberately set it, and its shipped placeholder is detected and
    reported by name here rather than surfacing much later as a confusing
    driver-plugin error.
    """
    if DATABASE_URL:
        return DATABASE_URL
    from_ini = config.get_main_option("sqlalchemy.url", "")
    if not from_ini or "user:pass@localhost" in from_ini:
        raise RuntimeError(
            "No database URL for migrations. Set DATABASE_URL (the same value "
            "the application uses) or a real sqlalchemy.url in alembic.ini."
        )
    return from_ini


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, so no DBAPI
    is needed. Calls to ``context.execute()`` emit the SQL to the script output.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure alembic over a sync connection and run the chain."""
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run the migrations over a sync facade."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
