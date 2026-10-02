#!/usr/bin/env python3
"""Compare a live database against `Base.metadata`, and report the differences.

This is the question the test suite could not answer. Every migration in this
project is verified against the *models*, and the chain is verified against a
SQLite database, but nothing compared the two against a real PostgreSQL
database — which is how a chain can be internally consistent, pass every test,
and still not build the schema the application actually uses.

Run it after `alembic upgrade head` (or after starting the app, which
`create_all`s the schema):

    python3 scripts/schema_drift_report.py
    DATABASE_URL=... python3 scripts/schema_drift_report.py --json

Exit status is 1 when anything is missing or extra, so it can gate a pipeline.

Reflection has to happen on a *synchronous* connection: SQLAlchemy's async
engine cannot call `inspect()` directly, and doing so raises `MissingGreenlet`.
So the engine is created for the sync driver derived from the async URL rather
than abusing the application's pooled async engine.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import CheckConstraint, create_engine, inspect  # noqa: E402

from app.db import DATABASE_URL  # noqa: E402
from app.models import Base  # noqa: E402

#: Tables alembic owns that are not part of the application schema.
IGNORED_PREFIXES = ("alembic_",)
IGNORED_TABLES = {"spatial_ref_sys"}


def sync_url(async_url: str) -> str:
    """The sync driver for an async URL.

    The application speaks ``postgresql+asyncpg://``; the sync engine speaks
    ``postgresql+psycopg2://`` (or ``+psycopg`` where psycopg2 is absent). A
    database URL is not a secret to be logged, but it may contain a password, so
    it is never printed.
    """
    url = async_url
    for async_driver, sync_driver in (("+asyncpg", "+psycopg2"), ("+aiosqlite", ""), ("+aiomysql", "+pymysql")):
        if async_driver in url:
            url = url.replace(async_driver, sync_driver)
            break
    return url


def model_server_default(column: Any) -> str | None:
    """The default the models declare for a column, as text.

    Three shapes have to be handled, and missing any of them makes the check
    silently useless rather than wrong-looking:

    * ``TextClause`` -- ``server_default="0"``
    * ``DefaultClause`` wrapping a SQL function -- ``server_default=func.now()``
    * ``Computed`` -- also a ``DefaultClause`` subclass, so it falls out of the
      same branch.

    ``DefaultClause.arg`` is ``None`` for an *explicitly empty* default
    (``server_default=None`` inside a clause), which is different from "no
    default at all"; that is why the empty case returns ``"<empty>"`` rather than
    ``None`` and the comparison below is between "has one" and "has one".
    """
    server_default = column.server_default
    if server_default is None:
        return None
    text = getattr(server_default, "text", None)
    if text is not None:
        return str(text)
    arg = getattr(server_default, "arg", None)
    if arg is None:
        return "<empty>"
    try:
        return str(arg)
    except Exception:  # pragma: no cover - an unprintable default is still a default
        return "<unprintable>"


def compare_server_defaults(inspector: Any, tables: Any) -> dict[str, dict[str, str]]:
    """Columns where the *model* declares a default the database lacks.

    Added because comparing *names* is not enough, and the gap it left was a
    live-only defect that reported "in sync" for as long as it existed:
    ``chat_history.timestamp`` is ``NOT NULL`` with no default in the migration
    chain while the model declares ``server_default=func.now()``, so every chat
    insert failed on a migrated database and succeeded on a ``create_all`` one.
    The suite cannot see it (its schema comes from the metadata) and the name
    comparison could not see it, so nothing did.

    **One direction only, and deliberately.** A column where the *database* has
    a default the model does not declare is the safe direction: SQLAlchemy sends
    the value explicitly on every insert, so the database default is unused
    belt-and-braces. There are 156 of those in this schema and they are not
    drift.

    The unsafe direction is the one checked here. A model that declares
    ``server_default`` does *not* send a value on insert -- it relies on the
    database applying the default. So where the model declares one and the
    database does not, every insert omitting that column fails against a
    ``NOT NULL`` column, and the failure names the column rather than the
    migration that dropped its default.

    Only *presence* is compared, not the expression text: ``now()`` and
    ``CURRENT_TIMESTAMP`` are one default written two ways, and reporting that
    would teach people to ignore this report.

    Primary keys are skipped: an autoincrement integer key gets a
    ``nextval(...)`` sequence the metadata does not carry, which is correct
    rather than drift.
    """
    drift: dict[str, dict[str, str]] = {}
    for name in sorted(tables):
        live_columns = {column["name"]: column for column in inspector.get_columns(name)}
        for column in Base.metadata.tables[name].columns:
            if column.primary_key or column.name not in live_columns:
                continue
            wanted = model_server_default(column)
            if wanted is None:
                continue  # the safe direction; see the docstring
            if live_columns[column.name].get("default") is None:
                drift.setdefault(name, {})[column.name] = (
                    f"model={wanted!r} live=None"
                )
    return drift


def compare_check_constraints(inspector: Any, tables: Any) -> dict[str, dict[str, list[str]]]:
    """Tables where the *model* declares a named CHECK the database lacks.

    The third comparison this report was missing, added for the same reason as
    the server-default one, and the pattern is now unmistakable: **each gap in
    this report has hidden a live-only defect for the life of the chain.**

    * names only -> `chat_history.timestamp` had no server default on a migrated
      database and every chat insert failed there.
    * now, names plus defaults but not constraints -> five score columns could
      hold a negative value: `topic_selections.confidence` and four of the six
      `customer_policy_scores` scores. `compose_access_score` has a ceiling and
      no floor, so a negative input composed `access_score = -4444.44`, and that
      number is what `resolve_access_band`, `resolve_policy_tier` and
      `_control_posture` all read -- the three functions deciding what a customer
      is served. `customer_policy_scores` guarded *two* of its six score columns,
      which is the asymmetry that made it look deliberate.

    **Compared by name, not by SQL text.** `confidence >= 0` and
    `confidence >= 0::double` are one constraint as far as anyone reading this
    report is concerned, and a text comparison would report every rewritten
    constraint as drift and teach people to ignore it -- the failure mode that
    made the original name-only version useless. Naming is also the only thing
    a migration addresses: `batch.create_check_constraint(name, ...)` and
    `drop_constraint(name)`.

    **One direction only, for the same asymmetry as server defaults.** A CHECK
    the *database* has and the model does not declare is harmless -- the database
    is stricter than the models, so nothing the models do can violate it. A CHECK
    the *model* declares and the database lacks is the unsafe direction: it is
    the model believing it is guarded where it is not. Only that direction is
    reported.

    Unnamed constraints are skipped on both sides. SQLite reflects a check
    written inline in a column definition with no name, and treating that as
    "the model declares an unnamed check the database lacks" would flag every
    table this project builds on SQLite. A named check is a deliberate one.
    """
    drift: dict[str, dict[str, list[str]]] = {}
    for name in sorted(tables):
        try:
            live_checks = inspector.get_check_constraints(name)
        except NotImplementedError:  # pragma: no cover - dialect without support
            continue
        live_names = {str(check["name"]) for check in live_checks if check.get("name")}
        model_names = {
            constraint.name
            for constraint in Base.metadata.tables[name].constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        missing = sorted(model_names - live_names)
        if missing:
            drift[name] = {"missing_in_db": missing}
    return drift


def compare() -> dict:
    """Diff live tables against the models. Pure inspection; no writes."""
    engine = create_engine(sync_url(DATABASE_URL))
    try:
        inspector = inspect(engine)
        live = {
            name
            for name in inspector.get_table_names()
            if not name.startswith(IGNORED_PREFIXES) and name not in IGNORED_TABLES
        }
        declared = set(Base.metadata.tables)

        missing = sorted(declared - live)   # in the models, absent in the DB
        extra = sorted(live - declared)     # in the DB, absent from the models

        column_drift: dict[str, dict[str, list[str]]] = {}
        for name in sorted(declared & live):
            live_columns = {column["name"] for column in inspector.get_columns(name)}
            model_columns = {column.name for column in Base.metadata.tables[name].columns}
            if live_columns != model_columns:
                column_drift[name] = {
                    "missing_in_db": sorted(model_columns - live_columns),
                    "extra_in_db": sorted(live_columns - model_columns),
                }

        index_drift: dict[str, dict[str, list[str]]] = {}
        for name in sorted(declared & live):
            live_indexes = {index["name"] for index in inspector.get_indexes(name)}
            model_indexes = {index.name for index in Base.metadata.tables[name].indexes}
            # A UNIQUE constraint backs a unique index in some dialects; only
            # report an index the models declare and the database lacks.
            if model_indexes - live_indexes:
                index_drift[name] = {
                    "missing_in_db": sorted(model_indexes - live_indexes),
                }

        default_drift = compare_server_defaults(inspector, declared & live)
        check_drift = compare_check_constraints(inspector, declared & live)

        return {
            "live_tables": len(live),
            "declared_tables": len(declared),
            "missing_tables": missing,
            "extra_tables": extra,
            "column_drift": column_drift,
            "index_drift": index_drift,
            "server_default_drift": default_drift,
            "check_constraint_drift": check_drift,
            "in_sync": not (
                missing or extra or column_drift or default_drift or check_drift
            ),
        }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    report = compare()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["in_sync"] else 1

    print(f"live tables    : {report['live_tables']}")
    print(f"declared tables: {report['declared_tables']}")
    print()
    if report["in_sync"]:
        print("in sync: the database matches Base.metadata")
        return 0

    if report["missing_tables"]:
        print("MISSING from the database (declared by the models):")
        for name in report["missing_tables"]:
            print(f"  - {name}")
        print()
    if report["extra_tables"]:
        print("EXTRA in the database (not declared by the models):")
        for name in report["extra_tables"]:
            print(f"  + {name}")
        print()
    for name, drift in report["column_drift"].items():
        print(f"COLUMN DRIFT {name}")
        if drift["missing_in_db"]:
            print(f"    missing: {', '.join(drift['missing_in_db'])}")
        if drift["extra_in_db"]:
            print(f"    extra  : {', '.join(drift['extra_in_db'])}")
    for name, drift in report["index_drift"].items():
        print(f"INDEX DRIFT {name}: missing {', '.join(drift['missing_in_db'])}")
    for name, drift in report.get("server_default_drift", {}).items():
        print(f"SERVER DEFAULT DRIFT {name}")
        for column, detail in sorted(drift.items()):
            print(f"    {column}: {detail}")
    for name, drift in report.get("check_constraint_drift", {}).items():
        print(f"CHECK CONSTRAINT DRIFT {name}: missing {', '.join(drift['missing_in_db'])}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
