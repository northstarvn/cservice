from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.high_throughput_pipeline import get_default_pipeline
from app.protobuf_transaction_spec import (
    build_transaction_spec_catalog,
    get_default_transaction_log,
)
from app.schemas.audit import (
    AuditLogEntryCreate,
    AuditLogListReport,
    AuditLogSummaryReport,
    EnhancementListReport,
    PipelineEventAccepted,
    PipelineEventCreate,
    PipelineStatsReport,
    SystemEfficiencyReport,
    TransactionLogReport,
    TransactionSpecReport,
)
from app.services.audit_log import (
    build_audit_log_catalog,
    build_audit_log_summary,
    count_audit_log_entries,
    entry_to_payload,
    list_audit_log_entries,
    record_audit_log_entry,
)
from app.services.efficiency_audit import (
    build_efficiency_audit_catalog,
    build_enhancement_list_report,
    build_system_efficiency_report,
)

router = APIRouter()


@router.get("/efficiency", response_model=SystemEfficiencyReport)
async def get_system_efficiency_report(
    limit: int = Query(default=20, ge=1, le=100),
    include_logs: bool = Query(default=True),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Classify every component by efficiency and propose prioritized enhancements."""
    return await build_system_efficiency_report(
        db, limit=limit, include_logs=include_logs
    )


@router.get("/enhancements", response_model=EnhancementListReport)
async def get_system_enhancements(
    limit: int = Query(default=20, ge=1, le=100),
    include_logs: bool = Query(default=True),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Return the prioritized enhancement list only (lighter payload)."""
    return await build_enhancement_list_report(
        db, limit=limit, include_logs=include_logs
    )


@router.get("/catalog")
async def get_efficiency_audit_catalog():
    """Expose the audit rule tables (components, scoring, enhancement endpoints)."""
    return build_efficiency_audit_catalog()


@router.get("/trail-catalog")
async def get_audit_trail_catalog():
    """Expose the audit-trail rule tables (actions, severities) for tooling."""
    return build_audit_log_catalog()


@router.post("/log", status_code=status.HTTP_201_CREATED)
async def create_audit_log_entry(
    payload: AuditLogEntryCreate,
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Record an operator/system action on the immutable audit trail."""
    try:
        entry = await record_audit_log_entry(
            db,
            action=payload.action,
            summary=payload.summary,
            actor_user_id=current_user.id,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            detail=payload.detail,
            severity=payload.severity,
            source=payload.source,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return entry_to_payload(entry)


@router.get("/logs", response_model=AuditLogListReport)
async def read_audit_log_entries(
    action: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None, ge=1),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """List the audit trail, newest first, with optional filters."""
    try:
        entries = await list_audit_log_entries(
            db,
            action=action,
            severity=severity,
            actor_user_id=actor_user_id,
            entity_type=entity_type,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    total = await count_audit_log_entries(db)
    return {
        "generated_at": datetime.now(timezone.utc),
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": [entry_to_payload(entry) for entry in entries],
    }


@router.get("/logs/summary", response_model=AuditLogSummaryReport)
async def read_audit_log_summary(
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Roll up the trail by action and severity."""
    return await build_audit_log_summary(db)


# --- High-velocity audit pipeline & immutable transactions --------------------


@router.post("/pipeline/event", status_code=status.HTTP_202_ACCEPTED, response_model=PipelineEventAccepted)
async def submit_security_signal(
    payload: PipelineEventCreate,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Enqueue a high-throughput security signal for async, immutable capture."""
    event = get_default_pipeline().submit_event(
        kind=payload.kind,
        severity=payload.severity,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        actor_user_id=payload.actor_user_id,
        tenant_id=payload.tenant_id,
        payload=payload.payload,
    )
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pipeline queue full; retry with backoff",
        )
    return {"accepted": True, "event_id": event.event_id}


@router.get("/pipeline/stats", response_model=PipelineStatsReport)
async def read_pipeline_stats(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Expose pipeline throughput/backpressure/dead-letter counters."""
    stats = get_default_pipeline().stats()
    stats["generated_at"] = datetime.now(timezone.utc)
    return stats


@router.get("/transactions", response_model=TransactionLogReport)
async def read_immutable_transactions(
    limit: int = Query(default=20, ge=1, le=500),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Read the immutable, hash-chained transaction log tail (append-only)."""
    log = get_default_transaction_log()
    return {
        "generated_at": datetime.now(timezone.utc),
        "total": log.total(),
        "tail": log.tail(limit),
        "chain": log.verify_chain(),
    }


@router.get("/transactions/spec", response_model=TransactionSpecReport)
async def read_transaction_spec():
    """Expose the protobuf transaction wire-format contract for tooling."""
    return {"spec": build_transaction_spec_catalog()}