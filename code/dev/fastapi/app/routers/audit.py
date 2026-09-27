import json
from datetime import datetime, timezone
from typing import Any

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
    AuditGateIntegrityReport,
    AuditLogEntryCreate,
    AuditLogEntryOut,
    AuditLogExportReport,
    AuditLogIntegrityReport,
    AuditLogListReport,
    AuditLogSummaryReport,
    AuditRetentionReport,
    AuditTimelineReport,
    AuditViewReport,
    AuditableLogEntryCreate,
    EnhancementListReport,
    PipelineDeadLetterReport,
    PipelineEventAccepted,
    PipelineEventCreate,
    PipelineReplayReport,
    PipelineReplayRequest,
    PipelineStatsReport,
    SystemEfficiencyReport,
    TransactionExportReport,
    TransactionIntegrityReport,
    TransactionLogReport,
    TransactionQueryReport,
    TransactionSpecReport,
)
from app.services.audit_log import (
    AUDIT_ANOMALY_DEFAULTS,
    AUDIT_INTEGRITY_GATES,
    AUDIT_VIEW_PROFILES,
    DEFAULT_AUDIT_VIEW_PROFILE,
    audit_integrity_report,
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
    project_audit_entries,
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


# --- Read-side projections & gates ---------------------------------------------
#
# `/audit/logs` above still returns whole entries to an admin. What follows is
# additive: a way to ask for *less* than the whole entry (`/logs/view`) and a way
# to ask whether the trail can be trusted at all (`/integrity/gates`). Both read
# the same rows as the endpoints above, so a caller can compare.


@router.get("/logs/view", response_model=AuditViewReport)
async def read_audit_log_view(
    profile: str = Query(
        default=DEFAULT_AUDIT_VIEW_PROFILE,
        description="One of the AUDIT_VIEW_PROFILES rows; see /audit/trail-catalog",
    ),
    action: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None, ge=1),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Project the trail through a view profile instead of returning it whole.

    `digest` omits payloads entirely, `operations` returns which keys a payload
    carries but not their values, `investigation` returns values (credential
    redaction from write time still applies). The unprojected `/audit/logs` is
    unaffected.
    """
    try:
        payloads = await _trail_payloads(
            db,
            limit=limit,
            action=action,
            severity=severity,
            actor_user_id=actor_user_id,
            entity_type=entity_type,
        )
        return project_audit_entries(payloads, profile=profile, limit=limit)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/integrity/gates", response_model=AuditGateIntegrityReport)
async def read_audit_integrity_gates(
    limit: int = Query(default=200, ge=1, le=1000),
    action: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None, ge=1),
    entity_type: str | None = Query(default=None),
    current_user: models.User = Depends(deps.get_current_admin_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Evaluate the integrity gate table over the trail.

    Unlike `/logs/integrity`, which answers "does the seal chain link up", this
    evaluates a table of named gates over derived metrics and says which one
    failed and how bad it is. `reject` means a problem worth stopping for: a
    broken chain, a credential still sitting in a payload, a sensitive action
    with no justification. It is a report, not an enforcement point -- the read
    endpoints keep answering either way.

    Entries are read newest-first by the list endpoint, so the chain is walked
    oldest-first.
    """
    entries = await list_audit_log_entries(
        db,
        action=action,
        severity=severity,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        limit=limit,
    )
    return audit_integrity_report(reversed(entries))


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


@router.get("/pipeline/dead-letters", response_model=PipelineDeadLetterReport)
async def read_pipeline_dead_letters(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Inspect the dead-letter ring: what failed, how often, and what is drainable.

    Read-only. `retained` is what the bounded ring still holds, which can be
    lower than `dead_lettered` if the ring evicted, and `replayable` is what the
    replay policy will actually re-send, which can be lower than `retained` if a
    kind is excluded. The `by_kind` / `by_error` split is the part worth reading:
    one kind failing recently is an outage, one kind failing permanently is a
    serialization bug.
    """
    return get_default_pipeline().dead_letter_report()


@router.post("/pipeline/replay", response_model=PipelineReplayReport)
async def replay_pipeline_dead_letters(
    payload: PipelineReplayRequest = PipelineReplayRequest(),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Re-submit retained dead letters under the configured replay policy.

    Bounded by `max_attempts` (a letter that already failed that many times is
    left in the ring for a human) and exponentially backed off. Pass
    `{"dry_run": true}` to see exactly what would be re-queued without touching
    the ring.
    """
    pipeline = get_default_pipeline()
    if payload.dry_run:
        report = pipeline.dead_letter_report()
        budget = int(
            payload.max_attempts
            if payload.max_attempts is not None
            else report["policy"].get("max_attempts", 0)
        )
        eligible = [
            str(entry.get("event_id", ""))
            for entry in report["recent"]
            if entry.get("replayable")
            and int(entry.get("attempts", 0) or 0) < budget
        ]
        return {
            "generated_at": datetime.now(timezone.utc),
            "dry_run": True,
            "attempted": len(eligible),
            "requeued": [],
            "exhausted": [
                str(entry.get("event_id", ""))
                for entry in report["recent"]
                if entry.get("replayable")
                and int(entry.get("attempts", 0) or 0) >= budget
            ],
            "exhausted_count": sum(
                1
                for entry in report["recent"]
                if entry.get("replayable") and int(entry.get("attempts", 0) or 0) >= budget
            ),
            "max_attempts": budget,
            "backoff_seconds": float(
                payload.backoff_seconds
                if payload.backoff_seconds is not None
                else report["policy"].get("backoff_seconds", 0.0)
            ),
            "remaining": int(report["retained"]),
            "policy": dict(report["policy"]),
            "note": "dry run: nothing was re-queued and the ring is unchanged",
        }
    result = await pipeline.replay_dead_letters(
        max_attempts=payload.max_attempts,
        backoff_seconds=payload.backoff_seconds,
    )
    return {
        "generated_at": datetime.now(timezone.utc),
        "dry_run": False,
        **result,
        "policy": dict(pipeline.dead_letter_report()["policy"]),
        "note": (
            "re-queued letters were removed from the ring so a later replay "
            "cannot double-send them"
        ),
    }


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
async def read_transaction_spec(
    section: str | None = Query(
        default=None,
        description=(
            "Return one section of the catalog instead of all of them; omit for "
            "the full contract, which is what tooling expects"
        ),
    ),
):
    """Expose the protobuf transaction wire-format contract for tooling.

    The full catalog is returned when `section` is omitted, so existing callers
    are unaffected. Passing a section narrows the response to that one part
    (e.g. `?section=query`) for a caller that only needs the query field table;
    an unknown section is a 422 rather than a silently empty response.
    """
    catalog = build_transaction_spec_catalog()
    if section is None:
        return {"spec": catalog}
    if section not in catalog:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown spec section {section!r}; expected one of {sorted(catalog)}",
        )
    return {"spec": {section: catalog[section]}}


@router.get("/transactions/query", response_model=TransactionQueryReport)
async def query_immutable_transactions(
    action: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    tenant_id: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None, ge=1),
    severity: str | None = Query(default=None),
    spec_version: int | None = Query(default=None, ge=1),
    occurred_after: datetime | None = Query(default=None),
    occurred_before: datetime | None = Query(default=None),
    signed: bool | None = Query(default=None),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    sort: str = Query(default="cursor"),
    view: str = Query(default="internal"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Filter, sort and paginate the transaction log; project it through a view.

    `matched` counts what passed the filters *before* pagination, so a caller can
    tell "no more pages" from "nothing matched". Undeclared fields and operators
    match nothing, so a typo narrows the result to empty rather than quietly
    returning everything.
    """
    filters: dict[str, Any] = {}
    for key, value in (
        ("action", action),
        ("entity_type", entity_type),
        ("entity_id", entity_id),
        ("tenant_id", tenant_id),
        ("actor_user_id", actor_user_id),
        ("severity", severity),
        ("version", spec_version),
    ):
        if value is not None:
            filters[key] = value
    if occurred_after is not None or occurred_before is not None:
        bounds: dict[str, Any] = {}
        if occurred_after is not None:
            bounds["gte"] = occurred_after.isoformat()
        if occurred_before is not None:
            bounds["lte"] = occurred_before.isoformat()
        filters["occurred_at"] = bounds
    if signed is not None:
        filters["signature_present"] = signed
    log = get_default_transaction_log()
    try:
        page = log.query(
            filters, order=order, sort=sort, limit=limit, offset=offset, view=view
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"generated_at": datetime.now(timezone.utc), **page}


@router.get("/transactions/integrity", response_model=TransactionIntegrityReport)
async def read_transaction_integrity(
    sample: int = Query(default=5, ge=0, le=100),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """One call: chain validity, merkle checkpoints, schema versions, signing.

    `verdict` folds those into a single word. `reject` means the hash chain is
    broken, or a frame that policy requires to be signed is not; `review` means
    the chain holds but the log carries more than one spec version, or there was
    nothing to sample; `clean` means the sampled tail is fully compliant.
    `sample=0` skips the per-frame schema and signing checks and reports the
    chain and the checkpoints only.
    """
    log = get_default_transaction_log()
    report = log.integrity_report(sample=sample)
    chain_valid = bool(report["chain"].get("valid"))
    unsigned = list(report["unsigned_but_required"])
    versions = list(report["spec_versions"])
    findings: list[str] = []
    if not chain_valid:
        findings.append(
            f"hash chain broken at frame {report['chain'].get('broken_at')}"
        )
    if unsigned:
        findings.append(
            f"{len(unsigned)} sampled frame(s) require a signature and have none: "
            + ", ".join(unsigned[:5])
        )
    if len(versions) > 1:
        findings.append(
            f"log carries {len(versions)} spec versions ({', '.join(map(str, versions))}); "
            "frames stay valid, but readers must handle more than one"
        )
    if sample and not unsigned and not report["sampled"]:
        findings.append("no frames available to sample")
    if not chain_valid or unsigned:
        verdict = "reject"
    elif findings:
        verdict = "review"
    else:
        verdict = "clean"
    return {
        "generated_at": datetime.now(timezone.utc),
        **report,
        "total": log.total(),
        "verdict": verdict,
        "findings": findings,
    }


@router.get("/transactions/export", response_model=TransactionExportReport)
async def export_immutable_transactions(
    format: str = Query(default="jsonl", pattern="^(jsonl|json|csv|base64)$"),
    view: str = Query(default="forensic"),
    action: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    tenant_id: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None, ge=1),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Serialise matching frames out of the transaction log.

    Defaults to `jsonl` + `forensic` because an export is an evidence handoff: it
    has to carry full payloads in a line format an investigator can `grep` and
    `diff` without this repo. `base64` reproduces the exact on-disk encoding, so
    an export in that format can be replayed into another log with
    `import_frames` -- and it is the only format whose signature bytes survive.
    """
    filters: dict[str, Any] = {}
    for key, value in (
        ("action", action),
        ("entity_type", entity_type),
        ("tenant_id", tenant_id),
        ("actor_user_id", actor_user_id),
    ):
        if value is not None:
            filters[key] = value
    log = get_default_transaction_log()
    try:
        content = log.export(fmt=format, view=view, filters=filters)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    # json is a single document rather than a line format, so counting lines
    # would report one frame for any number of them; csv carries a header row
    # that is not a frame.
    if format == "json":
        frame_count = len(json.loads(content) or [])
    else:
        lines = [line for line in content.splitlines() if line.strip()]
        frame_count = max(0, len(lines) - 1) if format == "csv" else len(lines)
    export_section = build_transaction_spec_catalog().get("export", {})
    return {
        "generated_at": datetime.now(timezone.utc),
        "format": format,
        "view": view,
        "frame_count": frame_count,
        "filters": [
            {"field": key, "operator": "eq", "value": value}
            for key, value in filters.items()
        ],
        "reimportable": format in set(export_section.get("reimportable", [])),
        "signature_survives": format in set(export_section.get("signature_survives", [])),
        "bytes": len(content.encode("utf-8")),
        "content": content,
        "note": (
            "jsonl/csv round-trip preserves content but not signature bytes; "
            "use base64 for a byte-exact, replayable export"
        ),
    }
