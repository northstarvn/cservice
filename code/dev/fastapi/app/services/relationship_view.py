"""The unified relationship view, and the copilot as its default pane (Stage E).

What was missing
----------------
By the end of Stage D this codebase answered six separate questions about a
customer -- what happened (``customer_360``), what is wrong
(``relationship_health``), what they are worth to us (``loyalty_status``), what
we offered and what came back (``customer_offers``), what stage the recovery is at
(``journey_orchestrator``), and what an agent should say (``agent_copilot``). Each
answer was correct. There was **one place that showed all six together**, so an
agent opened a customer and saw the 360, which contains none of health, status,
offers or journey state, and quietly formed a view from what was there.

That is the failure this module addresses, and it is not "the 360 needs more
sections". Adding Stage B/C/D into ``CUSTOMER_360_SECTIONS`` would have been the
obvious move and would have been wrong twice over: it would make a composition a
*reimplementation*, and ``customer_360`` is a read-side summary of transactional
history while these are *judgements* -- a health band is not a fact about the
account, it is a conclusion drawn from facts and it can be wrong. Mixing the two
would let a conclusion be read as a record.

So this is a **composition**, in the same shape as the copilot: it takes the
surfaces as arguments and never re-derives one. A sixth answer to a question the
codebase already answers is exactly what Stage C was written to avoid.

Why the copilot is the default pane
-----------------------------------
An agent opening a customer does not want the raw parts. They want to know what to
say, what not to say, and what to do next -- and if that is what you make them
click through three tabs to find, they will read the raw table instead, because the
raw table is right there. The copilot is therefore the **default pane**, and the
raw surfaces are reachable but never the first thing shown.

The default is also the safe direction, which is why it is a default and not a
policy. The copilot refuses to lead with internal facts, enforces human review, and
marks anything that leaked through; a raw pane has none of those protections,
because its caller has no reason to expect to need them. An agent who deliberately
asks for the raw health band should get it -- that is inspection, not
personalisation, and the two are different acts. But it should be a request.

A default that a client could silently opt out of by naming a pane would not be a
default at all, so :func:`resolve_pane` only honours a requested pane when
``explicit=True`` is passed, and the reason it did so is returned either way.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

RELATIONSHIP_VIEW_CATALOG_VERSION = 1

#: The panes a caller may ask for, and what each one is *for*.
#:
#: ``copilot`` first because it is the default, and the ordering is the ordering a
#: UI would render. Every pane names its reader: an agent choosing between "what do
#: I say" and "what is in the health table" is a real choice with two different
#: audiences, and collapsing them is what produced the six-tab situation.
RELATIONSHIP_PANES: tuple[dict[str, Any], ...] = (
    {
        "pane": "copilot",
        "label": "What to say",
        "default": True,
        "reader": "agent, on a call",
        "protections": (
            "refuses to lead with internal facts, requires a human, marks leaks"
        ),
        "why": (
            "the default because it is the only pane an agent can act on directly. "
            "Everything else is evidence for it, and evidence a reader has to go "
            "and find does not get read"
        ),
    },
    {
        "pane": "what_is_true",
        "label": "Current position",
        "default": False,
        "reader": "agent, checking the copilot",
        "protections": "none; this is a conclusion, not a record",
        "why": (
            "health, status and investment band side by side. they are kept apart "
            "because a customer can be Trusted and critical at once, and merging "
            "them into a single 'status' is how that distinction gets lost"
        ),
    },
    {
        "pane": "offers",
        "label": "Offers and outcomes",
        "default": False,
        "reader": "agent, after a commitment",
        "protections": "none",
        "why": (
            "what was offered, what was accepted, and what was actually "
            "fulfilled. acceptance and fulfilment are separate columns because "
            "high acceptance with poor fulfilment is a fulfilment problem and "
            "reads as a fulfilment problem when they sit together"
        ),
    },
    {
        "pane": "journey",
        "label": "Recovery stage",
        "default": False,
        "reader": "agent, deciding what happens next",
        "protections": "none",
        "why": (
            "the current stage and its timebox. a stage past its timebox is the "
            "single most actionable thing in this view, so it is stated as "
            "overdue rather than as a timestamp"
        ),
    },
    {
        "pane": "history",
        "label": "Account history",
        "default": False,
        "reader": "investigation",
        "protections": "none",
        "why": (
            "the existing 360, unchanged. it is the pane for finding out what "
            "happened, and it is deliberately last -- it is the most complete and "
            "the least useful for opening a conversation"
        ),
    },
)
PANE_BY_ID: dict[str, dict[str, Any]] = {str(row["pane"]): dict(row) for row in RELATIONSHIP_PANES}
PANE_IDS: tuple[str, ...] = tuple(PANE_BY_ID)
DEFAULT_PANE = "copilot"

#: What the view says is true, and what it deliberately does not.
#:
#: Written as data so a reader can disagree with it in one place, and so the
#: refusal of the tempting cross-section is visible rather than merely absent.
VIEW_STANDING: tuple[dict[str, Any], ...] = (
    {
        "claim": "health and status are independent",
        "detail": (
            "a customer can be Trusted and critical simultaneously. health is live "
            "and may fall; status is earned and does not decay for inactivity. a "
            "single 'how is this customer' number would have to choose, and either "
            "choice is a lie about one of them"
        ),
    },
    {
        "claim": "an offer's existence is not its delivery",
        "detail": (
            "the offer is created whatever the preference gate says, and the "
            "notification is deferred. an offer that exists undelivered is the "
            "system working; an offer that does not exist because of a preference "
            "is a preference that became a suppression"
        ),
    },
    {
        "claim": "no offer outcome is attributed to a generosity rule",
        "detail": (
            "offers record generosity_scale, not the rule id, so attributing an "
            "outcome to a rule is coarse. the view shows the scale and says so "
            "rather than naming a rule it cannot support"
        ),
    },
    {
        "claim": "the investment band is ordinal",
        "detail": (
            "five ordered bands, never summed and never averaged. it sizes a "
            "credit; it is not a quantity"
        ),
    },
    {
        "claim": "a conclusion is not a record",
        "detail": (
            "health, status, band and stage are judgements about a person. they "
            "are shown apart from history so an agent can tell which is which, and "
            "so an edit to a judgement is not mistaken for a change of fact"
        ),
    },
)
STANDING_BY_CLAIM: dict[str, dict[str, Any]] = {
    str(row["claim"]): dict(row) for row in VIEW_STANDING
}


def _get(source: Any, key: str, default: Any = None) -> Any:
    """Read a key from a mapping or an object, without assuming which."""
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_map(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    if isinstance(source, Mapping):
        return dict(source)
    if hasattr(source, "model_dump"):
        try:
            return dict(source.model_dump())
        except Exception:  # noqa: BLE001
            return {}
    if hasattr(source, "__dict__"):
        return {k: v for k, v in vars(source).items() if not k.startswith("_")}
    return {}


def _offers_by_state(offers: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for offer in offers or ():
        row = _as_map(offer)
        state = str(row.get("status") or row.get("state") or "unknown")
        grouped.setdefault(state, []).append(row)
    return grouped


def _offer_outcome_lines(offers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Offer lines that state acceptance and fulfilment **separately**.

    Not a combined "success" field. `accepted` and `fulfilled` are distinct
    statuses because accept-but-unfulfilled is a real and sizeable backlog: the
    customer said yes and we did not deliver, and a view that calls that a
    success hides the only part anyone can act on.
    """
    lines: list[dict[str, Any]] = []
    for offer in offers or ():
        row = _as_map(offer)
        state = str(row.get("status") or row.get("state") or "unknown")
        accepted = state == "accepted"
        fulfilled = state == "fulfilled"
        lines.append(
            {
                "reference": row.get("reference") or row.get("offer_reference") or "",
                "kind": str(row.get("kind") or row.get("offer_kind") or ""),
                "status": state,
                "accepted": accepted,
                "fulfilled": fulfilled,
                "accepted_not_fulfilled": bool(accepted and not fulfilled),
                "generosity_scale": row.get("generosity_scale"),
                "attributable_to_rule": False,
                "attribution_note": (
                    "offers record generosity_scale, not the rule id, so this "
                    "outcome cannot be attributed to a generosity rule. The scale "
                    "is shown instead of a rule name the data cannot support"
                ),
            }
        )
    return lines


