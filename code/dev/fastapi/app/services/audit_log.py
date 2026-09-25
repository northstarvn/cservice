"""Audit trail service.

Records operator and system actions so the backend can explain *who did what,
on which entity, and why*. The service is config-driven in the same spirit as
the other rule engines: allowed actions are a catalog, severity values are
constrained, and the writers are small helpers around the persisted
``AuditLogEntry`` model.
"""
from datetime import datetime, timezone
import json
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

ALLOWED_SEVERITIES = ("info", "warning", "critical")

# Actions the backend treats as auditable, grouped by domain family.
AUDIT_ACTION_CATALOG = {
    "auth": ["auth.login", "auth.register", "auth.password_change"],
    "booking": [
        "booking.create",
        "booking.update",
        "booking.cancel",
        "booking.assign",
        "booking.reassign",
    ],
    "policy": ["policy.override", "policy.restrict", "policy.block"],
    "communication": [
        "communication.override_set",
        "communication.override_clear",
    ],
    "payments": [
        "arrears.open",
        "arrears.settle",
        "arrears.waive_interest",
        "points.exchange",
        "points.adjust",
    ],
    "admin": ["admin.action"],
    "system": ["system.startup", "system.config_change"],
}
# Flat lookup for quick validation.
AUDIT_ACTIONS = {
    action
    for family in AUDIT_ACTION_CATALOG.values()
    for action in family
}


def validate_severity(severity: str) -> str:
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(
            f"severity must be one of {', '.join(ALLOWED_SEVERITIES)}"
        )
    return severity


def _detail_json(detail: dict | None) -> str:
    try:
        return json.dumps(detail or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _load_json(raw: str | None) -> Any:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def entry_to_payload(entry: models.AuditLogEntry) -> dict:
    return {
        "id": entry.id,
        "actor_user_id": entry.actor_user_id,
        "action": entry.action,
        "entity_type": entry.entity_type,
        "entity_id": str(entry.entity_id or ""),
        "summary": entry.summary,
        "detail": _load_json(entry.detail_json),
        "severity": entry.severity,
        "source": entry.source,
        "created_at": entry.created_at,
    }


def build_audit_log_catalog() -> dict[str, object]:
    """Introspectable catalog for metadata endpoints (config-only additions)."""
    return {
        "severities": list(ALLOWED_SEVERITIES),
        "actions": AUDIT_ACTION_CATALOG,
        "action_count": len(AUDIT_ACTIONS),
    }


async def record_audit_log_entry(
    db: AsyncSession,
    *,
    action: str,
    summary: str,
    actor_user_id: Optional[int] = None,
    entity_type: str = "system",
    entity_id: str = "",
    detail: dict | None = None,
    severity: str = "info",
    source: str = "api",
) -> models.AuditLogEntry:
    """Persist one audit trail entry and return it."""
    validate_severity(severity)
    entry = models.AuditLogEntry(
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id or ""),
        summary=summary,
        detail_json=_detail_json(detail),
        severity=severity,
        source=source,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def list_audit_log_entries(
    db: AsyncSession,
    *,
    action: Optional[str] = None,
    severity: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[models.AuditLogEntry]:
    """List the trail, newest first, with optional filters."""
    query = select(models.AuditLogEntry).order_by(
        models.AuditLogEntry.created_at.desc(),
        models.AuditLogEntry.id.desc(),
    )
    if action:
        query = query.where(models.AuditLogEntry.action == action)
    if severity:
        validate_severity(severity)
        query = query.where(models.AuditLogEntry.severity == severity)
    if actor_user_id is not None:
        query = query.where(models.AuditLogEntry.actor_user_id == actor_user_id)
    if entity_type:
        query = query.where(models.AuditLogEntry.entity_type == entity_type)
    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


async def count_audit_log_entries(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(models.AuditLogEntry.id)))
    return int(result.scalar() or 0)


async def build_audit_log_summary(db: AsyncSession) -> dict:
    """Roll up the trail by action and by severity."""
    total = await count_audit_log_entries(db)

    action_rows = await db.execute(
        select(models.AuditLogEntry.action, func.count())
        .group_by(models.AuditLogEntry.action)
        .order_by(models.AuditLogEntry.action)
    )
    by_action = [{"key": key, "count": count} for key, count in action_rows.all()]

    severity_rows = await db.execute(
        select(models.AuditLogEntry.severity, func.count())
        .group_by(models.AuditLogEntry.severity)
        .order_by(models.AuditLogEntry.severity)
    )
    by_severity = [{"key": key, "count": count} for key, count in severity_rows.all()]

    return {
        "generated_at": datetime.now(timezone.utc),
        "total": total,
        "by_action": by_action,
        "by_severity": by_severity,
    }