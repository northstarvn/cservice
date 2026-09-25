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
"""
from __future__ import annotations

import asyncio
import logging
import os
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


def build_partition_create_sql(policy: dict[str, Any], key: str) -> str:
    """DDL for a partition clone of the policy's parent table."""
    return (
        f'CREATE TABLE IF NOT EXISTS {partition_name(policy["table"], key)} '
        f'(LIKE {policy["table"]} INCLUDING ALL)'
    )


def build_archive_sql(partition: str) -> str:
    return f"ALTER TABLE {partition} RENAME TO {partition}__archived"


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


class PartitionManager:
    """Registry + executor for time-based partition lifecycles."""

    def __init__(self, policies: list[dict[str, Any]] | None = None):
        self._policies = list(policies if policies is not None else PARTITION_POLICIES)
        self._active: dict[str, dict[str, Any]] = {}

    def policies(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._policies]

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

    async def archive_partition(
        self,
        engine,
        table: str,
        key: str,
    ) -> dict[str, Any]:
        """Rename a partition to ``...__archived`` (keep it, stop serving it)."""
        pname = partition_name(table, key)
        async with engine.begin() as conn:
            await conn.execute(text(build_archive_sql(pname)))
        self._active.pop(pname, None)
        return {"partition": pname, "archived": True}

    async def drop_expired_partitions(
        self,
        engine,
        moment: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Drop partitions whose covered period is past the policy retention."""
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
            end = period_end(policy["interval"], record["key"])
            retention = timedelta(days=int(policy.get("retention_days", 90)))
            if moment - end > retention:
                async with engine.begin() as conn:
                    await conn.execute(text(f"DROP TABLE IF EXISTS {pname}"))
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

    async def run_cycle(self, engine, moment: datetime | None = None) -> dict[str, Any]:
        """One full lifecycle pass: ensure current partitions, drop expired."""
        ensured = [
            await self.ensure_partition(engine, policy, moment)
            for policy in self._policies
            if policy.get("enabled", True)
        ]
        dropped = await self.drop_expired_partitions(engine, moment)
        return {"ensured": ensured, "dropped": dropped}


_default_manager: PartitionManager | None = None


def get_default_manager() -> PartitionManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = PartitionManager()
    return _default_manager


def set_default_manager(manager: PartitionManager | None) -> None:
    global _default_manager
    _default_manager = manager


def build_partition_manager_catalog(manager: PartitionManager | None = None) -> dict[str, object]:
    manager = manager or get_default_manager()
    return {
        "policies": manager.policies(),
        "intervals": list(INTERVAL_FORMATS),
        "active_partitions": manager.list_partitions(),
        "worker_enabled": PARTITION_WORKER_ENABLED,
        "cycle_seconds": PARTITION_CYCLE_SECONDS,
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