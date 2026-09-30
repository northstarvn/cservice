"""Preference & Consent Center (Stage A: Trust & Visibility).

The gap this fills: nothing in the tree let a customer say *"tell me by email,
not SMS, and not so often"*, and nothing recorded whether they had. The
communication-strategy resolver picks a channel in **precedence order** and
reads four layers (admin select, policy, culture, profile) -- none of which is
the customer's own stated wish. This module supplies that missing layer, and it
sits *below* the existing ladder rather than beside it.

Precedence, and why
-------------------
``resolve_communication_strategy`` decides tone/channel/framing. The
customer's own preference is a *new* input, not a new output, so rather than
fork the ladder this module exposes one function the ladder consults:

1. ``is_outreach_permitted(purpose)`` -- may we contact this person at all?
2. ``preferred_channel(user_id)`` -- if we may, which channel?

Both are **opt-out by default for marketing and opt-in-by-default for
service**. That asymmetry is the whole design and it is deliberate: a customer
who has not opted in to promotional email must not receive it, but a customer
who never touched a preference form still has to receive the message telling
them their booking was cancelled.

What is deliberately *not* done
-------------------------------
This module does **not** filter the recovery playbooks, and that is a
decision rather than an omission. A goodwill credit after a service failure and
a service-recovery callback are the *response to a problem the customer
reported*. Suppressing those because a marketing consent flag is false would
turn a consent control into a way to lose a customer, and a customer who never
received the fix would have no way to know why. The consent gate therefore
applies to ``marketing`` and ``analytics`` and *not* to ``service`` or
``recovery``; :data:`CONSENT_PURPOSES` records the per-purpose default and
:func:`consent_report` says out loud which purposes are gated, so "no guard
exists here" is a published property rather than a gap somebody has to notice.

Config-driven
-------------
``PREFERENCE_CATALOG`` declares every key, its type, its options and whether it
is required. ``CONSENT_PURPOSES`` declares every purpose, its lawful basis and
its default. Adding a preference or a purpose is a data edit; no code and no
migration changes, because the key space is stored as a JSON blob on
``models.UserPreferenceProfile``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

#: Version of the shipped key/purpose space. A client holding a stale
#: ``consent_version`` is told the current one rather than silently accepted,
#: because a consent record asserted against a different catalogue of purposes
#: is not the same claim.
PREFERENCE_CATALOG_VERSION = "preference_catalog_v1"


# ---------------------------------------------------------------------------
# Preference catalog
# ---------------------------------------------------------------------------
# ``type`` drives validation: `select` requires membership in `options`,
# `boolean` is normalised to a real bool, `string` is length-bounded, `number`
# is clamped to [min, max]. `options` is also what the customer sees, so the
# validation vocabulary and the UI vocabulary cannot drift.

PREFERENCE_CATALOG: list[dict[str, Any]] = [
    {
        "key": "communication_channel",
        "category": "communication",
        "label": "Preferred channel",
        "description": (
            "Where we should send service and recovery messages. Left unset, the "
            "communication-strategy resolver decides from your profile."
        ),
        "type": "select",
        "required": False,
        "options": [
            {"value": "email", "label": "Email"},
            {"value": "sms", "label": "Text message"},
            {"value": "phone", "label": "Phone call"},
            {"value": "in_app", "label": "In-app notification only"},
        ],
    },
    {
        "key": "communication_frequency",
        "category": "communication",
        "label": "How often we may contact you",
        "description": (
            "Caps proactive messages. Service-critical messages about a booking or "
            "an open issue are not affected by this setting."
        ),
        "type": "select",
        "required": False,
        "options": [
            {"value": "realtime", "label": "As soon as something happens"},
            {"value": "daily", "label": "At most once a day"},
            {"value": "weekly", "label": "At most once a week"},
            {"value": "only_reactive", "label": "Only when I contact you first"},
        ],
    },
    {
        "key": "preferred_language",
        "category": "communication",
        "label": "Preferred language",
        "description": (
            "Used for outbound messages. Unset falls back to the locale resolved "
            "from your request."
        ),
        "type": "select",
        "required": False,
        "options": [
            {"value": "en", "label": "English"},
            {"value": "es", "label": "Español"},
            {"value": "fr", "label": "Français"},
        ],
    },
    {
        "key": "contact_window_start_hour",
        "category": "communication",
        "label": "Contact window start",
        "description": (
            "Local hour we may begin contacting you. 0-23. Used to place callbacks "
            "and non-urgent messages."
        ),
        "type": "number",
        "required": False,
        "min": 0,
        "max": 23,
    },
    {
        "key": "contact_window_end_hour",
        "category": "communication",
        "label": "Contact window end",
        "description": "Local hour we must stop contacting you. 0-23.",
        "type": "number",
        "required": False,
        "min": 0,
        "max": 23,
    },
    {
        "key": "quiet_hours_enabled",
        "category": "communication",
        "label": "Respect quiet hours",
        "description": (
            "Hold non-urgent messages until your contact window opens. Service "
            "messages about an open issue are still delivered."
        ),
        "type": "boolean",
        "required": False,
    },
    {
        "key": "topic_focus",
        "category": "topics",
        "label": "Topics you want to hear about",
        "description": (
            "An explicit topic selection. Empty means 'no restriction' -- it does "
            "not mean 'nothing', because an empty list is the absence of a "
            "preference rather than a stated one."
        ),
        "type": "string_list",
        "required": False,
        "max_items": 20,
    },
    {
        "key": "booking_reminders",
        "category": "service",
        "label": "Booking reminders",
        "description": "Remind me before an upcoming booking.",
        "type": "boolean",
        "required": False,
    },
    {
        "key": "proactive_updates",
        "category": "service",
        "label": "Proactive service updates",
        "description": (
            "Tell me about progress on my requests without me asking, including "
            "status changes on bookings in flight."
        ),
        "type": "boolean",
        "required": False,
    },
    {
        "key": "show_recovery_activity",
        "category": "transparency",
        "label": "Show me what was done for me",
        "description": (
            "Include customer-visible recovery actions -- goodwill credits, "
            "escalations, policy adjustments -- in your own status view. On by "
            "default: this is the transparency surface, and hiding it by default "
            "would mean the feature is only discoverable after an incident."
        ),
        "type": "boolean",
        "required": False,
        "default": True,
    },
    {
        "key": "plain_language_explanations",
        "category": "transparency",
        "label": "Explain decisions in plain language",
        "description": (
            "Show factor-by-factor reasoning in plain language instead of raw "
            "scores wherever an explanation is available."
        ),
        "type": "boolean",
        "required": False,
        "default": True,
    },
]

PREFERENCE_BY_KEY: dict[str, dict[str, Any]] = {
    str(row["key"]): row for row in PREFERENCE_CATALOG
}

#: Grouping published alongside the flat list so a settings screen can render
#: sections without inventing its own taxonomy.
PREFERENCE_CATEGORIES: list[dict[str, str]] = [
    {
        "category": "communication",
        "label": "How we contact you",
        "description": "Channel, timing, language and frequency.",
    },
    {
        "category": "service",
        "label": "Service updates",
        "description": "What we proactively tell you about your own bookings and requests.",
    },
    {
        "category": "topics",
        "label": "Topics",
        "description": "What you want to hear about, if you want to narrow it.",
    },
    {
        "category": "transparency",
        "label": "What we tell you",
        "description": "How much of our reasoning we show you.",
    },
]


# ---------------------------------------------------------------------------
# Consent purposes
# ---------------------------------------------------------------------------
# ``default`` is the state a user is in before touching the centre.
# ``gates_outreach`` says whether a withdrawal stops us contacting them.
# ``required`` purposes cannot be withdrawn at all: withdrawing the basis we
# need to run the contract is withdrawing from the service, and the honest
# response to that is a delete request, not a silent switch.
#
# `service` and `recovery` are ``default_granted`` and ``gates_outreach: False``.
# See the module docstring: the gate exists so this is a published property, not
# an oversight.

CONSENT_PURPOSES: list[dict[str, Any]] = [
    {
        "purpose": "service",
        "label": "Service messages",
        "description": (
            "Messages about your bookings, requests and open issues. Required to "
            "deliver the service, so it cannot be withdrawn here."
        ),
        "lawful_basis": "contract",
        "default": True,
        "gates_outreach": False,
        "required": True,
    },
    {
        "purpose": "recovery",
        "label": "Service recovery",
        "description": (
            "Goodwill credits, escalations and callbacks when we detect a problem "
            "with your experience. Follows your service consent and is not gated "
            "separately, so a problem you reported never goes unanswered because of "
            "a marketing setting."
        ),
        "lawful_basis": "contract",
        "default": True,
        "gates_outreach": False,
        "required": True,
    },
    {
        "purpose": "analytics",
        "label": "Analytics and improvement",
        "description": (
            "Use your interactions to improve the service. Withdrawing this stops "
            "scoring but keeps your history and your account."
        ),
        "lawful_basis": "legitimate_interest",
        "default": True,
        "gates_outreach": True,
        "required": False,
    },
    {
        "purpose": "marketing",
        "label": "Offers and promotions",
        "description": (
            "Promotional messages. Withdrawing this is a one-click opt-out and is "
            "honoured immediately, including for anything already scheduled."
        ),
        "lawful_basis": "consent",
        "default": False,
        "gates_outreach": True,
        "required": False,
    },
    {
        "purpose": "personalization",
        "label": "Personalised recommendations",
        "description": (
            "Tailor the topics and next-best-actions you are shown to your history. "
            "Withdrawing this falls the resolver back to non-personalised defaults "
            "rather than turning recommendations off entirely."
        ),
        "lawful_basis": "consent",
        "default": True,
        "gates_outreach": True,
        "required": False,
    },
    {
        "purpose": "third_party_sharing",
        "label": "Sharing with partners",
        "description": (
            "Share anonymised aggregates with service partners. Withdrawing this "
            "stops new sharing; aggregates already published cannot be recalled, "
            "which is stated here rather than discovered afterwards."
        ),
        "lawful_basis": "consent",
        "default": False,
        "gates_outreach": True,
        "required": False,
    },
]

CONSENT_PURPOSE_BY_NAME: dict[str, dict[str, Any]] = {
    str(row["purpose"]): row for row in CONSENT_PURPOSES
}

#: Purposes whose withdrawal actually stops outreach. Published so a reader can
#: check the claim "consent gates outreach" against the list instead of taking
#: it on faith.
CONSENT_GATED_PURPOSES: tuple[str, ...] = tuple(
    str(row["purpose"]) for row in CONSENT_PURPOSES if row.get("gates_outreach")
)
#: Purposes that can never be withdrawn.
CONSENT_REQUIRED_PURPOSES: tuple[str, ...] = tuple(
    str(row["purpose"]) for row in CONSENT_PURPOSES if row.get("required")
)

#: Frequency -> minimum hours between proactive messages. Deliberately a table
#: rather than arithmetic: "at most once a day" is a product decision, and
#: encoding it as `24 / n` would make a future 12-hour tier an expression
#: change nobody would think to review.
FREQUENCY_COOLDOWN_HOURS: dict[str, float] = {
    "realtime": 0.0,
    "daily": 24.0,
    "weekly": 168.0,
    "only_reactive": float("inf"),
}
FREQUENCY_DEFAULT = "daily"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def default_preferences() -> dict[str, Any]:
    """The preference map a user has before ever opening the centre.

    Derived from the catalog's ``default`` key so a new default is a data edit.
    A key with no declared default is *absent* rather than ``None``: "not set"
    and "set to nothing" are different states, and the resolver needs to tell
    them apart to know whether to fall through to its own ladder.
    """
    return {
        str(row["key"]): row["default"]
        for row in PREFERENCE_CATALOG
        if "default" in row
    }


def default_consents() -> dict[str, bool]:
    return {str(row["purpose"]): bool(row["default"]) for row in CONSENT_PURPOSES}


def _coerce_preference_value(spec: dict[str, Any], value: Any) -> tuple[Any, str | None]:
    """Validate one value against its catalog spec. Returns ``(value, error)``.

    An unknown key is an *error*, not a silent drop: a client that posts a typo
    and receives 200 has been told its preference was saved.
    """
    kind = str(spec.get("type", "string"))
    if kind == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}:
            return value.strip().lower() in {"true", "1", "yes"}, None
        return None, f"{spec['key']} expects a boolean, got {type(value).__name__}"
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{spec['key']} expects a number"
        low = spec.get("min")
        high = spec.get("max")
        if low is not None and number < float(low):
            return None, f"{spec['key']} must be >= {low}"
        if high is not None and number > float(high):
            return None, f"{spec['key']} must be <= {high}"
        return number, None
    if kind == "select":
        allowed = [str(option["value"]) for option in spec.get("options", [])]
        if str(value) not in allowed:
            return None, (
                f"{spec['key']} must be one of {', '.join(allowed) or '(none declared)'}"
            )
        return str(value), None
    if kind == "string_list":
        if isinstance(value, str):
            items = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value if str(item).strip()]
        else:
            return None, f"{spec['key']} expects a list of strings"
        max_items = int(spec.get("max_items", 0) or 0)
        if max_items and len(items) > max_items:
            return None, f"{spec['key']} accepts at most {max_items} entries"
        return items, None
    # Plain string. No length bound is declared, so none is enforced; adding one
    # is a catalog edit rather than a code change.
    if not isinstance(value, str):
        return None, f"{spec['key']} expects a string, got {type(value).__name__}"
    return value, None


def resolve_contact_window(preferences: dict[str, Any]) -> dict[str, Any]:
    """The contact window, with the wrap-around case handled explicitly.

    A window whose end is *before* its start is a legitimate overnight window
    (22:00-07:00), so "end < start" is not an error and not inverted by a
    naive comparison -- ``contains`` below is the only place that decides
    membership, and it knows about both shapes.
    """
    start = preferences.get("contact_window_start_hour")
    end = preferences.get("contact_window_end_hour")
    has_start = isinstance(start, (int, float)) and not isinstance(start, bool)
    has_end = isinstance(end, (int, float)) and not isinstance(end, bool)
    return {
        "start_hour": float(start) if has_start else None,
        "end_hour": float(end) if has_end else None,
        "configured": bool(has_start and has_end),
        "enabled": bool(preferences.get("quiet_hours_enabled", False)) and has_start and has_end,
        "overnight": bool(has_start and has_end and float(end) < float(start)),
    }


def hour_in_contact_window(hour: float, window: dict[str, Any]) -> bool:
    """Is ``hour`` inside ``window``? An unconfigured window admits everything."""
    if not window.get("configured"):
        return True
    start = float(window["start_hour"])
    end = float(window["end_hour"])
    moment = float(hour) % 24.0
    if start <= end:
        return start <= moment < end
    # Overnight window: permitted from start through midnight up to end.
    return moment >= start or moment < end


def frequency_cooldown_hours(frequency: Any) -> float:
    """Hours required between proactive messages, or ``inf`` for reactive-only.

    An unknown frequency falls back to the *default* rather than to
    ``realtime``: a typo in a stored preference must not remove the customer's
    own frequency cap, which is the direction that errs toward contacting them
    more than they asked for.
    """
    key = str(frequency or FREQUENCY_DEFAULT)
    if key in FREQUENCY_COOLDOWN_HOURS:
        return FREQUENCY_COOLDOWN_HOURS[key]
    return FREQUENCY_COOLDOWN_HOURS[FREQUENCY_DEFAULT]


def is_within_quiet_hours(preferences: dict[str, Any], hour: Optional[float]) -> bool:
    """Should a non-urgent message be held back right now?"""
    window = resolve_contact_window(preferences)
    if not window.get("enabled"):
        return False
    if hour is None:
        return False
    return not hour_in_contact_window(float(hour), window)


# ---------------------------------------------------------------------------
# Async loaders
# ---------------------------------------------------------------------------


async def get_or_create_preference_profile(
    db: AsyncSession, user_id: int
) -> models.UserPreferenceProfile:
    """Load the user's profile row, creating an empty one on first access.

    Created eagerly rather than lazily on write so a first read returns the
    full default surface with every key *absent* -- distinguishable from a key
    set to its default -- instead of an empty object the client has to
    interpret.
    """
    result = await db.execute(
        select(models.UserPreferenceProfile).where(
            models.UserPreferenceProfile.user_id == int(user_id)
        )
    )
    profile = result.scalar_one_or_none()
    if profile is not None:
        return profile
    profile = models.UserPreferenceProfile(
        user_id=int(user_id),
        preferences_json=json.dumps({}, sort_keys=True),
        consents_json=json.dumps(default_consents(), sort_keys=True),
        consent_version=PREFERENCE_CATALOG_VERSION,
    )
    db.add(profile)
    await db.flush()
    return profile


def _profile_maps(profile: models.UserPreferenceProfile) -> tuple[dict[str, Any], dict[str, bool]]:
    """Decode a profile row, merging stored values over the shipped defaults.

    Decoding is total: a malformed blob yields the defaults rather than raising,
    because this is a read path serving a settings screen and a corrupted
    preference must not become a 500.
    """
    try:
        stored_prefs = json.loads(getattr(profile, "preferences_json", "") or "{}")
    except (TypeError, ValueError):
        stored_prefs = {}
    if not isinstance(stored_prefs, dict):
        stored_prefs = {}
    try:
        stored_consents = json.loads(getattr(profile, "consents_json", "") or "{}")
    except (TypeError, ValueError):
        stored_consents = {}
    if not isinstance(stored_consents, dict):
        stored_consents = {}
    preferences = default_preferences()
    preferences.update({str(key): value for key, value in stored_prefs.items()})
    consents = default_consents()
    consents.update(
        {
            str(key): bool(value)
            for key, value in stored_consents.items()
            if str(key) in CONSENT_PURPOSE_BY_NAME
        }
    )
    return preferences, consents


async def load_user_preferences(
    db: AsyncSession, user_id: int
) -> tuple[dict[str, Any], dict[str, bool]]:
    """The user's effective preferences and consents, defaults merged in."""
    profile = await get_or_create_preference_profile(db, user_id)
    return _profile_maps(profile)


