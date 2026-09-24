"""Communication-strategy resolution: how to talk to a user.

Hypothesis
----------
The "right way to communicate" with a customer is not one formula — it is a
precedence ladder of criteria that get progressively more personal:

1. admin select       — an admin has explicitly hand-picked a communication
                        profile for this user (stored in the
                        `communication_overrides` table). Strongest signal.
2. policy             — policy-defined rules that must win regardless of the
                        user (compliance, sensitive complaints, critical churn,
                        dormancy re-entry, cancellation recovery).
3. culture            — culture/locale adaptation (warmth, formality, channel)
                        for the market the user operates in.
4. user profile       — the user's profile including the collected history
                        stats (lifecycle stage, value tier, churn risk, loyalty
                        score, journey family, booking counts, ...).
5. session mood       — the user's mood present in the current session
                        (sentiment + keyword cues from recent messages).
6. default            — neutral, universally safe fallback.

The resolver walks the ladder top-down and the first criterion that decisively
matches wins. Every resolution returns the full decision trail so callers can
see *why* a tone/channel/framing was chosen.

Implementation
--------------
Rules are config tables, not branches:

- `COMMUNICATION_OVERRIDE_CATALOG` - profiles an admin may select.
- `COMMUNICATION_POLICY_RULES`     - policy `when`-DSL rules.
- `COMMUNICATION_CULTURE_RULES`    - locale-keyed culture profiles.
- `COMMUNICATION_PROFILE_RULES`    - `when`-DSL rules over the user context.
- `COMMUNICATION_MOOD_RULES`       - mood-keyed session rules.
- `COMMUNICATION_MOOD_CUES`        - keyword cues used by mood detection.

Adding a profile, rule, culture, mood, or cue is a config change only.
`/meta/scoring-catalog` exposes the live catalogs.
"""
from __future__ import annotations

import operator
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    CommunicationAdminStrategyItem,
    CommunicationAdminStrategyReport,
    CommunicationAdminOverrideItem,
    CommunicationLayerDecision,
    CommunicationOverrideReport,
    CommunicationStrategyParams,
    CommunicationStrategyResult,
)
from app.services.chat_analytics import (
    analyze_sentiment,
    build_churn_prediction,
    build_summary,
    load_user_interaction_window,
)
from app.services.loyalty_journey import (
    build_journey_context,
    load_history_totals,
    match_loyalty_scenarios,
)

# ---------------------------------------------------------------------------
# Config tables (data-driven rule engines)
# ---------------------------------------------------------------------------

_COMMUNICATION_LAYERS: list[dict[str, Any]] = [
    {"layer": "admin_select", "precedence": 1, "criterion": "admin-selected communication profile for the user"},
    {"layer": "policy", "precedence": 2, "criterion": "policy-defined rules (compliance, safety, service levels)"},
    {"layer": "culture", "precedence": 3, "criterion": "culture/locale adaptation rules"},
    {"layer": "user_profile", "precedence": 4, "criterion": "user profile including collected history stats"},
    {"layer": "session_mood", "precedence": 5, "criterion": "user mood present in the current session"},
    {"layer": "default", "precedence": 6, "criterion": "neutral, universally safe fallback"},
]

COMMUNICATION_OVERRIDE_CATALOG: list[dict[str, Any]] = [
    {
        "profile_id": "standard_helpful",
        "label": "Standard helpful",
        "tone": "helpful",
        "channel": "in_app",
        "formality": "neutral",
        "framing": "clarity",
        "greeting_style": "personal",
        "reply_urgency": "normal",
        "description": "Balanced default for everyday service conversations.",
    },
    {
        "profile_id": "premium_concierge",
        "label": "Premium concierge",
        "tone": "appreciative",
        "channel": "personal_rep",
        "formality": "polished",
        "framing": "expansion",
        "greeting_style": "full_name",
        "reply_urgency": "normal",
        "description": "White-glove tone for high-value accounts.",
    },
    {
        "profile_id": "quiet_email",
        "label": "Quiet email",
        "tone": "concise",
        "channel": "email",
        "formality": "formal",
        "framing": "summary",
        "greeting_style": "none",
        "reply_urgency": "low",
        "description": "Low-touch scheduled updates for customers who prefer distance.",
    },
    {
        "profile_id": "escalation_hot",
        "label": "Escalation hot",
        "tone": "urgent_care",
        "channel": "immediate_call",
        "formality": "direct",
        "framing": "fast_resolution",
        "greeting_style": "none",
        "reply_urgency": "immediate",
        "description": "Supervisor-selected urgent personal outreach.",
    },
    {
        "profile_id": "senior_care",
        "label": "Senior care",
        "tone": "patient",
        "channel": "phone",
        "formality": "warm",
        "framing": "simplify",
        "greeting_style": "personal",
        "reply_urgency": "normal",
        "description": "Extra patience and simple language for senior callers.",
    },
    {
        "profile_id": "experiment_a",
        "label": "Experiment A tone",
        "tone": "upbeat",
        "channel": "in_app",
        "formality": "casual",
        "framing": "delight",
        "greeting_style": "first_name",
        "reply_urgency": "normal",
        "description": "A/B test variant for delight framing.",
    },
]

