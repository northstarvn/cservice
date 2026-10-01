"""The agent copilot: 360 + plain-language why + next best action (Stage C).

An agent about to speak to a customer has three things to want, and this assembles
them in the order they are actually needed:

1. **What is true right now** — read from ``customer_360``, not re-derived. The
   360 is already an aggregator over eight engines; a copilot that rebuilt any of
   it would be a second source of truth that drifts.
2. **Why it is true, in words a customer could hear** — ``customer_explain``
   again, for the same reason.
3. **What to do next** — ``loyalty_journey`` for the scenario plan, and the
   offer surface for anything that should be offered rather than said.

The one thing it adds
---------------------
**An ordering, and an explicit "do not lead with this".** Every one of those
inputs already existed and each is reachable over HTTP. What did not exist was a
view that says *start here* and, more importantly, one that says **which of these
you must not open with.**

That is the copilot's actual contribution, and it is worth stating why. The most
damaging thing an agent can say to someone who has just reported a problem is the
churn score. It is not wrong, it is just true in a way that accuses them of being
a problem. ``COPILOT_DO_NOT_LEAD_WITH`` exists so that constraint is machine
readable, and :func:`build_copilot_card` refuses to put one of those facts in the
headline slot no matter what order the inputs arrive in.

It is also an **assistance tool, not an autopilot.** Nothing here writes, sends,
or commits. Every ``say`` is a suggestion with the reason attached, and
``requires_human: True`` is on every card by default — a customer-facing
statement written by a rule engine is a promise the system cannot keep.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

COPILOT_CATALOG_VERSION = 1

#: Facts that must never be the opening line of a customer conversation.
#:
#: Declared as data rather than as prose in a prompt, because "the copilot should
#: be sensitive" is not something code can be tested against and this can be.
#: Each row says what to say instead, which is the part that is actually useful.
COPILOT_DO_NOT_LEAD_WITH: tuple[dict[str, Any], ...] = (
    {
        "fact_id": "churn_score",
        "label": "Their churn score",
        "why": (
            "true, and an accusation. a customer who has just told us something "
            "is wrong does not need to be told what our model thinks of them, and "
            "opening with it converts a service conversation into a dispute about "
            "the score"
        ),
        "say_instead": (
            "'you reported this on the 3rd and we have had it since -- here is "
            "exactly where it is'"
        ),
    },
    {
        "fact_id": "access_band",
        "label": "Their access band",
        "why": (
            "it is an internal entitlements label. a customer asking for help does "
            "not know they are in a band, and being told they are is an "
            "invitation to argue with the scoring"
        ),
        "say_instead": "'here is what I can do for you right now'",
    },
    {
        "fact_id": "policy_tier",
        "label": "Their policy tier",
        "why": (
            "administrative, not personal. it describes who is allowed to approve "
            "things inside the business, which is not a thing to say to somebody "
            "asking about their booking"
        ),
        "say_instead": "'I can arrange that' -- and escalate internally if you cannot",
    },
    {
        "fact_id": "investment_band",
        "label": "How much they are worth to us",
        "why": (
            "the single most indefensible thing to say out loud. it exists to size "
            "a credit, and saying it would be accurate and monstrous"
        ),
        "say_instead": "nothing -- this is an internal sizing signal and never appears to a customer",
    },
)
COPILOT_DO_NOT_LEAD_WITH_BY_ID: dict[str, dict[str, Any]] = {
    str(row["fact_id"]): dict(row) for row in COPILOT_DO_NOT_LEAD_WITH
}

#: How confident the copilot is that it is working from enough to be useful.
#: Below ``partial`` the card says so rather than implying a whole picture.
COPILOT_CONFIDENCE_FLOORS: dict[str, float] = {"full": 0.99, "partial": 0.60}

#: The order the card presents things in. Named, because the whole contribution
#: is an ordering and an ordering hidden in a function body is not reviewable.
COPILOT_SECTION_ORDER: tuple[str, ...] = (
    "opening",
    "what_is_true",
    "why_in_plain_language",
    "next_best_action",
    "offers",
    "do_not_say",
)


def _now() -> Optional[datetime]:
    return datetime.now(timezone.utc)


def _get(source: Any, key: str, default: Any = None) -> Any:
    """Read a key from a mapping, an ORM object, or a pydantic model."""
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    if hasattr(source, "model_dump"):
        try:
            return source.model_dump().get(key, default)
        except Exception:  # noqa: BLE001
            pass
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
            pass
    return {
        name: getattr(source, name)
        for name in dir(source)
        if not name.startswith("_") and not callable(getattr(source, name, None))
    }


def customer_facing_text(card: Mapping[str, Any]) -> str:
    """Everything on this card an agent might read aloud.

    **Not just the opening.** The first version of the leak detector scanned only
    ``opening`` and ``headline``, and it passed a card whose ``what_is_true``
    facts said "their churn score is 0.82 and access band is elite" -- which is
    the realistic path, because ``customer_360.summary_text`` is a summary of the
    customer's own record and it will happily contain our internal labels.

    So the scan covers the opening *and* every fact the card offers, which is
    also the honest reading of "do not lead with": a fact an agent can read is a
    fact an agent will say.
    """
    parts = [str(card.get("opening") or ""), str(card.get("headline") or "")]
    truth = card.get("what_is_true") or {}
    for fact in (truth.get("facts") or []) if isinstance(truth, Mapping) else ():
        if isinstance(fact, Mapping):
            parts.append(str(fact.get("fact") or ""))
    return " ".join(parts).lower()


def detect_internal_only_facts(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Which internal-only facts are present in this card.

    Reports rather than silently stripping. Hiding them would make the copilot
    look as though it simply does not know them, and an agent who cannot see that
    a field exists cannot reason about where else it might leak.
    """
    haystack = customer_facing_text(card)
    present: list[dict[str, Any]] = []
    for fact_id, row in COPILOT_DO_NOT_LEAD_WITH_BY_ID.items():
        for needle in _fact_needles(fact_id):
            if needle in haystack:
                present.append({**row, "found": needle})
                break
    return present


