"""Dynamic data partition lifecycle workers.

Append-only volumes (audit trails, security signals) grow without bound unless
someone retires old data. This module is a config-driven lifecycle manager for
time-based partitions:

- ``PARTITION_POLICIES`` — each policy binds a table to an interval
  (daily/weekly/monthly), an enabled flag, and a retention window in days.
- ``PartitionManager`` — creates the *current* partition when it is missing
  (``CREATE TABLE ... (LIKE parent INCLUDING ALL)``), knows what is active,
  can archive a partition (rename to ``...__archived``), and drops partitions
  whose covered period is older than the policy retention.
- ``run_partition_cycle_forever`` — the long-running worker used by the app
  lifespan. Every SQL statement is built from the config table; nothing is
  hardcoded to a table name, so adding a partitioned volume is config-only.

The lifecycle is fully opt-in: the worker is only started when
``CSERVICE_PARTITION_WORKER=1``, so existing single-process behavior (and the
test suite, which never reaches the real database) is unaffected.

Expansion notes (additive):

- Identifier validation. Every table/key that reaches SQL goes through
  :func:`validate_identifier`, so a policy typo cannot inject SQL.
- Declarative attach/detach. ``build_attach_sql`` / ``build_detach_sql`` make a
  cloned partition a real PostgreSQL partition of its parent.
- Dry runs. :meth:`PartitionManager.plan_lifecycle` is pure — it reports what a
  cycle *would* create and drop without touching the database.
- Legal hold. A partition under hold is never dropped, however old it is.
- Reconciliation. ``parse_partition_rows`` / :meth:`adopt_partitions` rehydrate
  the in-process registry from ``pg_class`` after a restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.db import _int_env, engine as default_engine, retry_async

logger = logging.getLogger(__name__)

PARTITION_WORKER_ENABLED = os.getenv("CSERVICE_PARTITION_WORKER", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PARTITION_CYCLE_SECONDS = _int_env("PARTITION_CYCLE_SECONDS", 3600)

INTERVAL_FORMATS: dict[str, str] = {
    "daily": "%Y-%m-%d",
    "weekly": "%Y-W%W",
    "monthly": "%Y-%m",
}

PARTITION_ARCHIVE_SUFFIX = "__archived"
# Identifiers that may reach SQL. Table and key names come from config and are
# interpolated unescaped, so this is the guard that keeps them harmless. Two
# patterns: a strict one for table/policy names, and a partition-name pattern
# that also admits the ``-`` inside period keys such as ``2026-09-25``.
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
PARTITION_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*__[A-Za-z0-9_-]+$"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
_PARTITION_NAME_RE = re.compile(PARTITION_NAME_PATTERN)
IDENTIFIER_MAX_LENGTH = 63  # PostgreSQL NAMEDATALEN - 1

# Required keys of a policy row; everything else is an operator's annotation.
PARTITION_POLICY_FIELDS = ("policy_id", "table", "interval", "retention_days", "enabled")

# Catalog query used to reconcile the registry with the live database.
PARTITION_CATALOG_SQL = (
    "SELECT c.relname AS partition, "
    "split_part(c.relname, '__', 1) AS parent, "
    "to_char(c.relname, 'UTF8') AS name "
    "FROM pg_class c "
    "JOIN pg_inherits i ON i.inhrelid = c.oid "
    "WHERE c.relkind IN ('r', 'p')"
)


def period_key(interval: str, moment: datetime | None = None) -> str:
    """Partition key for a moment: e.g. ``2026-09`` (monthly) or ``2026-09-25`` (daily)."""
    moment = moment or datetime.now(timezone.utc)
    fmt = INTERVAL_FORMATS.get(interval)
    if fmt is None:
        raise ValueError(f"unsupported partition interval: {interval}")
    if interval == "weekly":
        return moment.strftime("%Y-W%W")
    return moment.strftime(fmt)


def period_end(interval: str, key: str) -> datetime:
    """Instant *just past* the period a partition covers (for retention math)."""
    if interval == "monthly":
        first = datetime.strptime(key, "%Y-%m").replace(tzinfo=timezone.utc)
        year = first.year + (1 if first.month == 12 else 0)
        month = 1 if first.month == 12 else first.month + 1
        return datetime(year, month, 1, tzinfo=timezone.utc)
    if interval == "weekly":
        start = datetime.strptime(key, "%Y-W%W").replace(tzinfo=timezone.utc)
        return start + timedelta(days=7)
    start = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return start + timedelta(days=1)


def partition_name(table: str, key: str) -> str:
    return f"{table}__{key}"


def split_partition_name(partition: str) -> tuple[str, str] | None:
    """``("audit_log_entries", "2026-09")`` for ``audit_log_entries__2026-09``.

    Purely structural: it splits on the last ``__`` and strips the archive
    suffix. It cannot tell a real partition from a table that merely contains
    ``__`` — deciding that needs the policy table, which is why
    :meth:`PartitionManager.adopt_partitions` is the caller that filters on it.
    """
    name = str(partition or "")
    if not name.endswith(PARTITION_ARCHIVE_SUFFIX):
        parts = name.split("__")
    else:
        parts = name[: -len(PARTITION_ARCHIVE_SUFFIX)].split("__")
    if len(parts) < 2:
        return None
    table, key = "__".join(parts[:-1]), parts[-1]
    if not table or not key:
        return None
    return table, key


def validate_identifier(name: Any, *, label: str = "identifier") -> str:
    """Reject anything that could not be a plain SQL identifier.

    Policy table names and period keys are configuration, but they are
    interpolated into DDL without escaping — so they are validated here, once,
    at the only place they enter the SQL builders.
    """
    candidate = str(name or "")
    if not _IDENTIFIER_RE.match(candidate):
        raise ValueError(f"invalid {label}: {candidate!r}")
    if len(candidate) > IDENTIFIER_MAX_LENGTH:
        raise ValueError(
            f"invalid {label}: {candidate!r} exceeds {IDENTIFIER_MAX_LENGTH} characters"
        )
    return candidate


def validate_partition_name(name: Any) -> str:
    """Validate a full ``<table>__<key>`` partition name.

    Wider than :func:`validate_identifier` because period keys legitimately
    contain ``-``; still narrow enough that nothing but a table and a period can
    reach the DDL builders.
    """
    candidate = str(name or "")
    if not _PARTITION_NAME_RE.match(candidate):
        raise ValueError(f"invalid partition name: {candidate!r}")
    if len(candidate) > IDENTIFIER_MAX_LENGTH:
        raise ValueError(
            f"invalid partition name: {candidate!r} exceeds "
            f"{IDENTIFIER_MAX_LENGTH} characters"
        )
    return candidate


def policy_for(policy: dict[str, Any]) -> dict[str, Any]:
    """Validate a policy row's SQL-facing fields, returning the policy id."""
    validate_identifier(policy.get("table"), label="partition parent table")
    return str(policy["policy_id"])


