"""Contextual adaptive risk evaluation rules.

Zero-trust access means every request is re-evaluated in context: is this a
proven device? has the user validated a biometric? is the geo/auth velocity
plausible? is the target sensitive? This module turns those signals into a
single risk score and level.

Design (deliberately config-driven, not rigid):

- ``RISK_RULES`` — a config table. Each rule declares the context keys that
  trigger it (with comparison operators) and a base weight. Adding/removing a
  signal is config-only.
- ``RISK_LEVELS`` — score thresholds mapping to low/moderate/high/critical.
- Weight adaptation — weights are *learned*, not hardcoded: after outcomes
  (fraud / granted / false_positive) arrive, ``adapt_risk_weight`` nudges a
  rule's weight within ``ADAPTIVE_PARAMS`` bounds and bumps the weight version
  consumed by the pipeline. Rules stay explainable at every step.
"""
from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Any

RISK_LEVELS: list[dict[str, Any]] = [
    {"level": "low", "max": 25},
    {"level": "moderate", "max": 55},
    {"level": "high", "max": 80},
    {"level": "critical", "max": 100},
]

# Context keys the evaluator understands (documentation + validation).
RISK_CONTEXT_KEYS = [
    "device_proven",  # bool — device previously attested
    "biometric_valid",  # bool — biometric vault accepted the probe
    "hsm_verified",  # bool — request envelope verified by the HSM signer
    "geo_velocity_kmh",  # number — implausible travel speed between last two auths
    "auth_velocity_per_min",  # number — distinct auth attempts per minute
    "odd_hour",  # bool — activity outside the user's normal hours
    "target_sensitivity",  # str — low | medium | high
    "ip_reputation",  # str — good | neutral | poor
]

# Config table: each rule declares which context signals make it fire and its
# base weight. ``match`` supports scalars, value lists, and (op, value) tuples.
RISK_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "new_device",
        "weight": 22,
        "match": {"device_proven": False},
        "reason": "request originates from an unproven device",
    },
    {
        "rule_id": "biometric_mismatch",
        "weight": 35,
        "match": {"biometric_valid": False},
        "reason": "biometric probe did not validate",
    },
    {
        "rule_id": "unverified_hsm",
        "weight": 18,
        "match": {"hsm_verified": False},
        "reason": "request envelope not signed by the HSM",
    },
    {
        "rule_id": "geo_velocity_spike",
        "weight": 28,
        "match": {"geo_velocity_kmh": (">", 800)},
        "reason": "impossible travel speed between successive authenticators",
    },
    {
        "rule_id": "auth_velocity_spike",
        "weight": 30,
        "match": {"auth_velocity_per_min": (">=", 5)},
        "reason": "burst of authentication attempts in a single minute",
    },
    {
        "rule_id": "off_hours",
        "weight": 10,
        "match": {"odd_hour": True},
        "reason": "activity outside the user's normal time window",
    },
    {
        "rule_id": "sensitive_target",
        "weight": 12,
        "match": {"target_sensitivity": ("==", "high")},
        "reason": "request targets a high-sensitivity resource",
    },
    {
        "rule_id": "poor_ip_reputation",
        "weight": 15,
        "match": {"ip_reputation": "poor"},
        "reason": "request from a poor-reputation network",
    },
]
_RULE_BY_ID = {rule["rule_id"]: rule for rule in RISK_RULES}

_OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

ADAPTIVE_PARAMS: dict[str, float] = {
    "step": 4.0,
    "min_weight": 5.0,
    "max_weight": 60.0,
}

# Learned weight state — starts as the config base weights, drifts with outcomes.
_WEIGHT_STATE: dict[str, float] = {rid: float(rule["weight"]) for rid, rule in _RULE_BY_ID.items()}
_WEIGHT_VERSION = 1


def current_weights() -> dict[str, float]:
    return dict(_WEIGHT_STATE)


def reset_weight_state() -> None:
    global _WEIGHT_VERSION
    _WEIGHT_STATE.update({rid: float(rule["weight"]) for rid, rule in _RULE_BY_ID.items()})
    _WEIGHT_VERSION += 1