def _fact_needles(fact_id: str) -> tuple[str, ...]:
    return {
        "churn_score": ("churn score", "churn_score", "churn risk of"),
        "access_band": ("access band", "access_band"),
        "policy_tier": ("policy tier", "policy_tier"),
        "investment_band": ("investment band", "investment_band", "lifetime value", "ltv"),
    }.get(fact_id, ())


def build_copilot_card(
    *,
    customer_360: Any = None,
    explanations: Optional[Mapping[str, Any]] = None,
    journey_plan: Any = None,
    health: Optional[Mapping[str, Any]] = None,
    status: Optional[Mapping[str, Any]] = None,
    offers: Sequence[Mapping[str, Any]] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One agent's view: what is true, why, what to say, and what not to open with.

    Takes the existing surfaces as arguments rather than calling them, so the
    copilot is a **composition** and not a second implementation: the 360 is
    built by ``customer_360``, the explanations by ``customer_explain``, the plan
    by ``loyalty_journey``. A copilot that re-derived any of them would be a third
    answer to a question the codebase already answers twice, and the third would
    be the one an agent believed.

    Every suggestion is text plus a reason. There is no score to optimise and no
    action to take: ``requires_human`` is ``True`` on every card because a
    customer-facing statement written by a rule engine is a promise this system
    cannot keep.
    """
    moment = now or _now()
    view = _as_dict(customer_360)
    health_map = dict(health or {})
    status_map = dict(status or {})
    journey = _as_dict(journey_plan)
    explanations_map = dict(explanations or {})

    confidence, confidence_note = _confidence(view, health_map, journey)

    card: dict[str, Any] = {
        "generated_at": moment.isoformat(),
        "confidence": confidence,
        "confidence_note": confidence_note,
        "requires_human": True,
        "is_autopilot": False,
        "user_id": int(view.get("user_id") or 0) or None,
        "opening": "",
        "what_is_true": _what_is_true(view, health_map, status_map),
        "why_in_plain_language": _why(explanations_map),
        "next_best_action": _next_action(journey, health_map),
        "offers": _offer_lines(offers),
        "do_not_say": [dict(row) for row in COPILOT_DO_NOT_LEAD_WITH],
        "sources": {
            "customer_360": bool(view),
            "explanations": bool(explanations_map),
            "journey_plan": bool(journey),
            "relationship_health": bool(health_map),
            "loyalty_status": bool(status_map),
            "offers": len(list(offers or ())),
        },
        "section_order": list(COPILOT_SECTION_ORDER),
    }
    card["opening"] = _opening(health_map, status_map, offers)
    leaks = detect_internal_only_facts(card)
    card["internal_only_facts_present"] = leaks
    if leaks:
        # The *opening* is recomputed and every leaked fact is flagged in place.
        # A caller may legitimately pass a fact-bearing 360 summary -- that is
        # what a summary is -- so the card is not rejected; instead the safe line
        # becomes the one it leads with and the offending facts are marked so the
        # agent sees them as read-only background rather than as something to say.
        card["opening"] = _opening(health_map, status_map, offers, safe_only=True)
        card["opening_rewritten"] = True
        _mark_leaked_facts(card, leaks)
    return card


def _mark_leaked_facts(
    card: dict[str, Any], leaks: Sequence[Mapping[str, Any]]
) -> None:
    """Flag the specific facts that carry a protected term."""
    protected = {str(row["fact_id"]) for row in leaks}
    needles = {
        str(row["fact_id"]): _fact_needles(str(row["fact_id"])) for row in leaks
    }
    for fact in (card.get("what_is_true") or {}).get("facts") or []:
        if not isinstance(fact, Mapping):
            continue
        text = str(fact.get("fact") or "").lower()
        hits = [
            fact_id
            for fact_id in protected
            if any(needle in text for needle in needles.get(fact_id, ()))
        ]
        if hits:
            fact["internal_only"] = True
            fact["protected_facts"] = hits
            fact["say_instead"] = "do not read this line to the customer"
            fact["note"] = (
                "this line carries an internal-only fact. it is kept because an "
                "agent who cannot see the field cannot reason about where else it "
                "leaks, but it is background, not script"
            )


def _confidence(
    view: Mapping[str, Any], health: Mapping[str, Any], journey: Mapping[str, Any]
) -> tuple[str, str]:
    """How much of the picture is here.

    Reports its own coverage rather than letting a card with one section read as
    a whole picture. A copilot that says "I don't know" is more useful than one
    that fills the gap with a confident sentence about a customer it has not read.
    """
    have = [bool(view), bool(health), bool(journey)]
    if all(have):
        return "full", "the 360, the health band and the journey plan are all present"
    if any(have):
        missing = [
            name
            for name, present in (
                ("customer_360", bool(view)),
                ("relationship_health", bool(health)),
                ("journey_plan", bool(journey)),
            )
            if not present
        ]
        return "partial", (
            "built without " + ", ".join(missing) + "; treat the next-best-action "
            "as a suggestion rather than a plan"
        )
    return (
        "insufficient",
        "no 360, no health and no journey plan: this card has nothing to base an "
        "opening on, which is itself the finding",
    )


def _what_is_true(
    view: Mapping[str, Any], health: Mapping[str, Any], status: Mapping[str, Any]
) -> dict[str, Any]:
    """Facts, not interpretation. Each names where it came from."""
    facts: list[dict[str, Any]] = []
    summary = str(view.get("summary_text") or "").strip()
    if summary:
        facts.append(
            {
                "fact": summary,
                "source": "customer_360.summary_text",
                "internal_only": False,
            }
        )
    unavailable = view.get("unavailable_sections") or {}
    if unavailable:
        facts.append(
            {
                "fact": (
                    "could not build: "
                    + ", ".join(f"{name} ({reason})" for name, reason in dict(unavailable).items())
                ),
                "source": "customer_360.unavailable_sections",
                "internal_only": False,
                "note": (
                    "reported rather than omitted: an agent told what is missing "
                    "will ask for it, and one told nothing will assume it is fine"
                ),
            }
        )
    if health:
        facts.append(
            {
                "fact": (
                    f"relationship health is {health.get('band')} "
                    f"({health.get('score')})"
                ),
                "source": "relationship_health",
                "internal_only": False,
                "note": (
                    "health, not status -- allowed to be poor while their status is "
                    "high, and that combination is the one worth acting on"
                ),
            }
        )
    if status:
        facts.append(
            {
                "fact": f"their status is {status.get('label')}",
                "source": "loyalty_status",
                "internal_only": False,
            }
        )
    return {"facts": facts, "count": len(facts)}


def _why(explanations: Mapping[str, Any]) -> dict[str, Any]:
    """Plain-language reasoning, passed through from ``customer_explain``.

    Not rewritten here. That module already decides what to disclose and what a
    customer can hear, and a copilot that reworded it would be the third thing
    deciding what a customer is told about their own case.
    """
    if not explanations:
        return {
            "available": False,
            "lines": [],
            "note": (
                "no explanations were supplied. customer_explain produces these; "
                "the copilot assembles them and does not write its own, because "
                "that module already decides what may be disclosed"
            ),
        }
    lines: list[str] = []
    for key, value in explanations.items():
        text = _explanation_text(value)
        if text:
            lines.append(f"{key.replace('_', ' ')}: {text}")
    return {"available": bool(lines), "lines": lines, "source": "customer_explain"}


def _explanation_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    for key in ("plain_language", "explanation", "text", "summary", "why"):
        text = _get(value, key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _next_action(journey: Mapping[str, Any], health: Mapping[str, Any]) -> dict[str, Any]:
    """What to do next, and whether the health band overrides the plan.

    The override is the part worth writing down. ``loyalty_journey`` picks a
    scenario from the relationship's shape, which is right in the ordinary case.
    But a customer at ``critical`` needs a person regardless of which onboarding
    or activation scenario matched, and letting a plan argue them out of it would
    be the worst version of this feature -- a correct-looking recommendation to
    upsell somebody who has an open complaint.
    """
    actions = [
        str(_get(item, "action") or _get(item, "description") or "")
        for item in (journey.get("next_best_actions") or journey.get("actions") or [])
    ]
    actions = [item for item in actions if item]
    band = str(health.get("band") or "")
    declared = health.get("sla_hours")
    override = None
    if band == "critical":
        override = {
            "reason": (
                "critical health overrides the journey plan: an open complaint "
                "plus repeated chasing is two systems failing one customer, and a "
                "matched onboarding or activation scenario must not be allowed to "
                "argue them out of a human"
            ),
            "do": (
                "put the customer in front of somebody today"
                + (f" (SLA {declared}h)" if declared else "")
                + ", acknowledge what they have already had to chase, and do not "
                "open with a scenario or a score"
            ),
        }
    elif band == "at_risk":
        override = {
            "reason": (
                "at-risk health adds an offer and a follow-up to whatever the plan "
                "says; the plan is still the right shape, it just is not enough on "
                "its own here"
            ),
            "do": (
                "run the plan's action, and put a goodwill offer in front of them "
                "before the next contact"
            ),
        }
    return {
        "plan_actions": actions,
        "plan_source": "loyalty_journey" if journey else None,
        "health_override": override,
        "recommended": (
            [override["do"]] + actions
            if override
            else actions
        ),
        "note": (
            "suggestions with reasons, never actions. nothing in this module "
            "writes, sends or commits, and requires_human is True on every card"
        ),
    }


def _offer_lines(offers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Offers worth mentioning, actionable ones only.

    A fulfilled or expired offer in an agent's script is noise, and an agent
    reading one may well improvise. Filtered on the *effective* status the
    offer surface publishes rather than a status re-derived here.
    """
    lines: list[dict[str, Any]] = []
    for offer in offers or ():
        if not bool(_get(offer, "actionable", False)):
            continue
        lines.append(
            {
                "reference": str(_get(offer, "reference") or ""),
                "kind": str(_get(offer, "offer_kind") or ""),
                "headline": str(_get(offer, "headline") or ""),
                "say": (
                    "we have something for you that you can accept in one tap, "
                    "and you can ignore it without telling us why"
                ),
                "source": "customer_offers",
            }
        )
    return lines


def _opening(
    health: Mapping[str, Any],
    status: Mapping[str, Any],
    offers: Sequence[Mapping[str, Any]],
    *,
    safe_only: bool = False,
) -> str:
    """The first sentence. Never an internal fact.

    ``safe_only`` is what :func:`build_copilot_card` uses when it detects a leak:
    the opening is recomputed from the band alone. That is why the leak is
    corrected rather than merely reported -- an agent reading a card that *says*
    not to say something while saying it is being asked to choose, and the safe
    line should not be the one requiring a choice.
    """
    band = str(health.get("band") or "")
    actionable = sum(
        1 for offer in (offers or ()) if bool(_get(offer, "actionable", False))
    )
    if band == "critical":
        return (
            "I can see the problem you reported and I have it in front of me now. "
            "Let me get you the person who can actually fix it rather than "
            "explaining it again."
        )
    if band == "at_risk" and actionable:
        return (
            "I have got something for you that takes one tap and you can ignore "
            "it. Before that, has the thing that went wrong actually been sorted?"
        )
    if band in ("at_risk", "strained"):
        return "I have got a moment to look at this properly. What has happened since we last spoke?"
    if status and safe_only:
        return "Thanks for getting in touch -- tell me what you need and I will see what I can do."
    if band == "healthy" or status:
        return "What can I help you with today?"
    return (
        "Tell me what has happened and I will look at your history before I "
        "suggest anything."
    )


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_copilot() -> dict[str, Any]:
    """Check the copilot's own rules.

    The check that matters: **every internal-only fact must carry a replacement
    line.** A prohibition without a substitute is a dead end for the agent
    reading it, and a rule they work around is worse than no rule.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for row in COPILOT_DO_NOT_LEAD_WITH:
        fact_id = str(row["fact_id"])
        if not str(row.get("why") or "").strip():
            errors.append(
                f"COPILOT_DO_NOT_LEAD_WITH[{fact_id}] has no `why`; a prohibition "
                "nobody can reason about is one they will work around"
            )
        if not str(row.get("say_instead") or "").strip():
            errors.append(
                f"COPILOT_DO_NOT_LEAD_WITH[{fact_id}] has no `say_instead`; a "
                "prohibition with no substitute sends the agent looking for one"
            )
        if not _fact_needles(fact_id):
            errors.append(
                f"COPILOT_DO_NOT_LEAD_WITH[{fact_id}] has no detection needles, so "
                "detect_internal_only_facts can never find it"
            )

    if "investment_band" not in COPILOT_DO_NOT_LEAD_WITH_BY_ID:
        errors.append(
            "the investment band must be on the do-not-lead-with list: it exists "
            "to size a credit and saying it out loud would be accurate and monstrous"
        )

    floors = dict(COPILOT_CONFIDENCE_FLOORS)
    if sorted(floors.values(), reverse=True) != list(floors.values()):
        errors.append("COPILOT_CONFIDENCE_FLOORS are not descending")

    for section in ("opening", "what_is_true", "why_in_plain_language", "next_best_action"):
        if section not in COPILOT_SECTION_ORDER:
            errors.append(f"COPILOT_SECTION_ORDER omits {section!r}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "facts_protected": len(COPILOT_DO_NOT_LEAD_WITH),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(COPILOT_DO_NOT_LEAD_WITH)} protected facts"
        ),
    }


def build_copilot_catalog() -> dict[str, Any]:
    """The tables, published, so the policy is reviewable without reading code."""
    return {
        "catalog_version": COPILOT_CATALOG_VERSION,
        "do_not_lead_with": [dict(row) for row in COPILOT_DO_NOT_LEAD_WITH],
        "confidence_floors": dict(COPILOT_CONFIDENCE_FLOORS),
        "section_order": list(COPILOT_SECTION_ORDER),
        "note": (
            "the copilot's contribution is an ordering and a prohibition, not new "
            "knowledge: the 360, the explanations and the journey plan already "
            "existed and each is reachable over HTTP. it composes them and never "
            "re-derives one, because a third answer to a question the codebase "
            "already answers twice is the one an agent would believe. nothing here "
            "writes, sends or commits -- every card is requires_human"
        ),
    }