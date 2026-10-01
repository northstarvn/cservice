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
    ComplaintBlockagesOut,
    ComplaintCaseOut,
    ComplaintCatalogOut,
    ComplaintClusterReportOut,
    ComplaintDecisionSupportOut,
    ComplaintDetectOut,
    ComplaintLearningCatalogOut,
    ComplaintLearningPassOut,
    ComplaintNoteIn,
    ComplaintOpenIn,
    ComplaintOverrideIn,
    ComplaintProposalReportOut,
    ComplaintPublishOut,
    ComplaintQueueOut,
    ComplaintRaiseOut,
    ComplaintReopenIn,
    ComplaintResolveIn,
    ComplaintSlaReportOut,
    ComplaintSweepOut,
    ComplaintWeightReportOut,
)
from app.services import complaint_learning as learning_service
from app.services import complaints as complaints_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _learning_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Rename the service's ``ran_at`` to the schema's ``generated_at``.

    The services say ``ran_at`` because they also say ``opened_at``, ``closed_at``
    and ``observed_at``, and collapsing those into one word would be a small lie
    about when a thing happened. The response schemas say ``generated_at`` because
    that is the convention every other read surface in this API uses. Rather than
    make either side give up its vocabulary, the rename happens here, once.

    It is also the single place a future service field can be mapped onto a
    response shape, so a new endpoint inherits the behaviour rather than
    rediscovering that a name mismatch is a 500.
    """
    result = dict(payload)
    if "generated_at" not in result and result.get("ran_at"):
        result["generated_at"] = result["ran_at"]
    return result


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


# ---------------------------------------------------------------------------
# Complaint learning: system-raised cases, learned weights, stacked complaints
# ---------------------------------------------------------------------------
#
# All admin-gated, and all under `/complaints/admin/*` so they land under the
# existing `complaints_admin` authz rule rather than needing a new one. Three
# things about this group are worth stating, because each is a deliberate
# asymmetry rather than an oversight.
#
# `detect` and `learn` never write. `raise` and `publish` do. That split is the
# point: an operator should be able to see what the system *would* do, and what it
# *did*, from two different calls, and the second one should never be a
# side effect of reading the first.
#
# The learning pass is `POST` rather than `GET` even though it is a scheduled
# sweep with no parameters worth varying. It moves weights, and a `GET` that
# changes state is a caching bug waiting to happen.
#
# `blockages` renders and reports. It does not write the file, and there is no
# endpoint that does. `BLOCKAGES.md` is hand-authored governance prose; the
# suggestions are made pasteable and the diff is reported, which is the strongest
# thing this module can do without a human deciding that a machine's opinion
# belongs in a document whose whole purpose is human ones.


@router.get("/complaints/admin/learning/catalog", response_model=ComplaintLearningCatalogOut)
async def complaint_learning_catalog(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The objective, the signals, the detectors, and the cluster rules.

    The catalog is the document a reviewer reads to disagree with the module. It
    states what is *not* being optimised (contact frequency, time-on-service) as
    prominently as what is, and it reports the detector that ships inert, so a
    deliberately-disabled row cannot read as a missing feature.
    """
    return {
        "version": learning_service.COMPLAINT_LEARNING_VERSION,
        "generated_at": complaints_service._now().isoformat(),
        **learning_service.build_complaint_learning_catalog(),
        "validation": learning_service.validate_complaint_learning(),
    }


