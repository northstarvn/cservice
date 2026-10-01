"""The journey orchestrator: at-risk → offer → follow-up → status, as one loop (Stage C).

`services/loyalty_journey.py` is a **next-best-action engine**. It picks the
scenario that fits a relationship's shape — onboarding, activation, recovery,
retention — and returns the actions that fit that scenario. That is genuinely
good and it is already reachable.

What it does not do is *carry anything forward*. It is a function of the present:
run it twice on the same customer and it returns the same plan, because it has no
memory of what was already done. Which produces the specific failure this module
exists to fix —

> we recognise an at-risk customer, we offer them something, they accept, and at
> the next check they are recognised as at-risk *again*, so we offer them
> something *again*.

That loop is worse than having no orchestration, because each pass looks correct in
isolation and the repetition is only visible across passes. So this module adds
the missing half: a **state**, the evidence that justifies a transition out of it,
and a rule that a step may not repeat until its own precondition changes.

Four stages, and the loop is deliberate
----------------------------------------
``at_risk → offer → follow_up → status`` is a **cycle, not a staircase**. A
customer's health can fall back to ``at_risk`` at any point, and the orchestrator
treats that as a *re-entry* with a new attempt rather than a reset — which is why
``attempts`` is on the state and why the repeat rule compares the precondition
rather than the state.

The repeat rule
---------------
A step may not fire again while its **precondition is unchanged**, regardless of
how many times the customer has cycled. So a customer who declines a goodwill
offer three times is not offered a fourth until something about them changes —
a different offer kind, a different recovery context, a materially different
health band. Anything else is nagging, and nagging a customer who has said no
three times is how a goodwill programme becomes a reason to block a number.

That is also why ``state_hash`` includes the offer *kind* and the recovery context
and not merely the id: "we already offered this" is not the useful question, "we
already offered *this thing*, in *this situation*" is.

It composes; it does not re-decide
-----------------------------------
Eligibility and amount come from ``customer_offers`` and its upstream policy.
Generosity comes from ``loyalty_status``. The plan comes from ``loyalty_journey``.
The state machine lives here. Nothing here decides *whether* an offer is warranted
— that would be a fourth answer to a question the codebase already answers once,
in the one place that owns it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

ORCHESTRATOR_CATALOG_VERSION = 1

#: The cycle. ``at_risk`` is both the entry point and the re-entry point, which is
#: why this is a cycle rather than a list.
ORCHESTRATION_STAGES: tuple[str, ...] = ("at_risk", "offer", "follow_up", "status")
STAGE_RANK: dict[str, int] = {name: index for index, name in enumerate(ORCHESTRATION_STAGES)}

#: The transitions, as data. Fail closed: a transition not listed is refused, and
#: an unknown stage is refused rather than guessed.
#:
#: ``status → at_risk`` is the only backward edge, and it is the one that makes the
#: cycle real. Health can fall, and the orchestrator has to have an answer for what
#: happens when it does.
ORCHESTRATION_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "at_risk": ("offer",),
    "offer": ("follow_up", "status"),
    "follow_up": ("status",),
    "status": ("at_risk",),
}

#: What each stage is *for*, so a reader does not have to infer it from the name.
ORCHESTRATION_STAGE_PURPOSE: dict[str, str] = {
    "at_risk": "recognise and decide, using the health band as evidence",
    "offer": "put something concrete in front of them, or record that nothing is warranted",
    "follow_up": "make sure an accepted thing actually happened, and chase it if not",
    "status": "report what actually happened, which is what the next pass reads",
}

#: How long each stage is allowed to sit before the orchestrator considers it
#: stuck. Not an SLA on a person -- the difference is spelled out in
#: ``relationship_health`` and it matters here too: these are clocks on our own
#: bookkeeping.
STAGE_TIMEBOX_HOURS: dict[str, int] = {
    "at_risk": 24,
    "offer": 72,
    "follow_up": 48,
    "status": 168,
}

#: The maximum times a customer may be offered *the same thing* in the same
#: situation. Three declined offers is a decision, not a gap in persistence.
MAX_SAME_OFFER_ATTEMPTS = 3


def _now() -> Optional[datetime]:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    import datetime as _dt

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=_dt.timezone.utc)
    return None


def precondition_hash(
    *,
    offer_kind: str = "",
    recovery_context: Optional[Mapping[str, Any]] = None,
    health_band: str = "",
    investment_band: str = "",
) -> str:
    """A fingerprint of *the situation*, not of the customer.

    The repeat rule compares this, so it must exclude anything that changes for
    reasons unrelated to whether the offer still makes sense — the offer id, the
    timestamp, the customer id. Including the customer id would make every
    customer's attempts independent (correct) while including the offer id would
    make a *new* offer of the same kind look like a fresh start (wrong: it is the
    same pitch).

    Recovery context is included wholesale rather than field-by-field because it
    is the upstream engine's vocabulary and this module does not own it; picking
    fields out of it here would be a second, drifting reading of the same dict.
    """
    payload = {
        "offer_kind": str(offer_kind or ""),
        "health_band": str(health_band or ""),
        "investment_band": str(investment_band or ""),
        "recovery_context": _jsonable(recovery_context or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)
    return value


def resolve_transition(current: str, target: str) -> dict[str, Any]:
    """Is ``current -> target`` a move this orchestrator makes?

    Fail closed, and never guess a stage. ``STAGE_RANK`` is used for ordering only
    -- it is deliberately *not* used to allow a forward skip, because skipping
    ``follow_up`` would record an accepted offer as handled without anyone checking
    it happened.
    """
    now = str(current)
    nxt = str(target)
    if now not in STAGE_RANK:
        return {
            "allowed": False,
            "unknown": True,
            "reason": f"unknown stage {current!r}; known: {', '.join(ORCHESTRATION_STAGES)}",
        }
    if nxt not in STAGE_RANK:
        return {
            "allowed": False,
            "unknown": True,
            "reason": f"unknown stage {target!r}; known: {', '.join(ORCHESTRATION_STAGES)}",
        }
    if nxt not in ORCHESTRATION_TRANSITIONS.get(now, ()):
        reachable = ORCHESTRATION_TRANSITIONS.get(now, ())
        return {
            "allowed": False,
            "unknown": False,
            "reason": (
                f"{now} -> {nxt} is not an edge; from {now} the reachable stages are "
                + (", ".join(reachable) if reachable else "none (it is a dead end)")
            ),
            "dead_end": not reachable,
        }
    return {"allowed": True, "unknown": False, "reason": f"{now} -> {nxt}"}


def initial_state(*, user_id: int, detected_at: Optional[datetime] = None) -> dict[str, Any]:
    """A fresh entry into the cycle, with the evidence that justified entering.

    Starts at ``at_risk`` rather than at an ``assess`` stage: recognition and
    decision are the same step here, because there is nothing to decide before
    knowing someone is at risk.
    """
    moment = _aware(detected_at) or _now()
    return {
        "user_id": int(user_id),
        "stage": "at_risk",
        "entered_stage_at": moment.isoformat(),
        "detected_at": moment.isoformat(),
        "health_band": "",
        "attempts": [],
        "attempt_count": 0,
        "cycles": 0,
        "current_offer_reference": "",
        "last_outcome": "",
        "history": [
            {
                "stage": "at_risk",
                "at": moment.isoformat(),
                "reason": "entered the orchestration cycle",
            }
        ],
    }


def stage_timebox(stage: str) -> Optional[int]:
    """Hours a stage may sit before it counts as stuck, or ``None`` if unbounded."""
    value = STAGE_TIMEBOX_HOURS.get(str(stage))
    return int(value) if value is not None else None


def is_stuck(state: Mapping[str, Any], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Has this stage been sitting too long?

    Stuck is not the same as failed. A stage that has been in ``offer`` for a week
    has a real explanation — the customer has not opened their inbox — and the
    right response is to say so, not to escalate. A stage that has been in
    ``at_risk`` for a day with no offer at all is a different problem: the
    orchestrator is not running.
    """
    moment = _aware(now) or _now()
    stage = str(state.get("stage") or "")
    entered = _aware(state.get("entered_stage_at"))
    limit = stage_timebox(stage)
    if entered is None or limit is None:
        return {"stuck": False, "hours": None, "timebox_hours": limit, "reason": ""}
    hours = (moment - entered).total_seconds() / 3600.0
    stuck = hours > limit
    return {
        "stuck": stuck,
        "hours": round(hours, 2),
        "timebox_hours": limit,
        "stage": stage,
        "reason": (
            f"{stage} has been open for {hours:.1f}h against a {limit}h timebox"
            if stuck
            else ""
        ),
        "note": (
            "stuck is a fact about our bookkeeping, not about the customer. an "
            "unopened offer is stuck *for us*; the customer is not at fault and the "
            "response is to report, not to escalate"
        ),
    }


