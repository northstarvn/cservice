"""Complaint handling router: the case surface, the decision dossier, and the
admin governance views.

Why a separate router
---------------------
A complaint is not a chat message. It has an identity that outlives the
session that produced it, it is quoted over the phone days later, and it has
stakeholders beyond the person who typed it. Putting it under ``/chat`` would
imply that a case is part of a conversation, which is exactly the confusion
that made the old surface untrackable.

Read/write split
----------------
* **Customer** (``/complaints``, ``/complaints/me``) -- lodge, read, add a note,
  withdraw, reopen, rate. Scoped to the caller; a customer can never name
  another user's case id and reach it.
* **Operator** (``/complaints/{ref}/...`` transitions) -- acknowledge, assign,
  resolve, close. Any authenticated user may reach these, and every one writes
  a ``complaint_decisions`` row with the actor and their step-up level, so who
  did what is answerable from the case alone.
* **Admin** (``/complaints/admin/*``) -- the queue, the SLA report, the sweep,
  the catalog. Gated on ``get_current_admin_user``.

Two behaviours worth stating because they are easy to get wrong:

* **The read endpoints do not write.** ``GET /complaints/{reference}`` builds a
  decision dossier, and building it runs the escalation engine. None of that
  escalates, records a decision, or stamps a breach -- only the sweep and the
  explicit transition endpoints do. A dashboard polled every thirty seconds
  must not manufacture a thousand escalations.

* **A downgrade is refused, not forbidden.** ``apply`` accepts an operator
  override and returns ``applied: false`` with the rule that blocked it. An
  impossible request is something an operator can make, and recording the
  refusal teaches more than a 422.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.schemas.chat import (
    ComplaintAcknowledgeIn,
    ComplaintApplyOut,
    ComplaintAssignIn,
    ComplaintCaseOut,
    ComplaintCatalogOut,
    ComplaintDecisionSupportOut,
    ComplaintNoteIn,
    ComplaintOpenIn,
    ComplaintOverrideIn,
    ComplaintQueueOut,
    ComplaintReopenIn,
    ComplaintResolveIn,
    ComplaintSlaReportOut,
    ComplaintSweepOut,
)
from app.services import complaints as complaints_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor_role(user: Any) -> str:
    """The role recorded against an event or decision.

    Derived from what the dependency actually established rather than assumed:
    an admin is an admin because the dependency checked ``is_admin``, and
    writing "agent" for that would understate the authority the row is
    supposed to preserve.
    """
    return "admin" if getattr(user, "is_admin", False) else "agent"


async def _load_owned_case(
    db: AsyncSession,
    reference: str,
    user_id: int,
) -> Any:
    """Fetch a case, refusing one that belongs to somebody else.

    A 404 rather than a 403: telling a caller that a reference exists but is not
    theirs is a free oracle for enumerating other people's complaints.
    """
    case = await complaints_service._load_case(db, reference=reference)
    if int(case.user_id) != int(user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such complaint case"
        )
    return case


def _not_found(detail: str = "No such complaint case") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


# ---------------------------------------------------------------------------
# Customer surface
# ---------------------------------------------------------------------------


@router.post("/complaints", response_model=ComplaintCaseOut, status_code=status.HTTP_201_CREATED)
async def lodge_complaint(
    payload: ComplaintOpenIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Lodge a complaint. Returns the case with its reference and SLA clock.

    The response carries the deadlines rather than burying them, because the
    first thing a complainant is entitled to know is when they will next hear
    from us.
    """
    return await complaints_service.open_complaint(
        db,
        int(current_user.id),
        category=payload.category,
        severity=payload.severity or "",
        summary=payload.summary,
        source=payload.source,
        regulatory=payload.regulatory,
    )