@router.get("/complaints/admin/learning/weights", response_model=ComplaintWeightReportOut)
async def complaint_learned_weights(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Every signal, its learned weight, its prior, and the drift between them.

    Every signal appears, learned or not, and each row carries its `source`. A
    table listing only the learned entries would make the configured priors
    invisible, and a reviewer would then believe a number is a measurement when
    it is a default.
    """
    return _learning_payload(await learning_service.build_weight_report(db))


@router.post("/complaints/admin/learning/observe", response_model=ComplaintLearningPassOut)
async def complaint_learning_pass(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Observe what happened to customers, and move the weights accordingly.

    Writes `complaint_signal_observations` rows and updates
    `complaint_signal_weights`. `advisory_only` is `true` in the response and is
    asserted by the service: a learned weight reorders and explains
    recommendations, and it never moves a tier or triggers an escalation. A case
    opened yesterday is not judged at all -- `neutral` teaches nothing, because
    counting "not yet known" as agreement is how a learner concludes that doing
    nothing is correct.
    """
    return _learning_payload(await learning_service.run_learning_pass(db, limit=limit))


@router.get("/complaints/admin/learning/detect", response_model=ComplaintDetectOut)
async def complaint_detect_system(
    user_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Run the detectors and report what *would* be raised. Writes nothing.

    Read-only by construction, and this is the call to make first. Every
    candidate carries which of the three guards -- confidence, cooldown, dedupe --
    stopped it, so a detector that starts misbehaving is diagnosable from this
    payload rather than from a queue that mysteriously filled up.
    """
    return _learning_payload(await learning_service.detect_system_complaints(
        db, user_ids=[user_id] if user_id is not None else None, limit=limit
    ))


@router.post("/complaints/admin/learning/raise", response_model=ComplaintRaiseOut)
async def complaint_raise_system(
    user_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    max_raise: int = Query(default=25, ge=0, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Open system-raised cases for customers who never complained.

    A complaint the system raises is a real record with real consequences, so
    three guards apply and all three are reported: each detector's own confidence
    against its gate, a per-(detector, customer) cooldown, and dedupe against a
    genuinely separate live case. `max_raise` is a per-call ceiling on top of
    those -- the failure it exists for is a corrupt snapshot making every case
    look overdue, which without a cap would open a case for every customer at
    once.

    `held_back` and `suppressed` say why anything was not raised, so "nothing
    happened" is never the whole answer.
    """
    return _learning_payload(await learning_service.raise_system_complaints(
        db,
        user_ids=[user_id] if user_id is not None else None,
        limit=limit,
        max_raise=max_raise,
    ))


@router.get("/complaints/admin/learning/system-raised")
async def complaint_system_raised(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Which cases the system opened, broken down by detector.

    "The system opened 40 cases" is not actionable. "The billing detector opened
    39 of them" is.
    """
    return await learning_service.list_system_raised(db, limit=limit)


@router.get("/complaints/admin/learning/clusters", response_model=ComplaintClusterReportOut)
async def complaint_stacked_clusters(
    limit: int = Query(default=2000, ge=1, le=10000),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Group recent complaints and report which ones have stacked up. Writes nothing.

    Every threshold is multi-axis, and the one that matters is the reopen rate: a
    cluster of cases that were closed and never reopened is the system working,
    and a cluster that keeps coming back is the system not working. `near_misses`
    reports clusters that fell short and by how much, so a rule can be tuned
    deliberately rather than a threshold being lowered by guesswork.
    """
    return _learning_payload(await learning_service.detect_stacked_complaints(db, limit=limit))


@router.post("/complaints/admin/learning/proposals", response_model=ComplaintProposalReportOut)
async def complaint_build_proposals(
    limit: int = Query(default=2000, ge=1, le=10000),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Detect stacks, render suggestions, and persist them idempotently.

    Re-running on unchanged evidence updates the existing row's evidence and
    leaves its `status` alone, so a proposal a human has acknowledged or
    dismissed is not silently reset by the next sweep. Stacks that stop meeting
    their thresholds are reported in `no_longer_stacking` and never deleted: a
    proposal vanishing because a window slid would leave a reviewer who was
    mid-decision with no record.

    Priority is not case count. A large cluster nobody reopened is a capacity
    problem and caps at medium; a small one that keeps reopening is a correctness
    problem and outranks it.
    """
    return _learning_payload(await learning_service.build_and_store_proposals(db, limit=limit))


@router.get("/complaints/admin/learning/proposals")
async def complaint_list_proposals(
    status_filter: str = Query(default="", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Stored suggestions, with their evidence and their current state."""
    return await learning_service.list_proposals(db, status=status_filter, limit=limit)


@router.post("/complaints/admin/learning/proposals/{proposal_id}/status")
async def complaint_set_proposal_status(
    proposal_id: str,
    new_status: str = Query(
        alias="status",
        description="detected | published | acknowledged | dismissed",
    ),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Record a human's decision about a suggestion.

    The parameter is aliased to `status` on the wire but named `new_status` in
    code, because a local named `status` shadows the imported `fastapi.status`
    and every error raised from this function would fail with a `NameError`
    instead of the 422 it was written to return.

    An unknown status is a 422 rather than a stored string, because this is a
    write path and a typo that silently becomes a state nothing else reads is
    worse than a rejection.
    """
    try:
        return await learning_service.set_proposal_status(db, proposal_id, new_status)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post("/complaints/admin/learning/publish", response_model=ComplaintPublishOut)
async def complaint_publish_proposals(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Register stored suggestions on the release ladder at `l0_draft`.

    Only `detected` proposals are published, and only ever at draft. A dismissed
    proposal that got re-published would be a human's decision silently reversed
    by a scheduled job. Nothing here advances a candidate: promotion runs through
    the ladder's own gates, which is why suggestions go to the ladder rather than
    straight into a file.
    """
    return _learning_payload(await learning_service.publish_proposals(db))


@router.get("/complaints/admin/learning/blockages", response_model=ComplaintBlockagesOut)
async def complaint_blockages_preview(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Render a pasteable BLOCKAGES.md section and report what a paste would add.

    `writes` is always `false` and there is no endpoint that writes the file. The
    diff reports only additions; a heading already present is left alone, and
    nothing in the file is ever parsed for removal. This is allowed to add a
    suggestion to a human's document and is structurally incapable of taking one
    of their lines out.
    """
    import pathlib

    listing = await learning_service.list_proposals(db, status="", limit=200)
    root = pathlib.Path(__file__).resolve().parents[2]
    target = root / "BLOCKAGES.md"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    return _learning_payload(
        learning_service.diff_against_rendered(listing["proposals"], existing)
    )
