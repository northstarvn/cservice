"""Purpose-limited personalization, and trusted-device care paths (Stage E).

Two modules in one file because they answer the same question at different times:
*before* contact, what may this message use; *after* recognition, how much of this
can be done without asking again.

Why personalization needs a purpose at all
------------------------------------------
`CONSENT_GATED_PURPOSES` already lists ``personalization`` as consent-gated, and
`care_gate` already refuses a message whose purpose is gated and whose consent is
absent. That is the *outer* limit. What it cannot express is the inner one: a
message sent for ``recovery`` may legitimately use some personalization and not
others, and today nothing records which.

Without that, the inner limit is enforced by whoever writes the message — and the
temptation is always towards *more* personalization, because more of it feels more
relevant. So this table says, per purpose, exactly which dimensions are available,
and the refusal names the dimensions it refused rather than only saying "not
permitted".

Trusted-device care paths
-------------------------
`services/device_recognition.py` recognises devices. This connects what it
recognised to what care may then be done unattended, which is the actual reason to
know: a step-up challenge is friction on every call to a struggling customer, and
re-asking for a second factor every time is how a security control becomes a reason
people stop asking for help.

The bound is deliberately tight. **Recognition never changes what a recovery offer
is worth** — that is decided by the investment band and the offer policy, and a
device is not evidence of anything about a relationship. What it changes is
*whether we have to ask again*, and that is a genuine safety property rather than a
convenience one.

And it is not a bypass of the consent gate. A trusted device shortens *authentication*;
it does not shorten *permission*. A reactive-only customer on a recognised laptop
still does not get pushed, because that was their preference and a device has
nothing to do with it. `assert_trust_does_not_overreach` says so in code, so a later
"let's just let trusted devices skip the gate" is a visible edit rather than an
emergent consequence.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

# ===========================================================================
# Item 2 -- purpose-limited personalization
# ===========================================================================

PERSONALIZATION_LIMITS_CATALOG_VERSION = 1

#: The personalization dimensions this system knows about.
#:
#: Read as a closed vocabulary on purpose: an unlisted dimension is *unavailable*,
#: so adding a new one is a deliberate act with a consent consequence rather than a
#: property someone sets and forgets.
PERSONALIZATION_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "dimension": "tone",
        "label": "Match their stated tone",
        "sensitivity": "low",
        "why": (
            "how we phrase something, not what we say. a service message about "
            "their own problem is allowed to match their tone whatever the "
            "marketing position, because refusing it makes the message read as "
            "being about us"
        ),
    },
    {
        "dimension": "channel",
        "label": "Use their stated channel",
        "sensitivity": "low",
        "why": (
            "where it goes is a preference the customer set. this is the one "
            "dimension that is always permitted, because sending it somewhere they "
            "did not choose would violate the preference rather than honour it"
        ),
    },
    {
        "dimension": "timing",
        "label": "Send inside their stated window",
        "sensitivity": "low",
        "why": "same reasoning as channel; handled by care_gate and region_windows",
    },
    {
        "dimension": "history_reference",
        "label": "Refer to what they have actually done",
        "sensitivity": "medium",
        "why": (
            "'as you booked last month' is personalisation in the ordinary sense "
            "and is nearly always safe in a service message. it becomes exposure "
            "in a marketing one, where it says we are watching"
        ),
    },
    {
        "dimension": "segment_language",
        "label": "Name the segment they are in",
        "sensitivity": "high",
        "why": (
            "'as a valued customer' reads as flattery and, to some people, as a "
            "reminder that they are a line item. refused in marketing without "
            "consent and permitted in service only narrowly"
        ),
    },
    {
        "dimension": "peer_comparison",
        "label": "Compare them to other customers",
        "sensitivity": "high",
        "why": (
            "there is no purpose for it in a service message and it is the one "
            "that makes a relationship feel like a funnel. refused everywhere"
        ),
    },
    {
        "dimension": "value_tier",
        "label": "Signal their value tier",
        "sensitivity": "high",
        "why": (
            "the copilot's COPILOT_DO_NOT_LEAD_WITH already refuses to say this "
            "out loud, and this table refuses to use it to *write* the message "
            "either. the two must agree, so both are checked"
        ),
    },
    {
        "dimension": "churn_score",
        "label": "Signal their churn or complaint risk",
        "alias_note": (
            "named `churn_score` rather than `risk_score` so it matches "
            "COPILOT_DO_NOT_LEAD_WITH, which is the established vocabulary. Two "
            "names for one concept is how a cross-check starts reporting that a "
            "protected fact does not exist"
        ),
        "sensitivity": "high",
        "why": (
            "the copilot will not open with it, and a message that opens with it is "
            "worse than one that opens with an apology"
        ),
    },
    # The next three are here because `COPILOT_DO_NOT_LEAD_WITH` already protects
    # them. The copilot's list and this one have to agree, and
    # `validate_personalization_limits` reads the copilot's list rather than
    # restating it -- which is how these three were found missing. A fact kept out
    # of an agent's opening line while a copywriter is free to reach for it is not
    # protected at all.
    {
        "dimension": "access_band",
        "label": "Signal their access band",
        "sensitivity": "high",
        "why": (
            "an internal entitlements label. a customer asking for help does not "
            "know they are in a band, and telling them they are is an invitation to "
            "argue with the scoring"
        ),
    },
    {
        "dimension": "policy_tier",
        "label": "Signal their policy tier",
        "sensitivity": "high",
        "why": (
            "administrative, not personal: it describes who is allowed to approve "
            "things inside the business, which is not a thing to say to somebody "
            "asking about their booking"
        ),
    },
    {
        "dimension": "investment_band",
        "label": "Signal what they are worth to us",
        "sensitivity": "high",
        "why": (
            "it exists to size a credit. using it to shape what we say would be "
            "accurate and monstrous, which is the same reason the copilot will not "
            "say it out loud"
        ),
    },
)
PERSONALIZATION_DIMENSION_BY_ID: dict[str, dict[str, Any]] = {
    str(row["dimension"]): dict(row) for row in PERSONALIZATION_DIMENSIONS
}

#: Per purpose, which dimensions may be used, and what the remainder are called.
#:
#: ``service`` and ``recovery`` are the only purposes that may use anything beyond
#: tone and channel, and only ``history_reference`` beyond that. Everything marked
#: high-sensitivity is refused in a marketing purpose without consent -- which is
#: the case the existing ``personalization`` consent boolean was always about, and
#: which nothing in the codebase previously expressed per dimension.
PURPOSE_PERSONALIZATION_LIMITS: dict[str, dict[str, Any]] = {
    "service": {
        "purpose": "service",
        "allowed": ("channel", "timing", "tone", "history_reference"),
        "allowed_without_consent": ("channel", "timing", "tone", "history_reference"),
        "reason": (
            "everything up to history_reference. the message is about something "
            "that happened to this person, so referring to it is not personalisation "
            "in the sense the consent flag means -- it is accuracy"
        ),
    },
    "recovery": {
        "purpose": "recovery",
        "allowed": ("channel", "timing", "tone", "history_reference"),
        "allowed_without_consent": ("channel", "timing", "tone", "history_reference"),
        "reason": (
            "the same set as service, deliberately. they are two names for 'we are "
            "responding to something you told us', and a recovery message that had "
            "to be written more generically because of a marketing flag would be "
            "worse at the one job it has"
        ),
    },
    "marketing": {
        "purpose": "marketing",
        "allowed": ("channel", "timing", "tone", "history_reference"),
        "allowed_without_consent": ("channel", "timing", "tone"),
        "reason": (
            "history_reference needs the personalization consent here. it is the "
            "first dimension past tone and channel, and it is the one where 'we "
            "noticed something about you' starts to feel like surveillance rather "
            "than service"
        ),
    },
    "analytics": {
        "purpose": "analytics",
        "allowed": (),
        "allowed_without_consent": (),
        "reason": (
            "no content is sent under this purpose. it names a measurement, not a "
            "message, and a table that let it choose wording would be inviting a "
            "copywriter to reach for it"
        ),
    },
}
PERSONALIZATION_PURPOSES: tuple[str, ...] = tuple(PURPOSE_PERSONALIZATION_LIMITS)


def resolve_personalization(
    *,
    purpose: str,
    consents: Optional[Mapping[str, bool]] = None,
    channel: str = "",
) -> dict[str, Any]:
    """Which personalization dimensions a message for this purpose may use.

    Refuses by **naming the dimension**, not only saying "not permitted". A refusal
    that lists what was taken out is actionable by whoever writes the message, and
    a refusal that only says no just gets worked around -- which is what happened
    to the ``communication_suppression`` pack, authored and never called.
    """
    rule = PURPOSE_PERSONALIZATION_LIMITS.get(str(purpose))
    if rule is None:
        return {
            "purpose": str(purpose),
            "known_purpose": False,
            "allowed": [],
            "refused": sorted(PERSONALIZATION_DIMENSION_BY_ID),
            "refusal_reasons": {
                name: (
                    f"{name!r} is not a purpose this table knows. An unrecognised "
                    "purpose is refused rather than defaulted to permissive, "
                    "because the failure direction that matters here is writing a "
                    "marketing message with a service message's licence"
                )
                for name in PERSONALIZATION_DIMENSION_BY_ID
            },
            "channel": str(channel or ""),
            "note": "unknown purpose: every dimension refused",
        }
    granted = bool((consents or {}).get("personalization", True))
    permitted = set(rule["allowed_without_consent"])
    conditional = set(rule["allowed"]) - permitted
    allowed = sorted(permitted | (conditional if granted else set()))
    refused = sorted(set(PERSONALIZATION_DIMENSION_BY_ID) - set(allowed))
    reasons: dict[str, str] = {}
    for name in sorted(conditional):
        if name not in allowed:
            reasons[name] = (
                f"{name!r} is available for {purpose!r} but the personalization "
                "consent is absent"
            )
    for name in refused:
        if name in conditional:
            continue
        reasons[name] = f"{name!r} is never available for {purpose!r}"
    return {
        "purpose": str(purpose),
        "known_purpose": True,
        "personalization_consent": granted,
        "allowed": allowed,
        "allowed_unconditionally": sorted(permitted),
        "allowed_on_consent": sorted(conditional & set(allowed)),
        "refused": refused,
        "refusal_reasons": reasons,
        "channel": str(channel or ""),
        "reason": str(rule["reason"]),
        "note": (
            "refusals name the dimension. a refusal that only says 'not permitted' "
            "is not actionable by whoever writes the message, and a rule that is not "
            "actionable is a rule that gets worked around"
        ),
    }


# ===========================================================================
# Item 5 -- trusted-device care paths
# ===========================================================================

TRUSTED_DEVICE_CARE_CATALOG_VERSION = 1

#: What care steps a recognised device may complete without asking again.
#:
#: Read rather than inferred from a trust *score*. A score says how sure we are; it
#: does not say which steps that certainty unlocks, and letting a threshold
#: argument decide that silently is how "trusted" comes to mean "anything".
TRUSTED_DEVICE_CARE_STEPS: tuple[dict[str, Any], ...] = (
    {
        "step_id": "view_own_offers",
        "label": "Look at an offer addressed to them",
        "min_trust_band": "recognized",
        "requires_consent_gate": False,
        "mutates": False,
        "why": (
            "reading their own inbox is not a privilege escalation. the offer is "
            "already visible and already theirs"
        ),
    },
    {
        "step_id": "accept_own_offer",
        "label": "Accept an offer addressed to them",
        "min_trust_band": "recognized",
        "requires_consent_gate": False,
        "mutates": True,
        "why": (
            "their decision, on their own account, about something already offered "
            "to them. recognition is not what authorises it -- the fact that it is "
            "theirs is"
        ),
    },
    {
        "step_id": "decline_own_offer",
        "label": "Decline an offer addressed to them",
        "min_trust_band": "recognized",
        "requires_consent_gate": False,
        "mutates": True,
        "why": "the same reasoning as accept, and equally it must never be harder",
    },
    {
        "step_id": "acknowledge_recovery",
        "label": "Acknowledge a recovery message as received",
        "min_trust_band": "recognized",
        "requires_consent_gate": False,
        "mutates": True,
        "why": (
            "the cheapest possible signal that somebody read the thing. it closes "
            "the loop on 'have they seen this' without anybody being called"
        ),
    },
    {
        "step_id": "send_message_unattended",
        "label": "Send the next message from this device without asking again",
        "min_trust_band": "elevated",
        # The one step a device must NOT unlock on its own. It is *contact*, so it
        # is exactly where "recognition shortens authentication, not permission"
        # is testable rather than merely stated -- and this column was all-False
        # until this row existed, which left the whole branch untested.
        "requires_consent_gate": True,
        "mutates": True,
        "why": (
            "everything else here is the customer's own account being read or "
            "changed by them. this one is us initiating contact, and a recognised "
            "laptop says nothing about whether they want to be interrupted"
        ),
    },
    {
        "step_id": "widen_credit_limit",
        "label": "Raise a credit limit",
        "min_trust_band": "elevated",
        "requires_consent_gate": False,
        "mutates": True,
        "why": (
            "the first step that moves money against the customer's favour without "
            "their choosing to. it needs more than recognition, and it is listed "
            "here rather than omitted so the threshold is reviewable"
        ),
    },
    {
        "step_id": "apply_policy_adjustment",
        "label": "Change their own policy tier or control posture",
        "min_trust_band": "trusted",
        "requires_consent_gate": False,
        "mutates": True,
        "why": (
            "never on recognition. a device is not evidence of anything about a "
            "relationship, and this is the change that would decide what the next "
            "recovery is worth"
        ),
    },
)
TRUSTED_DEVICE_STEP_BY_ID: dict[str, dict[str, Any]] = {
    str(row["step_id"]): dict(row) for row in TRUSTED_DEVICE_CARE_STEPS
}

#: Trust bands, weakest first. Read from ``device_recognition``'s recognition mode
#: vocabulary where one exists, and declared here so the ordering is data.
TRUST_BANDS: tuple[dict[str, Any], ...] = (
    {"band": "unknown", "rank": 0, "label": "Not recognised"},
    {"band": "recognized", "rank": 1, "label": "Recognised device"},
    {"band": "elevated", "rank": 2, "label": "Seen before, consistently"},
    {"band": "trusted", "rank": 3, "label": "Long-standing and consistent"},
)
TRUST_BAND_RANK: dict[str, int] = {str(row["band"]): int(row["rank"]) for row in TRUST_BANDS}


def resolve_trust_band(
    *,
    recognized: bool = False,
    recognition_mode: str = "",
    signals_matched: int = 0,
    recognitions: int = 0,
) -> dict[str, Any]:
    """Turn what ``device_recognition`` found into one band.

    Conservative by construction: ``unknown`` when recognition is off, and
    ``recognized`` rather than anything stronger when only a single recognition
    exists. A band that could be reached by one sighting would make "trusted" mean
    "has logged in once".
    """
    mode = str(recognition_mode or "").strip().lower()
    if mode in ("off", "disabled", "none", ""):
        if not recognized:
            return {
                "band": "unknown",
                "rank": 0,
                "label": "Not recognised",
                "recognition_enabled": False,
                "reason": "device recognition is off, so nothing is known about this device",
            }
    rank = TRUST_BAND_RANK
    if not recognized:
        band, why = "unknown", "this device has not been recognised"
    elif recognitions >= 10 and signals_matched >= 3:
        band, why = "trusted", f"{recognitions} consistent recognitions with {signals_matched} signals"
    elif recognitions >= 3:
        band, why = "elevated", f"{recognitions} recognitions, not yet enough to be trusted"
    else:
        band, why = "recognized", f"one sighting ({recognitions} recognition(s))"
    return {
        "band": band,
        "rank": int(rank.get(band, 0)),
        "label": str(next((r["label"] for r in TRUST_BANDS if r["band"] == band), band)),
        "recognitions": int(recognitions),
        "signals_matched": int(signals_matched),
        "recognition_enabled": True,
        "recognition_mode": mode,
        "reason": why,
    }


def resolve_care_paths(
    trust: Mapping[str, Any],
    *,
    gate: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Which care steps this device may complete unattended, and why.

    **A trusted device shortens authentication; it never shortens permission.**
    The ``requires_consent_gate`` rows are the ones a device cannot unlock, and the
    gate's own verdict is passed in rather than recomputed -- so if the customer
    said reactive-only, a recognised laptop does not get them pushed. That is the
    overreach this module is built to refuse, and
    :func:`assert_trust_does_not_overreach` states it in code.
    """
    band = str(trust.get("band") or "unknown")
    rank = int(trust.get("rank") or TRUST_BAND_RANK.get(band, 0))
    gate_map = dict(gate or {})
    push_allowed = bool(gate_map.get("push", True))

    permitted: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for row in TRUSTED_DEVICE_CARE_STEPS:
        step_id = str(row["step_id"])
        required = TRUST_BAND_RANK.get(str(row["min_trust_band"]), 99)
        entry = {
            "step_id": step_id,
            "label": str(row["label"]),
            "min_trust_band": str(row["min_trust_band"]),
            "mutates": bool(row["mutates"]),
            "why": str(row["why"]),
        }
        if rank < required:
            refused.append(
                {
                    **entry,
                    "reason": (
                        f"needs {row['min_trust_band']}, this device is {band}. A "
                        "threshold that one sighting could cross would make 'trusted' "
                        "mean 'has logged in once'"
                    ),
                }
            )
            continue
        if not push_allowed and bool(row.get("requires_consent_gate")):
            refused.append(
                {
                    **entry,
                    "reason": (
                        "the preference gate refused contact on this path, and a "
                        "recognised device does not override a stated preference: "
                        "recognition shortens authentication, not permission"
                    ),
                }
            )
            continue
        # Note what is *not* refused: a customer on a recognised laptop can still
        # read and accept an offer addressed to them even when we may not push
        # them. A gate about *contact* must not become a gate about their own
        # account -- that would mean the preference stopped them acting on their
        # own behalf, which is a different and much worse thing.
        permitted.append(entry)
    return {
        "trust_band": band,
        "trust_rank": rank,
        "permitted": permitted,
        "refused": refused,
        "permitted_step_ids": [row["step_id"] for row in permitted],
        "refused_step_ids": [row["step_id"] for row in refused],
        "push_permitted_by_gate": push_allowed,
        "overreach": [],
        "note": (
            "a recognised device never changes what a recovery offer is worth. that "
            "is the investment band and the offer policy, and a device is not "
            "evidence of anything about a relationship. what it changes is whether we "
            "have to ask again, and only the steps above clear the bar"
        ),
    }


