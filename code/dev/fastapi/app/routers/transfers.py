"""Moving value between customers: credit transfers, and third-party debt payment.

Mounted under its own prefix rather than added to ``/chat`` or ``/users``, because
these are the only routes in the tree where one authenticated customer moves
something of value *out of* their own account on behalf of another. That is a
different exposure from everything else here and it gets its own authz rule,
which is why the drift report can see it.

What exists
-----------
* ``POST /transfers/points`` -- move loyalty credit. Both ledger legs, one
  idempotency key, wallets locked in a deterministic order.
* ``GET  /transfers/quote`` -- what would stop it, before the customer commits.
* ``GET  /transfers/history`` -- both directions, from the caller's own id.
* ``POST /transfers/settle-arrears`` -- pay *someone else's* arrears.

What deliberately does not exist
--------------------------------
**Moving a debt.** There is no endpoint for it and there will not be one.
``app/services/transfers.py`` records the reasoning under
``REFUSED_OPERATIONS``, because "why can't I hand my debt to someone?" needs an
answer and an answer that exists only as a missing feature is an answer nobody
wrote down.

The route that *is* here settles another person's debt without moving it: the
named debtor was always liable and still is. That is the useful version -- a family
member pays a bill, an employer settles a balance -- and it leaves no route to
shedding a liability.

One thing worth being blunt about in the response shapes: every settlement
response carries ``debt_transferred: false`` and ``credit_awarded_to_payer: 0``
even though both are structurally incapable of being anything else. A payer who
has just spent their own money on someone else's bill is entitled to be told
plainly that neither their debt nor their loyalty balance moved, rather than
having to infer it from an absence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.schemas import schemas
from app.services import transfers as tf

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(exc: tf.TransferError) -> HTTPException:
    """Turn a refusal into a response that says which rule refused.

    409 rather than 400 for a limit: the request was well-formed and the state
    made it impossible. The distinction is what lets a client tell "you asked for
    something malformed" from "not right now", which are different retries.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": exc.code, "reason": exc.reason},
    )


def _transfer_dict(row: models.PointsTransfer, *, direction: str) -> dict[str, Any]:
    return {
        "transfer_id": row.id,
        "direction": direction,
        "counterparty_user_id": row.to_user_id if direction == "sent" else row.from_user_id,
        "points": float(row.points),
        "point_type": row.point_type,
        "status": row.status,
        "reason": row.reason,
        "note": row.note,
        "consent_confirmed": bool(row.consent_confirmed),
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }


# ---------------------------------------------------------------------------
# Credit transfer
# ---------------------------------------------------------------------------


@router.post("/points", response_model=schemas.PointsTransferOut)
async def transfer_points(
    payload: schemas.PointsTransferIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    _principal: deps.Principal = Depends(deps.require_rate_tier("sensitive")),
):
    """Move loyalty credit to another customer.

    Refuses rather than clamps, and the reasons are returned as they were
    computed. A transfer silently reduced to fit a limit reads as a successful
    gift of a different amount than the one agreed.

    The 409 carries a ``code`` the client can branch on (``insufficient_balance``,
    ``over_per_transfer_limit``, ``transfer_to_self``, ...) alongside the prose,
    so a form can highlight the right field instead of showing one string.
    """
    try:
        result = await tf.transfer_points(
            db,
            from_user_id=current_user.id,
            to_user_id=payload.to_user_id,
            points=payload.points,
            point_type=payload.point_type,
            reason=payload.reason,
            note=payload.note,
            idempotency_key=payload.idempotency_key,
            sender_consent=payload.consent_confirmed,
        )
    except tf.TransferError as exc:
        raise _fail(exc)
    return result


