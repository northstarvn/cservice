"""Customer-facing offers (Stage B): the inbox, one-tap accept, and the trail.

Three routes under ``/chat/me/offers`` are a customer's own, and four under
``/chat/admin/recovery/offers`` are the operator's. The split is the point: the
customer half can only ever read *their* offers and act on them, and the admin
half is the only place that issues or marks one fulfilled.

Why a separate router rather than more of ``routers/chat.py``
------------------------------------------------------------
``chat.py`` is the largest router in the codebase and this surface has its own
shape -- an offer has an identity a customer quotes, a state machine, and a
compliance trail -- which is the same argument that put complaints in their own
router. Included with **no prefix**: the paths below already carry ``/chat``,
and adding one here produced ``/chat/chat/me/offers`` on the first attempt, which
the authorization drift report caught immediately.

Authorization
-------------
No new ``AUTHZ_RULES`` rows, deliberately. The existing wildcards already
classify every path here correctly and classify them by *prefix*, which is the
property that matters: a route added under ``/chat/me/offers`` inherits
``chat_reads`` / ``chat_writes`` and one under ``/chat/admin/recovery/offers``
inherits ``chat_admin`` (which carries loa2 step-up, correct for a human
committing a credit). Adding narrower rules would duplicate that with more
places to forget. ``test_the_offer_routes_inherit_their_prefix_rule`` pins the
classification so a later reorder that moved ``chat_admin`` below ``chat_reads``
would fail rather than quietly reclassify an admin route as customer-readable.

One thing this router refuses to do
------------------------------------
**Expire or fulfil an offer the customer can still act on.** Recording an
outcome goes through ``record_outcome``, whose state machine refuses both from
``offered``. A sweep that closed open offers would race the one-tap path, and the
loser of that race is a customer who pressed accept and got an error.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.services import customer_offers, preferences

router = APIRouter()


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DeclineOfferIn(BaseModel):
    """Why the customer said no.

    ``extra="forbid"`` for the same reason ``CandidateIn`` carries it: a body
    naming a status this endpoint does not set would otherwise be accepted and
    silently dropped, and a decline whose reason was quietly ignored is the one
    this subsystem exists to collect.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        default="",
        max_length=1000,
        description=(
            "free text on purpose: a customer explaining why they do not want an "
            "apology credit is usually describing the problem we failed to fix, "
            "and a controlled vocabulary would discard exactly that"
        ),
    )


class PolicyScoreIn(BaseModel):
    """The two fields ``evaluate_waiver_approval`` actually reads.

    Declared explicitly rather than accepting an arbitrary dict, because that
    function reads ``getattr(policy_score, "policy_tier", "")`` and
    ``getattr(policy_score, "access_score", 0.0)``. A plain dict has neither
    attribute, so both reads returned ``''`` and ``0.0`` -- and every waiver
    decision came back *denied for a missing tier* no matter what the caller
    sent. A dict typed as `Any` here would have reproduced that silently.
    """

    policy_tier: str = Field(default="", max_length=50)
    access_score: float = Field(default=0.0, ge=0.0, le=100.0)

    def as_snapshot(self) -> Any:
        """An object with the attributes the policy reader uses."""
        return SimpleNamespace(policy_tier=self.policy_tier, access_score=self.access_score)


