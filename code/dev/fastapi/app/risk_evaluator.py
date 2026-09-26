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
- ``RISK_ACTIONS`` — what a level means operationally (allow / step_up /
  review / deny) and which controls it requires.
- Signal hygiene — ``normalize_context`` reports the signals a caller never
  supplied. A rule only fires when its signal is present, so an omitted signal
  is a silent fail-open; ``context_gaps`` names them and
  ``evaluate_risk_decision(strict=True)`` refuses to score at all.
- Counterfactuals — ``cheapest_clearing_signal`` answers "which one signal
  would have changed the outcome?" without mutating live state.
- Weight history — every weight transition is snapshotted, so a stored decision
  can be re-scored at the weights that actually produced it.
- Weight adaptation — weights are *learned*, not hardcoded: after outcomes
  (fraud / granted / false_positive) arrive, ``adapt_risk_weight`` nudges a
  rule's weight within ``ADAPTIVE_PARAMS`` bounds and bumps the weight version
  consumed by the pipeline. Rules stay explainable at every step.
"""
from __future__ import annotations

import operator
from collections import deque
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

# Coarse type + benign default for every context key. The defaults are the
# *benign* reading, so a caller that simply omits a signal is treated as
# low-risk; ``context_gaps`` reports the omission so a strict caller can demand
# the signal instead of silently inheriting the default.
RISK_CONTEXT_TYPES: dict[str, str] = {
    "device_proven": "bool",
    "biometric_valid": "bool",
    "hsm_verified": "bool",
    "geo_velocity_kmh": "number",
    "auth_velocity_per_min": "number",
    "odd_hour": "bool",
    "target_sensitivity": "enum",
    "ip_reputation": "enum",
}
RISK_CONTEXT_DEFAULTS: dict[str, Any] = {
    "device_proven": True,
    "biometric_valid": True,
    "hsm_verified": True,
    "geo_velocity_kmh": 0.0,
    "auth_velocity_per_min": 0.0,
    "odd_hour": False,
    "target_sensitivity": "low",
    "ip_reputation": "good",
}
RISK_ENUMS: dict[str, tuple[str, ...]] = {
    "target_sensitivity": ("low", "medium", "high"),
    "ip_reputation": ("good", "neutral", "poor"),
}
# Wire encodings accepted for a boolean signal.
RISK_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y", "on"})
RISK_FALSE_STRINGS = frozenset({"0", "false", "f", "no", "n", "off"})

# What a level *means* operationally. Scoring alone answers "how risky", not
# "what now" — this table closes that gap without touching the score.
RISK_ACTIONS: dict[str, dict[str, Any]] = {
    "low": {
        "action": "allow",
        "required_controls": [],
        "review_after_seconds": None,
        "rationale": "context is consistent with the user's normal profile",
    },
    "moderate": {
        "action": "step_up",
        "required_controls": ["reauth"],
        "review_after_seconds": 900,
        "rationale": "one weak signal: confirm intent before proceeding",
    },
    "high": {
        "action": "review",
        "required_controls": ["reauth", "step_up_biometric", "notify_auditor"],
        "review_after_seconds": 300,
        "rationale": "multiple weak signals: human review before granting",
    },
    "critical": {
        "action": "deny",
        "required_controls": ["reauth", "notify_auditor", "open_case"],
        "review_after_seconds": None,
        "rationale": "context is inconsistent with the principal; refuse",
    },
}
# Actions are a closed set so a caller can branch exhaustively.
RISK_DECISION_ACTIONS = ("allow", "step_up", "review", "deny")
RISK_CONTROL_NAMES = (
    "reauth",
    "step_up_biometric",
    "notify_auditor",
    "open_case",
    "restrict_scope",
)
RISK_OUTCOMES = ("fraud", "denied_breach", "granted", "false_positive")
RISK_OUTCOME_DELTAS: dict[str, float] = {
    "fraud": ADAPTIVE_PARAMS["step"],
    "denied_breach": ADAPTIVE_PARAMS["step"],
    "granted": -ADAPTIVE_PARAMS["step"],
    "false_positive": -ADAPTIVE_PARAMS["step"],
}
# Retention bounds for the weight snapshot log and the decision log.
RISK_WEIGHT_HISTORY = 100
RISK_DECISION_HISTORY = 500

# Learned weight state — starts as the config base weights, drifts with outcomes.
_WEIGHT_STATE: dict[str, float] = {rid: float(rule["weight"]) for rid, rule in _RULE_BY_ID.items()}
_WEIGHT_VERSION = 1
# Bounded snapshot log so an old decision can be re-scored at the weight set
# that actually produced it. Entries are ``{"version", "weights"}``.
_WEIGHT_HISTORY: deque[dict[str, Any]] = deque(maxlen=RISK_WEIGHT_HISTORY)
_WEIGHT_HISTORY.append({"version": _WEIGHT_VERSION, "weights": dict(_WEIGHT_STATE)})


def _snapshot_weights() -> None:
    """Record the current weight set under the live version."""
    _WEIGHT_HISTORY.append(
        {"version": _WEIGHT_VERSION, "weights": dict(_WEIGHT_STATE)}
    )


def current_weights() -> dict[str, float]:
    return dict(_WEIGHT_STATE)


def weight_version() -> int:
    return _WEIGHT_VERSION


def weight_history() -> list[dict[str, Any]]:
    """Newest-last snapshot log, bounded by ``RISK_WEIGHT_HISTORY``."""
    return [
        {"version": row["version"], "weights": dict(row["weights"])}
        for row in _WEIGHT_HISTORY
    ]


def weights_at(version: int) -> dict[str, float] | None:
    """The weight set as of ``version``, or ``None`` if it aged out of the log."""
    for row in reversed(_WEIGHT_HISTORY):
        if row["version"] == int(version):
            return dict(row["weights"])
    return None


def reset_weight_state() -> None:
    global _WEIGHT_VERSION
    _WEIGHT_STATE.update({rid: float(rule["weight"]) for rid, rule in _RULE_BY_ID.items()})
    _WEIGHT_VERSION += 1
    _snapshot_weights()


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
    previous = _WEIGHT_STATE[rule_id]
    _WEIGHT_STATE[rule_id] = min(
        ADAPTIVE_PARAMS["max_weight"],
        max(ADAPTIVE_PARAMS["min_weight"], _WEIGHT_STATE[rule_id] + delta),
    )
    _WEIGHT_VERSION += 1
    _snapshot_weights()
    return {"rule_id": rule_id, "weight": _WEIGHT_STATE[rule_id], "version": _WEIGHT_VERSION}


def apply_risk_outcomes(outcomes: Any) -> dict[str, Any]:
    """Apply a batch of ``(rule_id, outcome)`` observations in one version bump.

    ``outcomes`` may be a mapping of ``rule_id -> outcome`` or an iterable of
    pairs. Unlike calling :func:`adapt_risk_weight` in a loop, the whole batch
    lands under a *single* weight version, so a burst of feedback produces one
    reproducible transition instead of N interleaved ones. An unknown rule is
    reported in ``applied`` rather than raised, so one bad row cannot discard
    the rest of the batch.
    """
    global _WEIGHT_VERSION
    pairs = list(outcomes.items()) if isinstance(outcomes, dict) else list(outcomes)
    start_version = _WEIGHT_VERSION
    applied: list[dict[str, Any]] = []
    low, high = ADAPTIVE_PARAMS["min_weight"], ADAPTIVE_PARAMS["max_weight"]
    for rule_id, outcome in pairs:
        name = str(rule_id)
        label = str(outcome)
        if name not in _WEIGHT_STATE:
            applied.append({"rule_id": name, "outcome": label, "applied": False})
            continue
        previous = _WEIGHT_STATE[name]
        raw = previous + RISK_OUTCOME_DELTAS.get(label, 0.0)
        _WEIGHT_STATE[name] = min(high, max(low, raw))
        applied.append(
            {
                "rule_id": name,
                "outcome": label,
                "applied": True,
                "previous_weight": previous,
                "weight": _WEIGHT_STATE[name],
                # True whenever the delta asked for a value outside the band,
                # including a rule already sitting on its bound.
                "clamped": raw > high or raw < low,
            }
        )
    if any(row["applied"] for row in applied):
        _WEIGHT_VERSION += 1
        _snapshot_weights()
    return {
        "from_version": start_version,
        "to_version": _WEIGHT_VERSION,
        "applied": applied,
        "accepted": sum(1 for row in applied if row["applied"]),
        "rejected": sum(1 for row in applied if not row["applied"]),
    }


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


# --- context normalization ----------------------------------------------------


def normalize_signal(key: str, value: Any) -> Any:
    """Coerce one signal to its declared type.

    Returns the coerced value, or ``None`` when the value cannot be read as the
    declared type — the caller decides whether that is a gap or a hard error.
    """
    kind = RISK_CONTEXT_TYPES.get(key)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        token = str(value).strip().lower()
        if token in RISK_TRUE_STRINGS:
            return True
        if token in RISK_FALSE_STRINGS:
            return False
        return None
    if kind == "number":
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if kind == "enum":
        token = str(value).strip().lower()
        allowed = RISK_ENUMS.get(key, ())
        return token if token in allowed else None
    return value


def context_gaps(context: dict[str, Any]) -> list[str]:
    """Signals the caller did not supply (the rules that stay silent because of it)."""
    return [key for key in RISK_CONTEXT_KEYS if key not in (context or {})]


def normalize_context(
    context: dict[str, Any] | None, *, fill: bool = True
) -> dict[str, Any]:
    """Read a caller-supplied context into a shape the rules can score.

    A rule only fires when its signal is *present*, so an omitted signal is a
    silent fail-open. This reports exactly that:

    - ``context``     — the usable signal set (defaults filled in when ``fill``)
    - ``gaps``        — signals the caller never supplied
    - ``unreadable``  — signals supplied with a value that does not parse
    - ``defaulted``   — signals filled from ``RISK_CONTEXT_DEFAULTS``
    - ``extra``       — keys the rule table does not know about
    - ``complete``    — no gaps and nothing unreadable
    """
    supplied = dict(context or {})
    usable: dict[str, Any] = {}
    gaps: list[str] = []
    unreadable: list[str] = []
    defaulted: list[str] = []
    for key in RISK_CONTEXT_KEYS:
        if key not in supplied:
            gaps.append(key)
            if fill:
                usable[key] = RISK_CONTEXT_DEFAULTS[key]
                defaulted.append(key)
            continue
        value = normalize_signal(key, supplied[key])
        if value is None:
            unreadable.append(key)
            if fill:
                usable[key] = RISK_CONTEXT_DEFAULTS[key]
                defaulted.append(key)
            continue
        usable[key] = value
    return {
        "context": usable,
        "gaps": gaps,
        "unreadable": unreadable,
        "defaulted": defaulted,
        "extra": sorted(str(key) for key in supplied if key not in RISK_CONTEXT_TYPES),
        "complete": not gaps and not unreadable,
    }


# --- level -> action ----------------------------------------------------------


def required_controls_for(level: str) -> list[str]:
    """Control names that must be satisfied before serving a ``level``."""
    if level not in RISK_ACTIONS:
        raise ValueError(f"unknown risk level: {level}")
    return list(RISK_ACTIONS[level]["required_controls"])


def action_for_level(level: str) -> dict[str, Any]:
    """The operational decision attached to a risk ``level``."""
    if level not in RISK_ACTIONS:
        raise ValueError(f"unknown risk level: {level}")
    return {"level": level, **RISK_ACTIONS[level], "required_controls": required_controls_for(level)}


def controls_missing_for(level: str, satisfied: Any) -> list[str]:
    """Which of a level's required controls the caller has not reported."""
    done = {str(item) for item in (satisfied or ())}
    return [control for control in required_controls_for(level) if control not in done]


