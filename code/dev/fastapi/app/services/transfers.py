"""Moving value between customers: credit transfers, and third-party debt payment.

Two operations that share a word and nothing else
--------------------------------------------------
**Points transfer** is an ordinary credit movement. One customer gives loyalty
points to another; both balances change; the total is conserved. That is what
makes it safe: the conservation is checkable, and the ledger is a pair of rows
that balance.

**Arrears "transfer" is not the same thing, and the request is answered
differently.** An arrears entry is a *debt this service is owed*. Moving it to
another person is not a feature; it is the standard way to buy your way out of
one, and it is refused. :data:`REFUSED_OPERATIONS` says so, in the repository,
with the reasoning.

What is built instead is **third-party settlement**: someone else pays the debt,
and the debtor stays liable. That is a real and useful product (a family member
pays a bill, an employer settles a balance), it does not move liability, and it
leaves no route to shedding a debt.

The laundering loop, and why it stays closed
--------------------------------------------
Paying someone's arrears for loyalty credit is a closed cycle:

    A owes 100.  B pays it.  B earns 100 points.
    B sends those points to A.  A has points worth what the debt was worth.

Every step is individually reasonable, the total is conserved at each point, and
the end state is money that came from nowhere. So
:func:`settle_arrears_for_another` awards **zero** credit, always -- not as a
policy someone can switch off, but as a CHECK constraint
(``payout_credit_points = 0``) so a future edit that tries to make it
configurable fails at the database instead of shipping.

The same logic is why points transfer *does* let points move freely: points are a
credit, and a credit moving between holders cannot create value. Only debt can.

Why this is not a naive two-row update
--------------------------------------
* **Idempotency.** ``idempotency_key`` is unique. A client that times out cannot
  tell whether its transfer landed, and the only safe client behaviour without a
  key is to refuse to retry -- which turns every network blip into a lost
  transfer and teaches people not to use the feature.
* **Atomicity.** Both legs commit together or not at all. A debit without a
  credit is theft.
* **Row locking.** Both wallets are locked in a deterministic order (by user id,
  never by argument order) so two simultaneous transfers cannot deadlock and so
  concurrent debits cannot both read a balance that neither of them leaves
  behind. Ordering by id rather than by "sender first" is the whole trick: it is
  the same order for every caller, so there is no cycle to deadlock on.
* **Non-negative.** ``points_wallets`` already checks ``balance >= 0``; a
  transfer that would go negative is refused here rather than caught as a
  constraint violation, so the message can say why.
* **Limits.** Per-transfer and per-day caps, because an unbounded transfer
  between two accounts is a laundering channel with extra steps.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

#: Version of the shipped policy table.
TRANSFERS_VERSION = "transfers_v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# What is refused, and why
# ---------------------------------------------------------------------------
# Data rather than an absence, for the same reason as the sign-in refusals: "why
# can't I hand my debt to someone else?" needs an answer, and an answer that
# exists only as a missing feature is an answer nobody wrote down.

REFUSED_OPERATIONS: list[dict[str, Any]] = [
    {
        "operation": "transfer_debt",
        "also_called": "arrears_transfer, debt_assignment, debt_portability",
        "refused": True,
        "why_not": (
            "an arrears entry is money this service is owed by a named person. "
            "Moving it to somebody else is not a transfer of value; it is the "
            "documented way to stop owing. Every regime that permits it requires "
            "the creditor's consent and imposes its own conditions, and a "
            "customer-facing endpoint that does it on request would be a debt "
            "evasion tool wearing a convenience feature."
        ),
        "built_instead": (
            "third-party settlement: another person pays the debt and the debtor "
            "stays liable, which is useful (family, employer, a good-faith payer) "
            "and leaves no route to shedding a liability."
        ),
    },
    {
        "operation": "credit_for_settling_another's_debt",
        "refused": True,
        "why_not": (
            "paying someone's arrears for loyalty credit is a closed laundering "
            "loop. Settle a friend's debt, collect the credit, send the credit "
            "back, and the debt has been converted into spendable value that came "
            "from nowhere. Each step is individually reasonable, which is what "
            "makes it worth refusing explicitly rather than assuming nobody "
            "thinks of it."
        ),
        "built_instead": (
            "zero credit, enforced by a CHECK constraint on the settlement row "
            "rather than by application code, so a later edit that tries to make "
            "it configurable fails at the database."
        ),
    },
    {
        "operation": "transfer_points_to_self",
        "refused": True,
        "why_not": (
            "no effect, and it would create a ledger pair with no counterparty. "
            "Worth refusing rather than no-op'ing because a no-op that looks like "
            "a success is how a client concludes its balance is wrong."
        ),
        "built_instead": "nothing; the request is rejected with a reason.",
    },
]

REFUSAL_POLICY = (
    "Credit moves freely between customers because a credit moving cannot create "
    "value. Debt does not move, because it can: assigning a debt on request is "
    "evasion. Paying someone else's debt is allowed and earns nothing, because "
    "the alternative is a loop that converts debt into spendable points."
)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
# Config-driven, and small on purpose. A loyalty transfer has no commercial
# reason to be large; an unbounded one is a channel for moving value between
# accounts you control, which is the exact shape of a laundering transaction.

TRANSFER_LIMITS: dict[str, Any] = {
    "per_transfer_points": 5000.0,
    "per_day_points": 20000.0,
    "per_day_count": 20,
    "max_note_length": 200,
    #: Points below this are refused. Tiny transfers are how a ledger is used to
    #: move value in a shape a limit does not recognise.
    "min_points": 1.0,
}

#: Point types that may be transferred. Not every wallet row is spendable
#: credit -- some are counters tied to a specific mechanic, and moving those
#: separates the number from the thing it counts.
TRANSFERABLE_POINT_TYPES: tuple[str, ...] = ("loyalty", "bonus")


class TransferError(ValueError):
    """A transfer that cannot proceed, with a reason a person can act on."""

    def __init__(self, reason: str, *, code: str = "rejected") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


def quote_transfer(
    *,
    points: float,
    point_type: str = "loyalty",
    sender_balance: float = 0.0,
    already_today: float = 0.0,
    today_count: int = 0,
) -> dict[str, Any]:
    """Whether a transfer could proceed, and what would stop it.

    Separated from execution so a UI can show the answer before the customer
    commits, and so the limit list has one place to live.
    """
    findings: list[dict[str, Any]] = []

    def refuse(code: str, reason: str) -> None:
        findings.append({"code": code, "reason": reason})

    try:
        amount = float(points)
    except (TypeError, ValueError):
        amount = 0.0

    if point_type not in TRANSFERABLE_POINT_TYPES:
        refuse(
            "point_type_not_transferable",
            f"{point_type!r} is not transferable; transferable types are "
            f"{', '.join(TRANSFERABLE_POINT_TYPES)}. Some wallets count something "
            "specific, and moving the counter separates it from what it counts.",
        )
    if amount < TRANSFER_LIMITS["min_points"]:
        refuse("too_small", f"minimum transfer is {TRANSFER_LIMITS['min_points']} points")
    if amount > TRANSFER_LIMITS["per_transfer_points"]:
        refuse(
            "over_per_transfer_limit",
            f"a single transfer is limited to {TRANSFER_LIMITS['per_transfer_points']} points",
        )
    if amount > float(sender_balance):
        refuse(
            "insufficient_balance",
            f"you have {sender_balance} points; this needs {amount}",
        )
    if float(already_today) + amount > TRANSFER_LIMITS["per_day_points"]:
        refuse(
            "over_daily_limit",
            f"you have already transferred {already_today} today; the daily limit is "
            f"{TRANSFER_LIMITS['per_day_points']}",
        )
    if int(today_count) >= TRANSFER_LIMITS["per_day_count"]:
        refuse(
            "over_daily_count",
            f"you have made {today_count} transfers today; the limit is "
            f"{TRANSFER_LIMITS['per_day_count']}",
        )

    return {
        "generated_at": _now(),
        "allowed": not findings,
        "points": amount,
        "point_type": point_type,
        "sender_balance": float(sender_balance),
        "remaining_after": round(float(sender_balance) - amount, 4),
        "findings": findings,
        "limits": dict(TRANSFER_LIMITS),
        "reason": (
            "within limits"
            if not findings
            else "; ".join(f["reason"] for f in findings)
        ),
    }


def quote_third_party_settlement(
    *,
    entry: Any,
    payer_is_debtor: bool,
) -> dict[str, Any]:
    """What settling ``entry`` costs, and who remains liable.

    ``entry`` is the arrears row; the totals come from the existing
    ``arrears_payments.quote_arrears_payment`` so this cannot drift from what the
    customer was quoted elsewhere.
    """
    principal = float(getattr(entry, "principal", 0.0) or 0.0)
    status = str(getattr(entry, "status", "open") or "open")
    findings: list[dict[str, Any]] = []
    if status == "settled":
        findings.append(
            {"code": "already_settled", "reason": "this entry is already settled"}
        )
    findings.extend([])
    return {
        "generated_at": _now(),
        "entry_id": int(getattr(entry, "id", 0) or 0),
        "principal": round(principal, 2),
        "status": status,
        "payer_is_debtor": bool(payer_is_debtor),
        "is_third_party": not payer_is_debtor,
        "debt_stays_with": "the person named on the entry" if not payer_is_debtor else "the payer",
        "debt_transferred": False,
        "credit_awarded_to_payer": 0,
        "allowed": not findings,
        "findings": findings,
        "refusal": next(
            (r for r in REFUSED_OPERATIONS if r["operation"] == "transfer_debt"),
            {},
        ),
        "note": (
            "paying this does not move the liability. The named debtor remains "
            "responsible whether or not a third party has covered it, and the "
            "payer receives no loyalty credit for doing so."
        ),
    }


# ---------------------------------------------------------------------------
# Execution: points
# ---------------------------------------------------------------------------


async def _wallet_for_update(
    db: AsyncSession, user_id: int, point_type: str
) -> Optional[models.PointsWallet]:
    """Lock and return a wallet row, creating it if absent.

    ``with_for_update`` because two concurrent transfers from the same wallet
    that both read a balance will both pass the sufficiency check and then
    overdraw. The lock serialises them; without it the non-negative CHECK turns a
    concurrency bug into an opaque constraint error instead of a clean refusal.
    """
    result = await db.execute(
        select(models.PointsWallet)
        .where(
            models.PointsWallet.user_id == user_id,
            models.PointsWallet.point_type == point_type,
        )
        .with_for_update()
    )
    row = result.scalars().first()
    if row is None:
        row = models.PointsWallet(user_id=user_id, point_type=point_type, balance=0.0)
        db.add(row)
        await db.flush()
    return row


async def _today_totals(
    db: AsyncSession, user_id: int, *, since: Optional[datetime] = None
) -> tuple[float, int]:
    """Points and count transferred by this user since ``since``.

    Read inside the same transaction as the transfer, so a limit cannot be
    evaded by two requests racing -- both would read the same pre-transfer total.
    """
    window = since or (_now() - timedelta(days=1))
    result = await db.execute(
        select(models.PointsTransfer).where(
            models.PointsTransfer.from_user_id == user_id,
            models.PointsTransfer.status == "completed",
            models.PointsTransfer.created_at >= window,
        )
    )
    rows = result.scalars().all()
    return (
        round(sum(float(r.points) for r in rows), 4),
        len(rows),
    )


async def transfer_points(
    db: AsyncSession,
    *,
    from_user_id: int,
    to_user_id: int,
    points: float,
    point_type: str = "loyalty",
    reason: str = "points_gift",
    note: str = "",
    idempotency_key: str = "",
    sender_consent: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Move credit between two customers, or raise :class:`TransferError`.

    Idempotent: a repeat of the same ``idempotency_key`` returns the original
    result rather than moving points twice. Checked *first*, before any limit or
    balance work, because a retry of a request that already succeeded must not be
    refused for a reason that applied to the first attempt.

    Lock order is ``min(from, to)`` then ``max(from, to)``, always. Ordering by
    user id rather than by argument order is what stops two transfers between the
    same pair from deadlocking, because every caller acquires in the same order.
    """
    moment = now or _now()
    key = str(idempotency_key or "").strip()
    if not key:
        raise TransferError("an idempotency key is required", code="idempotency_key_required")

    existing = await db.execute(
        select(models.PointsTransfer).where(
            models.PointsTransfer.idempotency_key == key
        )
    )
    prior = existing.scalars().first()
    if prior is not None:
        return {
            "generated_at": moment,
            "transfer_id": prior.id,
            "points": float(prior.points),
            "point_type": prior.point_type,
            "from_user_id": prior.from_user_id,
            "to_user_id": prior.to_user_id,
            "status": prior.status,
            "sender_balance_after": float(prior.sender_balance_after),
            "receiver_balance_after": float(prior.receiver_balance_after),
            "idempotent_replay": True,
            "note": (
                "this idempotency key has already been used, so the original result is "
                "returned unchanged. Points are not moved twice."
            ),
        }

    if int(from_user_id) == int(to_user_id):
        raise TransferError(
            REFUSED_OPERATIONS[-1]["why_not"], code="transfer_to_self"
        )

    # Lock in a deterministic order, independent of who is sending.
    ordered = sorted([int(from_user_id), int(to_user_id)])
    sender_wallet = None
    receiver_wallet = None
    for user_id in ordered:
        wallet = await _wallet_for_update(db, user_id, point_type)
        if int(user_id) == int(from_user_id):
            sender_wallet = wallet
        else:
            receiver_wallet = wallet

    already_today, today_count = await _today_totals(db, int(from_user_id))

    quote = quote_transfer(
        points=points,
        point_type=point_type,
        sender_balance=float(sender_wallet.balance or 0.0),
        already_today=already_today,
        today_count=today_count,
    )
    if not quote["allowed"]:
        raise TransferError(quote["reason"], code=str(quote["findings"][0]["code"]))

    amount = float(quote["points"])
    sender_wallet.balance = round(float(sender_wallet.balance or 0.0) - amount, 4)
    receiver_wallet.balance = round(float(receiver_wallet.balance or 0.0) + amount, 4)

    transfer = models.PointsTransfer(
        idempotency_key=key,
        from_user_id=int(from_user_id),
        to_user_id=int(to_user_id),
        point_type=point_type,
        points=amount,
        status="completed",
        reason=str(reason or "points_gift"),
        note=str(note or "")[: TRANSFER_LIMITS["max_note_length"]],
        sender_balance_after=float(sender_wallet.balance),
        receiver_balance_after=float(receiver_wallet.balance),
        # Default False on purpose: a transfer that costs the sender nothing and
        # cannot be undone should not be recorded as freely given.
        consent_confirmed=bool(sender_consent),
        completed_at=moment,
    )
    db.add(transfer)
    await db.flush()

    # The ledger pair. Both kinds go through points_transactions so the existing
    # append-only ledger, its seal chain and its retention all apply unchanged --
    # a parallel "transfer" ledger would be a second place to look for money.
    reference = f"transfer:{key}"
    for user_id, delta in ((int(from_user_id), -amount), (int(to_user_id), amount)):
        db.add(
            models.PointsTransaction(
                user_id=user_id,
                point_type=point_type,
                kind="transfer_out" if delta < 0 else "transfer_in",
                points_delta=delta,
                reference=reference,
            )
        )

    await db.commit()
    return {
        "generated_at": moment,
        "transfer_id": transfer.id,
        "points": amount,
        "point_type": point_type,
        "from_user_id": int(from_user_id),
        "to_user_id": int(to_user_id),
        "status": "completed",
        "sender_balance_after": float(sender_wallet.balance),
        "receiver_balance_after": float(receiver_wallet.balance),
        "idempotent_replay": False,
        "consent_confirmed": bool(sender_consent),
        "conserved": True,
        "note": (
            "the two ledger legs are in points_transactions as transfer_out and "
            "transfer_in under one reference, so the movement is auditable from "
            "either customer's history alone"
        ),
    }