COMMUNICATION_POLICY_RULES: list[dict[str, Any]] = [
    {
        "policy_id": "sensitive_complaint",
        "label": "Sensitive complaint handling",
        "priority": "high",
        "when": {
            "top_issue_1": [
                "refund request",
                "complaints",
                "billing dispute",
                "privacy concern",
            ]
        },
        "params": {
            "tone": "empathic",
            "channel": "email_followup",
            "formality": "polished",
            "framing": "resolution_first",
            "greeting_style": "personal",
            "reply_urgency": "high",
        },
        "when_hint": "A sensitive top issue (refund, complaint, billing, privacy) is present.",
    },
    {
        "policy_id": "critical_risk_outreach",
        "label": "Critical churn outreach",
        "priority": "high",
        "when": {"risk_level": "critical"},
        "params": {
            "tone": "urgent_care",
            "channel": "immediate_call",
            "formality": "direct",
            "framing": "fast_resolution",
            "greeting_style": "none",
            "reply_urgency": "immediate",
        },
        "when_hint": "Churn prediction risk level is critical.",
    },
    {
        "policy_id": "dormancy_reentry",
        "label": "Dormant re-entry policy",
        "priority": "medium",
        "when": {"dormant": True},
        "params": {
            "tone": "warm",
            "channel": "email",
            "formality": "warm",
            "framing": "retention",
            "greeting_style": "first_name",
            "reply_urgency": "normal",
        },
        "when_hint": "Customer has history but went quiet.",
    },
    {
        "policy_id": "cancellation_recovery",
        "label": "Cancellation recovery",
        "priority": "medium",
        "when": {"cancelled_bookings": {"gte": 1}, "completed_bookings": {"lte": 0}},
        "params": {
            "tone": "assurance",
            "channel": "email_followup",
            "formality": "neutral",
            "framing": "assurance",
            "greeting_style": "personal",
            "reply_urgency": "high",
        },
        "when_hint": "A booking was cancelled and no service was ever completed.",
    },
]

COMMUNICATION_CULTURE_RULES: list[dict[str, Any]] = [
    {
        "culture_id": "latam_warm",
        "label": "Latin America warm",
        "locale": "es_MX",
        "params": {
            "tone": "warm",
            "channel": "in_app",
            "formality": "informal",
            "framing": "relationship",
            "greeting_style": "first_name",
            "reply_urgency": "normal",
        },
        "when_hint": "Warm personal framing for Spanish-speaking markets.",
    },
    {
        "culture_id": "emea_formal",
        "label": "EMEA formal",
        "locale": "de_DE",
        "params": {
            "tone": "professional",
            "channel": "email",
            "formality": "formal",
            "framing": "structured",
            "greeting_style": "formal_title",
            "reply_urgency": "normal",
        },
        "when_hint": "Formal structured tone for DACH markets.",
    },
    {
        "culture_id": "jp_polite",
        "label": "Japan polite",
        "locale": "ja_JP",
        "params": {
            "tone": "patient",
            "channel": "email",
            "formality": "polished",
            "framing": "structured",
            "greeting_style": "formal_title",
            "reply_urgency": "normal",
        },
        "when_hint": "High-politeness framing for the Japanese market.",
    },
    {
        "culture_id": "global_clear",
        "label": "Global neutral",
        "locale": "global",
        "fallback": True,
        "params": {
            "tone": "helpful",
            "channel": "in_app",
            "formality": "neutral",
            "framing": "clarity",
            "greeting_style": "personal",
            "reply_urgency": "normal",
        },
        "when_hint": "Universally safe neutral defaults.",
    },
]

