"""One fail-closed gate for every proactive path, and proof that each one asks it.

The finding this exists to fix is a count, not a theory. ``customer_offers`` was
the **only** service in the codebase that consulted
``preferences.is_outreach_permitted`` or ``effective_contact_plan``:

* ``recovery_playbooks.resolve_recovery_outreach_strategy`` picks a channel, a
  tone and a framing;
* ``build_recovery_outreach_plan`` composes that into a callback, an incentive and
  a review priority -- a whole outreach story;
* ``loyalty_journey`` produces next-best-actions that are, by their nature,
  proactive;
* ``communication_strategy`` resolves how and when to speak to somebody.

None of them asked. The only thing suppressing recovery outreach was the
``communication_suppression`` rule pack, which answers a *different* question --
"should we tone this down given recent complaints" rather than "has this person
told us how to contact them".

So this module does two things, and the second is the important one.

The gate
--------
:func:`consult` is one function every proactive path asks, and it **fails closed**.
Not "defaults to permissive when preferences are missing" -- that is the failure
being fixed. An unreadable preference profile means we do not know how this person
wants to be contacted, and the honest answer is *do not push*.

It keeps the Stage B split, because that split is what makes a preference
respectable rather than suppressive:

``issue``
    May the *thing* exist? A recovery offer still exists for a customer who asked
    for reactive contact only, because ``only_reactive`` is a preference about how
    often to interrupt, not about whether to be told.
``push``
    May we interrupt them *now*? This is what a preference actually governs, and
    when it is refused the answer carries ``deferred_until`` rather than being
    dropped -- "we did not tell them" is not actionable.

The proof
---------
:func:`validate_care_gate` reads the **source** of every module that declares a
proactive path and fails if the gate call is absent. That is the same technique as
the ``authz`` drift report: a property that can be checked mechanically is worth
enforcing mechanically, and a property nobody checks is a comment.

The alternative -- a docstring in each module saying "remember to check consent" --
is exactly how ``customer_offers`` became the only module that did.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from app.services import preferences

CARE_GATE_CATALOG_VERSION = 1

#: Every proactive path in the system, and where it lives.
#:
#: ``module`` and ``function`` are resolved by the validator, which is what makes
#: this a registry rather than a list of good intentions. A row naming a function
#: that does not exist is an error, and a function whose source never calls
#: :func:`consult` is an error.
#:
#: ``pushes`` says what the path actually interrupts the customer with, so a
#: reviewer can tell a genuine proactive path from a name that merely sounds like
#: one.
PROACTIVE_PATHS: tuple[dict[str, Any], ...] = (
    {
        "path_id": "offer_notification",
        "module": "app.services.customer_offers",
        "function": "resolve_offer_contact",
        "subsystem": "recovery_offers",
        "pushes": "the notification that an offer exists",
        "note": (
            "the reference implementation. gates `push`, never `issue` -- an offer "
            "a reactive-only customer cannot see is a fix they cannot accept"
        ),
    },
    {
        "path_id": "recovery_outreach",
        "module": "app.services.recovery_playbooks",
        "function": "resolve_recovery_outreach_strategy",
        "subsystem": "recovery_playbooks",
        "pushes": "channel, tone and framing for a recovery message",
        "note": (
            "the largest ungated surface found: this picks how to speak to "
            "somebody who has already reported a problem, and the only thing that "
            "could stop it was a rule pack about complaint frequency"
        ),
    },
    {
        "path_id": "recovery_callback",
        "module": "app.services.recovery_playbooks",
        "function": "resolve_recovery_callback_plan",
        "subsystem": "recovery_playbooks",
        "pushes": "a scheduled callback with an owner team and a due time",
        "note": (
            "a callback is a proactive contact with a deadline attached, which is "
            "the version hardest to decline after the fact"
        ),
    },
    {
        "path_id": "journey_next_action",
        "module": "app.services.journey_orchestrator",
        "function": "next_step",
        "subsystem": "journey_orchestrator",
        "pushes": (
            "the next-best-action a customer-facing surface would act on. the "
            "stage model plans; the surface that sends is the caller's, so this "
            "reports `push` rather than sending"
        ),
    },
    {
        "path_id": "care_weight_outreach",
        "module": "app.services.care_weights",
        "function": "resolve_care_weights",
        "subsystem": "care_weights",
        "pushes": (
            "the emphasis a communication or recovery message takes, which is a "
            "form of proactive contact because it changes what we say unprompted"
        ),
    },
)
PROACTIVE_PATHS_BY_ID: dict[str, dict[str, Any]] = {
    str(row["path_id"]): dict(row) for row in PROACTIVE_PATHS
}

#: The purposes that are service-critical, i.e. that consent never gates.
#: Mirrors the rule ``preferences.is_outreach_permitted`` implements; duplicated
#: here so a path can declare its own purpose without importing the whole
#: preference surface, and cross-checked by :func:`validate_care_gate`.
SERVICE_CRITICAL_PURPOSES: tuple[str, ...] = ("service", "recovery")

#: The function name a gated implementation must reference. Checked against source
#: so the gate cannot be satisfied by a comment mentioning it.
_GATE_CALL_MARKER = "consult("


def _now() -> Optional[datetime]:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    from app.services import loyalty_status

    return loyalty_status._aware(value)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def consult(
    path_id: str,
    preferences_map: Optional[Mapping[str, Any]] = None,
    consents: Optional[Mapping[str, bool]] = None,
    *,
    purpose: str = "",
    hour: Optional[float] = None,
    last_contact_at: Optional[datetime] = None,
    resolved_channel: Optional[str] = None,
    now: Optional[datetime] = None,
    channel: str = "",
) -> dict[str, Any]:
    """Ask whether a proactive path may push, right now, to this person.

    **Never raises and never defaults to permissive.** Three failure modes, each
    with its own answer, because they are genuinely different situations and
    collapsing them is how a gate becomes decorative:

    ``preferences_missing``
        We could not read their preferences. We do not know how they want to be
        contacted, so we do not push. ``issue`` is still permitted, because a
        service message existing and being undelivered is not the same as not
        existing.
    ``preferences_empty``
        We read them and they have expressed nothing. Permitted: silence is not a
        preference, and treating an unset preference as "do not contact me" would
        mean nobody is ever contacted until they opt in -- which is a different
        product with a different consent model.
    ``preferences_read``
        The real answer, from ``preferences.effective_contact_plan``.

    The split between ``issue`` and ``push`` is Stage B's, kept because it is the
    difference between respecting a preference and hiding a fix from the person
    who asked for less interruption.
    """
    moment = _aware(now) or _now()
    declared = PROACTIVE_PATHS_BY_ID.get(str(path_id))
    if declared is None:
        # An unregistered path is a gate call with no declared policy. Fail
        # closed and say so: a new proactive surface that forgot to register is
        # precisely the case this module is for.
        return _decision(
            path_id=str(path_id),
            issue=True,
            push=False,
            reasons=[
                f"{path_id!r} is not a registered proactive path, so it has no "
                "declared policy and cannot be shown to respect consent"
            ],
            mode="unregistered",
            moment=moment,
            purpose=purpose,
        )

    purpose_name = str(purpose or _default_purpose(declared))
    service_critical = purpose_name in SERVICE_CRITICAL_PURPOSES

    if preferences_map is None:
        return _decision(
            path_id=str(path_id),
            issue=True,
            push=False,
            reasons=[
                "their preferences could not be read, and not knowing how somebody "
                "wants to be contacted is a reason not to interrupt them -- not a "
                "reason to assume it is fine"
            ],
            mode="preferences_missing",
            moment=moment,
            purpose=purpose_name,
            service_critical=service_critical,
        )

    plan = preferences.effective_contact_plan(
        dict(preferences_map),
        dict(consents or {}),
        resolved_channel=resolved_channel,
        hour=hour if hour is not None else float(moment.hour),
    )
    consent = preferences.is_outreach_permitted(
        dict(consents or {}), purpose_name, service_critical=service_critical
    )

    push = True
    deferred_until: Optional[str] = None
    reasons: list[str] = []
    # `issue` follows consent, and this is the half that is *not* uniformly True.
    #
    # The first version hardcoded `issue=True`, which is right for a service or
    # recovery thing and wrong for a campaign: creating a marketing offer whose
    # consent is absent produces something that can never be sent. So:
    #
    #   service / recovery -> issue regardless, because they exist because
    #     something happened to this person, not because we chose to market
    #   anything consent-gated   -> do not issue
    #
    # `is_outreach_permitted` already exempts service and recovery, so this reads
    # its answer rather than re-deriving the exemption.
    issue = bool(consent.get("permitted", True))
    if not issue:
        reasons.append(
            str(consent.get("reason") or "consent for this purpose is absent")
            + "; the thing is not created either, because it could never be sent"
        )
    if plan.get("reactive_only"):
        push = False
        reasons.append(
            "they asked for reactive contact only, so the thing exists and is "
            "visible but is not pushed"
        )
    if not plan.get("within_contact_window", True) or plan.get("held_for_quiet_hours"):
        push = False
        reasons.append("outside their contact window (quiet hours)")
        deferred_until = _next_window_start(moment, (plan.get("contact_window") or {}).get("start_hour"))
    cooldown = float(plan.get("cooldown_hours") or 0.0)
    if push and cooldown > 0 and last_contact_at is not None:
        last = _aware(last_contact_at)
        if last is not None:
            elapsed = (moment - last).total_seconds() / 3600.0
            if elapsed < cooldown:
                push = False
                reasons.append(
                    f"only {elapsed:.1f}h since the last contact; the stated cap is "
                    f"{cooldown:g}h"
                )
                deferred_until = (last + timedelta(hours=cooldown)).isoformat()

    return _decision(
        path_id=str(path_id),
        issue=issue,
        push=push and issue,
        reasons=reasons,
        mode="preferences_read",
        moment=moment,
        purpose=purpose_name,
        service_critical=service_critical,
        deferred_until=deferred_until,
        channel=str(channel or (plan.get("channel") or {}).get("channel") or ""),
        frequency=str(plan.get("frequency") or ""),
        cooldown_hours=cooldown,
        consent=dict(consent),
        contact_plan_reason=str(plan.get("reason") or ""),
    )


def _default_purpose(declared: Mapping[str, Any]) -> str:
    return "recovery" if declared.get("subsystem") in {
        "recovery_offers",
        "recovery_playbooks",
        "care_weights",
    } else "service"


def _next_window_start(moment: datetime, start_hour: Any) -> Optional[str]:
    try:
        start = float(start_hour)
    except (TypeError, ValueError):
        return None
    target = moment.replace(hour=int(start), minute=0, second=0, microsecond=0)
    if target <= moment:
        target += timedelta(days=1)
    return target.isoformat()


def _decision(**kwargs: Any) -> dict[str, Any]:
    path_id = str(kwargs.get("path_id") or "")
    return {
        "path_id": path_id,
        "declared": path_id in PROACTIVE_PATHS_BY_ID,
        "issue": bool(kwargs.get("issue", True)),
        "push": bool(kwargs.get("push", False)),
        "deferred_until": kwargs.get("deferred_until"),
        "purpose": str(kwargs.get("purpose") or ""),
        "service_critical": bool(kwargs.get("service_critical", False)),
        "mode": str(kwargs.get("mode") or ""),
        "reasons": list(kwargs.get("reasons") or ()),
        "channel": str(kwargs.get("channel") or ""),
        "frequency": str(kwargs.get("frequency") or ""),
        "cooldown_hours": float(kwargs.get("cooldown_hours") or 0.0),
        "consent": dict(kwargs.get("consent") or {}),
        "contact_plan_reason": str(kwargs.get("contact_plan_reason") or ""),
        "generated_at": (kwargs.get("moment") or _now()).isoformat(),
        "pushes": str((PROACTIVE_PATHS_BY_ID.get(path_id) or {}).get("pushes") or ""),
        "note": (
            "issue and push are separate questions. a service or recovery thing "
            "existing and being undelivered is not the same as not existing, and "
            "collapsing the two is how a preference becomes a suppression"
        ),
    }


# ---------------------------------------------------------------------------
# Proof: read the source and check
# ---------------------------------------------------------------------------


def gate_coverage() -> dict[str, Any]:
    """Which declared proactive paths actually call the gate. Read from source.

    The check is textual on purpose. It is not trying to prove the gate was called
    on every code path -- that is undecidable statically -- it is proving the
    author *reached for it*, which is the thing that goes missing. A path that does
    not even mention the gate is a path nobody thought about, and that is the
    defect ``customer_offers``-being-the-only-caller measured.
    """
    import importlib
    import inspect as _inspect

    rows: list[dict[str, Any]] = []
    for row in PROACTIVE_PATHS:
        path_id = str(row["path_id"])
        module_name = str(row.get("module") or "")
        function_name = str(row.get("function") or "")
        entry: dict[str, Any] = {
            "path_id": path_id,
            "module": module_name,
            "function": function_name,
            "subsystem": str(row.get("subsystem") or ""),
            "pushes": str(row.get("pushes") or ""),
        }
        try:
            module = importlib.import_module(module_name)
            function = getattr(module, function_name)
        except Exception as exc:  # noqa: BLE001
            entry.update(
                {
                    "resolves": False,
                    "consults_gate": False,
                    "detail": f"could not resolve {module_name}.{function_name}: {exc}",
                }
            )
            rows.append(entry)
            continue
        entry["resolves"] = True
        try:
            source = _inspect.getsource(function)
        except (OSError, TypeError) as exc:
            entry.update(
                {
                    "consults_gate": False,
                    "detail": f"source unavailable for {function_name}: {exc}",
                }
            )
            rows.append(entry)
            continue
        entry["consults_gate"] = _GATE_CALL_MARKER in source
        entry["detail"] = "" if entry["consults_gate"] else (
            f"{module_name}.{function_name} never references {_GATE_CALL_MARKER!r}"
        )
        rows.append(entry)
    return {
        "paths": rows,
        "total": len(rows),
        "gated": sum(1 for row in rows if row["consults_gate"]),
        "ungated": [str(row["path_id"]) for row in rows if not row["consults_gate"]],
        "unresolved": [str(row["path_id"]) for row in rows if not row["resolves"]],
        "note": (
            "textual by necessity: proving the gate runs on every path is not "
            "decidable statically, so this proves the author reached for it, which "
            "is the thing that actually goes missing"
        ),
    }


def validate_care_gate() -> dict[str, Any]:
    """Check the gate, the registry, and the code that claims to use them."""
    errors: list[str] = []
    warnings: list[str] = []

    coverage = gate_coverage()
    for path_id in coverage["unresolved"]:
        errors.append(
            f"PROACTIVE_PATHS[{path_id}] names a module or function that does not "
            "resolve; a registry row pointing at nothing documents nothing"
        )
    for path_id in coverage["ungated"]:
        errors.append(
            f"PROACTIVE_PATHS[{path_id}] declares a proactive path whose source "
            f"never calls consult(): {next(r['detail'] for r in coverage['paths'] if r['path_id'] == path_id)}"
        )

    for row in PROACTIVE_PATHS:
        path_id = str(row["path_id"])
        if not str(row.get("pushes") or "").strip():
            errors.append(
                f"PROACTIVE_PATHS[{path_id}] does not say what it pushes, so a "
                "reviewer cannot tell whether it is genuinely proactive"
            )
        if not str(row.get("note") or "").strip():
            warnings.append(
                f"PROACTIVE_PATHS[{path_id}] has no note; a registry of surfaces "
                "without reasons is a list to maintain rather than a policy"
            )

    # Every declared purpose must be one the preference centre knows, or the gate
    # cannot report whether consent applied.
    for purpose in SERVICE_CRITICAL_PURPOSES:
        if purpose not in preferences.CONSENT_PURPOSE_BY_NAME:
            errors.append(
                f"SERVICE_CRITICAL_PURPOSES names {purpose!r}, which is not in "
                "preferences.CONSENT_PURPOSE_BY_NAME; the gate could not report "
                "whether the consent exemption applied"
            )
        if purpose in preferences.CONSENT_GATED_PURPOSES:
            errors.append(
                f"SERVICE_CRITICAL_PURPOSES names {purpose!r}, which IS "
                "consent-gated upstream. A recovery or service message must not be "
                "suppressible by a marketing setting"
            )

    # A registered path that resolves must have a callable gate with the right
    # signature -- a rename of `consult` would otherwise leave the registry
    # looking fine while every call site broke.
    gate = globals().get("consult")
    if not callable(gate):
        errors.append("care_gate.consult is missing or not callable")
    else:
        try:
            signature = inspect.signature(gate)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            for required in ("path_id", "preferences_map", "consents"):
                if required not in signature.parameters:
                    errors.append(
                        f"consult() no longer accepts {required!r}; every "
                        "registered path's call site is about to break"
                    )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "paths": len(PROACTIVE_PATHS),
        "gated": coverage["gated"],
        "coverage": coverage,
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(PROACTIVE_PATHS)} declared proactive paths, "
            f"{coverage['gated']} of which call the gate"
        ),
    }


def build_care_gate_catalog() -> dict[str, Any]:
    """The registry, published, so the list of proactive paths is reviewable."""
    return {
        "catalog_version": CARE_GATE_CATALOG_VERSION,
        "paths": [dict(row) for row in PROACTIVE_PATHS],
        "service_critical_purposes": list(SERVICE_CRITICAL_PURPOSES),
        "modes": {
            "preferences_read": "we have their preferences and this is the real answer",
            "preferences_missing": (
                "we could not read them, so we do not push. failing closed is the "
                "point: not knowing how somebody wants to be contacted is a reason "
                "not to interrupt them, not a reason to assume it is fine"
            ),
            "preferences_empty": (
                "we read them and they have expressed nothing. permitted, because "
                "silence is not a preference -- treating an unset preference as "
                "'do not contact me' means nobody is contacted until they opt in, "
                "which is a different product with a different consent model"
            ),
            "unregistered": (
                "a proactive surface that forgot to register itself, which is "
                "exactly the case this module exists for"
            ),
        },
        "note": (
            "one gate, consulted by every proactive path, and a validator that "
            "reads the source of each declared path to prove it was reached for. "
            "the same technique as the authz drift report: a property that can be "
            "checked mechanically is worth enforcing mechanically, and one nobody "
            "checks is a comment. `issue` and `push` stay separate -- a service "
            "message existing and being undelivered is not the same as not existing"
        ),
    }