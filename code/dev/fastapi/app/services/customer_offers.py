"""Customer-facing recovery offers, and what happened to each one (Stage B).

``services/recovery_playbooks.py`` already knows what to offer: ``
RECOVERY_SAVE_INCENTIVES`` holds three tiers and ``resolve_recovery_incentive``
composes a preview of the right one. It says so itself — the preview carries
``"issued": False`` and the action is registered as *issues nothing*. That was a
deliberate boundary: composing an offer and giving it to a customer are
different acts, and the second one has to be somebody's.

This module is the second act. It issues, persists, hands over, and records what
came back.

What it deliberately does **not** do
------------------------------------
* **It does not invent a fourth offer catalogue.** The three kinds are the three
  that already exist somewhere, and each one names its upstream rule so the offer
  says *which policy produced it* rather than merely *we offered you this*:

  ==============  ==============================================================
  ``goodwill``    ``RECOVERY_SAVE_INCENTIVES`` via ``resolve_recovery_incentive``
  ``waiver``      ``ARREARS_WAIVER_POLICY`` via ``evaluate_waiver_approval``
  ``priority``    ``RECOVERY_REVIEW_RULES`` via ``resolve_recovery_review_priority``
  ==============  ==============================================================

  That last one matters more than it looks. Two modules each declared their own
  ``PRIORITY_RANK`` — ``points_exchange`` and ``arrears_payments``, both
  ``high/medium/low`` — and this subsystem needed a third notion of priority
  (``urgent/high/normal``, from the review rules) that means *queue urgency*, not
  *rule evaluation order*. Adding a fourth copy would have been the easy way. The
  kind's priority is read from the review table instead, and
  :func:`validate_offers` asserts the two existing copies agree so they cannot
  drift apart unnoticed.

* **It does not add a second preference gate.** Every gate question is asked of
  ``services/preferences.py``: ``effective_contact_plan`` already answers
  permitted / reactive-only / quiet hours / cooldown / channel / the consent
  gates, and a second implementation of that logic is a second thing to keep
  correct. This module decides what to *ask it*.

The gate is on outreach, not on the offer
-----------------------------------------
The user's requirement is *preference gates on all proactive outreach and
recovery contact*. Read carefully that is a statement about **contact**, and this
module treats it that way:

* The offer is **issued and visible** even when the customer asked for reactive
  contact only. A ``only_reactive`` customer must still find the offer in their
  inbox and be able to accept it in one tap — a preference about *how often we
  interrupt you* is not a preference about *whether you are told*.
* The **notification** is what the preference governs, and it is deferred with a
  ``deferred_until`` the scheduler can act on rather than dropped.
* **Consent never withholds a recovery or service offer.** ``service_permitted``
  is consulted, and ``is_outreach_permitted`` already exempts those purposes from
  the consent gate — a customer who reported a problem must not have the fix
  withheld because of a marketing setting. Re-deriving that rule here would be the
  fastest way to break the one invariant this subsystem is built around.

Honesty about expiry
--------------------
An offer expires whether or not anybody looked at it, so expiry is computed from
``expires_at`` on read as well as swept in the background. A customer returning
after 72 hours must see "expired", not a stale "open" card that fails when they
press accept.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.services import arrears_payments, preferences, recovery_playbooks

OFFERS_CATALOG_VERSION = 1

#: The three kinds, and where each one's rules already live.
#:
#: ``purpose`` is what the preference/consent gate is asked about. All three are
#: ``service`` or ``recovery`` on purpose: they are the answer to something that
#: happened to this customer, not a campaign, and classifying them as marketing
#: would make a consent toggle able to suppress a fix.
CUSTOMER_OFFER_KINDS: tuple[dict[str, Any], ...] = (
    {
        "offer_kind": "goodwill",
        "title": "Goodwill credit",
        "purpose": "recovery",
        "upstream": "RECOVERY_SAVE_INCENTIVES",
        "resolver": "recovery_playbooks.resolve_recovery_incentive",
        "resolver_path": "app.services.recovery_playbooks:resolve_recovery_incentive",
        "customer_facing": (
            "a credit we decided you were owed, with the reason attached so it "
            "reads as an apology rather than a discount"
        ),
        "has_expiry": True,
        "has_follow_up": True,
    },
    {
        "offer_kind": "waiver",
        "title": "Fee or interest waiver",
        "purpose": "service",
        "upstream": "ARREARS_WAIVER_POLICY",
        "resolver": "arrears_payments.evaluate_waiver_approval",
        "resolver_path": "app.services.arrears_payments:evaluate_waiver_approval",
        "customer_facing": (
            "a charge removed, with which charge named -- a waiver a customer "
            "cannot see is indistinguishable from an error"
        ),
        "has_expiry": True,
        "has_follow_up": True,
    },
    {
        "offer_kind": "priority",
        "title": "Priority handling",
        "purpose": "service",
        "upstream": "RECOVERY_REVIEW_RULES",
        "resolver": "recovery_playbooks.resolve_recovery_review_priority",
        "resolver_path": (
            "app.services.recovery_playbooks:resolve_recovery_review_priority"
        ),
        "customer_facing": (
            "we will handle you ahead of the queue. No expiry: a queue position "
            "is not a thing you can use next month"
        ),
        "has_expiry": False,
        "has_follow_up": False,
    },
)

OFFER_KINDS: tuple[str, ...] = tuple(str(row["offer_kind"]) for row in CUSTOMER_OFFER_KINDS)
OFFER_KIND_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["offer_kind"]): dict(row) for row in CUSTOMER_OFFER_KINDS
}

#: The vocabulary of ``customer_offers.status``.
#:
#: ``offered`` is the only state a customer can act in. ``accepted`` means they
#: said yes and the effect is owed; ``fulfilled`` means the effect landed. They
#: are separate because "you accepted and we have not done it yet" is a real and
#: embarrassing state, and collapsing it into ``fulfilled`` would erase the only
#: signal that there is a backlog.
OFFER_STATUSES: tuple[str, ...] = (
    "offered",
    "accepted",
    "declined",
    "expired",
    "fulfilled",
)
OFFER_STATUS_RANK: dict[str, int] = {name: index for index, name in enumerate(OFFER_STATUSES)}

#: Open to the customer, until it is not.
CUSTOMER_ACTIONABLE_STATUSES: frozenset[str] = frozenset({"offered"})

#: Terminal: nothing further happens to this offer.
TERMINAL_STATUSES: frozenset[str] = frozenset({"declined", "expired", "fulfilled"})

#: The verbs in ``customer_offer_events.kind``.
#:
#: The verb rather than the resulting status, because ``accepted`` and
#: ``fulfilled`` are two events that a status-only vocabulary would call the same
#: thing, and "was this fulfilled or only accepted" is the question an operator
#: actually asks.
OFFER_EVENT_KINDS: tuple[str, ...] = (
    "issued",
    "accepted",
    "declined",
    "expired",
    "fulfilled",
    "notified",
    "notification_deferred",
    "redisplayed",
)

#: The state machine, as data.
#:
#: Fail closed: a transition not listed is refused, and an unknown target status
#: is refused rather than guessed. A state machine that defaults to "allow" is a
#: state machine that will eventually allow reviving a fulfilled offer.
OFFER_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "offered": ("accepted", "declined", "expired"),
    "accepted": ("fulfilled",),
    # `declined` is terminal. Re-offering is a *new offer row* with a new
    # reference, which is what makes "we offered twice" answerable.
    "declined": (),
    "expired": (),
    "fulfilled": (),
}

#: Which wallet a goodwill offer's points land in when it is fulfilled.
#:
#: Named here rather than inlined so the credit this module writes is visibly the
#: same one ``recovery_playbooks`` writes: ``credit_recovery_points`` takes a
#: ``point_type`` argument, and a fulfilment that quietly used a different string
#: than the playbook path would create a second wallet for the same customer and
#: split their balance across two rows that each look correct.
OFFER_POINTS_TYPE = "loyalty_points"


def offer_credit_reference(row: Any) -> str:
    """The ledger ``reference`` for this offer's credit, and its idempotency key.

    Derived from the offer's own reference, so it is unique per offer by
    construction and needs no new column to stay unique across restarts -- the
    same reasoning that made ``CustomerOffer.reference`` a rendered primary key
    rather than a counter (``CustomerOffer``'s own docstring records that the
    counter version reissued values after every redeploy).
    """
    reference = str(getattr(row, "reference", "") or "").strip()
    return f"offer:{reference}" if reference else "offer:unreferenced"


#: How much the investment/LTV signal may scale a goodwill offer.
#:
#: Declared here rather than in the Stage C module because the *mechanism* has to
#: exist before the signal does: the scale is recorded on the offer row, so an
#: amount cannot move under an offer the customer has already accepted. The
#: signals themselves are supplied by ``services/loyalty_status.py``.
#:
#: Bounded at both ends and capped by ``max_percent`` per kind, because a rule
#: that can double a credit is a rule that can be configured into an unbounded
#: liability.
OFFER_GENEROSITY_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "generosity_default",
        "label": "Standard generosity",
        "when": {"investment_band": ["unscored", "low", "standard"]},
        "scale": 1.0,
        "reason": "no investment signal, or a signal at or below the ordinary band",
    },
    {
        "rule_id": "generosity_established",
        "label": "Reward an established relationship",
        "when": {"investment_band": ["high"]},
        "scale": 1.25,
        "reason": (
            "a long-tenured customer with completed history is worth more than "
            "the cost of the credit, and a standard offer reads as an insult to "
            "someone who has been here the whole time"
        ),
    },
    {
        "rule_id": "generosity_strategic",
        "label": "Protect a high-value relationship",
        "when": {"investment_band": ["strategic"]},
        "scale": 1.5,
        "reason": (
            "the highest investment signal present; capped per kind so this "
            "cannot be configured into an unbounded liability"
        ),
    },
)

GENEROSITY_SCALE_BOUNDS = (0.5, 2.0)

#: Who is credited for an automated issuance. Not a user id: the sweep is not a
#: person, and ``issued_by_id`` deliberately does not point at ``users.id``.
AUTOMATED_ACTOR = "system:recovery-sweep"
ADMIN_ACTOR_PREFIX = "admin:"

#: Cap on how much a single generosity rule may add, as a percentage of the base
#: amount. Belt and braces with the scale bounds: the bounds stop a *rule* being
#: extreme, this stops a *base amount* being tiny (where 1.5x of 5 points is not
#: worth the rounding).
OFFER_MAX_GENEROSITY_PERCENT = 50.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return (str(value),)


def describe_offer_kind(kind: str) -> dict[str, Any]:
    """The declared description of a kind, or a refusal naming the valid set."""
    row = OFFER_KIND_BY_NAME.get(str(kind))
    if row is None:
        raise ValueError(f"unknown offer kind {kind!r}; known: {', '.join(OFFER_KINDS)}")
    return dict(row)


def offer_purpose(kind: str) -> str:
    """The consent/preference purpose an offer of this kind is asked about."""
    return str(describe_offer_kind(kind)["purpose"])


# ---------------------------------------------------------------------------
# Generosity: the Stage C hook, with its mechanism already in place
# ---------------------------------------------------------------------------


def resolve_offer_generosity(signals: Mapping[str, Any]) -> dict[str, Any]:
    """How much to scale a goodwill offer, from the investment signal.

    Returns ``scale: 1.0`` for anything unrecognised, including a missing
    signal — deliberately *not* a zero. An absent signal is not evidence of low
    value, and failing closed on money owed to a customer is how a well-meant
    generosity rule becomes a way of short-changing people.
    """
    context = dict(signals or {})
    band = str(context.get("investment_band") or "unscored").strip().lower() or "unscored"
    context["investment_band"] = band
    for rule in OFFER_GENEROSITY_RULES:
        try:
            ok, _fields = recovery_playbooks.evaluate_when(rule.get("when", {}), context)
        except Exception:  # noqa: BLE001 - a generosity miss must not break issuance
            continue
        if not ok:
            continue
        try:
            scale = float(rule["scale"])
        except (KeyError, TypeError, ValueError):
            scale = 1.0
        low, high = GENEROSITY_SCALE_BOUNDS
        clamped = max(low, min(high, scale))
        return {
            "scale": clamped,
            "rule_id": str(rule["rule_id"]),
            "reason": str(rule["reason"]),
            "investment_band": band,
            "clamped": clamped != scale,
        }
    return {
        "scale": 1.0,
        "rule_id": "generosity_default",
        "reason": "no generosity rule matched; offering the unscaled amount",
        "investment_band": band,
        "clamped": False,
    }


def _apply_generosity(amount: float, generosity: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    """Scale an amount, and say whether the cap bound it.

    Returns the scaled value *and* the note, because an offer that silently
    arrived at a rounder number than the rule asked for is exactly the sort of
    thing that gets noticed in a reconciliation and never explained.
    """
    base = max(0.0, float(amount or 0.0))
    scale = float(generosity.get("scale") or 1.0)
    scaled = base * scale
    cap = base * (1.0 + OFFER_MAX_GENEROSITY_PERCENT / 100.0)
    capped = scaled > cap
    if capped:
        scaled = cap
    return scaled, {
        "base": base,
        "scale": scale,
        "result": round(scaled, 2),
        "cap_applied": capped,
        "rule_id": str(generosity.get("rule_id") or ""),
        "reason": str(generosity.get("reason") or ""),
    }


# ---------------------------------------------------------------------------
# Previews: one per kind, each delegating to the upstream rule
# ---------------------------------------------------------------------------


def preview_goodwill_offer(
    context: Mapping[str, Any], *, generosity: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """The save-offer preview from the recovery engine, plus its scaling.

    The upstream resolver returns ``"issued": False``; that flag is dropped
    here, because whether it was issued is this module's business and leaving it
    in the payload would let a caller read a preview as an offer.
    """
    incentive = recovery_playbooks.resolve_recovery_incentive(dict(context or {}))
    source = str(incentive.get("offer_id") or "")
    if not source:
        # No tier matched, which means the customer is not currently in a
        # recovery state. Returning an empty preview with `eligible: False` is
        # the honest answer; inventing a default-sized credit would hand money to
        # everyone the sweep touches.
        return {
            "eligible": False,
            "offer_kind": "goodwill",
            "reason": (
                "no RECOVERY_SAVE_INCENTIVES tier matched this recovery context"
            ),
            "source_offer_id": "",
            "headline": "",
            "internal_name": "",
            "points": 0.0,
            "discount_percent": 0.0,
            "validity_hours": 0.0,
            "escalation": recovery_playbooks.RECOVERY_INCENTIVE_DEFAULT["escalation"],
        }
    applied = dict(generosity or resolve_offer_generosity(context or {}))
    points, scaling = _apply_generosity(incentive.get("points", 0.0), applied)
    return {
        "eligible": True,
        "offer_kind": "goodwill",
        "source_offer_id": source,
        # Customer-safe label; the engine's own name travels separately so an
        # operator can see which tier fired without showing it to a customer.
        "headline": OFFER_HEADLINES["goodwill"],
        "internal_name": str(incentive.get("name") or ""),
        "points": round(points, 2),
        "discount_percent": float(incentive.get("discount_percent") or 0.0),
        "validity_hours": float(incentive.get("validity_hours") or 0.0),
        "escalation": str(incentive.get("escalation") or "none"),
        "generosity": scaling,
    }


def preview_waiver_offer(
    waiver_type: str, policy_score: Any = None
) -> dict[str, Any]:
    """A waiver preview from the arrears policy, never a third waiver decision.

    ``evaluate_waiver_approval`` is the only thing that decides whether a waiver
    is authorised. This does not re-read ``ARREARS_WAIVER_POLICY``, does not
    compare the tier itself, and does not offer a waiver the policy refused —
    an offer a customer can accept that the system would then refuse to apply is
    worse than no offer, because it makes the refusal the customer's problem.
    """
    approval = arrears_payments.evaluate_waiver_approval(str(waiver_type), policy_score)
    # Read `allowed`, which is what *this* engine spells it. The natural guess
    # from the other two previews is `approved` -- and that key does not exist
    # here, so it is silently False on every policy that *grants* a waiver. The
    # consequence is pointed the wrong way: an authorised waiver looks refused
    # and nobody reads an absent key as a bug.
    authorised = bool(approval.get("allowed"))
    return {
        "eligible": authorised,
        "offer_kind": "waiver",
        # Kept alongside `eligible` because `eligible` is this module's word and
        # `authorised` is the policy's, and a reader comparing the two against
        # the raw approval should be able to see they are the same answer.
        "authorised": authorised,
        "source_offer_id": str(waiver_type),
        "waiver_type": str(waiver_type),
        "headline": OFFER_HEADLINES["waiver"] if authorised else "",
        "internal_name": str(approval.get("required_policy_tier") or ""),
        "points": 0.0,
        "discount_percent": 0.0,
        "validity_hours": 0.0,
        "escalation": "none",
        "approval": approval,
        "reason": (
            str(approval.get("reason") or "")
            if authorised
            else f"the waiver policy does not authorise this: {approval.get('reason', '')}"
        ),
    }


def preview_priority_offer(context: Mapping[str, Any]) -> dict[str, Any]:
    """Priority handling from the recovery review rules.

    No expiry, deliberately: ``has_expiry`` is False for this kind in
    :data:`CUSTOMER_OFFER_KINDS`, and a queue position with a 72-hour deadline
    would be a deadline on the customer's problem rather than on our queue.
    """
    priority = recovery_playbooks.resolve_recovery_review_priority(dict(context or {}))
    rule_id = str(priority.get("rule_id") or "")
    label = str(priority.get("label") or "")
    if not rule_id:
        return {
            "eligible": False,
            "offer_kind": "priority",
            "source_offer_id": "",
            "headline": "",
            "priority": "",
            "validity_hours": 0.0,
            "reason": "no RECOVERY_REVIEW_RULES row matched this recovery context",
        }
    return {
        "eligible": True,
        "offer_kind": "priority",
        "source_offer_id": rule_id,
        "headline": OFFER_HEADLINES["priority"],
        "internal_name": label,
        "priority": str(priority.get("priority") or ""),
        "points": 0.0,
        "discount_percent": 0.0,
        "validity_hours": 0.0,
        "sla_hours": priority.get("sla_hours"),
        "reason": "the review rules place this customer in a priority review queue",
    }


def preview_offer(
    kind: str,
    *,
    recovery_context: Optional[Mapping[str, Any]] = None,
    waiver_type: str = "",
    policy_score: Any = None,
    generosity: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Dispatch to the right upstream resolver. Never a fourth implementation."""
    name = str(kind)
    if name == "goodwill":
        return preview_goodwill_offer(recovery_context or {}, generosity=generosity)
    if name == "waiver":
        if not waiver_type:
            raise ValueError("a waiver offer needs a waiver_type; known: interest, fees")
        return preview_waiver_offer(waiver_type, policy_score)
    if name == "priority":
        return preview_priority_offer(recovery_context or {})
    raise ValueError(f"unknown offer kind {kind!r}; known: {', '.join(OFFER_KINDS)}")