COMMUNICATION_PROFILE_RULES: list[dict[str, Any]] = [
    {
        "profile_rule_id": "new_customer_onboarding",
        "label": "New customer encouragement",
        "when": {"stage": "new"},
        "params": {
            "tone": "encouraging",
            "channel": "in_app",
            "formality": "warm",
            "framing": "first_value",
            "greeting_style": "first_name",
            "reply_urgency": "normal",
        },
        "when_hint": "New customer with little or no history.",
    },
    {
        "profile_rule_id": "premium_expansion",
        "label": "Premium expansion",
        "when": {"stage": "loyal", "value_tier": "premium"},
        "params": {
            "tone": "appreciative",
            "channel": "personal_rep",
            "formality": "polished",
            "framing": "expansion",
            "greeting_style": "full_name",
            "reply_urgency": "normal",
        },
        "when_hint": "Loyal premium customer ready for expansion.",
    },
    {
        "profile_rule_id": "high_churn_support",
        "label": "High churn support",
        "when": {"churn_risk": "high"},
        "params": {
            "tone": "supportive",
            "channel": "email_followup",
            "formality": "neutral",
            "framing": "retention",
            "greeting_style": "first_name",
            "reply_urgency": "high",
        },
        "when_hint": "High churn risk on the loyalty summary.",
    },
    {
        "profile_rule_id": "trust_rebuild",
        "label": "Trust rebuild",
        "when": {"journey_family": "trust"},
        "params": {
            "tone": "assurance",
            "channel": "email_followup",
            "formality": "neutral",
            "framing": "assurance",
            "greeting_style": "personal",
            "reply_urgency": "high",
        },
        "when_hint": "Journey is in the trust family (cancelled without history).",
    },
    {
        "profile_rule_id": "monetization_ready",
        "label": "Monetization-ready expansion",
        "when": {"journey_family": "monetization", "monetization_readiness": {"gte": 80.0}},
        "params": {
            "tone": "appreciative",
            "channel": "in_app",
            "formality": "warm",
            "framing": "expansion",
            "greeting_style": "first_name",
            "reply_urgency": "normal",
        },
        "when_hint": "Expansion-ready loyal customer with strong readiness.",
    },
    {
        "profile_rule_id": "active_engaged",
        "label": "Active engaged",
        "when": {"stage": "engaged"},
        "params": {
            "tone": "helpful",
            "channel": "in_app",
            "formality": "neutral",
            "framing": "clarity",
            "greeting_style": "first_name",
            "reply_urgency": "normal",
        },
        "when_hint": "Engaged customer in the current window.",
    },
]

COMMUNICATION_MOOD_RULES: list[dict[str, Any]] = [
    {
        "mood_id": "frustrated",
        "label": "Frustrated",
        "params": {
            "tone": "apologetic",
            "channel": "in_app",
            "formality": "neutral",
            "framing": "deescalation",
            "greeting_style": "personal",
            "reply_urgency": "immediate",
        },
        "when_hint": "Session shows frustration cues or negative sentiment.",
    },
    {
        "mood_id": "urgent",
        "label": "Urgent",
        "params": {
            "tone": "action_oriented",
            "channel": "immediate_call",
            "formality": "direct",
            "framing": "fast_resolution",
            "greeting_style": "none",
            "reply_urgency": "immediate",
        },
        "when_hint": "Session shows urgency cues (asap, deadline, right now).",
    },
    {
        "mood_id": "confused",
        "label": "Confused",
        "params": {
            "tone": "patient",
            "channel": "in_app",
            "formality": "neutral",
            "framing": "clarify",
            "greeting_style": "first_name",
            "reply_urgency": "normal",
        },
        "when_hint": "Session shows confusion cues.",
    },
    {
        "mood_id": "happy",
        "label": "Happy",
        "params": {
            "tone": "upbeat",
            "channel": "in_app",
            "formality": "casual",
            "framing": "delight",
            "greeting_style": "first_name",
            "reply_urgency": "normal",
        },
        "when_hint": "Session shows positive mood cues.",
    },
    {
        "mood_id": "neutral",
        "label": "Neutral",
        "params": {
            "tone": "helpful",
            "channel": "in_app",
            "formality": "neutral",
            "framing": "clarity",
            "greeting_style": "personal",
            "reply_urgency": "normal",
        },
        "when_hint": "No strong mood cues in the session.",
    },
]

COMMUNICATION_MOOD_CUES: dict[str, tuple[str, ...]] = {
    "frustrated": (
        "angry",
        "furious",
        "unacceptable",
        "worst",
        "frustrated",
        "frustrating",
        "ridiculous",
        "never again",
        "horrible",
        "fed up",
        "disappointed",
        "terrible",
        "awful",
    ),
    "urgent": (
        "asap",
        "immediately",
        "urgent",
        "emergency",
        "right now",
        "deadline",
        "expired",
        "can't wait",
        "hurry",
    ),
    "confused": (
        "confused",
        "don't understand",
        "do not understand",
        "unclear",
        "confusing",
        "help me understand",
        "how does",
        "what does this mean",
    ),
    "happy": (
        "amazing",
        "great",
        "love",
        "excellent",
        "thank you so much",
        "perfect",
        "happy",
        "awesome",
        "fantastic",
        "delighted",
    ),
}

_MOOD_ORDER: tuple[str, ...] = ("frustrated", "urgent", "confused", "happy")

_GUIDANCE_BY_TONE: dict[str, str] = {
    "apologetic": "Open with a genuine apology; own the issue before explaining anything else.",
    "empathic": "Acknowledge how the customer likely feels before listing next steps.",
    "urgent_care": "Signal clearly that this issue is being handled with priority.",
    "patient": "Keep sentences short and simple; check understanding early.",
    "warm": "Use a personal, friendly opening; avoid boilerplate language.",
    "encouraging": "Highlight early progress and the next easy win for the customer.",
    "appreciative": "Thank the customer explicitly for their loyalty and history.",
    "supportive": "Reassure the customer that support is close and available.",
    "action_oriented": "Lead with the concrete action being taken right now.",
    "upbeat": "Keep the tone positive and forward-looking.",
    "concise": "Prefer the shortest accurate answer; no filler.",
    "professional": "Keep structure formal and on-brand at all times.",
    "assurance": "State transparent commitments and exactly what happens next.",
    "helpful": "Answer directly first, then offer follow-up options.",
}