def build_partition_create_sql(policy: dict[str, Any], key: str) -> str:
    """DDL for a partition clone of the policy's parent table."""
    table = validate_identifier(policy.get("table"), label="partition parent table")
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(table, key)} "
        f"(LIKE {table} INCLUDING ALL)"
    )


def build_attach_sql(policy: dict[str, Any], key: str) -> str:
    """Attach a cloned partition to its parent as a real partition.

    Without this the child table is just a copy: the planner never prunes it
    and the parent never routes inserts into it.
    """
    table = validate_identifier(policy.get("table"), label="partition parent table")
    pname = partition_name(table, key)
    return f"ALTER TABLE {table} ATTACH PARTITION {pname} FOR VALUES FROM (MINVALUE) TO (MAXVALUE)"


def build_detach_sql(policy: dict[str, Any], key: str) -> str:
    """Detach a partition, leaving the data in place."""
    table = validate_identifier(policy.get("table"), label="partition parent table")
    return f"ALTER TABLE {table} DETACH PARTITION {partition_name(table, key)}"


def build_archive_sql(partition: str) -> str:
    name = validate_partition_name(partition)
    return f"ALTER TABLE {name} RENAME TO {name}{PARTITION_ARCHIVE_SUFFIX}"


def build_restore_sql(partition: str) -> str:
    """Reverse :func:`build_archive_sql` (un-renames an archived partition)."""
    name = validate_partition_name(partition)
    suffix = PARTITION_ARCHIVE_SUFFIX
    if not name.endswith(suffix):
        raise ValueError(f"not an archived partition: {name!r}")
    return f"ALTER TABLE {name} RENAME TO {name[: -len(suffix)]}"