def _journey_lines(journey: Mapping[str, Any], *, now: Optional[Any] = None) -> dict[str, Any]:
    """The stage, its timebox, and whether the timebox has already passed.

    Overdue is stated rather than left as a timestamp. A stage that passed its
    timebox is the most actionable fact in this view, and an agent reading a date
    has to do the comparison themselves every single time.
    """
    plan = _as_map(journey)
    stage = str(plan.get("stage") or plan.get("current_stage") or "unknown")
    due = plan.get("stage_due_at") or plan.get("timebox_ends_at") or plan.get("due_at")
    overdue = False
    if due is not None and now is not None:
        try:
            overdue = bool(due < now)
        except TypeError:
            overdue = False
    return {
        "stage": stage,
        "stage_due_at": due,
        "overdue": overdue,
        "overdue_note": (
            "this stage passed its timebox; the next step is to move it or say "
            "why not, not to wait"
            if overdue
            else ""
        ),
        "attempt": plan.get("attempts") or plan.get("attempt"),
        "precondition_hash": plan.get("precondition_hash") or "",
        "next_step": plan.get("next_step") or "",
        "timebox_expired": bool(plan.get("timebox_expired")),
    }


def _what_is_true(
    *,
    health: Mapping[str, Any],
    status: Mapping[str, Any],
    contact: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The current position, with the three judgements kept explicitly apart."""
    health_map = _as_map(health)
    status_map = _as_map(status)
    contact_map = _as_map(contact)
    return {
        "health_band": str(health_map.get("band") or health_map.get("health_band") or "unknown"),
        "health_reason": str(health_map.get("reason") or health_map.get("band_reason") or ""),
        "health_is_live": True,
        "loyalty_status": str(status_map.get("status") or "unknown"),
        "status_earned_not_decayed": True,
        "investment_band": status_map.get("investment_band"),
        "investment_band_is_ordinal": True,
        "can_be_trusted_and_critical": bool(
            str(status_map.get("status") or "") == "Trusted"
            and str(health_map.get("band") or health_map.get("health_band") or "") == "critical"
        ),
        "contact": contact_map or {},
        "note": (
            "health is live and may fall; status is earned and does not decay for "
            "inactivity. Keeping them in one 'status' field would have to pick one, "
            "and either pick is a lie about the other"
        ),
    }


def resolve_pane(requested: Optional[str] = None, *, explicit: bool = False) -> dict[str, Any]:
    """Which pane, and -- when it is not the default -- why the request was honoured.

    ``explicit`` is required to move off the default. A client that passes
    ``pane="history"`` without it gets the copilot plus a reason, because a
    "default" that any caller can override by naming something is not a default.
    The explicit case is real: an agent inspecting a health band is doing
    something different from an agent opening a call, and both deserve to work.
    """
    wanted = str(requested or "").strip()
    if not wanted or wanted == DEFAULT_PANE:
        return {
            "pane": DEFAULT_PANE,
            "requested": wanted or "",
            "honoured": True,
            "explicit": bool(explicit and wanted == DEFAULT_PANE),
            "reason": (
                "the default: an agent opening a customer wants to know what to say "
                "and what not to say, and every other pane is evidence for that"
            ),
        }
    known = wanted in PANE_BY_ID
    if explicit and known:
        return {
            "pane": wanted,
            "requested": wanted,
            "honoured": True,
            "explicit": True,
            "reason": (
                f"explicitly requested. {PANE_BY_ID[wanted]['pane']} is a pane for "
                f"{PANE_BY_ID[wanted]['reader']}, and inspection is a different act "
                "from opening a conversation"
            ),
        }
    return {
        "pane": DEFAULT_PANE,
        "requested": wanted,
        "honoured": False,
        "explicit": bool(explicit),
        "reason": (
            f"{wanted!r} is not a pane this view has"
            if not known
            else (
                f"{wanted!r} is a pane, but it was not requested explicitly, so the "
                f"default stands. Naming a pane is not the same as asking for one: "
                "a default that any caller can move by accident is not a default"
            )
        ),
    }


def build_relationship_view(
    *,
    copilot_card: Any = None,
    health: Optional[Mapping[str, Any]] = None,
    status: Optional[Mapping[str, Any]] = None,
    offers: Sequence[Mapping[str, Any]] = (),
    journey_plan: Any = None,
    customer_360: Any = None,
    contact_window: Optional[Mapping[str, Any]] = None,
    gate: Optional[Mapping[str, Any]] = None,
    pane: Optional[str] = None,
    explicit_pane: bool = False,
    now: Optional[Any] = None,
) -> dict[str, Any]:
    """One place that shows every judgement about a customer, and what it may say.

    Composition, not implementation: ``copilot_card`` is built by
    ``agent_copilot``, ``health`` by ``relationship_health``, ``status`` by
    ``loyalty_status``, the offers by ``customer_offers``, the plan by
    ``journey_orchestrator`` and the 360 by ``customer_360``. Nothing here
    re-derives any of them, and :func:`validate_relationship_view` fails the build
    if it finds a surface this module claims to show but cannot see.
    """
    chosen = resolve_pane(pane, explicit=explicit_pane)
    card = _as_map(copilot_card)
    journey = _journey_lines(_as_map(journey_plan), now=now)
    offer_rows = list(offers or ())
    grouped = _offers_by_state(offer_rows)
    gate_map = _as_map(gate)

    offered_not_accepted = len(grouped.get("offered", ()))
    accepted_not_fulfilled = len(grouped.get("accepted", ()))

    view: dict[str, Any] = {
        "catalog_version": RELATIONSHIP_VIEW_CATALOG_VERSION,
        "pane": chosen["pane"],
        "pane_requested": chosen["requested"],
        "pane_honoured": chosen["honoured"],
        "pane_reason": chosen["reason"],
        "default_pane": DEFAULT_PANE,
        "pane_was_defaulted": not chosen["explicit"],
        "available_panes": list(PANE_IDS),
        "user_id": card.get("user_id") or _get(customer_360, "user_id") or _get(status, "user_id"),
        # The default pane's content is inline, not behind a link, because the
        # entire argument for the default is that an agent will not go and find it.
        "opening": str(card.get("opening") or ""),
        "requires_human": True if not card else bool(card.get("requires_human", True)),
        "do_not_say": [dict(row) for row in (card.get("do_not_say") or ())],
        "what_is_true": _what_is_true(
            health=health or {}, status=status or {}, contact=contact_window
        ),
        "journey": journey,
        "offers": _offer_outcome_lines(offer_rows),
        "offer_counts": {
            state: len(rows) for state, rows in sorted(grouped.items())
        },
        "open_commitments": {
            "offered_not_accepted": offered_not_accepted,
            "accepted_not_fulfilled": accepted_not_fulfilled,
            "note": (
                "accepted-but-unfulfilled is the only part of an offer anybody can "
                "act on today. A view that called the whole thing a success rate "
                "would put it below the line where an agent would look"
            ),
        },
        "contact_gate": {
            "push": bool(gate_map.get("push", True)) if gate_map else None,
            "reasons": list(gate_map.get("reasons") or ()) if gate_map else [],
            "local_window": dict(gate_map.get("local_window") or {}) if gate_map else {},
            "note": (
                "shown here so the agent knows why a message did not go out. an "
                "offer that exists and was not delivered is the system working; "
                "an agent who cannot see that distinction will assume the customer "
                "ignored it"
            ),
        },
        "standing": [dict(row) for row in VIEW_STANDING],
        "sources": {
            "copilot_card": bool(card),
            "relationship_health": bool(_as_map(health)),
            "loyalty_status": bool(_as_map(status)),
            "offers": len(offer_rows),
            "journey_orchestrator": bool(_as_map(journey_plan)),
            "customer_360": bool(_as_map(customer_360)),
        },
    }
    view["history_available"] = bool(view["sources"]["customer_360"])
    return view


def render_relationship_view(view: Mapping[str, Any]) -> str:
    """The view as a few lines, for a channel that has no room for a card.

    Ordered by what an agent needs in the first second: the opening line, then the
    thing they will be asked about. It deliberately **leads with the opening**, even
    when the journey is overdue -- the overdue notice is a reason the opening might
    need to change, and an agent reading "OVERDUE" first will open with the
    process rather than with the person.
    """
    if not isinstance(view, Mapping):
        return ""
    true_map = _as_map(view.get("what_is_true"))
    journey = _as_map(view.get("journey"))
    offers = view.get("offers") or []
    counts = _as_map(view.get("open_commitments"))
    lines: list[str] = []
    opening = str(view.get("opening") or "").strip()
    if opening:
        lines.append(opening)
    identity = (
        f"{true_map.get('loyalty_status', 'unknown')} / "
        f"{true_map.get('health_band', 'unknown')}"
    )
    lines.append(f"Status: {identity}")
    if offers:
        lines.append(
            f"Offers: {len(offers)} on the account, "
            f"{counts.get('offered_not_accepted', 0)} awaiting a decision, "
            f"{counts.get('accepted_not_fulfilled', 0)} accepted and not delivered"
        )
    if journey.get("overdue"):
        lines.append(
            f"Journey: {journey.get('stage')} is past its timebox -- "
            f"{journey.get('overdue_note')}"
        )
    elif journey.get("stage") and journey.get("stage") != "unknown":
        lines.append(f"Journey: {journey.get('stage')}")
    gate = _as_map(view.get("contact_gate"))
    if gate.get("push") is False:
        local = _as_map(gate.get("local_window"))
        where = f" (local {local.get('local_hour')}:00 in {local.get('region_id')})" if local else ""
        lines.append(
            "Contact deferred"
            f"{where}: {'; '.join(str(r) for r in gate.get('reasons') or []) or 'no reason recorded'}"
        )
    if view.get("requires_human"):
        lines.append("A human decides what is sent. Nothing here is an instruction.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_relationship_view() -> dict[str, Any]:
    """Check the view against the surfaces it claims to compose.

    The check that matters is the first one: **every pane's source must be a
    surface this codebase actually has.** A pane that reads a source nothing
    produces is not a pane, it is a promise -- and it would render as an empty
    section every time, which an operator would eventually read as "this customer
    has no offers" rather than "the view is broken".
    """
    errors: list[str] = []
    warnings: list[str] = []

    defaults = [str(row["pane"]) for row in RELATIONSHIP_PANES if row.get("default")]
    if defaults != [DEFAULT_PANE]:
        errors.append(
            f"exactly one pane must be the default and it must be {DEFAULT_PANE!r}; "
            f"found {defaults or 'none'}. The copilot is the default because it is "
            "the only pane an agent can act on directly"
        )
    if PANE_IDS[0] != DEFAULT_PANE:
        warnings.append(
            "PANE_IDS does not start with the default pane, so a UI rendering the "
            "order will not show it first. The argument for the default is that an "
            "agent will not go and find it"
        )

    expected_sources = {
        "copilot": "agent_copilot.build_copilot_card",
        "what_is_true": "relationship_health / loyalty_status",
        "offers": "customer_offers",
        "journey": "journey_orchestrator",
        "history": "customer_360",
    }
    for pane_id in PANE_IDS:
        if pane_id not in expected_sources:
            errors.append(
                f"pane {pane_id!r} has no declared source; a pane whose source is "
                "undeclared is a section that renders empty forever"
            )
        for row in RELATIONSHIP_PANES:
            if str(row["pane"]) == pane_id and not str(row.get("why") or "").strip():
                warnings.append(
                    f"pane {pane_id!r} has no `why`; a default nobody can question is "
                    "one somebody will route around"
                )

    # The default must not be movable by accident.
    probe = resolve_pane("history", explicit=False)
    if probe.get("pane") != DEFAULT_PANE or probe.get("honoured"):
        errors.append(
            "resolve_pane honours a pane that was not requested explicitly. A "
            "default that any caller can move by naming something is not a default"
        )
    explicit = resolve_pane("history", explicit=True)
    if explicit.get("pane") != "history" or not explicit.get("honoured"):
        errors.append(
            "resolve_pane refuses an explicitly requested known pane. Inspection is "
            "a different act from opening a conversation and both deserve to work"
        )
    unknown = resolve_pane("not_a_pane", explicit=True)
    if unknown.get("pane") != DEFAULT_PANE or unknown.get("honoured"):
        errors.append(
            "resolve_pane honours an unknown pane. An unrecognised pane name is a "
            "caller bug, and rendering it as a blank section is a worse answer"
        )

    # Acceptance and fulfilment must stay separable in the rendered lines.
    lines = _offer_outcome_lines(
        [
            {"reference": "A1", "kind": "recovery", "status": "accepted"},
            {"reference": "B1", "kind": "recovery", "status": "fulfilled"},
        ]
    )
    if not any(row["accepted_not_fulfilled"] for row in lines):
        errors.append(
            "an accepted-but-unfulfilled offer did not surface as such. That is the "
            "backlog signal the whole split exists for"
        )
    if any(row.get("attributable_to_rule") for row in lines):
        errors.append(
            "an offer outcome claims rule attribution. Offers record "
            "generosity_scale, not the rule id, so this must stay False"
        )

    # The view must not be able to instruct anything.
    try:
        from app.services import agent_copilot

        if not agent_copilot.COPILOT_DO_NOT_LEAD_WITH_BY_ID:
            errors.append(
                "agent_copilot.COPILOT_DO_NOT_LEAD_WITH is empty, so the view's "
                "do_not_say would render as an empty list and an agent would read "
                "that as 'nothing is off limits'"
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"could not read agent_copilot: {exc}")

    if not STANDING_BY_CLAIM:
        errors.append("VIEW_STANDING is empty; the refusals need to be readable")
    for claim, row in STANDING_BY_CLAIM.items():
        if not str(row.get("detail") or "").strip():
            warnings.append(f"VIEW_STANDING[{claim!r}] has no detail")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "panes": len(RELATIONSHIP_PANES),
        "default_pane": DEFAULT_PANE,
        "standing_claims": len(VIEW_STANDING),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(RELATIONSHIP_PANES)} panes, default {DEFAULT_PANE!r}, and "
            f"{len(VIEW_STANDING)} standing claims"
        ),
    }


def build_relationship_catalog() -> dict[str, Any]:
    """The pane list and the standing claims, published."""
    return {
        "catalog_version": RELATIONSHIP_VIEW_CATALOG_VERSION,
        "panes": [dict(row) for row in RELATIONSHIP_PANES],
        "default_pane": DEFAULT_PANE,
        "standing": [dict(row) for row in VIEW_STANDING],
        "note": (
            "a composition. by the end of Stage D the codebase answered six "
            "questions about a customer correctly and showed them in six places, "
            "and an agent opening one saw the 360 -- which contains none of health, "
            "status, offers or journey. adding those to CUSTOMER_360_SECTIONS "
            "would have made a composition into a reimplementation, and worse, it "
            "would have put judgements (a health band is a conclusion about a "
            "person) next to records. so the copilot is the default pane and the "
            "raw surfaces are reachable but never first. the default is the safe "
            "direction, not a policy: the copilot refuses to lead with internal "
            "facts and requires a human, and an agent who deliberately asks for a "
            "raw pane is inspecting, not personalising"
        ),
    }
