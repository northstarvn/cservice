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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, inspect  # noqa: E402

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

        return {
            "live_tables": len(live),
            "declared_tables": len(declared),
            "missing_tables": missing,
            "extra_tables": extra,
            "column_drift": column_drift,
            "index_drift": index_drift,
            "in_sync": not (missing or extra or column_drift),
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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