def assert_trust_does_not_overreach(paths: Mapping[str, Any]) -> None:
    """The invariant, asserted rather than assumed.

    A future "let's just let trusted devices skip the gate" should be a visible
    edit here rather than an emergent consequence of a threshold.
    """
    if paths.get("overreach"):
        raise AssertionError(
            "a recognised device must never widen what a message may say or which "
            "gate it bypasses: " + ", ".join(str(item) for item in paths["overreach"])
        )
    for row in TRUSTED_DEVICE_CARE_STEPS:
        if str(row.get("min_trust_band")) == "recognized" and str(
            row.get("step_id")
        ) in {"widen_credit_limit", "apply_policy_adjustment"}:
            raise AssertionError(
                f"{row['step_id']} is unlocked by mere recognition, which makes a "
                "device evidence about a relationship. It is not"
            )


# ============================================================================
# Validation and catalog
# ============================================================================


def validate_personalization_limits() -> dict[str, Any]:
    """Check the limits against the consent vocabulary and the copilot's own list.

    The check that matters: **any dimension the copilot refuses to say out loud must
    also be refused as message *content*.** The two lists agreeing is what stops
    `value_tier` being kept out of an agent's opening line while a campaign copy
    writer reaches for it precisely because it works.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        from app.services import agent_copilot, preferences

        copilot_facts = set(agent_copilot.COPILOT_DO_NOT_LEAD_WITH_BY_ID)
        for fact_id in sorted(copilot_facts):
            if fact_id not in PERSONALIZATION_DIMENSION_BY_ID:
                errors.append(
                    f"COPILOT_DO_NOT_LEAD_WITH protects {fact_id!r}, which is not a "
                    "personalization dimension. The copilot's list and this table "
                    "have to agree, or one of them is decorative"
                )
        for purpose, rule in PURPOSE_PERSONALIZATION_LIMITS.items():
            if purpose not in preferences.CONSENT_PURPOSE_BY_NAME:
                errors.append(
                    f"purpose {purpose!r} is not in CONSENT_PURPOSE_BY_NAME, so the "
                    "gate cannot report whether consent applied"
                )
            allowed = set(rule["allowed"])
            unconditional = set(rule["allowed_without_consent"])
            if not unconditional <= allowed:
                errors.append(
                    f"PURPOSE_PERSONALIZATION_LIMITS[{purpose}] lists a dimension in "
                    "allowed_without_consent that is not in allowed, so it can never "
                    "be granted and the entry is dead"
                )
            for name in allowed | unconditional:
                if name not in PERSONALIZATION_DIMENSION_BY_ID:
                    errors.append(
                        f"PURPOSE_PERSONALIZATION_LIMITS[{purpose}] names "
                        f"{name!r}, which is not a declared dimension"
                    )
            for name in set(rule["allowed"]) - unconditional:
                spec = PERSONALIZATION_DIMENSION_BY_ID.get(name, {})
                if str(spec.get("sensitivity")) == "high":
                    warnings.append(
                        f"{purpose}: {name!r} is high-sensitivity and gated on the "
                        "personalization consent. That is the intended design -- but "
                        "it is also the one a copywriter will reach for twice"
                    )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"could not cross-check against the copilot: {exc}")

    for dimension in PERSONALIZATION_DIMENSION_BY_ID:
        if not str(PERSONALIZATION_DIMENSION_BY_ID[dimension].get("why") or "").strip():
            warnings.append(
                f"PERSONALIZATION_DIMENSIONS[{dimension}] has no `why`; a limit "
                "nobody can question is one somebody will route around"
            )
    # `peer_comparison` must be refused everywhere. It is the one with no service
    # purpose at all, and a future "let's add it to loyalty" would be a change of
    # product rather than of policy.
    for purpose, rule in PURPOSE_PERSONALIZATION_LIMITS.items():
        if "peer_comparison" in rule["allowed"]:
            errors.append(
                f"{purpose} permits peer_comparison. There is no service purpose for "
                "it and it is the dimension that makes a relationship feel like a "
                "funnel; adding it is a product change, not a policy tweak"
            )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "dimensions": len(PERSONALIZATION_DIMENSIONS),
        "purposes": len(PURPOSE_PERSONALIZATION_LIMITS),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(PERSONALIZATION_DIMENSIONS)} dimensions and "
            f"{len(PURPOSE_PERSONALIZATION_LIMITS)} purposes"
        ),
    }


def validate_trusted_device_care() -> dict[str, Any]:
    """Check the care paths against the recognition vocabulary and themselves."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        from app.services import device_recognition

        modes = {str(item).lower() for item in device_recognition.RECOGNITION_MODES}
        for mode in ("off", "disabled"):
            if modes and mode not in modes:
                warnings.append(
                    f"device_recognition.RECOGNITION_MODES has no {mode!r}; the "
                    "off-detection in resolve_trust_band may miss the real spelling"
                )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not read device_recognition.RECOGNITION_MODES: {exc}")

    ranks = [int(row["rank"]) for row in TRUST_BANDS]
    if ranks != sorted(ranks):
        errors.append("TRUST_BANDS ranks are not ascending")
    if len(set(ranks)) != len(ranks):
        errors.append("TRUST_BANDS repeats a rank")

    step_ids = [str(row["step_id"]) for row in TRUSTED_DEVICE_CARE_STEPS]
    if len(set(step_ids)) != len(step_ids):
        errors.append("TRUSTED_DEVICE_CARE_STEPS repeats a step_id")
    for row in TRUSTED_DEVICE_CARE_STEPS:
        step_id = str(row["step_id"])
        if str(row.get("min_trust_band")) not in TRUST_BAND_RANK:
            errors.append(
                f"TRUSTED_DEVICE_CARE_STEPS[{step_id}] names trust band "
                f"{row['min_trust_band']!r}, which TRUST_BANDS does not declare"
            )
        if not str(row.get("why") or "").strip():
            warnings.append(
                f"TRUSTED_DEVICE_CARE_STEPS[{step_id}] has no `why`; a step unlocked "
                "on a device nobody can argue with is one somebody will widen"
            )
        if "requires_consent_gate" not in row:
            errors.append(
                f"TRUSTED_DEVICE_CARE_STEPS[{step_id}] does not say whether it needs "
                "the consent gate, so a reader cannot tell whether recognition could "
                "bypass it"
            )

    # The money-moving steps must not be reachable by mere recognition.
    for step_id in ("widen_credit_limit", "apply_policy_adjustment"):
        row = TRUSTED_DEVICE_STEP_BY_ID.get(step_id, {})
        if str(row.get("min_trust_band")) == "recognized":
            errors.append(
                f"{step_id} is unlocked by mere recognition; a device is not evidence "
                "of anything about a relationship"
            )
    try:
        assert_trust_does_not_overreach(
            {"overreach": [], "trust_band": "trusted", "rank": 3}
        )
    except AssertionError as exc:
        errors.append(f"the overreach invariant failed: {exc}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "steps": len(TRUSTED_DEVICE_CARE_STEPS),
        "bands": len(TRUST_BANDS),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(TRUSTED_DEVICE_CARE_STEPS)} care steps and {len(TRUST_BANDS)} "
            "trust bands"
        ),
    }


def build_stage_e_catalog() -> dict[str, Any]:
    """Both tables, published, so the limits are reviewable without reading code."""
    return {
        "catalog_version": PERSONALIZATION_LIMITS_CATALOG_VERSION,
        "personalization_dimensions": [dict(row) for row in PERSONALIZATION_DIMENSIONS],
        "purpose_limits": {k: dict(v) for k, v in PURPOSE_PERSONALIZATION_LIMITS.items()},
        "trusted_device_care_steps": [dict(row) for row in TRUSTED_DEVICE_CARE_STEPS],
        "trust_bands": [dict(row) for row in TRUST_BANDS],
        "trusted_device_catalog_version": TRUSTED_DEVICE_CARE_CATALOG_VERSION,
        "note": (
            "two tables answering the same question at different times. "
            "personalization limits say what a message for a purpose may USE, and "
            "the consent gate already says whether it may be SENT -- the inner "
            "limit was previously enforced only by whoever wrote the message, and "
            "the temptation there is always towards more. refusals name the "
            "dimension, because a refusal that only says 'not permitted' is not "
            "actionable and gets routed around. trusted-device paths change whether "
            "we must ask AGAIN; they never change what a message may say and never "
            "widen what an offer is worth"
        ),
    }