_GUIDANCE_BY_FRAMING: dict[str, str] = {
    "first_value": "Push the customer toward completing their very first service.",
    "retention": "Focus the message on keeping the relationship, not selling.",
    "deescalation": "Offer one concrete next step and a human fallback; avoid blame.",
    "fast_resolution": "Give a timeline for resolution and a direct escalation path.",
    "expansion": "Frame offers around continued success with the service.",
    "clarify": "Ask one clear question before proceeding.",
    "delight": "Offer a small unexpected value or a personal touch.",
    "resolution_first": "Fix the core problem before suggesting anything else.",
    "assurance": "Restate commitments clearly and follow up with proof.",
    "clarity": "Restate the key point in plain language.",
    "simplify": "Break instructions into numbered, minimal steps.",
    "relationship": "Emphasize the human relationship, not the transaction.",
    "summary": "Send a short recap with the most important facts up top.",
    "structured": "Use clear sections and explicit next steps.",
}

_NUMERIC_OPS: dict[str, Any] = {
    "gte": operator.ge,
    "gt": operator.gt,
    "lte": operator.le,
    "lt": operator.lt,
    "eq": operator.eq,
    "ne": operator.ne,
}

_DEFAULT_PARAMS: dict[str, str] = {
    "tone": "helpful",
    "channel": "in_app",
    "formality": "neutral",
    "framing": "clarity",
    "greeting_style": "personal",
    "reply_urgency": "normal",
}

# ---------------------------------------------------------------------------
# Condition DSL (mirrors the loyalty-journey engine; dict-based context)
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return value


def _matches_rule(rule_value: Any, context_value: Any) -> bool:
    if isinstance(rule_value, dict):
        for op_name, threshold in rule_value.items():
            op = _NUMERIC_OPS.get(op_name)
            if op is None or context_value is None:
                return False
            try:
                if not op(context_value, threshold):
                    return False
            except TypeError:
                return False
        return True
    if isinstance(context_value, (list, tuple, set)):
        return _json_safe(rule_value) in list(context_value)
    if isinstance(rule_value, (list, tuple, set, frozenset)):
        return context_value in rule_value
    if isinstance(rule_value, bool):
        return bool(context_value) == rule_value
    return context_value == rule_value


