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

Expansion notes (tiered lifecycle):

- Named retention tiers. ``PARTITION_RETENTION_TIERS`` gives every volume the
  same three-stage shape — attached, then detached+archived, then dropped — with
  the numbers in one table instead of per policy.
- Guard bands. ``PARTITION_GUARD_BANDS`` pins the newest N periods per interval
  so a misconfigured retention (or a clock jump) cannot delete the partition
  currently being written to.
- Batched, reversible destruction. ``PARTITION_DROP_POLICY`` caps how much a
  single cycle may archive or drop, so a bad cycle is a reviewable event rather
  than an unrecoverable one. :meth:`PartitionManager.plan_retirement` is the
  pure preview; :meth:`PartitionManager.retire_expired` is the executor.
- Inventory findings. ``inventory_findings`` compares a live ``pg_class``
  inventory against the policy table and names the gaps: a missing current
  partition, an orphan nobody manages, an archive past its drop age.
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
# ``retention_tier`` is an optional annotation on top of ``retention_days``: it
# names a lifecycle in ``PARTITION_RETENTION_TIERS`` instead of carrying its own
# archive/drop ages, so tuning the shape of a lifecycle is a data change.
PARTITION_POLICIES: list[dict[str, Any]] = [
    {
        "policy_id": "audit_log_monthly",
        "table": "audit_log_entries",
        "interval": "monthly",
        "retention_days": 90,
        "retention_tier": "warm",
        "enabled": True,
        "note": "immutable audit trail — keep 90 days hot, drop the rest",
    },
    {
        "policy_id": "security_events_monthly",
        "table": "security_events",
        "interval": "monthly",
        "retention_days": 180,
        "retention_tier": "cold",
        "enabled": True,
        "note": "zero-trust security signals — keep for forensics, then drop",
    },
]

# --- tiered lifecycle (expansion) ---------------------------------------------
#
# Almost every append-only volume has the same three stages: recent periods stay
# attached and serving reads, older ones are detached and renamed out of the way,
# and the oldest are finally dropped. Naming that shape once means an operator
# changes a lifecycle in one place and every policy bound to it moves together.
#
# Recognised keys per tier:
#
# - ``archive_after_days``  days after the period ends before it is detached and
#   archived. ``None`` keeps the period attached until it is dropped.
# - ``drop_after_days``     days after the period ends before it is dropped.
#   ``None`` inherits the policy's own ``retention_days``, so a tier never has
#   to restate a number the policy already carries.
# - ``drop_enabled``        ``False`` makes the tier archive-only: the worker
#   will never drop a partition bound to it.
PARTITION_RETENTION_TIERS: dict[str, dict[str, Any]] = {
    "hot": {
        "description": "stay attached until the policy retention lapses, then drop",
        "archive_after_days": None,
        "drop_after_days": None,
        "drop_enabled": True,
    },
    "warm": {
        "description": "detached and archived after a hot window, dropped at the tier age",
        "archive_after_days": 30,
        "drop_after_days": 180,
        "drop_enabled": True,
    },
    "cold": {
        "description": "archived almost immediately, kept a long time for forensics",
        "archive_after_days": 1,
        "drop_after_days": 365,
        "drop_enabled": True,
    },
    "retain_only": {
        "description": "archived and never dropped by the worker; a hold break is the only exit",
        "archive_after_days": 30,
        "drop_after_days": None,
        "drop_enabled": False,
    },
}
# Used when a policy names no tier, so ``retention_days`` alone keeps its exact
# historical meaning.
DEFAULT_RETENTION_TIER = "hot"

# Guard bands: the newest N periods per interval are never retired, whatever
# their age. "The last three months" is a property of the calendar, not of one
# volume, so the band is keyed by interval and applies to every policy on it.
PARTITION_GUARD_BANDS: dict[str, dict[str, Any]] = {
    "daily": {"retain_periods": 7},
    "weekly": {"retain_periods": 4},
    "monthly": {"retain_periods": 3},
}

# Blast-radius control for the destructive pass. A cycle that would delete a
# thousand partitions should be a cycle an operator saw coming, so each stage is
# capped independently and archiving is a separate, reversible step from
# dropping.
PARTITION_DROP_POLICY: dict[str, Any] = {
    "max_archives_per_cycle": 25,
    "max_drops_per_cycle": 25,
    "archive_enabled": True,
    "drop_enabled": True,
    "guard_enabled": True,
}