@router.get("/quote", response_model=schemas.TransferQuoteOut)
async def quote_transfer(
    points: float,
    point_type: str = "loyalty",
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """What would stop this transfer, before the customer commits to it.

    Separate from the transfer endpoint because the limits change (daily caps,
    balances), and a client that only discovers them by failing has already shown
    the customer a transfer they cannot make.
    """
    wallet = await tf._wallet_for_update(db, current_user.id, point_type)
    already, count = await tf._today_totals(db, current_user.id)
    return tf.quote_transfer(
        points=points,
        point_type=point_type,
        sender_balance=float(wallet.balance or 0.0),
        already_today=already,
        today_count=count,
    )


@router.get("/history", response_model=schemas.TransferHistoryOut)
async def transfer_history(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
):
    """Everything this account has sent and received.

    The daily totals are included because they are what the limit is enforced
    against, and a customer who has hit it deserves to see why rather than being
    told the feature is unavailable.
    """
    sent_result = await db.execute(
        select(models.PointsTransfer)
        .where(models.PointsTransfer.from_user_id == current_user.id)
        .order_by(models.PointsTransfer.id.desc())
        .limit(50)
    )
    received_result = await db.execute(
        select(models.PointsTransfer)
        .where(models.PointsTransfer.to_user_id == current_user.id)
        .order_by(models.PointsTransfer.id.desc())
        .limit(50)
    )
    already, count = await tf._today_totals(db, current_user.id)
    return {
        "generated_at": _now(),
        "sent": [_transfer_dict(r, direction="sent") for r in sent_result.scalars().all()],
        "received": [_transfer_dict(r, direction="received") for r in received_result.scalars().all()],
        "transferred_today": already,
        "transfers_today": count,
        "note": (
            "each movement appears in both customers' ledger history under one "
            "reference, so the transfer is auditable from either side alone"
        ),
    }


# ---------------------------------------------------------------------------
# Third-party settlement
# ---------------------------------------------------------------------------


@router.post("/settle-arrears", response_model=schemas.SettlementOut)
async def settle_someone_elses_arrears(
    payload: schemas.ThirdPartySettlementIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    _principal: deps.Principal = Depends(deps.require_rate_tier("sensitive")),
):
    """Pay arrears owed by **someone else**.

    What this does *not* do is move the debt. The entry's named debtor remains
    liable before and after; what changes is that the balance is clear. There is
    no endpoint that moves a liability, and :data:`transfers.REFUSED_OPERATIONS`
    records why.

    The caller supplies ``debtor_user_id`` and the service checks it against the
    entry. That check is the point of the field: without it a payer could settle
    one person's debt and have the record attribute it to another, which is the
    shape of a very unpleasant support conversation.

    The payer receives **no loyalty credit**. Settling a friend's arrears for
    points, sending those points back, and repeating is a closed loop that
    converts debt into spendable value, and it is blocked by a CHECK constraint
    rather than by this handler.
    """
    try:
        result = await tf.settle_arrears_for_another(
            db,
            entry_id=payload.arrears_entry_id,
            payer_user_id=current_user.id,
            debtor_user_id=payload.debtor_user_id,
            amount=payload.amount,
            idempotency_key=payload.idempotency_key,
            payer_consent=payload.payer_consent_confirmed,
            note=payload.note,
        )
    except tf.TransferError as exc:
        raise _fail(exc)
    return result


@router.get("/refusals")
async def transfer_refusals():
    """The operations deliberately not offered, and why.

    Public and unauthenticated. A customer who has been told they cannot do
    something is owed the reason, and serving it from the same service that
    refused them means the answer cannot drift into a vague tooltip.
    """
    return {
        "generated_at": _now(),
        "policy": tf.REFUSAL_POLICY,
        "refused": tf.REFUSED_OPERATIONS,
        "available": [
            {
                "operation": "transfer_points",
                "summary": "move loyalty credit between customers",
            },
            {
                "operation": "settle_arrears_for_another",
                "summary": (
                    "pay another customer's arrears. The debtor stays liable and the "
                    "payer earns no loyalty credit."
                ),
            },
        ],
        "note": (
            "the asymmetry is deliberate. Credit is safe to move between holders "
            "because a credit moving cannot create value. Debt is not, and paying "
            "someone else's debt is safe precisely because it moves nothing and "
            "earns nothing."
        ),
    }