def evaluate_when(when: dict[str, object], context: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Evaluate every condition in `when` against the context dict (AND).

    Unknown fields fail the whole rule (fail-safe) so catalog typos surface in
    self-check tests instead of silently never matching.
    """
    matched_fields: dict[str, Any] = {}
    for field_name, rule_value in when.items():
        if field_name not in context:
            return False, {}
        context_value = context[field_name]
        if not _matches_rule(rule_value, context_value):
            return False, {}
        matched_fields[field_name] = _json_safe(context_value)
    return True, matched_fields


# ---------------------------------------------------------------------------
# Session mood detection
# ---------------------------------------------------------------------------


def detect_session_messages_mood(messages: Optional[list[str]]) -> Optional[dict[str, Any]]:
    """Classify the user's mood from the messages in the current session.

    Priority: keyword-cue buckets > negative sentiment -> frustrated >
    positive sentiment -> happy > neutral. Returns None when there are no
    messages to judge by.
    """
    msgs = [str(m or "") for m in (messages or []) if str(m or "").strip()]
    if not msgs:
        return None
    hits: dict[str, int] = {}
    for mood in _MOOD_ORDER:
        cues = COMMUNICATION_MOOD_CUES.get(mood, ())
        count = 0
        for message in msgs:
            lowered = message.lower()
            count += sum(1 for cue in cues if cue in lowered)
        if count:
            hits[mood] = count
    sentiment = analyze_sentiment(msgs[-1])
    if sentiment is None:
        sentiment_label, sentiment_score = "neutral", 0.0
    else:
        sentiment_label, sentiment_score = sentiment.label, sentiment.score
    if hits:
        label = max(hits, key=lambda mood: (hits[mood], -_MOOD_ORDER.index(mood)))
    elif sentiment_label == "negative":
        label = "frustrated"
    elif sentiment_label == "positive":
        label = "happy"
    else:
        label = "neutral"
    confidence = min(0.5 + sum(hits.values()) * 0.15, 0.98) if hits else 0.4
    return {
        "label": label,
        "score": round(float(sentiment_score), 2),
        "cue_hits": dict(hits),
        "top_cue": max(hits, key=hits.get) if hits else "",
        "sentiment_label": sentiment_label,
        "confidence": round(float(confidence), 2),
    }


# ---------------------------------------------------------------------------
# Strategy resolution (pure)
# ---------------------------------------------------------------------------


def _profile_params(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "tone": str(entry.get("tone", _DEFAULT_PARAMS["tone"])),
        "channel": str(entry.get("channel", _DEFAULT_PARAMS["channel"])),
        "formality": str(entry.get("formality", _DEFAULT_PARAMS["formality"])),
        "framing": str(entry.get("framing", _DEFAULT_PARAMS["framing"])),
        "greeting_style": str(entry.get("greeting_style", _DEFAULT_PARAMS["greeting_style"])),
        "reply_urgency": str(entry.get("reply_urgency", _DEFAULT_PARAMS["reply_urgency"])),
    }


def _layer_decision(layer: str, precedence: int, matched: bool, reason: str) -> dict[str, Any]:
    return {"layer": layer, "precedence": precedence, "matched": bool(matched), "reason": str(reason)}


def _guidance_for(params: dict[str, str]) -> list[str]:
    guidance = []
    tone_line = _GUIDANCE_BY_TONE.get(params.get("tone", ""))
    if tone_line:
        guidance.append(tone_line)
    framing_line = _GUIDANCE_BY_FRAMING.get(params.get("framing", ""))
    if framing_line:
        guidance.append(framing_line)
    return guidance


def resolve_communication_strategy(
    context: dict[str, Any],
    *,
    admin_override: Optional[dict[str, Any]] = None,
    locale: str = "global",
    mood: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Resolve the communication strategy by walking the precedence ladder.

    Highest-precedence criterion that matches wins (admin select -> policy ->
    culture -> user profile -> session mood -> default). The decision trail
    records every layer evaluated so callers can explain the choice.
    """
    context = dict(context or {})
    trail: list[dict[str, Any]] = []

    # 1) admin select
    if admin_override is not None:
        profile = _catalog_profile(str(admin_override.get("profile_id", "")))
        trail.append(
            _layer_decision(
                "admin_select",
                1,
                True,
                f"Admin-selected profile '{profile['profile_id']}' ({profile['label']})",
            )
        )
        return _strategy_payload(
            resolved_layer="admin_select",
            precedence=1,
            layer_label=f"Admin select · {profile['label']}",
            strategy_id=profile["profile_id"],
            params=_profile_params(profile),
            matched_rule={
                "layer": "admin_select",
                "id": profile["profile_id"],
                "label": profile["label"],
                "priority": "high",
                "when_hint": "Explicitly selected by an administrator.",
            },
            context=context,
            mood=mood,
            trail=trail,
        )
    trail.append(_layer_decision("admin_select", 1, False, "No admin-selected override for this user."))

    # 2) policy defined
    for rule in COMMUNICATION_POLICY_RULES:
        ok, fields = evaluate_when(dict(rule["when"]), context)
        if ok:
            trail.append(
                _layer_decision(
                    "policy", 2, True, f"Policy '{rule['policy_id']}' matched: {rule.get('when_hint', '')}"
                )
            )
            return _strategy_payload(
                resolved_layer="policy",
                precedence=2,
                layer_label=f"Policy · {rule.get('label', rule['policy_id'])}",
                strategy_id=str(rule["policy_id"]),
                params=_profile_params(rule["params"]),
                matched_rule={
                    "layer": "policy",
                    "id": rule["policy_id"],
                    "label": rule.get("label", rule["policy_id"]),
                    "priority": rule.get("priority", "medium"),
                    "when_hint": rule.get("when_hint", ""),
                    "matched_conditions": fields,
                },
                context=context,
                mood=mood,
                trail=trail,
            )
    trail.append(_layer_decision("policy", 2, False, "No policy rule matched the user state."))

    # 3) culture
    for rule in COMMUNICATION_CULTURE_RULES:
        if rule.get("fallback"):
            continue
        if rule.get("locale") != locale:
            continue
        extra = dict(rule.get("when") or {})
        if extra:
            ok, _ = evaluate_when(extra, context)
            if not ok:
                continue
        trail.append(
            _layer_decision(
                "culture", 3, True, f"Culture rule '{rule['culture_id']}' applies for locale '{locale}'."
            )
        )
        return _strategy_payload(
            resolved_layer="culture",
            precedence=3,
            layer_label=f"Culture · {rule.get('label', rule['culture_id'])}",
            strategy_id=str(rule["culture_id"]),
            params=_profile_params(rule["params"]),
            matched_rule={
                "layer": "culture",
                "id": rule["culture_id"],
                "label": rule.get("label", rule["culture_id"]),
                "priority": "medium",
                "when_hint": rule.get("when_hint", ""),
            },
            context=context,
            mood=mood,
            trail=trail,
        )
    trail.append(_layer_decision("culture", 3, False, f"No non-fallback culture rule for locale '{locale}'."))

    # 4) user profile (including collected history stats)
    for rule in COMMUNICATION_PROFILE_RULES:
        ok, fields = evaluate_when(dict(rule["when"]), context)
        if ok:
            trail.append(
                _layer_decision(
                    "user_profile",
                    4,
                    True,
                    f"Profile rule '{rule['profile_rule_id']}' matched: {rule.get('when_hint', '')}",
                )
            )
            return _strategy_payload(
                resolved_layer="user_profile",
                precedence=4,
                layer_label=f"User profile · {rule.get('label', rule['profile_rule_id'])}",
                strategy_id=str(rule["profile_rule_id"]),
                params=_profile_params(rule["params"]),
                matched_rule={
                    "layer": "user_profile",
                    "id": rule["profile_rule_id"],
                    "label": rule.get("label", rule["profile_rule_id"]),
                    "priority": rule.get("priority", "medium"),
                    "when_hint": rule.get("when_hint", ""),
                    "matched_conditions": fields,
                },
                context=context,
                mood=mood,
                trail=trail,
            )
    trail.append(_layer_decision("user_profile", 4, False, "No profile rule matched the user context."))

    # 5) session mood
    if mood is not None:
        rule = next(
            (r for r in COMMUNICATION_MOOD_RULES if r["mood_id"] == mood.get("label")),
            None,
        )
        if rule is not None:
            trail.append(
                _layer_decision(
                    "session_mood", 5, True, f"Session mood '{mood.get('label')}' detected."
                )
            )
            return _strategy_payload(
                resolved_layer="session_mood",
                precedence=5,
                layer_label=f"Session mood · {rule.get('label', mood.get('label', ''))}",
                strategy_id=str(rule["mood_id"]),
                params=_profile_params(rule["params"]),
                matched_rule={
                    "layer": "session_mood",
                    "id": rule["mood_id"],
                    "label": rule.get("label", rule["mood_id"]),
                    "priority": "medium",
                    "when_hint": rule.get("when_hint", ""),
                },
                context=context,
                mood=mood,
                trail=trail,
            )
    trail.append(_layer_decision("session_mood", 5, False, "No session mood detected (no messages)."))

    # 6) default fallback
    default_params = _default_culture_params()
    trail.append(_layer_decision("default", 6, True, "No higher-priority criterion matched; using neutral defaults."))
    return _strategy_payload(
        resolved_layer="default",
        precedence=6,
        layer_label="Default · Global neutral",
        strategy_id="default",
        params=default_params,
        matched_rule={
            "layer": "default",
            "id": "default",
            "label": "Global neutral",
            "priority": "low",
            "when_hint": "Universal safe fallback.",
        },
        context=context,
        mood=mood,
        trail=trail,
    )


def _strategy_payload(
    *,
    resolved_layer: str,
    precedence: int,
    layer_label: str,
    strategy_id: str,
    params: dict[str, str],
    matched_rule: dict[str, Any],
    context: dict[str, Any],
    mood: Optional[dict[str, Any]],
    trail: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "resolved_layer": resolved_layer,
        "precedence": precedence,
        "layer_label": layer_label,
        "profile_id": strategy_id,
        "params": params,
        "guidance": _guidance_for(params),
        "matched_rule": matched_rule,
        "mood": mood,
        "decision_trail": trail,
        "context_snapshot": _context_snapshot(context),
    }


def _context_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "stage",
        "churn_risk",
        "risk_level",
        "value_tier",
        "customer_classification",
        "journey_family",
        "sentiment_label",
        "dormant",
        "loyalty_score",
        "churn_risk_score",
        "monetization_readiness",
        "signal_strength",
        "chat_count",
        "booking_count",
        "completed_bookings",
        "pending_bookings",
        "cancelled_bookings",
        "top_issue",
        "days_since_last_activity",
        "locale",
    )
    return {key: _json_safe(context.get(key)) for key in keys if key in context}


