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
    AuditActorActivityReport,
    AuditAnomalyReport,
    AuditLogEntryCreate,
    AuditLogEntryOut,
    AuditLogExportReport,
    AuditLogIntegrityReport,
    AuditLogListReport,
    AuditLogSummaryReport,
    AuditRetentionReport,
    AuditTimelineReport,
    AuditableLogEntryCreate,
    EnhancementListReport,
    PipelineEventAccepted,
    PipelineEventCreate,
    PipelineStatsReport,
    SystemEfficiencyReport,
    TransactionLogReport,
    TransactionSpecReport,
)
from app.services.audit_log import (
    AUDIT_ANOMALY_DEFAULTS,
    build_audit_log_catalog,
    build_audit_log_summary,
    build_entity_timeline,
    coerce_entry,
    count_audit_log_entries,
    detect_audit_anomalies,
    entry_to_payload,
    export_audit_trail,
    list_audit_log_entries,
    plan_retention,
    record_audit_log_entry,
    record_auditable,
    seal_entries,
    summarize_actor_activity,
    verify_seal_chain,
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


# --- Governed writes & forensic reads -----------------------------------------
#
# The original `/audit/log`, `/audit/logs` and `/audit/logs/summary` above are
# unchanged. The endpoints below cover the expanded `services/audit_log.py`
# surface: a governed write path, tamper verification, actor/entity rollups,
# anomaly findings, retention planning, and export.


async def _trail_payloads(
    db: AsyncSession,
    *,
    limit: int,
    action: str | None = None,
    severity: str | None = None,
    actor_user_id: int | None = None,
    entity_type: str | None = None,
) -> list[dict]:
    """Fetch the trail and coerce it into payload dicts for the pure helpers."""
    entries = await list_audit_log_entries(
        db,
        action=action,
        severity=severity,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        limit=limit,
    )
    return [coerce_entry(entry) for entry in entries]


@router.post(
    "/log/auditable",
    status_code=status.HTTP_201_CREATED,
    response_model=AuditLogEntryOut,
)
async def create_governed_audit_log_entry(
    payload: AuditableLogEntryCreate,
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Record a governed entry: alias-resolved, redacted, justified, sealed."""
    try:
        entry = await record_auditable(
            db,
            action=payload.action,
            summary=payload.summary,
            actor_user_id=current_user.id,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            detail=payload.detail,
            severity=payload.severity,
            source=payload.source,
            strict=payload.strict,
            require_justification=payload.require_justification,
            seal=payload.seal,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return entry_to_payload(entry)


@router.get("/logs/integrity", response_model=AuditLogIntegrityReport)
async def read_audit_log_integrity(
    limit: int = Query(default=200, ge=1, le=1000),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Re-derive the tamper-evident seal chain over the most recent entries."""
    entries = await list_audit_log_entries(db, limit=limit)
    # ``seal_entries`` walks oldest-first; the list endpoint is newest-first.
    chain = verify_seal_chain(seal_entries(reversed(entries)))
    return chain


@router.get("/logs/actors", response_model=AuditActorActivityReport)
async def read_audit_actor_activity(
    limit: int = Query(default=200, ge=1, le=1000),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Per-actor volume, span, and severity/action mix."""
    payloads = await _trail_payloads(db, limit=limit)
    return summarize_actor_activity(payloads)


@router.get("/logs/timeline", response_model=AuditTimelineReport)
async def read_audit_entity_timeline(
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Chronological history for one entity (all entities when unfiltered)."""
    payloads = await _trail_payloads(db, limit=limit, entity_type=entity_type)
    events = build_entity_timeline(payloads, entity_type=entity_type, entity_id=entity_id)
    return {
        "generated_at": datetime.now(timezone.utc),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "count": len(events),
        "events": events,
    }


@router.get("/logs/anomalies", response_model=AuditAnomalyReport)
async def read_audit_anomalies(
    limit: int = Query(default=500, ge=1, le=2000),
    burst_threshold: int | None = Query(default=None, ge=1),
    window_minutes: int | None = Query(default=None, ge=1),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Run the anomaly rule table (bursts, after-hours, unjustified actions)."""
    thresholds = dict(AUDIT_ANOMALY_DEFAULTS)
    if burst_threshold is not None:
        thresholds["burst_threshold"] = burst_threshold
    if window_minutes is not None:
        thresholds["window_minutes"] = window_minutes
    payloads = await _trail_payloads(db, limit=limit)
    findings = detect_audit_anomalies(payloads, thresholds=thresholds)
    return {
        "generated_at": datetime.now(timezone.utc),
        "scanned": len(payloads),
        "finding_count": len(findings),
        "rules": sorted({finding["rule"] for finding in findings}),
        "thresholds": thresholds,
        "findings": findings,
    }


@router.get("/logs/retention", response_model=AuditRetentionReport)
async def read_audit_retention(
    limit: int = Query(default=500, ge=1, le=2000),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Split the trail into entries past their retention window and the rest."""
    payloads = await _trail_payloads(db, limit=limit)
    return plan_retention(payloads)


@router.get("/logs/export", response_model=AuditLogExportReport)
async def export_audit_logs(
    format: str = Query(default="ndjson", pattern="^(ndjson|csv)$"),
    action: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None, ge=1),
    entity_type: str | None = Query(default=None),
    page_size: int = Query(default=200, ge=1, le=1000),
    max_pages: int = Query(default=25, ge=1, le=200),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Stream the trail out as a single NDJSON or CSV document."""
    try:
        return await export_audit_trail(
            db,
            fmt=format,
            page_size=page_size,
            max_pages=max_pages,
            action=action,
            severity=severity,
            actor_user_id=actor_user_id,
            entity_type=entity_type,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


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