# ---------------------------------------------------------------------------
# Outreach gate
# ---------------------------------------------------------------------------


def is_outreach_permitted(
    consents: dict[str, bool],
    purpose: str,
    *,
    service_critical: bool = False,
) -> dict[str, Any]:
    """May we contact this person for ``purpose`` right now?

    The decision always carries its reason, including when it says yes. "We
    contacted them because their purpose is service-critical" and "we
    contacted them because they are fine with marketing" are different claims
    and a caller that cannot tell them apart cannot audit either.

    ``service_critical`` overrides a withdrawn gated purpose. It exists for
    exactly one class of message: a reply to something the customer sent us.
    It is *not* a general bypass, which is why it is a named keyword argument
    rather than a flag on the consent record -- so a caller has to say out loud
    which kind of message it is sending.
    """
    name = str(purpose or "service")
    spec = CONSENT_PURPOSE_BY_NAME.get(name)
    if spec is None:
        # An undeclared purpose is refused rather than defaulted to allowed.
        # Defaulting an unknown purpose to permitted is how a typo in a
        # caller's string turns into an unconsented message.
        return {
            "permitted": False,
            "purpose": name,
            "reason": f"purpose {name!r} is not declared in CONSENT_PURPOSES",
            "known_purpose": False,
            "gated": False,
            "granted": False,
            "override": "none",
        }
    granted = bool(consents.get(name, spec.get("default", False)))
    gated = bool(spec.get("gates_outreach"))
    required = bool(spec.get("required"))
    if service_critical:
        return {
            "permitted": True,
            "purpose": name,
            "reason": "service-critical message; consent gate not applied",
            "known_purpose": True,
            "gated": gated,
            "granted": granted,
            "override": "service_critical",
        }
    if required:
        return {
            "permitted": True,
            "purpose": name,
            "reason": f"purpose {name!r} is required to deliver the service and cannot be withdrawn",
            "known_purpose": True,
            "gated": gated,
            "granted": True,
            "override": "required_purpose",
        }
    if gated and not granted:
        return {
            "permitted": False,
            "purpose": name,
            "reason": f"consent for {name!r} is withdrawn",
            "known_purpose": True,
            "gated": True,
            "granted": False,
            "override": "none",
        }
    return {
        "permitted": True,
        "purpose": name,
        "reason": f"consent for {name!r} is granted" if granted else f"purpose {name!r} is not gated",
        "known_purpose": True,
        "gated": gated,
        "granted": granted,
        "override": "none",
    }