def _catalog_profile(profile_id: str) -> dict[str, Any]:
    for profile in COMMUNICATION_OVERRIDE_CATALOG:
        if profile["profile_id"] == profile_id:
            return profile
    return {
        "profile_id": profile_id,
        "label": profile_id,
        "tone": _DEFAULT_PARAMS["tone"],
        "channel": _DEFAULT_PARAMS["channel"],
        "formality": _DEFAULT_PARAMS["formality"],
        "framing": _DEFAULT_PARAMS["framing"],
        "greeting_style": _DEFAULT_PARAMS["greeting_style"],
        "reply_urgency": _DEFAULT_PARAMS["reply_urgency"],
        "description": "Unknown profile; neutral defaults applied.",
    }


def _default_culture_params() -> dict[str, str]:
    for rule in COMMUNICATION_CULTURE_RULES:
        if rule.get("fallback"):
            return _profile_params(rule["params"])
    return dict(_DEFAULT_PARAMS)


# ---------------------------------------------------------------------------
# Admin override persistence
# ---------------------------------------------------------------------------


def _override_item_dict(row: Any, username: Optional[str]) -> dict[str, Any]:
    catalog = {p["profile_id"]: p for p in COMMUNICATION_OVERRIDE_CATALOG}
    profile_id = str(getattr(row, "profile_id", "") or "")
    profile = catalog.get(profile_id)
    return {
        "user_id": int(getattr(row, "user_id", 0) or 0),
        "username": username or "",
        "profile_id": profile_id,
        "profile_label": profile["label"] if profile else profile_id,
        "note": str(getattr(row, "note", "") or ""),
        "set_by_admin_id": int(getattr(row, "set_by_admin_id", 0) or 0),
        "created_at": getattr(row, "created_at", None),
        "updated_at": getattr(row, "updated_at", None),
    }