@router.get("/complaints/me", response_model=list[ComplaintCaseOut])
async def list_my_complaints(
    include_closed: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """The caller's own cases, newest first. Read-only."""
    return await complaints_service.list_complaints(
        db,
        user_id=int(current_user.id),
        include_closed=include_closed,
        limit=limit,
    )


@router.get("/complaints/me/decision-support", response_model=ComplaintDecisionSupportOut)
async def my_complaint_decision_support(
    reference: str = Query(..., description="the case reference, e.g. CMP-000042"),
    locale: str = Query(default="global"),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """The customer's own case, with the status and next step in plain terms.

    This is the transparency surface: the same dossier an operator sees,
    scoped to one case the caller owns, so "what is happening with my complaint"
    has an answer that is not a ticket number.
    """
    case = await _load_owned_case(db, reference, int(current_user.id))
    return await complaints_service.build_escalation_decision_support(
        db, case, locale=locale
    )


@router.get("/complaints/{reference}", response_model=ComplaintCaseOut)
async def get_complaint(
    reference: str,
    include_history: bool = Query(default=True),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """One case with its timeline and decision history. Read-only.

    Scoped to the caller. An operator reads a case through the admin/queue
    surface or the transition endpoints, which are separately gated -- a
    customer-facing GET is deliberately not a way to read somebody else's
    dispute.
    """
    case = await _load_owned_case(db, reference, int(current_user.id))
    if not include_history:
        return complaints_service.case_to_dict(case)
    return await complaints_service.get_complaint_case(db, reference=reference)


@router.post("/complaints/{reference}/notes", response_model=ComplaintCaseOut)
async def add_complaint_note(
    reference: str,
    payload: ComplaintNoteIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Add to the case timeline without changing its state."""
    await _load_owned_case(db, reference, int(current_user.id))
    return await complaints_service.add_complaint_note(
        db,
        int((await complaints_service._load_case(db, reference=reference)).id),
        payload.note,
        actor_user_id=int(current_user.id),
        actor_role=_actor_role(current_user),
    )


@router.post("/complaints/{reference}/withdraw", response_model=ComplaintCaseOut)
async def withdraw_complaint(
    reference: str,
    payload: ComplaintNoteIn = ComplaintNoteIn(),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Withdraw a complaint. Recorded as a real outcome, not a deletion."""
    await _load_owned_case(db, reference, int(current_user.id))
    case = await complaints_service._load_case(db, reference=reference)
    try:
        return await complaints_service.withdraw_complaint(
            db,
            int(case.id),
            actor_user_id=int(current_user.id),
            actor_role="customer",
            note=payload.note,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/complaints/{reference}/reopen", response_model=ComplaintCaseOut)
async def reopen_complaint(
    reference: str,
    payload: ComplaintReopenIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Say the resolution did not work.

    The most valuable event a complainant can produce: it marks every decision
    made on the case as a bad precedent, which is how the policy learns rather
    than accumulating.
    """
    await _load_owned_case(db, reference, int(current_user.id))
    case = await complaints_service._load_case(db, reference=reference)
    try:
        return await complaints_service.reopen_complaint(
            db,
            int(case.id),
            reason=payload.reason,
            actor_user_id=int(current_user.id),
            actor_role="customer",
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


# ---------------------------------------------------------------------------
# Operator transitions
# ---------------------------------------------------------------------------


@router.post("/complaints/{reference}/acknowledge", response_model=ComplaintCaseOut)
async def acknowledge_complaint(
    reference: str,
    payload: ComplaintAcknowledgeIn = ComplaintAcknowledgeIn(),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """First human contact. This is what stops the response SLA clock."""
    case = await complaints_service._load_case(db, reference=reference)
    try:
        return await complaints_service.acknowledge_complaint(
            db,
            int(case.id),
            actor_user_id=int(current_user.id),
            actor_role=_actor_role(current_user),
            note=payload.note,
        )
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/complaints/{reference}/assign", response_model=ComplaintCaseOut)
async def assign_complaint(
    reference: str,
    payload: ComplaintAssignIn = ComplaintAssignIn(),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Put a named human or a team on a case.

    The tier may be raised here but not lowered: a downgrade has to go through
    ``/apply`` so the authority floor is evaluated rather than bypassed.
    """
    case = await complaints_service._load_case(db, reference=reference)
    try:
        return await complaints_service.assign_complaint(
            db,
            int(case.id),
            owner_user_id=payload.owner_user_id,
            owner_team=payload.owner_team,
            tier=payload.tier or "",
            actor_user_id=int(current_user.id),
            actor_role=_actor_role(current_user),
            note=payload.note,
        )
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/complaints/{reference}/resolve", response_model=ComplaintCaseOut)
async def resolve_complaint(
    reference: str,
    payload: ComplaintResolveIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Offer a resolution. The case stays reopenable until it is closed."""
    case = await complaints_service._load_case(db, reference=reference)
    try:
        return await complaints_service.resolve_complaint(
            db,
            int(case.id),
            resolution_code=payload.resolution_code,
            resolution_note=payload.resolution_note,
            actor_user_id=int(current_user.id),
            actor_role=_actor_role(current_user),
            satisfaction_score=payload.satisfaction_score,
        )
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/complaints/{reference}/close", response_model=ComplaintCaseOut)
async def close_complaint(
    reference: str,
    payload: ComplaintNoteIn = ComplaintNoteIn(),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    case = await complaints_service._load_case(db, reference=reference)
    try:
        return await complaints_service.close_complaint(
            db,
            int(case.id),
            actor_user_id=int(current_user.id),
            actor_role=_actor_role(current_user),
            note=payload.note,
        )
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/complaints/{reference}/decision-support", response_model=ComplaintDecisionSupportOut)
async def complaint_decision_support(
    reference: str,
    locale: str = Query(default="global"),
    to_tier: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    owner_team: Optional[str] = Query(default=None),
    reason: str = Query(default=""),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """The full decision dossier for one case, plus a dry-run of an override.

    **This endpoint does not escalate.** It computes what *would* happen,
    including what an override would be allowed to do, and reports the guards.
    Applying is a separate, explicit POST below, so a reviewer can see the
    consequence before committing to it.
    """
    case = await complaints_service._load_case(db, reference=reference)
    overrides = None
    if to_tier or severity or owner_team:
        overrides = {
            "to_tier": to_tier or "",
            "severity": severity or "",
            "owner_team": owner_team or "",
            "reason": reason,
        }
    return await complaints_service.build_escalation_decision_support(
        db, case, locale=locale, overrides=overrides
    )


@router.post("/complaints/{reference}/apply", response_model=ComplaintApplyOut)
async def apply_complaint_escalation(
    reference: str,
    payload: ComplaintOverrideIn = ComplaintOverrideIn(),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Apply the escalation, or apply an override of it.

    Records the decision either way. A refused downgrade is written with
    ``applied: false`` and the rule that blocked it, so "someone tried to route
    this below policy" is a queryable fact rather than a 422 that vanished.
    """
    case = await complaints_service._load_case(db, reference=reference)
    support = await complaints_service.build_escalation_decision_support(
        db,
        case,
        overrides={
            "to_tier": payload.to_tier or "",
            "severity": payload.severity or "",
            "owner_team": payload.owner_team or "",
            "reason": payload.reason,
        },
    )
    precedent = support["precedent"]["precedents"]
    applied = await complaints_service.apply_escalation(
        db,
        case,
        support["decision"],
        actor_user_id=int(current_user.id),
        actor_role=_actor_role(current_user),
        rationale=payload.reason,
        precedent_refs=precedent,
    )
    return applied


# ---------------------------------------------------------------------------
# Admin surface
# ---------------------------------------------------------------------------


@router.get("/complaints/admin/queue", response_model=ComplaintQueueOut)
async def complaint_queue(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The queue, the SLA posture, and how often decisions proved wrong."""
    return await complaints_service.build_complaint_admin_report(db, limit=limit)


@router.get("/complaints/admin/sla", response_model=ComplaintSlaReportOut)
async def complaint_sla_report(
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Which SLAs are breached, and which are about to be.

    Computed from the case rows on every read, so a breach cannot be made to
    disappear by not running the sweep that would have noticed it.
    """
    return await complaints_service.build_sla_report(db, limit=limit)


@router.post("/complaints/admin/sweep", response_model=ComplaintSweepOut)
async def complaint_auto_escalation_sweep(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Re-evaluate every live case; apply only auto-authorised escalations.

    Advisory triggers are reported in the result and deliberately not applied.
    The whole design rests on that split: a statutory deadline and a breached
    clock may act without a person, and a judgement call waits for one.
    """
    return await complaints_service.run_auto_escalation_sweep(db, limit=limit)


@router.get("/complaints/admin/governance", response_model=ComplaintCatalogOut)
async def complaint_governance(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The configured policy: categories, tiers, triggers, guards, feeds, SLAs.

    Separate from the queue on purpose. The queue is what is happening; this is
    what *would* happen, and the difference between the two is where a
    misconfiguration shows up.
    """
    return complaints_service.build_complaints_catalog()


@router.get("/complaints/admin/decisions")
async def complaint_decision_audit(
    limit: int = Query(default=50, ge=1, le=200),
    user_id: Optional[int] = Query(default=None),
    contradicted_only: bool = Query(default=False),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The decision log across all cases, newest first.

    ``contradicted_only`` is the interesting filter: it returns exactly the
    decisions whose case was later reopened, abandoned, or escalated again --
    the institutional memory's self-audit.
    """
    from sqlalchemy import select as _select

    statement = _select(models.ComplaintDecision)
    if user_id is not None:
        statement = statement.where(models.ComplaintDecision.user_id == int(user_id))
    if contradicted_only:
        statement = statement.where(
            models.ComplaintDecision.outcome_observed.in_(
                ("reopened", "complainant_left", "escalated_further")
            )
        )
    result = await db.execute(
        statement.order_by(models.ComplaintDecision.created_at.desc()).limit(int(limit))
    )
    rows = [complaints_service.decision_to_dict(row) for row in result.scalars().all()]
    return {
        "generated_at": complaints_service._now().isoformat(),
        "catalog_version": complaints_service.COMPLAINTS_CATALOG_VERSION,
        "returned": len(rows),
        "contradicted_only": contradicted_only,
        "decisions": rows,
    }