def build_drop_sql(partition: str) -> str:
    name = validate_partition_name(partition)
    return f"DROP TABLE IF EXISTS {name}"


# Policies: which volumes are partitioned, how often, and how long to keep.
PARTITION_POLICIES: list[dict[str, Any]] = [
    {
        "policy_id": "audit_log_monthly",
        "table": "audit_log_entries",
        "interval": "monthly",
        "retention_days": 90,
        "enabled": True,
        "note": "immutable audit trail — keep 90 days hot, drop the rest",
    },
    {
        "policy_id": "security_events_monthly",
        "table": "security_events",
        "interval": "monthly",
        "retention_days": 180,
        "enabled": True,
        "note": "zero-trust security signals — keep for forensics, then drop",
    },
]


def build_partition_policy(
    policy_id: str,
    table: str,
    interval: str = "monthly",
    *,
    retention_days: int = 90,
    enabled: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """Build a validated policy row.

    The alternative — hand-writing a dict — is how an unsupported interval or a
    table name that is not an identifier reaches the DDL builders.
    """
    if interval not in INTERVAL_FORMATS:
        raise ValueError(f"unsupported partition interval: {interval}")
    validate_identifier(table, label="partition parent table")
    validate_identifier(policy_id, label="partition policy id")
    days = int(retention_days)
    if days < 0:
        raise ValueError(f"retention_days must be >= 0: {retention_days}")
    return {
        "policy_id": str(policy_id),
        "table": str(table),
        "interval": str(interval),
        "retention_days": days,
        "enabled": bool(enabled),
        "note": note,
    }


def validate_policies(policies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check a whole policy table; raises on the first problem it finds."""
    seen: set[str] = set()
    for policy in policies or []:
        missing = [field for field in PARTITION_POLICY_FIELDS if field not in policy]
        if missing:
            raise ValueError(
                f"partition policy is missing {', '.join(missing)}: {policy.get('policy_id')!r}"
            )
        if policy["policy_id"] in seen:
            raise ValueError(f"duplicate partition policy: {policy['policy_id']}")
        seen.add(policy["policy_id"])
        policy_for(policy)
        if policy["interval"] not in INTERVAL_FORMATS:
            raise ValueError(
                f"unsupported partition interval: {policy['interval']} "
                f"(policy {policy['policy_id']})"
            )
        if int(policy["retention_days"]) < 0:
            raise ValueError(
                f"retention_days must be >= 0: {policy['retention_days']} "
                f"(policy {policy['policy_id']})"
            )
    return [dict(policy) for policy in policies or []]


class PartitionManager:
    """Registry + executor for time-based partition lifecycles."""

    def __init__(self, policies: list[dict[str, Any]] | None = None):
        self._policies = list(policies if policies is not None else PARTITION_POLICIES)
        self._active: dict[str, dict[str, Any]] = {}
        self._legal_holds: set[str] = set()
        self._last_plan: dict[str, Any] | None = None

    def policies(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._policies]

    def policy(self, policy_id: str) -> dict[str, Any] | None:
        found = next((p for p in self._policies if p.get("policy_id") == policy_id), None)
        return dict(found) if found else None

    # --- legal hold ---------------------------------------------------------

    def set_legal_hold(
        self, table: str, key: str, *, hold: bool = True, reason: str = ""
    ) -> dict[str, Any]:
        """Pin a partition in place regardless of how old it is.

        A litigation hold or an open investigation outranks the retention
        policy, so the drop path has to be able to lose.
        """
        pname = partition_name(table, key)
        if hold:
            self._legal_holds.add(pname)
        else:
            self._legal_holds.discard(pname)
        return {
            "partition": pname,
            "hold": hold,
            "reason": reason,
            "held_partitions": sorted(self._legal_holds),
        }

    def legal_holds(self) -> list[str]:
        return sorted(self._legal_holds)

    def is_held(self, partition: str) -> bool:
        return str(partition) in self._legal_holds

    # --- dry run ------------------------------------------------------------

    def _retention_expiry(
        self, policy: dict[str, Any], key: str, moment: datetime
    ) -> datetime:
        end = period_end(policy["interval"], key)
        return end + timedelta(days=int(policy.get("retention_days", 90)))

    def plan_lifecycle(self, moment: datetime | None = None) -> dict[str, Any]:
        """What a cycle would create and drop right now — with no SQL.

        Operators need to see the blast radius of a cycle (and a ``dry_run``
        preview is the only safe way to review it) before letting the worker
        run against production.
        """
        reference = moment or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        create: list[dict[str, Any]] = []
        drop: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for policy in self._policies:
            if not policy.get("enabled", True):
                skipped.append(
                    {"policy_id": policy["policy_id"], "reason": "policy_disabled"}
                )
                continue
            key = period_key(policy["interval"], reference)
            pname = partition_name(policy["table"], key)
            if pname not in self._active:
                create.append({"policy_id": policy["policy_id"], "partition": pname, "key": key})
        for pname, record in sorted(self._active.items()):
            policy = self.policy(record["policy_id"])
            if policy is None or not policy.get("enabled", True):
                skipped.append(
                    {"partition": pname, "reason": "policy_unavailable_or_disabled"}
                )
                continue
            expires_on = self._retention_expiry(policy, record["key"], reference)
            if reference <= expires_on:
                continue
            row = {
                "policy_id": record["policy_id"],
                "partition": pname,
                "key": record["key"],
                "retention_days": int(policy.get("retention_days", 90)),
                "expired_on": expires_on.isoformat(),
            }
            if pname in self._legal_holds:
                held.append(row)
            else:
                drop.append(row)
        plan = {
            "reference": reference,
            "create": create,
            "drop": drop,
            "held": held,
            "skipped": skipped,
            "create_count": len(create),
            "drop_count": len(drop),
            "held_count": len(held),
        }
        self._last_plan = plan
        return plan

    def last_plan(self) -> dict[str, Any] | None:
        return self._last_plan

    # --- reconciliation -----------------------------------------------------

    def adopt_partitions(self, records: Any) -> list[dict[str, Any]]:
        """Rehydrate the registry from :func:`parse_partition_rows` output.

        The in-process registry starts empty on every boot, so without this the
        manager would happily re-create partitions that already exist and never
        know which ones it may drop.
        """
        adopted: list[dict[str, Any]] = []
        stamp = datetime.now(timezone.utc).isoformat()
        for record in records or []:
            pname = str(record.get("partition") or "")
            table, key = split_partition_name(pname)
            if not table or not key:
                continue
            policy = next((p for p in self._policies if p.get("table") == table), None)
            if policy is None:
                continue
            self._active[pname] = {
                "policy_id": policy["policy_id"],
                "table": table,
                "key": key,
                "interval": policy["interval"],
                "created_at": record.get("created_at") or stamp,
                "adopted": True,
            }
            adopted.append({"partition": pname, "policy_id": policy["policy_id"], "key": key})
        return adopted

    async def ensure_partition(
        self,
        engine,
        policy: dict[str, Any],
        moment: datetime | None = None,
    ) -> dict[str, Any]:
        """Create the current partition for a policy if it does not exist."""
        key = period_key(policy["interval"], moment)
        pname = partition_name(policy["table"], key)
        if pname in self._active:
            return {"policy_id": policy["policy_id"], "partition": pname, "created": False}
        async with engine.begin() as conn:
            await conn.execute(text(build_partition_create_sql(policy, key)))
        self._active[pname] = {
            "policy_id": policy["policy_id"],
            "table": policy["table"],
            "key": key,
            "interval": policy["interval"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"policy_id": policy["policy_id"], "partition": pname, "created": True}

    async def attach_partition(
        self,
        engine,
        policy: dict[str, Any],
        key: str,
    ) -> dict[str, Any]:
        """Attach a cloned partition to its parent (idempotent at the SQL level)."""
        pname = partition_name(policy["table"], key)
        async with engine.begin() as conn:
            await conn.execute(text(build_attach_sql(policy, key)))
        return {"policy_id": policy["policy_id"], "partition": pname, "attached": True}

    async def archive_partition(
        self,
        engine,
        table: str,
        key: str,
    ) -> dict[str, Any]:
        """Rename a partition to ``...__archived`` (keep it, stop serving it)."""
        pname = partition_name(table, key)
        if pname in self._legal_holds:
            raise ValueError(f"partition is under legal hold: {pname}")
        async with engine.begin() as conn:
            await conn.execute(text(build_archive_sql(pname)))
        self._active.pop(pname, None)
        return {"partition": pname, "archived": True}

    async def restore_partition(
        self,
        engine,
        table: str,
        key: str,
    ) -> dict[str, Any]:
        """Reverse an archive: un-rename the partition and re-register it."""
        pname = partition_name(table, key)
        archived = f"{pname}{PARTITION_ARCHIVE_SUFFIX}"
        policy = next((p for p in self._policies if p.get("table") == table), None)
        if policy is None:
            raise ValueError(f"no policy for partition table: {table}")
        async with engine.begin() as conn:
            await conn.execute(text(build_restore_sql(archived)))
        self._active[pname] = {
            "policy_id": policy["policy_id"],
            "table": table,
            "key": key,
            "interval": policy["interval"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "restored": True,
        }
        return {
            "partition": pname,
            "archived_partition": archived,
            "restored": True,
        }

    async def drop_expired_partitions(
        self,
        engine,
        moment: datetime | None = None,
        *,
        honor_holds: bool = True,
    ) -> list[dict[str, Any]]:
        """Drop partitions whose covered period is past the policy retention.

        A partition under legal hold is skipped unless ``honor_holds`` is
        turned off, which is the deliberate, audited way to break a hold.
        """
        moment = moment or datetime.now(timezone.utc)
        dropped: list[dict[str, Any]] = []
        for pname, record in list(self._active.items()):
            policy_id = record["policy_id"]
            policy = next(
                (p for p in self._policies if p["policy_id"] == policy_id),
                None,
            )
            if policy is None or not policy.get("enabled", True):
                continue
            if honor_holds and pname in self._legal_holds:
                continue
            end = period_end(policy["interval"], record["key"])
            retention = timedelta(days=int(policy.get("retention_days", 90)))
            if moment - end > retention:
                async with engine.begin() as conn:
                    await conn.execute(text(build_drop_sql(pname)))
                self._active.pop(pname, None)
                dropped.append(
                    {
                        "policy_id": policy_id,
                        "partition": pname,
                        "retention_days": int(policy.get("retention_days", 90)),
                    }
                )
        return dropped

    def list_partitions(self) -> list[dict[str, Any]]:
        return [
            {
                "policy_id": record["policy_id"],
                "table": record["table"],
                "key": record["key"],
                "interval": record["interval"],
                "partition": pname,
                "created_at": record["created_at"],
            }
            for pname, record in sorted(self._active.items())
        ]

    async def run_cycle(
        self, engine, moment: datetime | None = None, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """One full lifecycle pass: ensure current partitions, drop expired.

        With ``dry_run=True`` nothing is executed — the pass is planned and the
        same shape is returned, so a caller can preview a cycle safely.
        """
        if dry_run:
            plan = self.plan_lifecycle(moment)
            return {
                "ensured": [
                    {
                        "policy_id": row["policy_id"],
                        "partition": row["partition"],
                        "created": True,
                        "dry_run": True,
                    }
                    for row in plan["create"]
                ],
                "dropped": [{**row, "dry_run": True} for row in plan["drop"]],
                "held": plan["held"],
                "skipped": plan["skipped"],
                "dry_run": True,
            }
        ensured = [
            await self.ensure_partition(engine, policy, moment)
            for policy in self._policies
            if policy.get("enabled", True)
        ]
        dropped = await self.drop_expired_partitions(engine, moment)
        # Stamped so a completed cycle leaves a reviewable trace even though the
        # plan that produced it is no longer current.
        self._last_plan = self.plan_lifecycle(moment)
        return {"ensured": ensured, "dropped": dropped, "dry_run": False}


_default_manager: PartitionManager | None = None


def get_default_manager() -> PartitionManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = PartitionManager()
    return _default_manager


def set_default_manager(manager: PartitionManager | None) -> None:
    global _default_manager
    _default_manager = manager


def parse_partition_rows(rows: Any) -> list[dict[str, Any]]:
    """Turn ``pg_class`` rows into partition records for :meth:`adopt_partitions`.

    Pure, so reconciliation can be tested without a live database. Anything that
    is not a recognisable ``<table>__<key>`` name for a known policy table is
    dropped rather than guessed at.
    """
    parsed: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            name = row.get("partition") or row.get("relname") or row.get("name")
        else:
            name = getattr(row, "partition", None) or getattr(row, "relname", None)
        if not name:
            continue
        split = split_partition_name(str(name))
        if split is None:
            continue
        table, key = split
        parsed.append(
            {
                "partition": str(name),
                "table": table,
                "key": key,
                "archived": str(name).endswith(PARTITION_ARCHIVE_SUFFIX),
            }
        )
    return sorted(parsed, key=lambda record: record["partition"])


async def read_partition_catalog(engine) -> list[dict[str, Any]]:
    """Read the live partition inventory (see ``PARTITION_CATALOG_SQL``)."""
    async with engine.begin() as conn:
        result = await conn.execute(text(PARTITION_CATALOG_SQL))
        rows = result.fetchall() if hasattr(result, "fetchall") else result
    return parse_partition_rows(rows)


def build_partition_manager_catalog(manager: PartitionManager | None = None) -> dict[str, object]:
    manager = manager or get_default_manager()
    return {
        "policies": manager.policies(),
        "intervals": list(INTERVAL_FORMATS),
        "active_partitions": manager.list_partitions(),
        "worker_enabled": PARTITION_WORKER_ENABLED,
        "cycle_seconds": PARTITION_CYCLE_SECONDS,
        # --- expansion surface ----------------------------------------------
        "policy_ids": [policy["policy_id"] for policy in manager.policies()],
        "policy_fields": list(PARTITION_POLICY_FIELDS),
        "archive_suffix": PARTITION_ARCHIVE_SUFFIX,
        "identifier_pattern": IDENTIFIER_PATTERN,
        "identifier_max_length": IDENTIFIER_MAX_LENGTH,
        "legal_holds": manager.legal_holds(),
        "plan": {
            "create": manager.plan_lifecycle()["create"],
            "drop_count": manager.plan_lifecycle()["drop_count"],
            "held_count": manager.plan_lifecycle()["held_count"],
        },
    }


async def run_partition_cycle_forever(
    engine=None,
    manager: PartitionManager | None = None,
    interval_seconds: int | None = None,
) -> None:
    """Long-running worker: run a lifecycle pass every ``interval_seconds``.

    A failed pass is logged and skipped (never kills the process).
    """
    manager = manager or get_default_manager()
    engine = engine or default_engine
    interval = max(1.0, float(interval_seconds or PARTITION_CYCLE_SECONDS))
    while True:
        try:
            await retry_async(
                lambda: manager.run_cycle(engine),
                attempts=3,
                delay_seconds=1.0,
                logger=logger,
            )
            logger.debug("Partition cycle completed")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Partition cycle failed: %s", exc)
        await asyncio.sleep(interval)