def record_attempt(
    state: Mapping[str, Any],
    *,
    outcome: str,
    precondition: str = "",
    offer_reference: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Append an attempt, and report whether it was a repeat.

    **A repeat is judged on ``precondition`` and not on stage or count.** A
    customer who has cycled four times and is at risk again has not been offered
    anything new; what has changed is the clock. Comparing stages would let a
    re-entry silently reset the attempt history and permit a fourth identical
    offer, which is the loop this module exists to close.
    """
    moment = _aware(now) or _now()
    attempts = [dict(row) for row in (state.get("attempts") or ())]
    prior = sum(
        1
        for row in attempts
        if str(row.get("precondition") or "") == str(precondition or "")
    )
    result = dict(state)
    entry = {
        "at": moment.isoformat(),
        "outcome": str(outcome or ""),
        "precondition": str(precondition or ""),
        "offer_reference": str(offer_reference or ""),
        "attempt_number": len(attempts) + 1,
        "same_precondition_before": prior,
    }
    attempts.append(entry)
    result["attempts"] = attempts
    result["attempt_count"] = len(attempts)
    # Advisory only. `next_step` recomputes this for its own precondition, so this
    # field is a description of the last recorded attempt rather than a cache.
    result["same_precondition_note"] = (
        "counts the most recent record_attempt call's precondition; next_step "
        "recomputes the count for the situation it is actually being asked about"
    )
    # Counts *this* attempt, not only the ones before it. The first version
    # reported `prior`, which meant the count lagged by one and
    # `MAX_SAME_OFFER_ATTEMPTS = 3` permitted a **fourth** identical offer: the
    # off-by-one landed exactly where nagging a customer who has already said no
    # three times becomes a blocked number.
    result["same_precondition_attempts"] = prior + 1
    result["last_outcome"] = str(outcome or "")
    result["current_offer_reference"] = str(offer_reference or "")
    result["repeat_verdict"] = "repeat" if prior else "first"
    result["may_repeat"] = (prior + 1) < MAX_SAME_OFFER_ATTEMPTS
    return result


def advance(
    state: Mapping[str, Any],
    target: str,
    *,
    reason: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Move one stage, or refuse and say why.

    Never skips. The edges are the edges: ``offer → status`` exists for the case
    where nothing was warranted, and ``follow_up`` is not optional for something
    that was.
    """
    moment = _aware(now) or _now()
    move = resolve_transition(str(state.get("stage") or ""), str(target))
    if not move["allowed"]:
        return {
            "advanced": False,
            "stage": str(state.get("stage") or ""),
            "reason": move["reason"],
            "move": move,
        }
    result = dict(state)
    previous = str(state.get("stage") or "")
    cycles = int(state.get("cycles") or 0)
    if target == "at_risk" and previous == "status":
        # The only backward edge, and the one that makes this a cycle. Counted
        # explicitly because "how many times has this customer cycled" is the
        # question that tells you whether the programme is working or nagging.
        cycles += 1
    result["stage"] = str(target)
    result["entered_stage_at"] = moment.isoformat()
    result["cycles"] = cycles
    history = [dict(row) for row in (state.get("history") or ())]
    history.append(
        {
            "stage": str(target),
            "from": previous,
            "at": moment.isoformat(),
            "reason": str(reason or move["reason"]),
        }
    )
    result["history"] = history
    return {
        "advanced": True,
        "stage": str(target),
        "from": previous,
        "cycles": cycles,
        "reason": move["reason"],
        "state": result,
    }


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def next_step(
    *,
    state: Optional[Mapping[str, Any]] = None,
    health: Optional[Mapping[str, Any]] = None,
    journey_plan: Optional[Mapping[str, Any]] = None,
    offer_kind: str = "",
    recovery_context: Optional[Mapping[str, Any]] = None,
    investment_band: str = "",
    offer_warranted: bool = False,
    preferences_map: Optional[Mapping[str, Any]] = None,
    consents: Optional[Mapping[str, bool]] = None,
    last_contact_at: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """What the orchestrator should do next, and the evidence for it.

    Returns a *plan for the act*, never the act. Issuing an offer writes to the
    database and moving a customer between stages writes a state change; doing
    either from a function that also decides what to offer is how an engine ends
    up with two responsibilities and one of them untested.

    So this reads the health band and the journey plan, checks the repeat rule
    against ``state``, and returns the step with the precondition hash attached --
    which is the value the caller feeds back to :func:`record_attempt`. The
    ``precondition`` field is what makes the loop closable, and it is returned even
    when the answer is "nothing to do" so a caller recording a no-op still has one.
    """
    moment = _aware(now) or _now()
    health_map = dict(health or {})
    band = str(health_map.get("band") or "")
    precondition = precondition_hash(
        offer_kind=offer_kind,
        recovery_context=recovery_context,
        health_band=band,
        investment_band=investment_band,
    )
    purpose = "recovery" if band in ("critical", "at_risk") else "service"
    current_stage = str((state or {}).get("stage") or "at_risk")
    # Counted here rather than read off the state. The state's
    # `same_precondition_attempts` is written by `record_attempt` for *its* call's
    # precondition, so reading it here reported the count for a situation the
    # caller has since left -- and a changed situation stayed suppressed by a
    # decision made about the old one. The same class of bug as
    # `capability_audit` reading a derived index instead of the table.
    same_before = sum(
        1
        for row in ((state or {}).get("attempts") or ())
        if str(row.get("precondition") or "") == precondition
    )
    plan = _as_dict(journey_plan)
    actions = [
        str(_get(item, "action") or _get(item, "description") or "")
        for item in (plan.get("next_best_actions") or plan.get("actions") or [])
    ]
    actions = [item for item in actions if item]

    evidence: list[str] = []
    if band:
        evidence.append(f"relationship health is {band}")
    else:
        evidence.append("no health band was supplied, so nothing is being asserted")
    if band == "critical":
        evidence.append(
            "critical health is an offer *and* a person; the stage model alone "
            "cannot produce the person, so this is reported as a required action "
            "rather than an optional one"
        )
    if offer_warranted and not offer_kind:
        evidence.append(
            "an offer is warranted but no offer kind was named, so the step cannot "
            "be executed; this is refused rather than defaulted"
        )
    if same_before >= MAX_SAME_OFFER_ATTEMPTS:
        evidence.append(
            f"the same offer has already been made {same_before} times in this "
            "situation, so the repeat rule suppresses it. nagging a customer who "
            "has said no is how a goodwill programme becomes a reason to block a "
            "number"
        )

    suppressed = same_before >= MAX_SAME_OFFER_ATTEMPTS
    step, action = _choose_step(
        stage=current_stage,
        band=band,
        offer_warranted=offer_warranted,
        offer_kind=offer_kind,
        suppressed=suppressed,
        actions=actions,
    )
    from app.services import care_gate

    gate = care_gate.consult(
        "journey_next_action",
        preferences_map,
        consents,
        purpose=purpose,
        last_contact_at=last_contact_at,
    )
    evidence.extend(gate["reasons"])
    return {
        "stage": current_stage,
        "next_stage": step,
        "action": action,
        "push_permitted": bool(gate["push"]),
        "gate": gate,
        "actions": actions,
        "plan_source": "loyalty_journey" if plan else None,
        "health_band": band,
        "offer_kind": str(offer_kind or ""),
        "precondition": precondition,
        "suppressed_by_repeat_rule": suppressed,
        "same_precondition_attempts": same_before,
        "may_repeat": not suppressed,
        "evidence": evidence,
        "generated_at": moment.isoformat(),
        "executable": bool(step) and not suppressed and bool(
            offer_kind or current_stage != "offer"
        ),
        "note": (
            "this returns a plan for the act, never the act. issuing an offer and "
            "moving a stage both write; deciding what to do and doing it are two "
            "responsibilities and the second is the caller's"
        ),
    }


def _choose_step(
    *,
    stage: str,
    band: str,
    offer_warranted: bool,
    offer_kind: str,
    suppressed: bool,
    actions: Sequence[str],
) -> tuple[str, str]:
    """One stage forward, chosen by the evidence rather than by the clock."""
    if stage == "at_risk":
        if band == "critical":
            return (
                "offer",
                "put a human on this now, and an offer alongside it. the SLA is "
                "in the health band and the offer is a courtesy that costs nothing "
                "if the person solves it first",
            )
        if offer_warranted and offer_kind:
            if suppressed:
                return ("status", "record that we considered offering and chose not to repeat ourselves")
            return (
                "offer",
                f"issue a {offer_kind} offer. the upstream policy in customer_offers "
                "decides the amount; this only says that now is the moment",
            )
        if actions:
            return ("follow_up", f"no offer is warranted; work the plan instead: {actions[0]}")
        return ("status", "nothing is warranted and there is no plan; record the pass so it is visible")

    if stage == "offer":
        if suppressed:
            return ("status", "the offer has already been made in this situation; stop asking")
        return (
            "follow_up",
            "check whether the offer was accepted, declined, or is still open. "
            "accepting is not the same as fulfilling, and this is the stage that "
            "notices the difference",
        )

    if stage == "follow_up":
        return (
            "status",
            "make sure an accepted offer actually happened, and chase it if not. "
            "an accepted offer nobody fulfilled is the state this whole subsystem "
            "is most embarrassed by",
        )

    if stage == "status":
        if band in ("critical", "at_risk"):
            return (
                "at_risk",
                f"health is still {band}, so the cycle re-enters. a re-entry is a "
                "new attempt, not a reset -- the attempt history carries over and "
                "the repeat rule still applies",
            )
        return ("status", "health has recovered; close the cycle and leave the status reported")

    return ("", f"unknown stage {stage!r}")


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_dict(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    if isinstance(source, Mapping):
        return dict(source)
    if hasattr(source, "model_dump"):
        try:
            return dict(source.model_dump())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def summarise_cycle(state: Mapping[str, Any], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """One screen of a customer's position in the loop.

    ``cycles`` is published because it is the number that distinguishes a working
    programme from a nagging one: a customer who has cycled six times has been
    recognised as at risk six times and the stage model has not resolved anything
    about it.
    """
    moment = _aware(now) or _now()
    attempts = list(state.get("attempts") or ())
    by_outcome: dict[str, int] = {}
    for row in attempts:
        name = str(row.get("outcome") or "unknown")
        by_outcome[name] = by_outcome.get(name, 0) + 1
    stuck = is_stuck(state, now=moment)
    return {
        "user_id": int(state.get("user_id") or 0),
        "stage": str(state.get("stage") or ""),
        "stage_purpose": ORCHESTRATION_STAGE_PURPOSE.get(str(state.get("stage") or ""), ""),
        "cycles": int(state.get("cycles") or 0),
        "attempts": len(attempts),
        "attempts_by_outcome": by_outcome,
        "current_offer_reference": str(state.get("current_offer_reference") or ""),
        "last_outcome": str(state.get("last_outcome") or ""),
        "stuck": stuck["stuck"],
        "stuck_reason": stuck["reason"],
        "timebox_hours": stuck.get("timebox_hours"),
        "history_length": len(state.get("history") or ()),
        "note": (
            "cycles is published because it is the number that distinguishes a "
            "working programme from a nagging one: a customer who has cycled six "
            "times has been recognised as at risk six times and nothing has been "
            "resolved about it"
        ),
    }


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_orchestrator() -> dict[str, Any]:
    """Check the stage machine against itself.

    The check that matters: **every stage must be reachable and every stage must
    have a way out.** An unreachable stage is a place a customer can arrive and
    never leave; a stage with no outgoing edge is a dead end where a customer
    stops, which reads as "resolved" and is not.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for stage in ORCHESTRATION_STAGES:
        if stage not in ORCHESTRATION_TRANSITIONS:
            errors.append(f"ORCHESTRATION_TRANSITIONS omits stage {stage!r}")
        else:
            for target in ORCHESTRATION_TRANSITIONS[stage]:
                if target not in STAGE_RANK:
                    errors.append(
                        f"ORCHESTRATION_TRANSITIONS[{stage}] names unknown stage {target!r}"
                    )
        if not ORCHESTRATION_STAGE_PURPOSE.get(stage):
            warnings.append(
                f"ORCHESTRATION_STAGE_PURPOSE[{stage}] is empty; a stage whose "
                "purpose has to be inferred from its name is a name that will drift"
            )
        if stage_timebox(stage) is None:
            warnings.append(
                f"stage {stage!r} has no timebox, so a customer sitting in it is "
                "never reported as stuck"
            )

    incoming: dict[str, int] = {stage: 0 for stage in ORCHESTRATION_STAGES}
    outgoing: dict[str, int] = {stage: 0 for stage in ORCHESTRATION_STAGES}
    for stage, targets in ORCHESTRATION_TRANSITIONS.items():
        outgoing[stage] = len(targets)
        for target in targets:
            incoming[target] = incoming.get(target, 0) + 1
    for stage in ORCHESTRATION_STAGES:
        if incoming.get(stage, 0) == 0:
            errors.append(
                f"stage {stage!r} is unreachable: nothing transitions into it, so a "
                "customer can never arrive there"
            )
        if outgoing.get(stage, 0) == 0:
            errors.append(
                f"stage {stage!r} is a dead end: nothing transitions out of it, so a "
                "customer who arrives there stops, which reads as resolved"
            )

    if not any(
        target == "at_risk" for target in ORCHESTRATION_TRANSITIONS.get("status", ())
    ):
        errors.append(
            "the cycle has no backward edge: `status -> at_risk` is what lets a "
            "customer whose health fell be recognised again. Without it, "
            "orchestration is a staircase and a re-entry is impossible"
        )

    if MAX_SAME_OFFER_ATTEMPTS < 1:
        errors.append("MAX_SAME_OFFER_ATTEMPTS below 1 would suppress every offer")

    for band, hours in STAGE_TIMEBOX_HOURS.items():
        if band not in STAGE_RANK:
            errors.append(f"STAGE_TIMEBOX_HOURS names unknown stage {band!r}")
        elif int(hours) <= 0:
            errors.append(f"STAGE_TIMEBOX_HOURS[{band}] is not positive")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "stages": len(ORCHESTRATION_STAGES),
        "transitions": sum(len(v) for v in ORCHESTRATION_TRANSITIONS.values()),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(ORCHESTRATION_STAGES)} stages and "
            f"{sum(len(v) for v in ORCHESTRATION_TRANSITIONS.values())} transitions"
        ),
    }


def build_orchestrator_catalog() -> dict[str, Any]:
    """The tables, published, so the state machine is reviewable as data."""
    return {
        "catalog_version": ORCHESTRATOR_CATALOG_VERSION,
        "stages": list(ORCHESTRATION_STAGES),
        "stage_purpose": dict(ORCHESTRATION_STAGE_PURPOSE),
        "stage_timebox_hours": dict(STAGE_TIMEBOX_HOURS),
        "transitions": {key: list(value) for key, value in ORCHESTRATION_TRANSITIONS.items()},
        "max_same_offer_attempts": MAX_SAME_OFFER_ATTEMPTS,
        "note": (
            "at_risk -> offer -> follow_up -> status is a cycle, not a staircase: "
            "`status -> at_risk` is the only backward edge and it is what lets a "
            "customer whose health fell be recognised again. a re-entry is a new "
            "attempt, not a reset. the repeat rule compares a precondition hash of "
            "(offer kind, recovery context, health band, investment band) rather "
            "than the stage or the count, so cycling four times does not reset the "
            "attempt history and permit a fourth identical offer"
        ),
    }