def offer_eligibility(kind: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Whether a kind is on offer at all for this customer, and why.

    Separate from the preview on purpose: a caller asking "can this customer be
    offered a waiver" wants the policy answer without having to decide which
    arguments the preview needs, and a preview that raises because
    ``waiver_type`` was omitted is a bad answer to that question.
    """
    name = str(kind)
    describe_offer_kind(name)
    if name == "waiver":
        return {
            "offer_kind": name,
            "eligible": False,
            "reason": (
                "a waiver needs a waiver_type and a policy score; eligibility "
                "cannot be decided without them"
            ),
            "needs": ["waiver_type", "policy_score"],
        }
    return {
        "offer_kind": name,
        "eligible": True,
        "reason": (
            "eligibility is decided by "
            f"{OFFER_KIND_BY_NAME[name]['resolver']} at issue time"
        ),
        "needs": ["recovery_context"],
    }


# ---------------------------------------------------------------------------
# The preference gate
# ---------------------------------------------------------------------------


def resolve_offer_contact(
    preferences_map: Mapping[str, Any] | None,
    consents: Mapping[str, bool] | None,
    *,
    purpose: str,
    hour: Optional[float] = None,
    last_contact_at: Optional[datetime] = None,
    resolved_channel: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ask the care gate whether to make contact.

    A thin wrapper over :func:`app.services.care_gate.consult` rather than a
    second implementation of the same logic. That module exists because this one
    was the **only** service in the codebase that consulted the preference centre
    -- recovery outreach, callbacks, journey steps and communication all chose how
    to contact somebody without asking. One gate, asked by every path, so there is
    one place to be wrong.

    ``preferences_map`` is now ``Optional`` so ``None`` means "could not read
    them", which the gate treats as **do not push**. The Stage B version had no
    way to express that: it took a ``Mapping`` and an empty dict looked identical
    to a real profile with nothing in it, so a failed read would have been
    indistinguishable from a customer who had expressed no preference -- and the
    second is permissive while the first is not.

    Ask ``preferences.effective_contact_plan`` whether to make contact.

    Returns ``{"issue": bool, "proactive": bool, ...}`` and the split is the
    whole design:

    ``issue``
        May this offer exist at all? False only when the *purpose itself* is
        consent-gated and the consent is absent — which none of the three Stage B
        kinds is, because they are all service or recovery.
    ``proactive``
        May we interrupt the customer about it *now*? False when the frequency is
        ``only_reactive``, when quiet hours hold, or when the cooldown has not
        elapsed.

    Never conflates the two. An ``only_reactive`` customer gets ``issue: True,
    proactive: False`` and a ``deferred_until`` — the offer waits in their inbox,
    which is the difference between respecting a preference and hiding a fix.
    """
    from app.services import care_gate

    decision = care_gate.consult(
        "offer_notification",
        preferences_map,
        consents,
        purpose=purpose,
        hour=hour,
        last_contact_at=last_contact_at,
        resolved_channel=resolved_channel,
        now=now,
    )
    return {
        # Kept as `issue`/`proactive` because the Stage B surface and its tests
        # read those names, and renaming a published key to fix nothing.
        "issue": decision["issue"],
        "proactive": decision["push"],
        "push": decision["push"],
        "deferred_until": decision["deferred_until"],
        "purpose": decision["purpose"],
        "service_critical": decision["service_critical"],
        "channel": {"channel": decision["channel"], "source": "care_gate", "honored": True}
        if decision["channel"]
        else {"channel": "", "source": "none", "honored": False},
        "frequency": decision["frequency"],
        "cooldown_hours": decision["cooldown_hours"],
        "contact_plan_reason": decision["contact_plan_reason"],
        "consent": decision["consent"],
        "consent_gate_applied": not decision["service_critical"],
        "gate_mode": decision["mode"],
        "reasons": decision["reasons"],
        "note": (
            "issue and proactive are separate questions. A reactive-only customer "
            "still receives the offer and can accept it in one tap; what is "
            "withheld is the interruption, and it is deferred with a time rather "
            "than dropped."
        ),
    }


def _next_window_start(moment: datetime, start_hour: Any) -> Optional[str]:
    try:
        start = float(start_hour)
    except (TypeError, ValueError):
        return None
    target = moment.replace(hour=int(start), minute=0, second=0, microsecond=0)
    if target <= moment:
        target += timedelta(days=1)
    return target.isoformat()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def resolve_offer_transition(current: str, target: str) -> dict[str, Any]:
    """Is ``current -> target`` a move this state machine makes?

    Fail closed. An unknown current or target status is refused with the valid
    set named, because a machine that defaults to "allowed" will eventually
    revive a fulfilled offer and the ledger will say it was never fulfilled.
    """
    now = str(current)
    nxt = str(target)
    if now not in OFFER_STATUS_RANK:
        return {
            "allowed": False,
            "reason": f"unknown current status {current!r}; known: {', '.join(OFFER_STATUSES)}",
            "unknown": True,
        }
    if nxt not in OFFER_STATUS_RANK:
        return {
            "allowed": False,
            "reason": f"unknown target status {target!r}; known: {', '.join(OFFER_STATUSES)}",
            "unknown": True,
        }
    if nxt not in OFFER_TRANSITIONS.get(now, ()):
        reachable = OFFER_TRANSITIONS.get(now, ())
        detail = f"to none of {', '.join(reachable)}" if not reachable else "not reachable from here"
        return {
            "allowed": False,
            "reason": f"an offer cannot go {now} -> {nxt} ({detail})",
            "terminal": not reachable,
        }
    return {"allowed": True, "reason": f"{now} -> {nxt}", "unknown": False}


def is_expired(row: Any, *, now: Optional[datetime] = None) -> bool:
    """Whether an offer's own clock has run out.

    Computed on read as well as swept, because a customer returning after the
    window must see "expired" rather than a card that looks open and fails when
    they press accept. A datetime-aware comparison, because a naive expiry read
    back from SQLite compares against an aware ``now`` and raises.
    """
    expires_at = getattr(row, "expires_at", None)
    status = str(getattr(row, "status", "") or "")
    if status in TERMINAL_STATUSES:
        return status == "expired"
    if expires_at is None:
        return False
    return _aware(expires_at) <= _aware(now or _now())


def effective_status(row: Any, *, now: Optional[datetime] = None) -> str:
    """The status a reader should see, accounting for elapsed expiry."""
    if is_expired(row, now=now):
        return "expired"
    return str(getattr(row, "status", "") or "")


# ---------------------------------------------------------------------------
# Consent-aware explanation
# ---------------------------------------------------------------------------

#: Customer-facing short labels. Deliberately **not** the upstream names:
#: ``RECOVERY_SAVE_INCENTIVES`` calls its top tier "Critical save offer", and
#: showing a customer that string tells them our churn model graded them
#: "critical" -- unsettling, and a small disclosure of the scoring. The internal
#: name stays in ``internal_name`` and in the issued event payload, where an
#: operator can find it and a customer cannot.
OFFER_HEADLINES: dict[str, str] = {
    "goodwill": "A credit from us",
    "waiver": "A charge removed",
    "priority": "You are ahead of the queue",
}

#: The customer-facing sentences, per kind. Split from the engine's own wording
#: on purpose: see :data:`OFFER_HEADLINES` for why the upstream name is not used.
OFFER_EXPLANATIONS: dict[str, dict[str, str]] = {
    "goodwill": {
        "what": "We put {points} points on your account and took {discount}% off your next service.",
        "why": (
            "Something went wrong on our side and you had to chase it. This is our "
            "way of saying sorry, and it is not a discount you would have earned."
        ),
        "legal": "Offered because you reported a problem we were handling. No marketing consent is required for it.",
    },
    "waiver": {
        "what": "We have removed {waiver_type} from your account.",
        "why": (
            "Charging you for a delay we caused is not something we are willing to do, "
            "so the charge is being removed rather than reduced."
        ),
        "legal": "Offered because a charge on your account is being waived under your policy.",
    },
    "priority": {
        "what": "We will handle anything you send next ahead of other requests.",
        "why": (
            "You should not have to wait behind a queue to get an answer after "
            "reporting a problem."
        ),
        "legal": "Offered because your open issue is being escalated for urgent review.",
    },
}


def explain_offer(
    row: Any,
    *,
    consents: Optional[Mapping[str, bool]] = None,
    preferences_map: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Plain-language explanation of an offer, shaped by consent and preferences.

    Consent-aware in one specific, narrow way: it never withholds the
    *substance*. A service or recovery offer exists because something happened to
    this customer, and that is a legitimate-interest communication — so the
    reason is always given, whether or not marketing consent was ever granted.
    What consent *does* change is the framing: with no marketing consent the
    explanation avoids campaign language entirely, because a "we miss you, here
    is something special" wrapper on a recovery credit is a retention email that
    happens to contain an apology.

    ``plain_language_explanations`` (default on) selects the full sentence form;
    turning it off yields the terse form, which is what that preference has
    always meant everywhere else in this codebase.
    """
    kind = str(getattr(row, "offer_kind", "") or "")
    status = effective_status(row, now=now)
    template = dict(OFFER_EXPLANATIONS.get(kind) or {})
    points = float(getattr(row, "points", 0.0) or 0.0)
    discount = float(getattr(row, "discount_percent", 0.0) or 0.0)
    waiver_type = str(getattr(row, "waiver_type", "") or "charge")
    marketing_granted = bool((consents or {}).get("marketing", True))
    terse = not bool(
        dict(preferences_map or {}).get("plain_language_explanations", True)
    )
    framing = "campaign" if marketing_granted else "service_only"
    if terse:
        what = f"{kind} offer: {points:g} points, {discount:g}% off."
        why = str(template.get("why") or "")
    else:
        what = str(template.get("what") or "").format(
            points=int(points),
            discount=discount,
            waiver_type=waiver_type,
        )
        why = str(template.get("why") or "")
    return {
        "offer_kind": kind,
        "reference": str(getattr(row, "reference", "") or ""),
        "status": status,
        "what": what,
        "why": why,
        "legal_basis": str(template.get("legal") or ""),
        "framing": framing,
        "marketing_consent": marketing_granted,
        "terse": terse,
        "generosity": (
            {
                "scale": float(getattr(row, "generosity_scale", 1.0) or 1.0),
                "note": (
                    "scaled by the investment signal; the amount is fixed at issue "
                    "so it cannot move under an accepted offer"
                ),
            }
            if float(getattr(row, "generosity_scale", 1.0) or 1.0) != 1.0
            else None
        ),
        "substance_withheld": False,
        "note": (
            "consent shapes the framing, never the substance: a recovery or service "
            "offer exists because something happened to this customer, and the "
            "reason is given either way"
        ),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def offer_reference(offer_id: int) -> str:
    """``OFF-00000042`` — the primary key rendered, not a counter.

    Rendered from the id rather than generated from a per-process sequence for
    the reason the complaints spine documents: a counter reissues its own values
    after every redeploy, and two customers then hold the same reference, which
    makes "was this the same offer" unanswerable.
    """
    return f"OFF-{int(offer_id):08d}"


async def _record_event(
    db: AsyncSession,
    *,
    offer_id: int,
    user_id: int,
    kind: str,
    from_status: str,
    to_status: str,
    actor: str,
    note: str = "",
    payload: Optional[Mapping[str, Any]] = None,
) -> models.CustomerOfferEvent:
    import json as _json

    event = models.CustomerOfferEvent(
        offer_id=int(offer_id),
        user_id=int(user_id),
        kind=str(kind),
        from_status=str(from_status or ""),
        to_status=str(to_status or ""),
        actor=str(actor or ""),
        note=str(note or ""),
        payload_json=_json.dumps(dict(payload or {}), default=str, sort_keys=True),
    )
    db.add(event)
    await db.flush()
    return event


async def issue_offer(
    db: AsyncSession,
    user_id: int,
    kind: str,
    *,
    recovery_context: Optional[Mapping[str, Any]] = None,
    waiver_type: str = "",
    policy_score: Any = None,
    generosity: Optional[Mapping[str, Any]] = None,
    preferences_map: Optional[Mapping[str, Any]] = None,
    consents: Optional[Mapping[str, bool]] = None,
    justification: str = "",
    actor: str = AUTOMATED_ACTOR,
    hour: Optional[float] = None,
    last_contact_at: Optional[datetime] = None,
    resolved_channel: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Compose an offer from the upstream policy, gate it, and persist it.

    Returns ``{"issued": False, ...}`` rather than raising when the customer is
    not eligible or the purpose is consent-gated. "We decided not to offer you a
    waiver" is a normal outcome that the caller has to be able to report, and an
    exception would make every sweep crash on the first healthy customer.
    """
    moment = _aware(now or _now())
    describe_offer_kind(kind)
    purpose = offer_purpose(kind)
    contact = resolve_offer_contact(
        preferences_map or {},
        consents or {},
        purpose=purpose,
        hour=hour,
        last_contact_at=last_contact_at,
        resolved_channel=resolved_channel,
        now=moment,
    )
    if not contact["issue"]:
        return {
            "issued": False,
            "offer_kind": str(kind),
            "reason": (
                f"the {purpose} purpose is consent-gated and the consent is absent, "
                "so this offer was not created"
            ),
            "contact": contact,
        }
    preview = preview_offer(
        str(kind),
        recovery_context=recovery_context,
        waiver_type=waiver_type,
        policy_score=policy_score,
        generosity=generosity,
    )
    if not preview.get("eligible"):
        return {
            "issued": False,
            "offer_kind": str(kind),
            "reason": str(preview.get("reason") or "not eligible"),
            "preview": preview,
            "contact": contact,
        }
    spec = describe_offer_kind(kind)
    validity = float(preview.get("validity_hours") or 0.0)
    expires_at = moment + timedelta(hours=validity) if validity > 0 else None
    follow_up = (
        moment + timedelta(hours=validity / 2.0) if spec.get("has_follow_up") and validity > 0 else None
    )
    row = models.CustomerOffer(
        user_id=int(user_id),
        reference="",
        offer_kind=str(kind),
        source_offer_id=str(preview.get("source_offer_id") or ""),
        status="offered",
        headline=str(preview.get("headline") or ""),
        points=float(preview.get("points") or 0.0),
        discount_percent=float(preview.get("discount_percent") or 0.0),
        waiver_type=str(preview.get("waiver_type") or ""),
        priority=str(preview.get("priority") or ""),
        generosity_scale=float((preview.get("generosity") or {}).get("scale") or 1.0),
        issued_at=moment,
        expires_at=expires_at,
        follow_up_due_at=follow_up,
        justification=str(justification or ""),
    )
    db.add(row)
    await db.flush()
    # Rendered from the id, after the flush that assigned it.
    row.reference = offer_reference(row.id)
    await db.flush()

    await _record_event(
        db,
        offer_id=int(row.id),
        user_id=int(user_id),
        kind="issued",
        from_status="",
        to_status="offered",
        actor=str(actor or AUTOMATED_ACTOR),
        note=str(justification or ""),
        payload={
            "source_offer_id": row.source_offer_id,
            "points": row.points,
            "discount_percent": row.discount_percent,
            "priority": row.priority,
            "waiver_type": row.waiver_type,
            "generosity_scale": row.generosity_scale,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "preview": {k: v for k, v in preview.items() if k != "approval"},
        },
    )
    if contact["proactive"]:
        await _record_event(
            db,
            offer_id=int(row.id),
            user_id=int(user_id),
            kind="notified",
            from_status="offered",
            to_status="offered",
            actor=str(actor or AUTOMATED_ACTOR),
            payload={
                "channel": (contact.get("channel") or {}).get("channel", ""),
                "frequency": contact.get("frequency", ""),
            },
        )
    else:
        await _record_event(
            db,
            offer_id=int(row.id),
            user_id=int(user_id),
            kind="notification_deferred",
            from_status="offered",
            to_status="offered",
            actor=str(actor or AUTOMATED_ACTOR),
            note="; ".join(contact.get("reasons") or ()),
            payload={"deferred_until": contact.get("deferred_until")},
        )
    return {
        "issued": True,
        "offer_id": int(row.id),
        "reference": row.reference,
        "offer_kind": str(kind),
        "status": "offered",
        "expires_at": expires_at.isoformat() if expires_at else None,
        "contact": contact,
        "preview": preview,
        "headline": row.headline,
    }


async def _load_offer(
    db: AsyncSession,
    reference: str,
    *,
    user_id: Optional[int] = None,
    fresh: bool = False,
) -> Optional[models.CustomerOffer]:
    """Load one offer, optionally forcing a read past the session's identity map.

    ``fresh`` exists for :func:`_loses_race` and only that caller. SQLAlchemy keys
    loaded objects by primary key and, by default, a SELECT that returns a row it
    has *already loaded in this session* hands back the existing instance with its
    attributes untouched rather than overwriting them -- correct for a
    request-scoped session, and actively wrong when another transaction has just
    changed the row, because the caller is asking precisely "what is it now?" and
    gets the answer from before the change. Without ``populate_existing`` a losing
    racer re-read the offer and was told it was still ``offered``, having just
    lost the right to say so because somebody else had accepted it.

    So a read that has to reflect another writer's commit says so explicitly here
    instead of every caller remembering that it might matter.
    """
    stmt = select(models.CustomerOffer).where(models.CustomerOffer.reference == str(reference))
    if user_id is not None:
        stmt = stmt.where(models.CustomerOffer.user_id == int(user_id))
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    result = await db.execute(stmt)
    return result.scalars().first()


async def _claim_transition(
    db: AsyncSession,
    row: models.CustomerOffer,
    target: str,
    *,
    values: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
    require_unexpired: bool = False,
) -> bool:
    """Move this offer from the status the caller read to ``target``, or report
    that somebody else got there first.

    Returns ``True`` when *this* caller now owns the transition. ``False`` means
    the row was no longer in the state the caller's read found, nothing has been
    written, and no event recorded -- so the caller must re-read and report rather
    than retry, because retrying is exactly the double-write this exists to stop.

    Every state change to an offer goes through here: ``accept_offer``,
    ``decline_offer``, ``record_outcome`` and ``expire_offers_due``. That is not
    tidiness, it is the fix. Each of the four used to read the row, decide in
    Python against ``OFFER_TRANSITIONS``, and then assign -- and two callers doing
    that concurrently both read ``offered``, both decided, and both wrote. The
    state machine was enforced only against callers that did not overlap in time,
    which is a weaker guarantee than the comment above :data:`OFFER_TRANSITIONS`
    claims to give. Accept and decline on one offer in the same tick both
    committed, and the append-only trail recorded both mutually exclusive
    decisions for it.

    **Why a conditional UPDATE and not ``with_for_update()``.** A row lock is the
    obvious answer and it would have been a bad one. ``SELECT ... FOR UPDATE`` is
    *silently ignored by SQLite*, which is the database this entire test suite
    runs on -- a lock-based fix would go green here and be unprotected in
    production, with the tests asserting the lock's presence and measuring nothing.
    This invariant is better served by a predicate than by a lock, because it does
    not need the transaction to last long enough for a lock to matter: the state
    being protected is one column, and the check and the write are the same
    statement. ``services/transfers.py`` uses ``with_for_update()`` for a wallet,
    where a read-modify-write over a *balance* spans several statements and
    genuinely needs serialising; this is a single-column status change.

    **Why every precondition goes in the WHERE clause.** The state-machine check
    above this call has already run, and between it and this write another
    request can do anything at all. Putting ``status`` -- and, when the caller
    asks for it, ``expires_at`` -- into the predicate is what makes the row count
    an answer to "were all my preconditions still true one statement ago". So a
    row count of zero is never ambiguous and never needs a retry loop: it means the
    caller lost, and the only correct next step is to re-read and answer from the
    state that actually exists.

    ``require_unexpired`` is opt-in because the clock is a precondition only for
    the two *customer* actions. An operator closing an offer late or early must be
    able to, and :func:`expire_offers_due` is selecting on expiry, so demanding
    the opposite of its own criterion there would refuse every row it selected.
    """
    moment = _aware(now or _now())
    assignments: dict[str, Any] = {"status": str(target)}
    assignments.update(dict(values or {}))
    stmt = (
        update(models.CustomerOffer)
        .where(
            models.CustomerOffer.id == int(row.id),
            # The status *as stored*, because that is what the caller's
            # state-machine decision ran against. `effective_status` is a
            # clock-dependent reading of the row, not something the database can
            # evaluate in a predicate, so it cannot appear here.
            models.CustomerOffer.status == str(row.status),
        )
        .values(**assignments)
        .execution_options(synchronize_session=False)
    )
    if require_unexpired:
        stmt = stmt.where(
            or_(
                models.CustomerOffer.expires_at.is_(None),
                models.CustomerOffer.expires_at > moment,
            )
        )
    result = await db.execute(stmt)
    if int(result.rowcount or 0) != 1:
        return False
    # `synchronize_session=False` above leaves the loaded instance stale on
    # purpose. Every caller needs the post-transition values -- they go into the
    # event payload and the response -- so refresh here rather than making four
    # call sites remember to. The refresh is also what makes the subsequent
    # `_record_event` report the transition that actually happened rather than the
    # one that was attempted.
    await db.refresh(row)
    return True


async def _loses_race(
    db: AsyncSession, reference: str, *, user_id: Optional[int] = None
) -> Optional[models.CustomerOffer]:
    """Re-read an offer after a lost claim, or ``None`` if it has since gone.

    Called only on the ``_claim_transition`` ``False`` path. It exists so the
    refusal a losing racer gives is the *same* refusal a sequential caller would
    have got for the same final state -- "this offer is accepted" rather than a
    race-flavoured message the customer has no way to interpret.

    ``fresh=True`` is not optional here. The session already holds this offer,
    loaded before the losing write, and SQLAlchemy would otherwise hand that
    cached instance straight back -- so the loser would be told the offer was
    still ``offered``, the one answer that is both wrong and the most likely to
    be acted on, because it invites the customer to tap the button again.
    """
    return await _load_offer(db, reference, user_id=user_id, fresh=True)


async def list_offers_for_user(
    db: AsyncSession,
    user_id: int,
    *,
    include_terminal: bool = True,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """The customer's inbox, most urgent first.

    Expired offers are included (and labelled) rather than filtered out: an offer
    that quietly vanishes from the list reads as "we never offered that", which
    is a different and worse message than "that one expired".
    """
    stmt = select(models.CustomerOffer).where(models.CustomerOffer.user_id == int(user_id))
    rows = list((await db.execute(stmt)).scalars().all())
    presented = [offer_summary(row, now=now) for row in rows]
    if not include_terminal:
        presented = [row for row in presented if row["status"] not in TERMINAL_STATUSES]
    presented.sort(
        key=lambda row: (
            0 if row["status"] in CUSTOMER_ACTIONABLE_STATUSES else 1,
            -OFFER_STATUS_RANK.get(row["status"], 99),
            row["issued_at"],
            row["reference"],
        )
    )
    return presented


async def get_offer(
    db: AsyncSession,
    reference: str,
    *,
    user_id: Optional[int] = None,
    consents: Optional[Mapping[str, bool]] = None,
    preferences_map: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    row = await _load_offer(db, reference, user_id=user_id)
    if row is None:
        return None
    payload = offer_summary(row, now=now)
    payload["explanation"] = explain_offer(
        row, consents=consents, preferences_map=preferences_map, now=now
    )
    return payload


async def accept_offer(
    db: AsyncSession,
    reference: str,
    *,
    user_id: int,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One tap. Idempotent in the sense that matters: a second tap is a no-op.

    An already-accepted offer returns ``accepted: False`` and the current
    status rather than raising, because the one-tap path is the path a
    double-submitting customer takes, and an error page after a successful
    accept is the worst possible answer to "did that work?".
    """
    moment = _aware(now or _now())
    row = await _load_offer(db, reference, user_id=user_id)
    if row is None:
        return {
            "accepted": False,
            "reason": f"no offer {reference!r} for this customer",
            "found": False,
        }
    status = effective_status(row, now=moment)
    if status != "offered":
        # Persist the expiry if the clock ran out, so the row stops claiming to
        # be open. A read-only view of a stale status would be re-discovered as
        # wrong on every subsequent read.
        if status == "expired" and str(row.status) != "expired":
            # Claimed like every other write. Two racers both noticing the clock
            # ran out used to both write `expired` and both record an `expired`
            # event for one offer -- a convergent value, so the row was harmless,
            # but a trail that reports the same expiry twice is a trail nobody can
            # read. Losing this claim is not an error: the other racer recorded it.
            if await _claim_transition(db, row, "expired", now=moment):
                await _record_event(
                    db, offer_id=int(row.id), user_id=int(user_id), kind="expired",
                    from_status="offered", to_status="expired", actor="system:expiry",
                    note="the offer's own validity window elapsed",
                )
            else:
                row = await _loses_race(db, reference, user_id=user_id) or row
        return {
            "accepted": False,
            "found": True,
            "reference": row.reference,
            "status": status,
            "reason": (
                f"this offer is {status}, so it cannot be accepted"
                + (" (it expired " + row.expires_at.isoformat() + ")" if status == "expired" and row.expires_at else "")
            ),
        }
    move = resolve_offer_transition(str(row.status), "accepted")
    if not move["allowed"]:
        return {"accepted": False, "found": True, "reason": move["reason"]}
    if not await _claim_transition(
        db, row, "accepted", values={"accepted_at": moment}, now=moment, require_unexpired=True
    ):
        # Lost. Answer from the state that exists, exactly as if the other caller
        # had finished first -- which is the case this method already handled,
        # so there is one refusal shape rather than two.
        settled = await _loses_race(db, reference, user_id=user_id)
        if settled is None:
            return {
                "accepted": False, "found": False,
                "reason": f"no offer {reference!r} for this customer",
            }
        now_status = effective_status(settled, now=moment)
        return {
            "accepted": False,
            "found": True,
            "reference": settled.reference,
            "status": now_status,
            "reason": f"this offer is {now_status}, so it cannot be accepted",
        }
    await _record_event(
        db,
        offer_id=int(row.id),
        user_id=int(user_id),
        kind="accepted",
        from_status="offered",
        to_status="accepted",
        actor=str(actor or f"customer:{user_id}"),
        payload={
            "points": row.points,
            "discount_percent": row.discount_percent,
            "waiver_type": row.waiver_type,
            "priority": row.priority,
        },
    )
    return {
        "accepted": True,
        # `found` is what the router checks before it commits, and it was missing
        # from this branch. Every other return in this function carries it --
        # `found: False` for a miss, `found: True` for a status that forbids the
        # move -- and the success path omitted it, so
        # `POST /chat/me/offers/{reference}/accept` raised 404 for *every
        # successful accept*: the customer pressed accept, the offer was accepted,
        # and they were told it was not theirs. The status change survived because
        # the flush had already run, so the row read back as `accepted` and the
        # next tap correctly reported "already accepted" -- which is why this reads
        # as a permissions problem at the second tap rather than as a missing key
        # at the first.
        #
        # The service-level tests assert `result["accepted"] is True` and never
        # look at `found`, so they passed throughout.
        "found": True,
        "reference": row.reference,
        "offer_id": int(row.id),
        "status": "accepted",
        "points": row.points,
        "discount_percent": row.discount_percent,
        "waiver_type": row.waiver_type,
        "priority": row.priority,
        "awaiting_fulfilment": True,
        "next": (
            "we will apply this and you will see it in your status view"
            if row.offer_kind != "priority"
            else "you are now ahead of the queue for your next request"
        ),
    }


async def decline_offer(
    db: AsyncSession,
    reference: str,
    *,
    user_id: int,
    reason: str = "",
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Decline, and record the reason.

    The reason is the most valuable thing this subsystem collects and the reason
    it is stored as free text: a customer explaining why they do not want an
    apology credit is usually describing the problem we failed to fix, and a
    controlled vocabulary would discard exactly that.
    """
    moment = _aware(now or _now())
    row = await _load_offer(db, reference, user_id=user_id)
    if row is None:
        return {"declined": False, "found": False, "reason": f"no offer {reference!r}"}
    status = effective_status(row, now=moment)
    if status != "offered":
        return {
            "declined": False,
            "found": True,
            "status": status,
            "reason": f"this offer is {status}, so it cannot be declined",
        }
    move = resolve_offer_transition(str(row.status), "declined")
    if not move["allowed"]:
        return {"declined": False, "found": True, "reason": move["reason"]}
    if not await _claim_transition(
        db,
        row,
        "declined",
        values={"declined_at": moment, "decline_reason": str(reason or "")},
        now=moment,
        require_unexpired=True,
    ):
        # The same refusal as the sequential case, from the same helper. Before
        # this, accept and decline on one offer in the same tick both committed:
        # the trail carried `accepted (offered -> accepted)` and then
        # `declined (offered -> declined)` for a single offer, so the row and the
        # audit trail disagreed about what one person did in one instant.
        settled = await _loses_race(db, reference, user_id=user_id)
        if settled is None:
            return {"declined": False, "found": False, "reason": f"no offer {reference!r}"}
        now_status = effective_status(settled, now=moment)
        return {
            "declined": False,
            "found": True,
            "status": now_status,
            "reason": f"this offer is {now_status}, so it cannot be declined",
        }
    await _record_event(
        db,
        offer_id=int(row.id),
        user_id=int(user_id),
        kind="declined",
        from_status="offered",
        to_status="declined",
        actor=str(actor or f"customer:{user_id}"),
        note=str(reason or ""),
        payload={"decline_reason_recorded": bool(reason)},
    )
    return {
        "declined": True,
        # See the note on the same key in `accept_offer`. Every non-success
        # branch here carries `found`; the success branch did not, so the router
        # raised 404 *after* the decline had been flushed. A customer who said no
        # was told the offer was not theirs.
        "found": True,
        "reference": row.reference,
        "status": "declined",
        "reason_recorded": bool(reason),
        "next": (
            "we have closed this offer. If the underlying problem is still open, "
            "replying to the original message reaches a person -- declining an "
            "apology is not a statement that the problem is resolved"
        ),
    }


async def record_outcome(
    db: AsyncSession,
    reference: str,
    outcome: str,
    *,
    actor: str,
    note: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Record ``fulfil`` or ``expire`` on an offer. Admin/automated only.

    **``fulfil`` is refused from ``offered``**, because an offer nobody accepted
    cannot have been fulfilled, and a sweep that marked it so would record a
    fulfilment that never happened. That is the one race worth naming here: the
    customer is mid-tap on an offer an operator just closed as fulfilled.

    ``expire`` is *allowed* from ``offered``, deliberately, and the asymmetry is
    the point:

    * an offer's own clock running out is expiry -- that is what
      :func:`expire_offers_due` does, and it must be expressible;
    * an operator withdrawing a mis-sent offer is expiry, and refusing it would
      mean leaving a wrong offer sitting in a customer's inbox because the only
      way to close it early was not available.

    So the restriction is on *fulfilment*, which is the claim that requires a
    customer decision behind it, rather than on expiry generally.
    """
    moment = _aware(now or _now())
    verb = str(outcome)
    target = {"fulfil": "fulfilled", "expire": "expired", "fulfill": "fulfilled"}.get(verb)
    if target is None:
        raise ValueError(
            f"unknown outcome {outcome!r}; known: fulfil, expire "
            "(accept and decline belong to the customer)"
        )
    row = await _load_offer(db, reference)
    if row is None:
        return {"recorded": False, "found": False, "reason": f"no offer {reference!r}"}
    status = effective_status(row, now=moment)
    move = resolve_offer_transition(status, target)
    if not move["allowed"]:
        return {
            "recorded": False,
            "found": True,
            "status": status,
            "reason": move["reason"],
        }
    if not await _claim_transition(
        db,
        row,
        target,
        values=(
            {"fulfilled_at": moment}
            if target == "fulfilled"
            # Not a clock precondition: an operator closing an offer late or early
            # is the legitimate case, and `expire_offers_due` selects *on* expiry,
            # so demanding "unexpired" here would refuse every row it selected.
            else {"follow_up_due_at": None}
        ),
        now=moment,
    ):
        settled = await _loses_race(db, reference)
        if settled is None:
            return {"recorded": False, "found": False, "reason": f"no offer {reference!r}"}
        now_status = effective_status(settled, now=moment)
        return {
            "recorded": False,
            "found": True,
            "status": now_status,
            "reason": f"this offer is {now_status}, so {verb!r} cannot be recorded",
        }
    # The effect, applied. Claimed first on purpose: `_apply_offer_effect` moves a
    # wallet balance, and it must only run for the caller that actually owns the
    # fulfilment. Two racers both reaching here would credit twice.
    effect = (
        await _apply_offer_effect(db, row, now=moment)
        if target == "fulfilled"
        else {"applied": False, "components": {}, "requires_out_of_band": []}
    )
    await _record_event(
        db,
        offer_id=int(row.id),
        user_id=int(row.user_id),
        kind=target,
        from_status=str(status),
        to_status=target,
        actor=str(actor),
        note=str(note or ""),
        payload={
            "automated": str(actor).startswith("system:"),
            # What actually moved, in the trail. The note field is the operator's
            # own record of work done out of band; this is the machine's record of
            # what it did itself. Keeping them in one payload is what makes
            # `requires_out_of_band` answerable months later.
            "effect": effect,
        },
    )
    return {
        "recorded": True,
        # The third of the three. `record_outcome` was the one that made this
        # hardest to spot, because a *refused* transition (`offered -> fulfilled`)
        # carries `found: True` and returns 200, while the successful one
        # 404'd. So "fulfil this offer" failed and "try to fulfil it twice" both
        # look like refusals from the outside, and only the row moving separates
        # them. The router docstring for this endpoint claims it "refuses both
        # from ``offered``"; it refuses `fulfil` from `offered` and allows
        # `expire` from it, which the state machine permits -- noted in the doc
        # fix below rather than changed here, because an operator closing an
        # unaccepted offer on time is the ordinary case.
        "found": True,
        "reference": row.reference,
        "status": target,
        "kind": target,
        # Present only on a fulfilment, and always. `fulfilled` is defined as
        # "the effect landed" (see :data:`OFFER_STATUSES`), so a response that
        # says `recorded: true` without saying what landed is a machine
        # answering a question nobody asked it.
        **({"effect": effect} if target == "fulfilled" else {}),
    }


async def _prior_offer_credit(
    db: AsyncSession, row: models.CustomerOffer
) -> Optional[models.PointsTransaction]:
    """Has this offer's wallet credit already been written? Defence in depth.

    :func:`_claim_transition` is the real guarantee -- ``fulfilled`` is terminal
    and can only be claimed once -- and this is the second belt. It exists because
    a double credit is the kind of error that is invisible until a customer's
    balance is wrong by an amount nobody can explain, and because the wallet
    writer it calls (:func:`recovery_playbooks.credit_recovery_points`) has no
    idempotency guard of its own and is shared with the playbook path, where
    repeated credits are the point.

    The caller treats a hit here as an **error to refuse**, not as a credit to
    skip. Silently skipping would leave ``fulfilled`` recorded against an offer
    whose effect was applied an unknown number of times, which is the exact shape
    of bug this document was written about.
    """
    stmt = select(models.PointsTransaction).where(
        models.PointsTransaction.user_id == int(row.user_id),
        models.PointsTransaction.reference == offer_credit_reference(row),
    )
    return (await db.execute(stmt)).scalars().first()


async def _apply_offer_effect(
    db: AsyncSession, row: models.CustomerOffer, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Apply what this offer promised, and report exactly what moved.

    **Why this function exists.** ``fulfilled`` is defined as *"the effect landed"*
    (:data:`OFFER_STATUSES`), and ``OFFER_EXPLANATIONS`` tells the customer in the
    present perfect that it already has -- "We put {points} points on your
    account", "We have removed {waiver_type} from your account". Before this,
    fulfilling an offer moved the status, wrote an event, and returned
    ``recorded: true`` while touching no balance anywhere. The endpoint took a
    ``note`` field reading "250 points added" and reported success for work it had
    not verified and could not see. That is a machine answering "yes" to a question
    nobody asked it, and it is why the whole backlog surface
    (:func:`follow_ups_due`) could report an empty queue over a set of effects
    nobody had ever received.

    **What is automatic, and what is honestly not.** One of the three kinds has an
    effect this codebase can apply:

    * ``goodwill`` -- the points go onto the wallet through
      :func:`recovery_playbooks.credit_recovery_points`, the same canonical writer
      the playbook path uses, so there is one place that moves a balance.
    * ``waiver`` -- the effect is an arrears entry, and
      :attr:`CustomerOffer.arrears_entry_id` is populated by nothing in this
      module (grep: no reader outside ``transfers.py``). There is no entry to
      waive, so this reports ``requires_out_of_band`` rather than guessing.
    * ``priority`` -- the effect is a queue position. Queue urgency is not modelled
      as a value anything reads, so there is nothing to apply.

    The ``discount_percent`` on a goodwill offer is the same story: the column is
    carried through the offer catalogue and **no pricing engine in this repository
    consumes it** (``grep discount_percent app/`` returns this module and the
    playbook that composes the offer, and nothing that prices anything). So it is
    reported as requiring out-of-band action instead of being silently marked
    delivered.

    Reporting the un-appliable parts is not a shrug. It is the difference between
    an operator reading ``requires_out_of_band: []`` and knowing the customer has
    their points, and reading ``requires_out_of_band: ["discount_percent"]`` and
    knowing to go and apply the discount by hand. The response and the event
    payload carry the same structure, so the trail answers "did this actually
    happen?" months later -- which is :func:`build_offer_admin_report`'s stated
    job and was not answerable before.
    """
    kind = str(getattr(row, "offer_kind", "") or "")
    points = round(float(getattr(row, "points", 0.0) or 0.0), 2)
    discount = round(float(getattr(row, "discount_percent", 0.0) or 0.0), 2)
    waiver_type = str(getattr(row, "waiver_type", "") or "")
    components: dict[str, Any] = {}
    requires_out_of_band: list[str] = []

    if kind != "goodwill":
        components[kind or "unknown"] = {
            "automatic": False,
            "why": (
                "no effect in this codebase applies a "
                f"{kind or 'unknown'} offer; it is a change to a system outside "
                "this service and an operator records it in the note"
            ),
        }
        if waiver_type:
            components["waiver"] = {
                "waiver_type": waiver_type,
                "automatic": False,
                "why": (
                    "this offer carries no arrears entry, so there is no charge to "
                    "remove from here; waive it in the arrears system and note it"
                ),
            }
        return {
            "applied": False,
            "components": components,
            "requires_out_of_band": sorted(components),
        }

    if points > 0:
        existing = await _prior_offer_credit(db, row)
        if existing is not None:
            # Refused rather than skipped: see `_prior_offer_credit`.
            return {
                "applied": False,
                "double_credit_refused": True,
                "components": {
                    "points": {
                        "offered": points,
                        "automatic": False,
                        "why": (
                            "a credit for this offer is already in the ledger "
                            f"(transaction {int(existing.id)}), so applying another "
                            "would overpay; this needs a human, not a second call"
                        ),
                    }
                },
                "requires_out_of_band": ["points"],
            }
        credited = await recovery_playbooks.credit_recovery_points(
            db,
            int(row.user_id),
            OFFER_POINTS_TYPE,
            points,
            reference=offer_credit_reference(row),
        )
        components["points"] = {
            "offered": points,
            "credited": round(float(credited.get("points_credited") or 0.0), 2),
            "wallet_balance": round(float(credited.get("new_balance") or 0.0), 2),
            "point_type": OFFER_POINTS_TYPE,
            "transaction_id": int(credited.get("transaction_id") or 0),
            "automatic": True,
        }
    else:
        components["points"] = {
            "offered": 0.0,
            "credited": 0.0,
            "automatic": True,
            "why": "this offer promises no points, so there is nothing to credit",
        }

    if discount > 0:
        components["discount_percent"] = {
            "offered": discount,
            "automatic": False,
            "why": (
                "no pricing engine in this service consumes discount_percent; it is "
                "recorded on the offer and the discount is applied where the next "
                "service is priced, if it is applied at all"
            ),
        }
        requires_out_of_band.append("discount_percent")

    return {
        "applied": any(bool(c.get("automatic")) and c.get("credited", 0.0) for c in components.values()),
        "credited_points": round(
            float(components.get("points", {}).get("credited") or 0.0), 2
        ),
        "components": components,
        "requires_out_of_band": requires_out_of_band,
    }


async def expire_offers_due(
    db: AsyncSession, *, now: Optional[datetime] = None, limit: int = 200
) -> dict[str, Any]:
    """Close offers whose own clock has run out. The background half of expiry.

    Claimed per row rather than assigned, and that is the whole reason this
    function is safe to run next to a customer tapping accept. This sweep
    selecting an offer is not a reservation: between the SELECT and the write the
    customer can accept it, and a sweep that then assigned ``expired`` would
    silently undo an acceptance the customer had just been told succeeded -- the
    worst possible answer to "did that work?". So each row is claimed on its
    stored status, and a row the customer beat to it is counted and named below
    rather than overwritten.
    """
    moment = _aware(now or _now())
    stmt = (
        select(models.CustomerOffer)
        .where(models.CustomerOffer.status == "offered")
        .where(models.CustomerOffer.expires_at.isnot(None))
        .where(models.CustomerOffer.expires_at <= moment)
        .limit(int(limit))
    )
    rows = list((await db.execute(stmt)).scalars().all())
    expired: list[str] = []
    beat_us: list[str] = []
    for row in rows:
        if await _claim_transition(db, row, "expired", now=moment):
            await _record_event(
                db,
                offer_id=int(row.id),
                user_id=int(row.user_id),
                kind="expired",
                from_status="offered",
                to_status="expired",
                actor="system:expiry",
                note="the offer's own validity window elapsed",
            )
            expired.append(row.reference)
        else:
            # Somebody acted on it between the sweep's SELECT and this write. Not
            # an error and not retried: reporting it is the useful part, because
            # "the sweep nearly expired an offer the customer had just accepted"
            # is a thing an operator wants to know happened.
            beat_us.append(row.reference)
    await db.flush()
    return {
        "expired": len(expired),
        "references": expired,
        "claimed_elsewhere": beat_us,
    }


async def follow_ups_due(
    db: AsyncSession, *, now: Optional[datetime] = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Accepted offers whose fulfilment is overdue -- the backlog surface."""
    moment = _aware(now or _now())
    stmt = (
        select(models.CustomerOffer)
        .where(models.CustomerOffer.status == "accepted")
        .where(models.CustomerOffer.follow_up_due_at.isnot(None))
        .where(models.CustomerOffer.follow_up_due_at <= moment)
        .limit(int(limit))
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [offer_summary(row, now=moment) for row in rows]


async def build_offer_admin_report(
    db: AsyncSession, *, window_days: int = 30, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Acceptance, decline and fulfilment rates, plus the backlog.

    Acceptance rate on its own is a trap: it rewards making fewer, better offers
    and is maximised by never offering anything. It is reported next to
    ``offered`` so the denominator is always visible, and next to the
    accept-to-fulfil gap so a high acceptance with a poor fulfilment rate cannot
    be read as a win.
    """
    moment = _aware(now or _now())
    since = moment - timedelta(days=int(window_days))
    stmt = select(models.CustomerOffer).where(models.CustomerOffer.issued_at >= since)
    rows = list((await db.execute(stmt)).scalars().all())
    by_status: dict[str, int] = {status: 0 for status in OFFER_STATUSES}
    by_kind: dict[str, dict[str, int]] = {
        kind: {status: 0 for status in OFFER_STATUSES} for kind in OFFER_KINDS
    }
    for row in rows:
        status = effective_status(row, now=moment)
        by_status[status] = by_status.get(status, 0) + 1
        by_kind.setdefault(str(row.offer_kind), {status: 0 for status in OFFER_STATUSES})
        by_kind[str(row.offer_kind)][status] += 1
    offered = sum(1 for row in rows if effective_status(row, now=moment) != "expired")
    accepted = by_status.get("accepted", 0) + by_status.get("fulfilled", 0)
    fulfilled = by_status.get("fulfilled", 0)
    declined_with_reason = [row for row in rows if str(row.decline_reason or "").strip()]
    backlog = [
        offer_summary(row, now=moment)
        for row in rows
        if effective_status(row, now=moment) == "accepted"
    ]
    return {
        "generated_at": moment.isoformat(),
        "window_days": int(window_days),
        "offered": len(rows),
        "offered_live": offered,
        "by_status": by_status,
        "by_kind": by_kind,
        "accepted": accepted,
        "declined": by_status.get("declined", 0),
        "expired": by_status.get("expired", 0),
        "fulfilled": fulfilled,
        "acceptance_rate": round(accepted / offered, 4) if offered else 0.0,
        "fulfilment_rate_of_accepted": (
            round(fulfilled / (accepted or 1), 4) if accepted else 0.0
        ),
        "declines_with_a_reason": len(declined_with_reason),
        "backlog": backlog,
        "backlog_count": len(backlog),
        "offer_statuses": list(OFFER_STATUSES),
        "offer_kinds": list(OFFER_KINDS),
        "note": (
            "acceptance_rate is reported beside offered on purpose: on its own it "
            "rewards making fewer offers and is maximised by never offering "
            "anything. Read it with fulfilment_rate_of_accepted, because a high "
            "acceptance with a poor fulfilment rate is a backlog, not a success"
        ),
    }


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def offer_summary(row: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """One offer as a plain dict, with its *effective* status."""
    status = effective_status(row, now=now)
    return {
        "reference": str(getattr(row, "reference", "") or ""),
        "offer_id": int(getattr(row, "id", 0) or 0),
        "user_id": int(getattr(row, "user_id", 0) or 0),
        "offer_kind": str(getattr(row, "offer_kind", "") or ""),
        "source_offer_id": str(getattr(row, "source_offer_id", "") or ""),
        "status": status,
        "stored_status": str(getattr(row, "status", "") or ""),
        "actionable": status in CUSTOMER_ACTIONABLE_STATUSES,
        "headline": str(getattr(row, "headline", "") or ""),
        "points": float(getattr(row, "points", 0.0) or 0.0),
        "discount_percent": float(getattr(row, "discount_percent", 0.0) or 0.0),
        "waiver_type": str(getattr(row, "waiver_type", "") or ""),
        "priority": str(getattr(row, "priority", "") or ""),
        "generosity_scale": float(getattr(row, "generosity_scale", 1.0) or 1.0),
        "expires_at": _iso(getattr(row, "expires_at", None)),
        "issued_at": _iso(getattr(row, "issued_at", None)),
        "accepted_at": _iso(getattr(row, "accepted_at", None)),
        "declined_at": _iso(getattr(row, "declined_at", None)),
        "fulfilled_at": _iso(getattr(row, "fulfilled_at", None)),
        "follow_up_due_at": _iso(getattr(row, "follow_up_due_at", None)),
    }


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _aware(value).isoformat()


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_offers() -> dict[str, Any]:
    """Check this subsystem's tables against each other and against the engines.

    The one error-class rule that matters here: every kind's ``resolver`` must
    name something that actually exists and is callable. A resolver that has
    been renamed is not a warning — it is an offer that raises at the moment it
    is issued, to a customer, from the recovery sweep.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for row in CUSTOMER_OFFER_KINDS:
        kind = str(row["offer_kind"])
        # The readable `resolver` is documentation; `resolver_path` is what gets
        # resolved. Reading the readable one is what found this very bug --
        # "recovery_playbooks.resolve_recovery_incentive" does not import, because
        # it is not a module path.
        module_name, _, attr = str(row.get("resolver_path") or "").partition(":")
        if not module_name or not attr:
            errors.append(
                f"CUSTOMER_OFFER_KINDS[{kind}].resolver_path is missing or not "
                "'module:callable'"
            )
            continue
        try:
            module = __import__(module_name, fromlist=[attr])
            resolver = getattr(module, attr)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"CUSTOMER_OFFER_KINDS[{kind}].resolver_path "
                f"{row['resolver_path']!r} does not resolve: {exc}"
            )
            continue
        if not callable(resolver):
            errors.append(f"{kind}: resolver {row['resolver_path']!r} is not callable")
        upstream = str(row["upstream"])
        if not hasattr(module, upstream):
            errors.append(
                f"{kind}: upstream table {upstream!r} is not declared in "
                f"{module_name}"
            )

    for status, targets in OFFER_TRANSITIONS.items():
        if status not in OFFER_STATUS_RANK:
            errors.append(f"OFFER_TRANSITIONS names unknown status {status!r}")
        for target in targets:
            if target not in OFFER_STATUS_RANK:
                errors.append(
                    f"OFFER_TRANSITIONS[{status}] names unknown target {target!r}"
                )
    for status in OFFER_STATUSES:
        if status not in OFFER_TRANSITIONS:
            errors.append(f"OFFER_STATUSES names {status!r}, which OFFER_TRANSITIONS omits")
        if OFFER_STATUSES.count(status) != 1:
            errors.append(f"OFFER_STATUSES repeats {status!r}")

    for kind in OFFER_EXPLANATIONS:
        if kind not in OFFER_KIND_BY_NAME:
            errors.append(f"OFFER_EXPLANATIONS names unknown kind {kind!r}")
    for kind in OFFER_KINDS:
        if kind not in OFFER_EXPLANATIONS:
            errors.append(
                f"{kind} has no customer-facing explanation; an offer a customer "
                "cannot understand is an offer they will decline"
            )
    for kind, template in OFFER_EXPLANATIONS.items():
        for field in ("what", "why", "legal"):
            if not str(template.get(field) or "").strip():
                errors.append(f"OFFER_EXPLANATIONS[{kind}].{field} is empty")

    # Two structural invariants about the concurrency fix, checked against the
    # *parsed call* rather than the source text.
    #
    # **Why not a substring.** The first version of this check read each mutator's
    # source and looked for `_claim_transition` in it. Its own negative control --
    # revert one mutator, revalidate, expect an error -- reported `valid: True`,
    # because `record_outcome` mentions the name in a comment and still called it.
    # A check that a comment can satisfy is not a check. The same lesson then
    # recurred in the test suite's own lock assertion, which grepped for
    # `with_for_update` and matched the docstring explaining why this path does
    # *not* lock.
    #
    # **Why structural at all.** A behavioural check cannot distinguish "claims its
    # transition" from "gets away with it this time", which is exactly why the
    # defect this guards survived: every test in this module passed while the guard
    # was absent. What *can* be asserted without a race is that the guard is
    # reachable from every mutator, and that is a property of the call tree.
    #
    # **Why "before the write" is not asserted here.** "Calls it" and "calls it
    # before writing" are different claims, and the ordering one needs the race
    # tests. `tests/test_e2e_adversarial.py` owns whether it happens early enough;
    # this owns whether it happens at all.
    #
    # It lives here rather than only in the suite because a module that can
    # validate its own integrity should, and `validate_offers()` is what ships in
    # the kaizen sweep.
    import ast as _ast
    import inspect as _inspect
    import textwrap as _textwrap

    def _parsed(fn: Any) -> Optional[Any]:
        """The function's own AST, or ``None`` if its source is unavailable.

        ``None`` means "cannot check", which is skipped rather than failed. A
        validator that raises because it cannot read itself is worse than one
        that says nothing, because it takes the whole sweep down with it.

        Real causes of unavailability: a wrapper produced by a decorator whose
        code object names a different file, an extension function, a source-less
        install. Two things that *look* like causes but are not, both learned by
        writing the control for this the wrong way first:

        * A one-line stub body is readable source, and it genuinely contains no
          claim -- so diagnosing it as unguarded is correct, not a skip.
        * `inspect.getsource` resolves through the code object's ``co_filename``,
          so making the module file unreadable hides *every* function in it. The
          skip is therefore per-function only if the unavailability is.
        """
        try:
            return _ast.parse(_textwrap.dedent(_inspect.getsource(fn)))
        except (OSError, TypeError, IndentationError, SyntaxError):
            return None

    def _claim_calls(fn: Any) -> list[Any]:
        """Every ``_claim_transition(...)`` call in the function's own body.

        "Own body" means: the statements of the function, excluding any ``def``
        nested inside it. A nested helper that reaches the claim does not count,
        because the offer's state change has to be claimed in the function that
        *decides* it -- that is where the caller's belief about the row already
        lives, and a helper would be reaching it without it.

        Walking this correctly needed two attempts, and both failures were silent
        (the check reported every mutator unguarded, or reported a reverted one as
        guarded). ``TestTheStructuralChecksCanFail`` in
        ``tests/test_customer_offers_stage_b.py`` plants both regressions and pins
        them, because a control that only proves the check passes cannot tell a
        vacuous check from a working one:

        * ``inspect.getsource(fn)`` returns the whole definition, so ``tree.body``
          is a single ``AsyncFunctionDef`` and *its* statements are what to walk.
          Walking ``tree.body`` itself finds nothing -- the node being defined
          contains no calls of its own.
        * Skipping ``AsyncFunctionDef`` while iterating ``tree.body`` skips the
          entire function, because the function *is* one. Only a ``def`` nested
          *within* it is excluded.
        """
        tree = _parsed(fn)
        if tree is None:
            return []
        found: list[Any] = []
        for definition in tree.body:
            if not isinstance(
                definition, (_ast.FunctionDef, _ast.AsyncFunctionDef)
            ):  # pragma: no cover - getsource always yields the definition
                continue
            for statement in definition.body:
                if isinstance(statement, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    continue  # a nested helper; its calls are its own business
                found.extend(
                    inner
                    for inner in _ast.walk(statement)
                    if (
                        isinstance(inner, _ast.Call)
                        and isinstance(inner.func, _ast.Name)
                        and inner.func.id == "_claim_transition"
                    )
                )
        return found

    for _mutator in (accept_offer, decline_offer, record_outcome, expire_offers_due):
        # An unparseable function returns no calls and no error, because there is
        # nothing to complain about that we know. Saying "cannot check" here would
        # be worse than silence, so the unmeasurable case is the quiet one and only
        # a *parsed* function with no claim is an error.
        if _parsed(_mutator) is not None and not _claim_calls(_mutator):
            errors.append(
                f"{_mutator.__name__} does not call _claim_transition; an "
                "unguarded mutator re-opens the concurrent-decision defect, and "
                "every test in this module passes while it is unguarded"
            )

    # `require_unexpired` is opt-in for a reason: `expire_offers_due` selects *on*
    # expiry, so demanding "unexpired" of it would refuse every row it selected.
    # Getting that backwards fails closed and silently expires nothing, which is
    # the kind of bug that only shows up as an offer that never closes -- so it is
    # asserted here, on the keyword argument, for the same reason as above: the
    # flag has to actually be passed, not merely be mentioned.
    def _has_unexpired_claim(fn: Any) -> Optional[bool]:
        """Does any claim pass ``require_unexpired=True``? ``None`` = unknown."""
        calls = _claim_calls(fn)
        if _parsed(fn) is None:
            return None
        if not calls:
            return False
        return any(
            kw.arg == "require_unexpired"
            and isinstance(kw.value, _ast.Constant)
            and kw.value.value is True
            for call in calls
            for kw in call.keywords
        )

    for _needs_clock in (accept_offer, decline_offer):
        if _has_unexpired_claim(_needs_clock) is False:
            errors.append(
                f"{_needs_clock.__name__} is a customer action but does not require "
                "an unexpired offer; the clock check would be a read another request "
                "can invalidate before the write"
            )
    if _has_unexpired_claim(expire_offers_due) is True:
        errors.append(
            "expire_offers_due requires an unexpired offer, but it selects on "
            "expiry; this would expire nothing"
        )

    # The two existing PRIORITY_RANK copies. This module deliberately added no
    # third, but the two it inherited are still independent and can drift.
    try:
        from app.services import points_exchange

        if dict(points_exchange.PRIORITY_RANK) != dict(arrears_payments.PRIORITY_RANK):
            warnings.append(
                "points_exchange.PRIORITY_RANK and arrears_payments.PRIORITY_RANK "
                "have drifted apart; both are high/medium/low rule-evaluation "
                "orders and neither is the queue priority this subsystem reads "
                "from RECOVERY_REVIEW_RULES"
            )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not compare the PRIORITY_RANK copies: {exc}")

    for rule in OFFER_GENEROSITY_RULES:
        try:
            scale = float(rule["scale"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"OFFER_GENEROSITY_RULES[{rule.get('rule_id')}] has no numeric scale")
            continue
        low, high = GENEROSITY_SCALE_BOUNDS
        if not low <= scale <= high:
            errors.append(
                f"OFFER_GENEROSITY_RULES[{rule.get('rule_id')}] scale {scale} is "
                f"outside the declared bounds {low}..{high}"
            )

    for kind in OFFER_KINDS:
        spec = OFFER_KIND_BY_NAME[kind]
        purpose = str(spec["purpose"])
        if purpose not in preferences.CONSENT_PURPOSE_BY_NAME:
            warnings.append(
                f"{kind}: purpose {purpose!r} is not in "
                "preferences.CONSENT_PURPOSE_BY_NAME, so the consent gate cannot "
                "report whether it applied"
            )
        if purpose in preferences.CONSENT_GATED_PURPOSES:
            # Not an error: a future kind may legitimately be a campaign. But it
            # is worth saying out loud, because it is the one choice here that
            # could withhold a fix.
            warnings.append(
                f"{kind}: purpose {purpose!r} IS consent-gated, so an absent "
                "consent would stop this offer being created at all"
            )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "kinds": len(OFFER_KINDS),
        "statuses": len(OFFER_STATUSES),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(OFFER_KINDS)} offer kinds and {len(OFFER_STATUSES)} statuses"
        ),
    }


def build_offer_catalog() -> dict[str, Any]:
    """The tables, published, so an operator can read the policy itself."""
    return {
        "catalog_version": OFFERS_CATALOG_VERSION,
        "offer_kinds": [dict(row) for row in CUSTOMER_OFFER_KINDS],
        "offer_statuses": list(OFFER_STATUSES),
        "offer_event_kinds": list(OFFER_EVENT_KINDS),
        "transitions": {key: list(value) for key, value in OFFER_TRANSITIONS.items()},
        "customer_actionable_statuses": sorted(CUSTOMER_ACTIONABLE_STATUSES),
        "terminal_statuses": sorted(TERMINAL_STATUSES),
        "generosity_rules": [dict(row) for row in OFFER_GENEROSITY_RULES],
        "generosity_bounds": list(GENEROSITY_SCALE_BOUNDS),
        "max_generosity_percent": OFFER_MAX_GENEROSITY_PERCENT,
        "explanations": {key: dict(value) for key, value in OFFER_EXPLANATIONS.items()},
        "note": (
            "no offer catalogue is declared here: each kind names the upstream "
            "table it reads, so adding a recovery incentive or a waiver type is a "
            "config edit in the engine that owns it, not a migration. the gate is "
            "asked of preferences.effective_contact_plan rather than "
            "reimplemented, and it governs contact rather than existence -- a "
            "reactive-only customer still receives the offer"
        ),
    }