async def load_admin_override(db: AsyncSession, user_id: int) -> Optional[dict[str, Any]]:
    result = await db.execute(
        select(models.UserCommunicationOverride).where(
            models.UserCommunicationOverride.user_id == user_id
        )
    )
    row = result.scalars().first()
    if row is None:
        return None
    return _override_item_dict(row, username=None)


async def _load_override_row(db: AsyncSession, user_id: int):
    result = await db.execute(
        select(models.UserCommunicationOverride).where(
            models.UserCommunicationOverride.user_id == user_id
        )
    )
    return result.scalars().first()


async def set_communication_admin_override(
    db: AsyncSession,
    user_id: int,
    profile_id: str,
    admin_id: int,
    note: str = "",
) -> dict[str, Any]:
    known = {p["profile_id"] for p in COMMUNICATION_OVERRIDE_CATALOG}
    if profile_id not in known:
        raise ValueError(f"Unknown communication profile: {profile_id}")
    existing = await _load_override_row(db, user_id)
    if existing is not None:
        await db.delete(existing)
        await db.flush()
    override = models.UserCommunicationOverride(
        user_id=int(user_id),
        profile_id=str(profile_id),
        set_by_admin_id=int(admin_id or 0),
        note=str(note or ""),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(override)
    await db.commit()
    await db.refresh(override)
    return _override_item_dict(override, username=None)


async def delete_communication_admin_override(db: AsyncSession, user_id: int) -> bool:
    row = await _load_override_row(db, user_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def list_communication_admin_overrides(
    db: AsyncSession, limit: int = 50
) -> CommunicationOverrideReport:
    result = await db.execute(
        select(models.UserCommunicationOverride)
        .order_by(desc(models.UserCommunicationOverride.updated_at))
        .limit(max(1, int(limit)))
    )
    rows = result.scalars().all()
    user_result = await db.execute(select(models.User.id, models.User.username))
    names = {int(row[0]): str(row[1]) for row in user_result.all()}
    items = [
        _override_item_dict(row, names.get(getattr(row, "user_id", None))) for row in rows
    ]
    return CommunicationOverrideReport(
        generated_at=datetime.now(timezone.utc),
        total=len(items),
        overrides=[CommunicationAdminOverrideItem(**item) for item in items],
    )


# ---------------------------------------------------------------------------
# User-context loading
# ---------------------------------------------------------------------------


async def _load_user_strategy_inputs(
    db: AsyncSession, user_id: int, window_days: int
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    chat_rows, bookings = await load_user_interaction_window(
        db, user_id, window_days, chat_limit=50, booking_limit=50
    )
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
    churn_prediction = await build_churn_prediction(db, user_id, window_days)
    totals = await load_history_totals(db, user_id)
    journey = build_journey_context(
        summary,
        churn_prediction,
        chat_rows,
        bookings,
        latest_sentiment,
        total_chat_count=int(totals["total_chat_count"]),
        total_booking_count=int(totals["total_booking_count"]),
        latest_chat_at=totals["latest_chat_at"],
        latest_booking_at=totals["latest_booking_at"],
    )
    matched = match_loyalty_scenarios(journey)
    family = str(matched[0]["family"]) if matched else "none"
    metadata = getattr(summary, "metadata", None) or {}
    try:
        signal_strength = float(metadata.get("signal_strength", 0.0) or 0.0)
    except (TypeError, ValueError):
        signal_strength = 0.0
    top_issues = list(getattr(summary, "top_issues", []) or [])
    context: dict[str, Any] = {
        "stage": str(journey.lifecycle_stage),
        "churn_risk": str(journey.churn_risk),
        "risk_level": str(journey.risk_level),
        "value_tier": str(journey.value_tier),
        "customer_classification": str(journey.customer_classification),
        "journey_family": family,
        "sentiment_label": str(journey.sentiment_label),
        "at_risk": bool(journey.at_risk),
        "has_history": bool(journey.has_history),
        "dormant": bool(journey.dormant),
        "chat_count": int(journey.chat_count),
        "booking_count": int(journey.booking_count),
        "completed_bookings": int(journey.completed_bookings),
        "confirmed_bookings": int(journey.confirmed_bookings),
        "pending_bookings": int(journey.pending_bookings),
        "cancelled_bookings": int(journey.cancelled_bookings),
        "loyalty_score": float(journey.loyalty_score),
        "monetization_readiness": float(journey.monetization_readiness),
        "churn_risk_score": float(journey.risk_score),
        "signal_strength": round(signal_strength, 2),
        "top_issue_1": top_issues[0] if len(top_issues) > 0 else "",
        "top_issue_2": top_issues[1] if len(top_issues) > 1 else "",
        "top_issue_3": top_issues[2] if len(top_issues) > 2 else "",
        "top_issue": top_issues[0] if top_issues else "",
        "days_since_last_activity": journey.days_since_last_activity,
    }
    mood = detect_session_messages_mood([r.message for r in chat_rows]) if chat_rows else None
    return context, mood


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def _result_model(
    *,
    user_id: int,
    user_name: str,
    window_days: int,
    locale: str,
    payload: dict[str, Any],
) -> CommunicationStrategyResult:
    return CommunicationStrategyResult(
        generated_at=datetime.now(timezone.utc),
        user_id=user_id,
        user_name=user_name,
        window_days=window_days,
        resolved_layer=str(payload["resolved_layer"]),
        precedence=int(payload["precedence"]),
        layer_label=str(payload["layer_label"]),
        profile_id=str(payload["profile_id"]),
        locale=locale,
        params=CommunicationStrategyParams(**payload["params"]),
        mood=payload.get("mood"),
        guidance=list(payload.get("guidance", [])),
        matched_rule=payload.get("matched_rule"),
        decision_trail=[CommunicationLayerDecision(**d) for d in payload.get("decision_trail", [])],
        context=payload.get("context_snapshot", {}),
    )


async def resolve_communication_strategy_for_user(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    locale: str = "global",
    user_name: str = "",
) -> CommunicationStrategyResult:
    context, mood = await _load_user_strategy_inputs(db, user_id, window_days)
    context["locale"] = locale or "global"
    override = await load_admin_override(db, user_id)
    payload = resolve_communication_strategy(
        context, admin_override=override, locale=locale or "global", mood=mood
    )
    return _result_model(
        user_id=user_id,
        user_name=user_name,
        window_days=window_days,
        locale=locale or "global",
        payload=payload,
    )


async def build_communication_strategy_admin_report(
    db: AsyncSession,
    window_days: int = 30,
    limit: int = 20,
    *,
    locale: str = "global",
    user_ids: Optional[list[int]] = None,
) -> CommunicationAdminStrategyReport:
    user_result = await db.execute(select(models.User.id, models.User.username))
    users = [(int(row[0]), str(row[1])) for row in user_result.all()]
    if user_ids is not None:
        wanted = {int(uid) for uid in user_ids}
        users = [(uid, uname) for uid, uname in users if uid in wanted]
    users = users[: max(1, int(limit))]

    items: list[CommunicationAdminStrategyItem] = []
    for user_id, username in users:
        context, mood = await _load_user_strategy_inputs(db, user_id, window_days)
        context["locale"] = locale or "global"
        override = await load_admin_override(db, user_id)
        payload = resolve_communication_strategy(
            context, admin_override=override, locale=locale or "global", mood=mood
        )
        items.append(
            CommunicationAdminStrategyItem(
                user_id=user_id,
                username=username,
                resolved_layer=str(payload["resolved_layer"]),
                profile_id=str(payload["profile_id"]),
                tone=str(payload["params"]["tone"]),
                channel=str(payload["params"]["channel"]),
                reply_urgency=str(payload["params"]["reply_urgency"]),
                loyalty_score=float(context.get("loyalty_score", 0.0) or 0.0),
                journey_family=str(context.get("journey_family", "") or ""),
                mood_label=str((payload.get("mood") or {}).get("label", "") or ""),
                precedence=int(payload["precedence"]),
            )
        )

    coverage = Counter(item.resolved_layer for item in items)
    top_layer = coverage.most_common(1)[0][0] if coverage else None
    return CommunicationAdminStrategyReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        limit=max(1, int(limit)),
        total_users=len(users),
        total_matched=len(items),
        coverage_by_layer=dict(coverage),
        top_layer=top_layer,
        users=items,
    )


def build_communication_strategy_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog`."""
    return {
        "catalog_version": "communication_strategy_v1",
        "precedence": [
            {"layer": entry["layer"], "precedence": entry["precedence"], "criterion": entry["criterion"]}
            for entry in _COMMUNICATION_LAYERS
        ],
        "override_catalog": [_json_safe(p) for p in COMMUNICATION_OVERRIDE_CATALOG],
        "policy_rules": [
            {**rule, "params": rule["params"]} for rule in COMMUNICATION_POLICY_RULES
        ],
        "culture_rules": [_json_safe(r) for r in COMMUNICATION_CULTURE_RULES],
        "profile_rules": [
            {**rule, "params": rule["params"]} for rule in COMMUNICATION_PROFILE_RULES
        ],
        "mood_rules": [_json_safe(r) for r in COMMUNICATION_MOOD_RULES],
        "mood_cues": {mood: list(cues) for mood, cues in COMMUNICATION_MOOD_CUES.items()},
        "params_fields": ["tone", "channel", "formality", "framing", "greeting_style", "reply_urgency"],
        "endpoints": {
            "self": "/chat/communication-strategy",
            "admin_report": "/chat/admin/communication-strategy",
            "overrides": "/chat/admin/communication-overrides",
        },
    }