class IssueOfferIn(BaseModel):
    """Issue one offer. Admin only.

    ``kind`` is a ``Literal`` because a typo must not reach the upstream
    resolver: ``preview_offer`` raises on an unknown kind, and an exception in
    the middle of a recovery sweep is a worse outcome than a 422.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["goodwill", "waiver", "priority"]
    user_id: int = Field(gt=0)
    waiver_type: str = Field(default="", max_length=40)
    recovery_context: dict[str, Any] = Field(
        default_factory=dict,
        description="read by the upstream resolver; not stored verbatim",
    )
    policy_score: Optional[PolicyScoreIn] = Field(
        default=None,
        description=(
            "the administrative policy snapshot a waiver is authorised against. "
            "Omit it only for an unscored context, where "
            "`evaluate_waiver_approval` takes its documented legacy path"
        ),
    )
    investment_band: str = Field(
        default="",
        max_length=30,
        description=(
            "steers how generous a goodwill offer is; resolved from the Stage C "
            "investment signal when omitted"
        ),
    )
    justification: str = Field(default="", max_length=1000)
    hour: Optional[float] = Field(default=None, ge=0.0, lt=24.0)


class RecordOutcomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["fulfil", "expire"] = Field(
        description=(
            "accept and decline are the customer's, not an operator's; offering "
            "them here would let a sweep close an offer the customer is mid-tap on"
        )
    )
    note: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# The customer's half
# ---------------------------------------------------------------------------


@router.get("/chat/me/offers")
async def get_own_offers(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    include_terminal: bool = True,
):
    """The offer inbox: everything we have offered, and where each one stands.

    Expired and declined offers are **included and labelled**, not filtered out.
    An offer that quietly vanishes from the list reads as "we never offered
    that", which is a different and worse message than "that one expired" -- and
    it is the kind of disappearance that makes a customer stop believing the
    status surface.

    ``actionable`` marks the ones that can still be pressed. That is computed
    against the offer's own clock, not its stored status, so a card never looks
    live and then fail on tap.
    """
    return {
        "offers": await customer_offers.list_offers_for_user(
            db,
            current_user.id,
            include_terminal=include_terminal,
        ),
        "statuses": list(customer_offers.OFFER_STATUSES),
        "kinds": list(customer_offers.OFFER_KINDS),
    }


@router.get("/chat/me/offers/{reference}")
async def get_own_offer(
    reference: str,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """One offer, with the plain-language explanation of why it exists.

    Consent-aware in one narrow way: it changes the framing, never the
    substance. A recovery offer exists because something happened to this
    customer, which is a legitimate-interest communication, so the reason is
    given whether or not marketing consent was ever granted.
    """
    prefs_map, consents = await preferences.load_user_preferences(db, current_user.id)
    payload = await customer_offers.get_offer(
        db,
        reference,
        user_id=current_user.id,
        consents=consents,
        preferences_map=prefs_map,
    )
    if payload is None:
        raise _not_found(f"No offer {reference!r} on your account.")
    return payload


@router.post("/chat/me/offers/{reference}/accept")
async def accept_own_offer(
    reference: str,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """One tap.

    Idempotent in the sense that matters: pressing twice, or pressing an offer
    that expired while the page was open, returns **200 with the current status
    and an explanation** rather than an error page. The customer asked "did that
    work?" and the answer must never be a stack trace.

    Scoped to ``user_id`` on the query, so one customer cannot accept another's
    offer by guessing a reference. The reference is a rendered id and therefore
    guessable, which is why the scope is in the lookup and not in the handler.
    """
    result = await customer_offers.accept_offer(db, reference, user_id=current_user.id)
    if not result.get("found"):
        raise _not_found(f"No offer {reference!r} on your account.")
    await db.commit()
    return result


@router.post("/chat/me/offers/{reference}/decline")
async def decline_own_offer(
    reference: str,
    payload: DeclineOfferIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Decline, and say so kindly.

    The response tells the customer that declining an apology is not a statement
    that the problem is resolved -- because people do read it that way, and then
    stop replying to us about the thing they still have not had fixed.
    """
    result = await customer_offers.decline_offer(
        db,
        reference,
        user_id=current_user.id,
        reason=payload.reason,
    )
    if not result.get("found"):
        raise _not_found(f"No offer {reference!r} on your account.")
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# The operator's half
# ---------------------------------------------------------------------------


@router.get("/chat/admin/recovery/offers")
async def admin_offer_report(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
    window_days: int = 30,
):
    """Acceptance, decline and fulfilment, plus the accept-but-unfulfilled backlog.

    ``acceptance_rate`` is reported beside ``offered`` on purpose. On its own it
    rewards making fewer offers and is maximised by never offering anything at
    all, which is a real and observed way for a retention programme to look
    healthy while doing nothing.
    """
    _ = current_user  # the dependency *is* the gate; nothing else reads it
    return await customer_offers.build_offer_admin_report(db, window_days=window_days)


@router.post("/chat/admin/recovery/offers")
async def admin_issue_offer(
    payload: IssueOfferIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Issue one offer to one customer.

    Returns **200 with ``issued: false``** when the customer is not eligible,
    rather than 409. "We did not offer this because the waiver policy does not
    authorise it for this customer's tier" is an answer an operator asked the
    question and needs; a 409 would throw it away and look like a bug.

    ``investment_band`` is passed straight to the generosity rules rather than
    derived here, so an operator can see what scaling was applied on the response
    and in the issued event.
    """
    prefs_map, consents = await preferences.load_user_preferences(db, payload.user_id)
    result = await customer_offers.issue_offer(
        db,
        payload.user_id,
        payload.kind,
        recovery_context=payload.recovery_context,
        waiver_type=payload.waiver_type,
        policy_score=(
            payload.policy_score.as_snapshot()
            if payload.policy_score is not None
            else None
        ),
        generosity=(
            customer_offers.resolve_offer_generosity(
                {"investment_band": payload.investment_band}
            )
            if payload.investment_band
            else None
        ),
        preferences_map=prefs_map,
        consents=consents,
        justification=payload.justification,
        actor=f"admin:{getattr(current_user, 'username', 'unknown')}",
        hour=payload.hour,
    )
    if result.get("issued"):
        await db.commit()
    else:
        await db.rollback()
    return result


@router.post("/chat/admin/recovery/offers/{reference}/outcome")
async def admin_record_outcome(
    reference: str,
    payload: RecordOutcomeIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Mark an accepted offer fulfilled, or close one as expired.

    Refuses both from ``offered``. An offer nobody accepted cannot be fulfilled
    and one that has not been fulfilled has not expired -- recording either
    would let an operator close an offer the customer is about to accept, which
    is the one race in this subsystem worth naming.
    """
    try:
        result = await customer_offers.record_outcome(
            db,
            reference,
            payload.outcome,
            actor=f"admin:{getattr(current_user, 'username', 'unknown')}",
            note=payload.note,
        )
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    if not result.get("found"):
        raise _not_found(f"No offer {reference!r}.")
    if result.get("recorded"):
        await db.commit()
    else:
        await db.rollback()
    return result