def preferred_channel(
    preferences: dict[str, Any],
    resolved_channel: str | None = None,
) -> dict[str, Any]:
    """Override a resolved channel with the customer's stated preference.

    Returns the decision rather than a bare string: ``in_app`` is a *downgrade*
    (we still email, and the app copy is a courtesy), so a caller that only
    received ``"in_app"`` would believe we stopped emailing when we did not.
    """
    stated = preferences.get("communication_channel")
    if not stated:
        return {
            "channel": str(resolved_channel or ""),
            "source": "resolved" if resolved_channel else "none",
            "honored": bool(resolved_channel),
            "stated": None,
            "note": "no stated channel preference; the resolved strategy stands",
        }
    value = str(stated)
    if not resolved_channel:
        return {
            "channel": value,
            "source": "stated_preference",
            "honored": True,
            "stated": value,
            "note": "no resolved strategy to override; the stated preference is used",
        }
    if value == str(resolved_channel):
        return {
            "channel": value,
            "source": "stated_preference",
            "honored": True,
            "stated": value,
            "note": "stated preference matches the resolved strategy",
        }
    if value == "in_app":
        return {
            "channel": str(resolved_channel),
            "supplemental_channel": "in_app",
            "source": "resolved_with_in_app_supplement",
            "honored": False,
            "honored_as": "supplemental",
            "stated": value,
            "note": (
                "'in_app' does not withdraw the resolved channel: service messages "
                "still go out, with an in-app copy alongside"
            ),
        }
    return {
        "channel": value,
        "source": "stated_preference",
        "honored": True,
        "honored_as": "override",
        "stated": value,
        "overrides": str(resolved_channel),
        "note": "customer's stated channel replaces the resolved strategy",
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def preference_catalog() -> dict[str, Any]:
    """The declared key space, for a settings screen and for validation."""
    return {
        "catalog_version": PREFERENCE_CATALOG_VERSION,
        "total_preferences": len(PREFERENCE_CATALOG),
        "total_categories": len(PREFERENCE_CATEGORIES),
        "preferences": [
            {
                "key": row["key"],
                "category": row["category"],
                "label": row["label"],
                "description": row["description"],
                "type": row["type"],
                "required": bool(row.get("required", False)),
                "options": list(row.get("options", []) or []),
                "default": row.get("default"),
                "min": row.get("min"),
                "max": row.get("max"),
                "max_items": row.get("max_items"),
            }
            for row in PREFERENCE_CATALOG
        ],
        "categories": [dict(row) for row in PREFERENCE_CATEGORIES],
        "defaults": default_preferences(),
        "frequency_cooldown_hours": {
            key: (None if value == float("inf") else value)
            for key, value in FREQUENCY_COOLDOWN_HOURS.items()
        },
        "frequency_default": FREQUENCY_DEFAULT,
    }


def consent_report(consents: dict[str, bool]) -> dict[str, Any]:
    """The consent surface, including which purposes actually gate outreach."""
    records: list[dict[str, Any]] = []
    for row in CONSENT_PURPOSES:
        name = str(row["purpose"])
        granted = bool(consents.get(name, row.get("default", False)))
        records.append(
            {
                **dict(row),
                "granted": granted,
                "withdrawn": not granted and not row.get("required", False),
                "at_default": consents.get(name) is None,
            }
        )
    gated = [name for name in CONSENT_GATED_PURPOSES]
    ungated = [
        str(row["purpose"])
        for row in CONSENT_PURPOSES
        if not row.get("gates_outreach")
    ]
    return {
        "version": PREFERENCE_CATALOG_VERSION,
        "purposes": records,
        "gated_purposes": gated,
        "ungated_purposes": ungated,
        "required_purposes": list(CONSENT_REQUIRED_PURPOSES),
        "granted_count": sum(1 for row in records if row["granted"]),
        "withdrawn_count": sum(1 for row in records if row["withdrawn"]),
        "note": (
            "'service' and 'recovery' do not gate outreach on purpose: they are the "
            "response to a problem, and a consent switch that could suppress the "
            "fix for a customer's own complaint would be a way to lose them "
            "silently. Every other purpose that gates is enforced by "
            "is_outreach_permitted()."
        ),
    }


def effective_contact_plan(
    preferences: dict[str, Any],
    consents: dict[str, bool],
    *,
    resolved_channel: str | None = None,
    hour: Optional[float] = None,
) -> dict[str, Any]:
    """The composed answer to "how may we contact this person right now".

    One function because the three answers are not independent: a channel the
    customer named still has to pass the consent gate, and a permitted channel
    can still be inside quiet hours. Evaluating them separately at three call
    sites is how the combination gets dropped.
    """
    service_gate = is_outreach_permitted(consents, "service", service_critical=True)
    marketing_gate = is_outreach_permitted(consents, "marketing")
    channel = preferred_channel(preferences, resolved_channel)
    quiet = is_within_quiet_hours(preferences, hour)
    frequency = str(preferences.get("communication_frequency") or FREQUENCY_DEFAULT)
    cooldown = frequency_cooldown_hours(frequency)
    # The service gate is unconditionally permitted by design (see the module
    # docstring), so it can never be what holds a message. Quiet hours have to
    # be evaluated against the *proactive* permission, not against a gate that
    # is always true: an earlier version computed `held = quiet and not
    # permitted`, which was structurally always False and silently made
    # `quiet_hours_enabled` a no-op on the contact plan.
    proactive_updates = bool(preferences.get("proactive_updates", True))
    held = bool(quiet and proactive_updates)
    return {
        "permitted": bool(service_gate["permitted"]),
        "marketing_permitted": bool(marketing_gate["permitted"]),
        "service_permitted": bool(service_gate["permitted"]),
        "channel": channel,
        "frequency": frequency,
        "cooldown_hours": None if cooldown == float("inf") else cooldown,
        "reactive_only": cooldown == float("inf"),
        "within_contact_window": not quiet,
        "held_for_quiet_hours": held,
        "contact_window": resolve_contact_window(preferences),
        "proactive_updates": proactive_updates,
        "booking_reminders": bool(preferences.get("booking_reminders", True)),
        "show_recovery_activity": bool(preferences.get("show_recovery_activity", True)),
        "plain_language_explanations": bool(
            preferences.get("plain_language_explanations", True)
        ),
        "personalization": bool(consents.get("personalization", True)),
        "analytics": bool(consents.get("analytics", True)),
        "service_gate": service_gate,
        "marketing_gate": marketing_gate,
        "reason": (
            "held until the contact window opens"
            if held
            else (
                "proactive updates are switched off, so only reactive contact applies"
                if not proactive_updates
                else service_gate["reason"]
            )
        ),
    }


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def update_user_preferences(
    db: AsyncSession,
    user_id: int,
    preferences: Optional[dict[str, Any]] = None,
    consents: Optional[dict[str, Any]] = None,
    *,
    recorded_by_id: Optional[int] = None,
    note: str = "",
) -> dict[str, Any]:
    """Apply preference and/or consent changes. Returns what changed and what did not.

    Partial success is reported, not raised: an unknown preference key and a
    valid one in the same request both land, and the caller is told which. A
    write that validated the whole batch and rejected all of it would make the
    settings screen brittle in a way the customer cannot fix.

    Every consent *change* appends a ``UserConsentEvent``. A change that
    submits the value already stored is **not** an event: a consent log that
    records a re-confirmation as a fresh grant cannot answer "when did this
    customer first consent", which is the question the log exists for.
    """
    profile = await get_or_create_preference_profile(db, user_id)
    current_prefs, current_consents = _profile_maps(profile)

    updated_preferences: list[str] = []
    updated_consents: list[str] = []
    errors: list[str] = []
    blocked: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for raw_key, value in (preferences or {}).items():
        key = str(raw_key)
        spec = PREFERENCE_BY_KEY.get(key)
        if spec is None:
            errors.append(f"unknown preference {key!r}; not in the catalog")
            continue
        coerced, error = _coerce_preference_value(spec, value)
        if error is not None:
            errors.append(error)
            continue
        if current_prefs.get(key) == coerced:
            continue
        current_prefs[key] = coerced
        updated_preferences.append(key)

    for raw_purpose, value in (consents or {}).items():
        name = str(raw_purpose)
        spec = CONSENT_PURPOSE_BY_NAME.get(name)
        if spec is None:
            errors.append(f"unknown consent purpose {name!r}; not in CONSENT_PURPOSES")
            continue
        if spec.get("required") and not bool(value):
            blocked.append(
                {
                    "purpose": name,
                    "requested": False,
                    "granted": True,
                    "reason": str(spec["description"]),
                }
            )
            continue
        granted = bool(value)
        if current_consents.get(name) == granted:
            continue
        current_consents[name] = granted
        updated_consents.append(name)
        db.add(
            models.UserConsentEvent(
                user_id=int(user_id),
                purpose=name,
                granted=granted,
                version=str(
                    getattr(profile, "consent_version", PREFERENCE_CATALOG_VERSION)
                ),
                lawful_basis=str(spec.get("lawful_basis", "legitimate_interest")),
                recorded_by_id=recorded_by_id,
                note=note,
            )
        )

    if updated_preferences or updated_consents or blocked:
        profile.preferences_json = json.dumps(
            {
                key: value
                for key, value in current_prefs.items()
                if key in PREFERENCE_BY_KEY
            },
            sort_keys=True,
        )
        profile.consents_json = json.dumps(
            {
                key: value
                for key, value in current_consents.items()
                if key in CONSENT_PURPOSE_BY_NAME
            },
            sort_keys=True,
        )
        profile.updated_at = now
        await db.commit()
        await db.refresh(profile)
    else:
        # Nothing to persist. Still commit so a freshly created profile row is
        # not left pending in the session for the caller to trip over.
        await db.commit()

    return {
        "generated_at": now,
        "user_id": int(user_id),
        "updated_preferences": updated_preferences,
        "updated_consents": updated_consents,
        "blocked_consents": blocked,
        "errors": errors,
        "preferences": current_prefs,
        "consents": current_consents,
        "consent_version": PREFERENCE_CATALOG_VERSION,
        "summary": (
            f"{len(updated_preferences)} preference(s) and "
            f"{len(updated_consents)} consent(s) updated"
            + (f"; {len(errors)} rejected" if errors else "")
            + (f"; {len(blocked)} required consent(s) refused" if blocked else "")
        ),
    }


async def build_preference_consent_report(
    db: AsyncSession, user_id: int
) -> dict[str, Any]:
    """The full preference + consent surface for one user."""
    preferences, consents = await load_user_preferences(db, user_id)
    catalog = preference_catalog()
    contact_plan = effective_contact_plan(preferences, consents)
    preference_rows = [
        {
            "key": str(row["key"]),
            "category": str(row["category"]),
            "label": str(row["label"]),
            "description": str(row["description"]),
            "type": str(row["type"]),
            "required": bool(row.get("required", False)),
            "options": list(row.get("options", []) or []) or None,
            "value": preferences.get(str(row["key"])),
            "set": str(row["key"]) in preferences,
            "default": row.get("default"),
            "at_default": preferences.get(str(row["key"])) == row.get("default"),
        }
        for row in PREFERENCE_CATALOG
    ]
    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": int(user_id),
        "catalog_version": PREFERENCE_CATALOG_VERSION,
        "preferences": preference_rows,
        "consents": consent_report(consents)["purposes"],
        "consent": consent_report(consents),
        "categories": catalog["categories"],
        "contact_plan": contact_plan,
        "declared_keys": len(PREFERENCE_CATALOG),
        "set_keys": sum(1 for row in preference_rows if row["set"]),
        "summary": (
            f"{sum(1 for row in preference_rows if row['set'])}/{len(preference_rows)} "
            f"preference(s) set; contact via {contact_plan['channel']['channel'] or 'n/a'}"
            f" at {contact_plan['frequency']} cadence"
        ),
    }