def build_partition_policy(
    policy_id: str,
    table: str,
    interval: str = "monthly",
    *,
    retention_days: int = 90,
    enabled: bool = True,
    note: str = "",
    retention_tier: str | None = None,
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
    policy = {
        "policy_id": str(policy_id),
        "table": str(table),
        "interval": str(interval),
        "retention_days": days,
        "enabled": bool(enabled),
        "note": note,
    }
    # Only added when asked for, so the historical field set is unchanged.
    if retention_tier is not None:
        policy["retention_tier"] = validate_retention_tier(retention_tier)
    return policy


def validate_retention_tier(name: Any) -> str:
    """Tier names are data; a typo must fail loudly rather than fall back."""
    candidate = str(name)
    if candidate not in PARTITION_RETENTION_TIERS:
        raise ValueError(
            f"unknown partition retention tier: {candidate} "
            f"(known: {', '.join(sorted(PARTITION_RETENTION_TIERS))})"
        )
    return candidate


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
        if policy.get("retention_tier") is not None:
            try:
                validate_retention_tier(policy["retention_tier"])
            except ValueError as exc:
                raise ValueError(f"{exc} (policy {policy['policy_id']})") from exc
    return [dict(policy) for policy in policies or []]


# --- tiered lifecycle helpers (expansion) ------------------------------------


def retention_tier(policy: dict[str, Any]) -> dict[str, Any]:
    """A policy's effective lifecycle, resolved from its named tier.

    Pure, so the executor and the preview can never disagree about what a tier
    means. A policy that names no tier resolves to ``hot``, which restates the
    pre-existing behaviour exactly: stay attached, drop when ``retention_days``
    lapses.
    """
    name = validate_retention_tier(policy.get("retention_tier") or DEFAULT_RETENTION_TIER)
    tier = PARTITION_RETENTION_TIERS[name]
    policy_days = int(policy.get("retention_days", 90))
    archive_after = tier.get("archive_after_days")
    tier_drop = tier.get("drop_after_days")
    return {
        "tier": name,
        "description": tier.get("description", ""),
        "archive_after_days": None if archive_after is None else int(archive_after),
        "drop_after_days": policy_days if tier_drop is None else int(tier_drop),
        "drop_enabled": bool(tier.get("drop_enabled", True)),
        "policy_retention_days": policy_days,
        "inherits_policy_retention": tier_drop is None,
    }


def guard_band(interval: str) -> int:
    """Newest periods of ``interval`` that retirement must leave alone."""
    band = PARTITION_GUARD_BANDS.get(str(interval)) or {}
    return max(0, int(band.get("retain_periods", 0) or 0))


def batch_rows(
    rows: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``rows`` into the batch a cycle may act on and what it defers.

    The oldest partition is retired first — a partition that has been eligible
    longest is the one whose marginal value is lowest — and a ``limit`` of zero
    or less means "no cap", which is how an operator opts out of batching.
    """
    ordered = sorted(rows, key=lambda row: (row.get("age_days", 0.0), row["partition"]))
    if limit and limit > 0:
        return ordered[:limit], ordered[limit:]
    return ordered, []


def guarded_keys(policy: dict[str, Any], keys: Any) -> set[str]:
    """Which of ``keys`` the guard band protects, newest first.

    The band is compared on the *end* of each period rather than on the key
    text, so ``2026-9`` and ``2026-10`` order correctly for every interval.
    """
    if not PARTITION_DROP_POLICY.get("guard_enabled", True):
        return set()
    band = guard_band(policy.get("interval", ""))
    if band <= 0:
        return set()
    interval = policy["interval"]
    ordered = sorted(keys, key=lambda key: period_end(interval, key), reverse=True)
    return set(ordered[:band])


def period_age_days(interval: str, key: str, moment: datetime | None = None) -> float:
    """Days between the end of a period and ``moment`` (negative while open)."""
    reference = moment or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return round((reference - period_end(interval, key)).total_seconds() / 86400.0, 6)


class PartitionManager:
    """Registry + executor for time-based partition lifecycles."""

    def __init__(self, policies: list[dict[str, Any]] | None = None):
        self._policies = list(policies if policies is not None else PARTITION_POLICIES)
        self._active: dict[str, dict[str, Any]] = {}
        self._legal_holds: set[str] = set()
        self._last_plan: dict[str, Any] | None = None
        self._last_retirement: dict[str, Any] | None = None

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

    # --- tiered retirement --------------------------------------------------

    def _tier_rows(
        self, policy: dict[str, Any], record: dict[str, Any], moment: datetime
    ) -> dict[str, Any]:
        """One active partition resolved against its tier's ages."""
        tier = retention_tier(policy)
        age = period_age_days(policy["interval"], record["key"], moment)
        archive_after = tier["archive_after_days"]
        drop_after = tier["drop_after_days"]
        return {
            "policy_id": policy["policy_id"],
            "partition": partition_name(record["table"], record["key"]),
            "table": record["table"],
            "key": record["key"],
            "tier": tier["tier"],
            "age_days": age,
            "archive_due": archive_after is not None and age >= archive_after,
            "drop_due": tier["drop_enabled"] and age >= drop_after,
        }

    def plan_retirement(self, moment: datetime | None = None) -> dict[str, Any]:
        """What the *tiered* pass would archive, drop, defer and protect.

        :meth:`plan_lifecycle` is the historical plan and is unchanged. This one
        layers the three things a destructive pass needs on top of it: the
        named tier's archive stage, the guard band that protects the newest
        periods, and the per-cycle batch cap. Like its predecessor it is pure —
        a review of a retirement is the only safe way to approve one.
        """
        reference = moment or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        archive: list[dict[str, Any]] = []
        drop: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        guarded: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        for pname, record in sorted(self._active.items()):
            policy = self.policy(record["policy_id"])
            if policy is None or not policy.get("enabled", True):
                skipped.append({"partition": pname, "reason": "policy_unavailable_or_disabled"})
                continue
            rows.append(self._tier_rows(policy, record, reference))
        # The guard band is per policy, so it is resolved per policy over the
        # rows that policy actually owns.
        for policy in self._policies:
            owned = [row for row in rows if row["policy_id"] == policy["policy_id"]]
            protected = guarded_keys(policy, [row["key"] for row in owned])
            for row in owned:
                if row["drop_due"] and row["key"] in protected:
                    guarded.append({**row, "reason": "guard_band"})
                    continue
                if row["drop_due"]:
                    drop.append(row)
                elif row["archive_due"]:
                    archive.append(row)
                elif not retention_tier(policy)["drop_enabled"]:
                    retained.append({**row, "reason": "tier_disables_drop"})
        for row in rows:
            if row["partition"] in self._legal_holds:
                held.append({**row, "reason": "legal_hold"})

        archive_cap = int(PARTITION_DROP_POLICY.get("max_archives_per_cycle", 0) or 0)
        drop_cap = int(PARTITION_DROP_POLICY.get("max_drops_per_cycle", 0) or 0)
        archive_batch, archive_deferred = batch_rows(archive, archive_cap)
        drop_batch, drop_deferred = batch_rows(drop, drop_cap)
        return {
            "reference": reference,
            "tiers": sorted({row["tier"] for row in rows}),
            "archive": archive_batch,
            "drop": drop_batch,
            "held": held,
            "guarded": guarded,
            "retained": retained,
            "skipped": skipped,
            "archive_count": len(archive_batch),
            "drop_count": len(drop_batch),
            "deferred_archive_count": len(archive_deferred),
            "deferred_drop_count": len(drop_deferred),
            "held_count": len(held),
            "guarded_count": len(guarded),
        }

    def last_retirement(self) -> dict[str, Any] | None:
        return self._last_retirement

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

    async def retire_expired(
        self,
        engine,
        moment: datetime | None = None,
        *,
        honor_holds: bool = True,
        archive: bool | None = None,
        drop: bool | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute the tiered plan: detach+archive what is due, drop what is due.

        Distinct from :meth:`run_cycle` on purpose. A cycle *creates* the current
        period and clears the *historical* tail under one retention number; a
        retirement walks the named tier's stages and is separately gated, so an
        operator can archive without ever dropping, or dry-run either.

        Both stages are batched by ``PARTITION_DROP_POLICY`` and the guard band
        always wins, so the newest periods survive even with ``retention_days``
        mis-set. With ``dry_run=True`` no statement is issued.
        """
        do_archive = (
            bool(PARTITION_DROP_POLICY.get("archive_enabled", True))
            if archive is None
            else bool(archive)
        )
        do_drop = (
            bool(PARTITION_DROP_POLICY.get("drop_enabled", True))
            if drop is None
            else bool(drop)
        )
        plan = self.plan_retirement(moment)
        # A held partition is removed from both stages, which is what makes a
        # hold outrank a tier.
        held_names = {row["partition"] for row in plan["held"]}
        if honor_holds:
            archive_rows = [row for row in plan["archive"] if row["partition"] not in held_names]
        else:
            archive_rows = list(plan["archive"])
        drop_rows = [row for row in plan["drop"] if not (honor_holds and row["partition"] in held_names)]

        archived: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for row in archive_rows if do_archive else []:
            if not dry_run:
                await self.archive_partition(engine, row["table"], row["key"])
            archived.append({**row, "archived": True, "dry_run": dry_run})
        for row in drop_rows if do_drop else []:
            if not dry_run:
                async with engine.begin() as conn:
                    await conn.execute(text(build_drop_sql(row["partition"])))
                self._active.pop(row["partition"], None)
            dropped.append({**row, "dropped": True, "dry_run": dry_run})
        result = {
            "archived": archived,
            "dropped": dropped,
            "held": plan["held"],
            "guarded": plan["guarded"],
            "retained": plan["retained"],
            "skipped": plan["skipped"],
            "deferred_archives": plan["deferred_archive_count"],
            "deferred_drops": plan["deferred_drop_count"],
            "archive_stage": do_archive,
            "drop_stage": do_drop,
            "honor_holds": honor_holds,
            "dry_run": dry_run,
        }
        if not dry_run:
            self._last_retirement = result
        return result

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


def inventory_findings(
    records: Any, policies: list[dict[str, Any]] | None = None, *, moment: datetime | None = None
) -> dict[str, Any]:
    """Compare a live partition inventory against the policy table.

    The reconciliation gap, stated as findings rather than as a guess: a
    partition the manager believes it created but the database does not have, a
    partition in the database that no policy manages, and an archive that has
    outlived even its tier. Pure, so the same answer is reachable from a
    dry-run, a report and a test.
    """
    reference = moment or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    table = [dict(p) for p in (PARTITION_POLICIES if policies is None else policies)]
    by_table = {policy["table"]: policy for policy in table}
    rows = list(records or [])
    # Accept either a live ``pg_class`` result or the output of
    # ``parse_partition_rows``, which is already normalised.
    already_parsed = bool(rows) and isinstance(rows[0], dict) and "archived" in rows[0]
    parsed = [dict(row) for row in rows] if already_parsed else parse_partition_rows(rows)

    missing_current: list[dict[str, Any]] = []
    for policy in table:
        if not policy.get("enabled", True):
            continue
        key = period_key(policy["interval"], reference)
        pname = partition_name(policy["table"], key)
        if not any(row["partition"] == pname for row in parsed):
            missing_current.append(
                {
                    "policy_id": policy["policy_id"],
                    "partition": pname,
                    "interval": policy["interval"],
                    "reason": "current_period_absent",
                }
            )

    orphans = [
        {"partition": row["partition"], "table": row["table"], "reason": "no_matching_policy"}
        for row in parsed
        if row["table"] not in by_table
    ]
    stale_archives: list[dict[str, Any]] = []
    for row in parsed:
        if not row["archived"] or row["table"] not in by_table:
            continue
        policy = by_table[row["table"]]
        tier = retention_tier(policy)
        age = period_age_days(policy["interval"], row["key"], reference)
        if tier["drop_enabled"] and age >= tier["drop_after_days"]:
            stale_archives.append(
                {
                    "partition": row["partition"],
                    "policy_id": policy["policy_id"],
                    "age_days": age,
                    "drop_after_days": tier["drop_after_days"],
                    "reason": "archived_past_drop_age",
                }
            )
    findings = missing_current + orphans + stale_archives
    return {
        "generated_at": reference.isoformat(),
        "reference": reference,
        "policies": len(table),
        "partitions": len(parsed),
        "archived": sum(1 for row in parsed if row["archived"]),
        "missing_current": missing_current,
        "orphans": orphans,
        "stale_archives": stale_archives,
        "findings": findings,
        "finding_count": len(findings),
        "healthy": not findings,
    }


def build_partition_lifecycle_policy(manager: PartitionManager | None = None) -> dict[str, object]:
    """The tiered lifecycle that governs :meth:`PartitionManager.retire_expired`.

    Kept out of :func:`build_partition_manager_catalog` because that catalog's
    key set is a pinned contract; this is the surface for the tier table, the
    guard bands, the batch caps and the inventory findings.
    """
    manager = manager or get_default_manager()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tiers": {
            name: dict(config) for name, config in sorted(PARTITION_RETENTION_TIERS.items())
        },
        "default_tier": DEFAULT_RETENTION_TIER,
        "resolved": {
            policy["policy_id"]: retention_tier(policy) for policy in manager.policies()
        },
        "guard_bands": {
            interval: int(band.get("retain_periods", 0) or 0)
            for interval, band in sorted(PARTITION_GUARD_BANDS.items())
        },
        "drop_policy": dict(PARTITION_DROP_POLICY),
        "age_days": {
            policy["policy_id"]: {
                record["key"]: period_age_days(policy["interval"], record["key"])
                for record in manager.list_partitions()
                if record["policy_id"] == policy["policy_id"]
            }
            for policy in manager.policies()
        },
        "last_retirement": manager.last_retirement(),
        "note": (
            "tiers name a lifecycle (attached -> archived -> dropped) so the shape "
            "is one data change; the guard band always outranks a tier, and both "
            "stages are batched by PARTITION_DROP_POLICY"
        ),
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