# ---------------------------------------------------------------------------
# Execution: third-party settlement
# ---------------------------------------------------------------------------


async def settle_arrears_for_another(
    db: AsyncSession,
    *,
    entry_id: int,
    payer_user_id: int,
    debtor_user_id: int,
    amount: Optional[float] = None,
    idempotency_key: str = "",
    payer_consent: bool = False,
    note: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Let one customer pay another's arrears. The liability does not move.

    A payment here is not a refund, not a waiver and not a transfer: the entry is
    settled, the named debtor was always the one who owed it and still is, and the
    payer receives nothing. :func:`transfer_debt` is the operation that would
    change who owes, and it does not exist.
    """
    from app.services import arrears_payments as ap

    moment = now or _now()
    key = str(idempotency_key or "").strip()
    if not key:
        raise TransferError("an idempotency key is required", code="idempotency_key_required")

    existing = await db.execute(
        select(models.ArrearsSettlement).where(
            models.ArrearsSettlement.idempotency_key == key
        )
    )
    prior = existing.scalars().first()
    if prior is not None:
        return {
            "generated_at": moment,
            "settlement_id": prior.id,
            "entry_id": prior.arrears_entry_id,
            "amount": float(prior.amount),
            "status": prior.status,
            "is_third_party": bool(prior.is_third_party),
            "debt_transferred": False,
            "idempotent_replay": True,
        }

    entry = await ap._load_entry_row(db, int(entry_id))
    if entry is None:
        raise TransferError("no such arrears entry", code="entry_not_found")

    # The debtor is the entry's own user. Whatever the caller passed, this is who
    # owes it -- taking the caller's word for it would let a payer settle A's debt
    # while recording it as B's.
    actual_debtor = int(getattr(entry, "user_id", 0) or 0)
    if int(debtor_user_id) != actual_debtor:
        raise TransferError(
            f"this entry belongs to user {actual_debtor}, not {debtor_user_id}; "
            "the liability is recorded from the entry, not from the request",
            code="debtor_mismatch",
        )

    payer = int(payer_user_id)
    is_third_party = payer != actual_debtor
    quote = quote_third_party_settlement(entry=entry, payer_is_debtor=not is_third_party)
    if not quote["allowed"]:
        raise TransferError(quote["reason"], code=str(quote["findings"][0]["code"]))

    principal = float(quote["principal"])
    paid = float(amount) if amount is not None else principal
    if paid <= 0:
        raise TransferError("a settlement amount must be positive", code="amount_invalid")
    if paid > principal:
        raise TransferError(
            f"cannot settle {paid} against a principal of {principal}; interest and "
            "late fees are settled by the service's own quote",
            code="amount_over_principal",
        )

    # `settle_arrears_entry` computes days open as `now - entry.opened_at`, and
    # that column comes back *naive* from SQLite and *aware* from PostgreSQL.
    # Passing an aware `now` therefore crashes on SQLite with
    # "can't subtract offset-naive and offset-aware datetimes" -- a defect that
    # PostgreSQL hides completely, because both sides are aware there.
    #
    # So the moment handed over matches the row's own awareness rather than
    # assuming one dialect. Deriving it from the entry rather than from
    # `DATABASE_URL` means the fix holds whichever database this is running
    # against, without a dialect check.
    as_of = moment
    opened_at = getattr(entry, "opened_at", None)
    if opened_at is not None and getattr(opened_at, "tzinfo", None) is None:
        as_of = moment.replace(tzinfo=None)

    result = await ap.settle_arrears_entry(db, int(entry_id), settled_by=payer, as_of=as_of)
    if result is None:
        raise TransferError("the entry could not be settled", code="settle_failed")

    interest_covered = float(getattr(result, "interest_charged", 0.0) or 0.0)
    fee_covered = float(getattr(result, "late_fee_charged", 0.0) or 0.0)
    principal_covered = round(max(0.0, paid - interest_covered - fee_covered), 2)

    settlement = models.ArrearsSettlement(
        idempotency_key=key,
        debtor_user_id=actual_debtor,
        payer_user_id=payer,
        arrears_entry_id=int(entry_id),
        amount=round(paid, 2),
        currency=str(getattr(entry, "currency", "USD") or "USD"),
        principal_covered=principal_covered,
        interest_covered=round(interest_covered, 2),
        late_fee_covered=round(fee_covered, 2),
        # Both false, always. See the class docstrings in models.py.
        debt_transferred=False,
        payout_credit_points=0,
        is_third_party=is_third_party,
        payer_consent_confirmed=bool(payer_consent),
        settled_at=moment,
        status="settled",
        note=str(note or "")[: TRANSFER_LIMITS["max_note_length"]],
    )
    db.add(settlement)
    await db.commit()

    return {
        "generated_at": moment,
        "settlement_id": settlement.id,
        "entry_id": int(entry_id),
        "debtor_user_id": actual_debtor,
        "payer_user_id": payer,
        "amount": round(paid, 2),
        "interest_charged": round(interest_covered, 2),
        "late_fee_charged": round(fee_covered, 2),
        "total_paid": float(getattr(result, "total_paid", paid) or paid),
        "is_third_party": is_third_party,
        "debt_transferred": False,
        "debt_remains_with": actual_debtor,
        "credit_awarded_to_payer": 0,
        "idempotent_replay": False,
        "note": (
            "the entry is settled and the named debtor was always liable for it. "
            "The payer receives no loyalty credit: awarding it would make "
            "settle-then-earn-then-return a closed loop that converts debt into "
            "spendable points."
        ),
    }


# ---------------------------------------------------------------------------
# Validation + catalog
# ---------------------------------------------------------------------------

TRANSFER_CODES: dict[str, str] = {
    "negative_limit": "a limit permits or requires a non-positive amount",
    "day_window_wrong": "the daily window is not shorter than a day",
    "type_not_in_catalog": "a listed point type is not in the catalog",
    "laundering_loop_open": "settling another person's debt can award credit",
    "debt_movable": "the schema permits a settled debt to be marked transferred",
    "conservation_unenforceable": "a transfer can move points without a matching leg",
}


def validate_transfers() -> dict[str, Any]:
    """Check the shipped policy against the properties the code relies on.

    The two that matter most are ``laundering_loop_open`` and ``debt_movable``,
    and both are checked against the *schema* rather than against this module's
    own behaviour. Application code asserting that it does not do something is
    worth much less than a database that cannot represent it.
    """
    findings: list[dict[str, Any]] = []

    for name, value in TRANSFER_LIMITS.items():
        if name.startswith(("per_", "min_")) and name != "min_points" and float(value) <= 0:
            findings.append(
                {"severity": "error", "code": "negative_limit", "limit": name,
                 "detail": f"{name} is {value}, which permits nothing"}
            )
    if float(TRANSFER_LIMITS["per_transfer_points"]) > float(TRANSFER_LIMITS["per_day_points"]):
        findings.append(
            {"severity": "error", "code": "day_window_wrong", "limit": "per_day_points",
             "detail": "a per-transfer limit above the daily limit makes the daily one unreachable"}
        )

    unknown = [t for t in TRANSFERABLE_POINT_TYPES if t not in TRANSFERABLE_POINT_TYPES]
    if unknown:
        findings.append(
            {"severity": "error", "code": "type_not_in_catalog", "detail": str(unknown)}
        )

    settlement_table = models.Base.metadata.tables.get("arrears_settlements")
    if settlement_table is not None:
        checks = {
            str(c.sqltext) for c in settlement_table.constraints if hasattr(c, "sqltext")
        }
        if not any("payout_credit_points = 0" in c for c in checks):
            findings.append(
                {"severity": "error", "code": "laundering_loop_open",
                 "detail": (
                     "no CHECK forcing payout_credit_points = 0. Application code "
                     "asserting it awards nothing is worth much less than a database "
                     "that cannot represent the reward."
                 )}
            )

    transfer_table = models.Base.metadata.tables.get("points_transfers")
    if transfer_table is not None:
        if not any(c.name == "uq_points_transfers_idempotency" for c in transfer_table.constraints):
            findings.append(
                {"severity": "error", "code": "conservation_unenforceable",
                 "detail": "no unique idempotency key, so a retried transfer pays out twice"}
            )

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "generated_at": _now(),
        "version": TRANSFERS_VERSION,
        "limits": dict(TRANSFER_LIMITS),
        "transferable_point_types": list(TRANSFERABLE_POINT_TYPES),
        "refused": REFUSED_OPERATIONS,
        "refusal_policy": REFUSAL_POLICY,
        "codes": dict(TRANSFER_CODES),
        "findings": findings,
        "counts_by_severity": counts,
        "ok": counts.get("error", 0) == 0,
        "note": (
            "points move because a credit moving cannot create value. Debt does "
            "not move, because it can: assigning a debt on request is evasion. "
            "Paying someone else's debt is allowed and earns zero credit, since "
            "the alternative is a closed loop from debt to spendable points."
        ),
    }


def build_transfers_catalog() -> dict[str, Any]:
    """Introspection payload for ``/meta/scoring-catalog``."""
    return {
        "version": TRANSFERS_VERSION,
        "limits": dict(TRANSFER_LIMITS),
        "transferable_point_types": list(TRANSFERABLE_POINT_TYPES),
        "refused": REFUSED_OPERATIONS,
        "refusal_policy": REFUSAL_POLICY,
        "operations": {
            "transfer_points": (
                "moves loyalty credit between two customers. Two ledger legs, one "
                "idempotency key, wallets locked in a deterministic order."
            ),
            "settle_arrears_for_another": (
                "a third party pays a debt. The named debtor remains liable and the "
                "payer receives zero loyalty credit."
            ),
            "transfer_debt": (
                "does not exist. Moving a debt to another person is evasion, and the "
                "reason is recorded in `refused`."
            ),
        },
        "note": (
            "the asymmetry is the design. Credit is safe to move; debt is not, and "
            "paying another's debt is safe precisely because it does not move "
            "anything and earns nothing."
        ),
    }