async def list_consent_events(
    db: AsyncSession, user_id: int, limit: int = 50
) -> dict[str, Any]:
    """This user's consent history, newest first. The provable trail."""
    result = await db.execute(
        select(models.UserConsentEvent)
        .where(models.UserConsentEvent.user_id == int(user_id))
        .order_by(desc(models.UserConsentEvent.created_at))
        .limit(max(1, int(limit)))
    )
    rows = list(result.scalars().all())
    events = [
        {
            "id": int(getattr(row, "id", 0) or 0),
            "purpose": str(getattr(row, "purpose", "")),
            "granted": bool(getattr(row, "granted", False)),
            "version": str(getattr(row, "version", "")),
            "lawful_basis": str(getattr(row, "lawful_basis", "")),
            "recorded_by_id": getattr(row, "recorded_by_id", None),
            "note": str(getattr(row, "note", "") or ""),
            "recorded_at": getattr(row, "created_at", None),
        }
        for row in rows
    ]
    purposes: dict[str, dict[str, Any]] = {}
    for event in events:
        entry = purposes.setdefault(
            event["purpose"],
            {
                "purpose": event["purpose"],
                "events": 0,
                "grants": 0,
                "revocations": 0,
                "first_at": None,
                "last_at": None,
            },
        )
        entry["events"] += 1
        entry["grants"] += int(bool(event["granted"]))
        entry["revocations"] += int(not event["granted"])
        stamp = event["recorded_at"]
        if stamp is not None:
            if entry["first_at"] is None or stamp < entry["first_at"]:
                entry["first_at"] = stamp
            if entry["last_at"] is None or stamp > entry["last_at"]:
                entry["last_at"] = stamp
    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": int(user_id),
        "limit": int(limit),
        "total": len(events),
        "events": events,
        "by_purpose": dict(sorted(purposes.items())),
        "summary": (
            f"{len(events)} consent event(s) across {len(purposes)} purpose(s)"
            if events
            else "no consent changes recorded; all purposes are at their shipped defaults"
        ),
    }