# --- counterfactual analysis --------------------------------------------------


def counterfactual(
    context: dict[str, Any], signal: str, value: Any, *, levels: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Re-score the context with one signal changed, without touching live state.

    This is the question a support engineer actually asks: "if this one signal
    had read differently, would the outcome have differed?"
    """
    if signal not in RISK_CONTEXT_TYPES:
        raise ValueError(f"unknown risk signal: {signal}")
    baseline = score_with_config(effective_risk_rules(), context, levels=levels)
    trial = {**context, signal: value}
    variant = score_with_config(effective_risk_rules(), trial, levels=levels)
    return {
        "signal": signal,
        "from": context.get(signal),
        "to": value,
        "baseline_score": baseline["score"],
        "variant_score": variant["score"],
        "score_delta": variant["score"] - baseline["score"],
        "baseline_level": baseline["level"],
        "variant_level": variant["level"],
        "level_flip": baseline["level"] != variant["level"],
    }


def cheapest_clearing_signal(
    context: dict[str, Any], *, target_level: str = "low", levels: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """The single signal change that would move the context to ``target_level``.

    Returns the option with the largest score drop, or ``None`` when no single
    change is enough — a decision that genuinely needs more than one fix.
    """
    if target_level not in RISK_ACTIONS:
        raise ValueError(f"unknown risk level: {target_level}")
    variants: list[dict[str, Any]] = []
    for key, kind in RISK_CONTEXT_TYPES.items():
        if kind == "bool":
            options: list[Any] = [not bool(context.get(key))]
        elif kind == "number":
            options = [0.0]
        else:
            options = [RISK_CONTEXT_DEFAULTS[key], *RISK_ENUMS.get(key, ())]
        for option in options:
            if option == context.get(key):
                continue
            trial = counterfactual(context, key, option, levels=levels)
            if trial["variant_level"] == target_level:
                variants.append(trial)
    if not variants:
        return None
    # Prefer the smallest score drop; ties broken by signal name for stability.
    return sorted(variants, key=lambda row: (row["score_delta"], row["signal"]))[0]


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


def evaluate_risk_at(context: dict[str, Any], version: int) -> dict[str, Any]:
    """Re-score with the weight set that was live at ``version``.

    ``evaluate_risk`` reflects the weights of *now*; a stored decision that has
    to be re-examined months later needs the weights of then, otherwise the
    same inputs produce a different answer than the one that was acted on.
    """
    weights = weights_at(version)
    if weights is None:
        raise KeyError(f"weight version {version} is no longer retained")
    rules = [
        {**rule, "weight": weights.get(rule["rule_id"], rule["weight"])}
        for rule in RISK_RULES
    ]
    result = score_with_config(rules, context)
    return {
        **result,
        "weight_version": int(version),
        "replayed": True,
    }


# --- full decision (score + action + signal gaps) ------------------------------


def evaluate_risk_decision(
    context: dict[str, Any] | None,
    *,
    fill_gaps: bool = True,
    strict: bool = False,
    satisfied_controls: Any = None,
) -> dict[str, Any]:
    """``evaluate_risk`` plus everything a caller needs to *act* on the score.

    Adds the level's action and required controls, the signal gaps behind the
    score, and the controls the caller has not reported. With ``strict=True`` a
    gap raises instead of inheriting the benign default — the fail-closed mode
    for endpoints that must not score on a half-supplied context.
    """
    prepared = normalize_context(context, fill=True)
    if strict and prepared["gaps"]:
        raise ValueError(
            "missing risk signals: " + ", ".join(prepared["gaps"])
        )
    if not fill_gaps:
        prepared = normalize_context(context, fill=False)
    scored = evaluate_risk(prepared["context"])
    level = scored["level"]
    action = action_for_level(level)
    return {
        **scored,
        "action": action["action"],
        "required_controls": list(action["required_controls"]),
        "missing_controls": controls_missing_for(level, satisfied_controls),
        "rationale": action["rationale"],
        "review_after_seconds": action["review_after_seconds"],
        "signals": {
            "gaps": prepared["gaps"],
            "unreadable": prepared["unreadable"],
            "defaulted": prepared["defaulted"],
            "extra": prepared["extra"],
            "complete": prepared["complete"],
        },
    }


class RiskDecisionStore:
    """Bounded ring of scored decisions, for replay and post-hoc analysis."""

    def __init__(self, capacity: int = RISK_DECISION_HISTORY):
        self._capacity = max(1, int(capacity))
        self._rows: deque[dict[str, Any]] = deque(maxlen=self._capacity)

    def record(
        self, decision: dict[str, Any], *, subject: str | None = None, outcome: str | None = None
    ) -> dict[str, Any]:
        row = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "subject": subject,
            "score": decision.get("score"),
            "level": decision.get("level"),
            "action": decision.get("action"),
            "weight_version": decision.get("weight_version"),
            "signal_gaps": list((decision.get("signals") or {}).get("gaps") or ()),
            "present_rules": [
                factor["rule_id"]
                for factor in decision.get("factors") or ()
                if factor.get("present")
            ],
            "outcome": outcome,
        }
        self._rows.append(row)
        return row

    def recent(self, *, limit: int = 20, subject: str | None = None) -> list[dict[str, Any]]:
        rows = [row for row in self._rows if subject is None or row["subject"] == subject]
        return [dict(row) for row in rows[-max(1, int(limit)) :]]

    def stats(self) -> dict[str, Any]:
        by_level: dict[str, int] = {}
        by_action: dict[str, int] = {}
        for row in self._rows:
            by_level[str(row["level"])] = by_level.get(str(row["level"]), 0) + 1
            by_action[str(row["action"])] = by_action.get(str(row["action"]), 0) + 1
        return {
            "capacity": self._capacity,
            "total": len(self._rows),
            "by_level": dict(sorted(by_level.items())),
            "by_action": dict(sorted(by_action.items())),
            "pending_outcomes": sum(
                1 for row in self._rows if row["outcome"] is None
            ),
        }

    def replay(self, row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Re-score a stored decision's context at the weight version it used."""
        return evaluate_risk_at(context, row.get("weight_version", 1))

    def clear(self) -> None:
        self._rows.clear()


RISK_DECISIONS = RiskDecisionStore()


def record_risk_decision(
    context: dict[str, Any] | None,
    *,
    subject: str | None = None,
    store: RiskDecisionStore | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Score, remember, and return a decision in one call.

    The stored row is what makes ``RISK_DECISIONS.replay`` possible later, so
    this is the path a request handler should use rather than a bare
    :func:`evaluate_risk`.
    """
    decision = evaluate_risk_decision(context, **kwargs)
    (store or RISK_DECISIONS).record(decision, subject=subject)
    return decision


def build_risk_evaluator_catalog() -> dict[str, object]:
    return {
        "levels": list(RISK_LEVELS),
        "context_keys": list(RISK_CONTEXT_KEYS),
        "rules": [
            {**rule, "current_weight": _WEIGHT_STATE[rule["rule_id"]]}
            for rule in RISK_RULES
        ],
        "adaptive": {**ADAPTIVE_PARAMS, "weight_version": _WEIGHT_VERSION},
        # --- expansion surface ------------------------------------------------
        "context_types": dict(RISK_CONTEXT_TYPES),
        "context_defaults": dict(RISK_CONTEXT_DEFAULTS),
        "enums": {key: list(values) for key, values in RISK_ENUMS.items()},
        "actions": {
            level: {
                "action": config["action"],
                "required_controls": list(config["required_controls"]),
                "review_after_seconds": config["review_after_seconds"],
                "rationale": config["rationale"],
            }
            for level, config in RISK_ACTIONS.items()
        },
        "decision_actions": list(RISK_DECISION_ACTIONS),
        "control_names": list(RISK_CONTROL_NAMES),
        "outcomes": {
            "accepted": list(RISK_OUTCOMES),
            "deltas": dict(RISK_OUTCOME_DELTAS),
        },
        "weight_history": {
            "capacity": RISK_WEIGHT_HISTORY,
            "retained": len(_WEIGHT_HISTORY),
            "versions": [row["version"] for row in _WEIGHT_HISTORY],
        },
        "decision_store": RISK_DECISIONS.stats(),
    }