def adapt_risk_weight(rule_id: str, outcome: str) -> dict[str, Any]:
    """Nudge a rule's learned weight after an observed outcome.

    ``outcome`` in ``{"fraud", "denied_breach", "granted", "false_positive"}``:
    positive signals raise the weight, clearing signals lower it, always within
    ``ADAPTIVE_PARAMS`` bounds. Returns the new weight + version.
    """
    global _WEIGHT_VERSION
    if rule_id not in _WEIGHT_STATE:
        raise ValueError(f"unknown risk rule: {rule_id}")
    step = ADAPTIVE_PARAMS["step"]
    if outcome in {"fraud", "denied_breach"}:
        delta = step
    elif outcome in {"granted", "false_positive"}:
        delta = -step
    else:
        delta = 0.0
    _WEIGHT_STATE[rule_id] = min(
        ADAPTIVE_PARAMS["max_weight"],
        max(ADAPTIVE_PARAMS["min_weight"], _WEIGHT_STATE[rule_id] + delta),
    )
    _WEIGHT_VERSION += 1
    return {"rule_id": rule_id, "weight": _WEIGHT_STATE[rule_id], "version": _WEIGHT_VERSION}


def _matches(rule: dict[str, Any], context: dict[str, Any]) -> bool:
    for key, spec in rule["match"].items():
        if key not in context:
            return False
        value = context[key]
        if isinstance(spec, tuple) and len(spec) == 2 and spec[0] in _OPS:
            op_name, expected = spec
            try:
                if not _OPS[op_name](value, expected):
                    return False
            except (TypeError, ValueError):
                return False
        elif isinstance(spec, (list, tuple)):
            if value not in spec:
                return False
        elif value != spec:
            return False
    return True


def level_for_score(score: int) -> str:
    for bucket in RISK_LEVELS:
        if score <= bucket["max"]:
            return bucket["level"]
    return RISK_LEVELS[-1]["level"]


def effective_risk_rules() -> list[dict[str, Any]]:
    """The ``RISK_RULES`` table with the current learned weights applied.

    This is the *effective* rule table (config + learned drift) — the canonical
    snapshot used to seed model registries, canary candidates, and simulations.
    Additive: does not mutate live weight state.
    """
    return [
        {**rule, "weight": _WEIGHT_STATE[rule["rule_id"]]}
        for rule in RISK_RULES
    ]


def score_with_config(
    rules: list[dict[str, Any]],
    context: dict[str, Any],
    levels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure, config-parameterized scoring — the basis for canary shadow runs and
    what-if simulation.

    Takes an arbitrary rules table (each entry mirrors ``RISK_RULES``:
    ``rule_id`` / ``weight`` / ``match`` / optional ``reason``) and returns the
    same shape as ``evaluate_risk`` — score, level, factor trail — without
    touching the live ``_WEIGHT_STATE``. ``levels`` defaults to ``RISK_LEVELS``.
    """
    factors = []
    for rule in rules:
        present = _matches(rule, context)
        factors.append(
            {
                "rule_id": rule["rule_id"],
                "weight": float(rule.get("weight", 0)),
                "present": present,
                "reason": rule.get("reason", ""),
            }
        )
    score = min(100, round(sum(f["weight"] for f in factors if f["present"])))
    buckets = levels if levels is not None else RISK_LEVELS
    level = buckets[-1]["level"] if buckets else RISK_LEVELS[-1]["level"]
    for bucket in buckets:
        if score <= bucket["max"]:
            level = bucket["level"]
            break
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "level": level,
        "factors": factors,
    }


def evaluate_risk(context: dict[str, Any]) -> dict[str, Any]:
    """Score a request context and return the risk decision + factor trail."""
    factors = []
    for rule in RISK_RULES:
        present = _matches(rule, context)
        factors.append(
            {
                "rule_id": rule["rule_id"],
                "weight": _WEIGHT_STATE[rule["rule_id"]],
                "present": present,
                "reason": rule["reason"],
            }
        )
    score = min(100, round(sum(f["weight"] for f in factors if f["present"])))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "level": level_for_score(score),
        "weight_version": _WEIGHT_VERSION,
        "factors": factors,
    }


def build_risk_evaluator_catalog() -> dict[str, object]:
    return {
        "levels": list(RISK_LEVELS),
        "context_keys": list(RISK_CONTEXT_KEYS),
        "rules": [
            {**rule, "current_weight": _WEIGHT_STATE[rule["rule_id"]]}
            for rule in RISK_RULES
        ],
        "adaptive": {**ADAPTIVE_PARAMS, "weight_version": _WEIGHT_VERSION},
    }