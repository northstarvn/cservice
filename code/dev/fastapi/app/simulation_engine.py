"""Simulation / what-if engines (S-02).

Before committing a change to a live config — rule weights, level thresholds,
retention windows — you can simulate the change against real contexts and
partitions *without touching the live tables*:

- ``simulate_risk_whatif`` — apply weight overrides, disable rules, or retune
  level thresholds on a copy of the rule table and re-score a context.
- ``simulate_retention_whatif`` — replay partition lifecycle decisions under a
  modified retention policy without executing any DDL.

Both are pure: they never mutate ``risk_evaluator`` weight state, the partition
manager, or the model registry. Every run returns a ``SimulationReport``
(baseline vs variant, delta, affected rows) and can be recorded as an
explainability trace via ``run_simulation``.

``run_simulation(kind, ...)`` is the single dispatch used by tooling/endpoints.

Expansion (thin-group pass): the original engine answers "what happens if I
change *this* one thing". Real tuning asks three harder questions, so the
engine is now config-driven and batch-capable:

- ``SIMULATION_MODIFIERS`` — named modifier packs (weight deltas, disabled
  rules, level retunes, retention overrides) resolved from the shared
  ``=formula`` param engine, so "the cautious variant" is a config row that
  several simulations can share instead of a dict literal per call site.
- ``SCENARIO_LIBRARY`` — named, reusable scenarios (each a modifier pack plus
  its inputs) so a standard tuning run is one reference, not a script.
- ``PROMOTION_GATES`` — declarative promotion policy. ``safety_verdict`` turns
  a simulation into a promote/hold decision with named failing gates, which is
  the missing link between "the score moved" and "we may ship this".
- ``sweep_sensitivity`` — one-at-a-time sensitivity over a parameter space:
  which knob actually moves the outcome, ranked.
- ``simulate_population`` — replays a batch of real contexts and reports the
  level-flip distribution, so a retune is judged on blast radius, not one row.
- ``compare_reports`` — diff two simulation reports field-by-field.

``SIMULATION_KINDS`` stays exactly ``("risk", "retention")`` — it is a pinned
public contract — so the new capabilities are additional *analyses over* those
two kinds, not new kinds.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.explainability import record_decision
from app.partition_manager import PARTITION_POLICIES, period_end
from app.risk_evaluator import RISK_LEVELS, effective_risk_rules, score_with_config

SIMULATION_KINDS = ("risk", "retention")

#: Retention-override key meaning *every* policy, not one policy id.
#:
#: Without it "cut every retention window" is inexpressible, because an override
#: is looked up by policy id. The obvious workaround -- spelling out each id in
#: the pack -- is the failure mode this constant exists to prevent: the list has
#: to be hand-edited every time a policy is added to `PARTITION_POLICIES`, and a
#: forgotten edit is not an error, it is an override that silently does nothing.
#: A wildcard does not go stale.
#:
#: Precedence is specific id > wildcard > the policy's own baseline, so a pack
#: can set a floor for everything and then retune one policy.
RETENTION_OVERRIDE_WILDCARD = "*"

# --- Expansion: modifier packs, scenario library, promotion gates ------------

# named, reusable "what if" packs. `weight_deltas` are *relative* to the
# current table and may carry `=formula` params resolved against `params`.
SIMULATION_MODIFIERS: list[dict[str, Any]] = [
    {
        "modifier_id": "baseline",
        "label": "No change (control)",
        "description": "Re-scores with the untouched rule table; the null result.",
        "params": {},
        "weight_deltas": {},
        "disabled_rules": [],
        "level_max_overrides": {},
        "retention_overrides": {},
        "when_hint": "Control arm for any sweep — always include it.",
    },
    {
        "modifier_id": "cautious_risk",
        "label": "Cautious risk posture",
        "description": "Doubles the device/biometric rules and tightens the high band.",
        "params": {"device_multiplier": 2.0, "high_band_max": 45},
        "weight_deltas": {
            "new_device": "=base * device_multiplier",
            "biometric_mismatch": "=base * device_multiplier",
        },
        "disabled_rules": [],
        "level_max_overrides": {"high": 45},
        "retention_overrides": {},
        "when_hint": "Use when false positives are the expensive mistake.",
    },
    {
        "modifier_id": "permissive_risk",
        "label": "Permissive risk posture",
        "description": "Halves device/biometric rules and widens the high band.",
        "params": {"device_multiplier": 0.5, "high_band_max": 75},
        "weight_deltas": {
            "new_device": "=base * device_multiplier",
            "biometric_mismatch": "=base * device_multiplier",
        },
        "disabled_rules": [],
        "level_max_overrides": {"high": 75},
        "retention_overrides": {},
        "when_hint": "Use when missed detections are the expensive mistake.",
    },
    {
        "modifier_id": "drop_device_signal",
        "label": "Disable device signals",
        "description": "Removes the new_device rule entirely (an ablation, not a retune).",
        "params": {},
        "weight_deltas": {},
        "disabled_rules": ["new_device"],
        "level_max_overrides": {},
        "retention_overrides": {},
        "when_hint": "Ablation arm: how much of the score is the device signal?",
    },
    {
        "modifier_id": "short_retention",
        "label": "Aggressive partition retention",
        "description": "Cuts every retention window to 30 days.",
        "params": {"retention_days": 30},
        "weight_deltas": {},
        "disabled_rules": [],
        "level_max_overrides": {},
        "retention_overrides": {
            RETENTION_OVERRIDE_WILDCARD: "=retention_days",
        },
        "when_hint": "Storage-cost arm of a retention what-if.",
    },
]

# Named scenario = modifier pack + the inputs it is meant to be run against.
# `context` is merged with the caller's context by `run_named_scenario`.
SCENARIO_LIBRARY: list[dict[str, Any]] = [
    {
        "scenario_id": "new_device_high_score",
        "label": "Unknown device on a high-value account",
        "modifier_id": "cautious_risk",
        "context": {"device_proven": False, "value_tier": "premium", "odd_hour": True},
        "expect": "score rises under the cautious pack; the band may flip upward",
        "when_hint": "The canonical 'is the device rule doing anything' probe.",
    },
    {
        "scenario_id": "trusted_device_low_score",
        "label": "Known device, daytime, low value",
        "modifier_id": "permissive_risk",
        "context": {"device_proven": True, "value_tier": "new", "odd_hour": False},
        "expect": "no fired rules; both arms score identically",
        "when_hint": "Control: a clean context must not move under any retune.",
    },
    {
        "scenario_id": "device_ablation",
        "label": "How much is the device signal worth?",
        "modifier_id": "drop_device_signal",
        "context": {"device_proven": False, "value_tier": "premium"},
        "expect": "score falls by exactly the device rule's weight",
        "when_hint": "Ablation: isolates one signal's contribution.",
    },
    {
        "scenario_id": "partition_cost_trim",
        "label": "Retention cut to 30 days",
        "modifier_id": "short_retention",
        "context": {},
        "expect": "partitions older than 30 days flip keep -> drop",
        "when_hint": "Retention what-if; needs active_partitions to be meaningful.",
    },
]

# Declarative promotion policy. Every gate must pass for `safety_verdict` to
# allow a promotion; `direction` says which movement is acceptable.
PROMOTION_GATES: list[dict[str, Any]] = [
    {
        "gate_id": "bounded_score_move",
        "label": "Score movement stays bounded",
        "metric": "abs_score_delta",
        "operator": "lte",
        "threshold": 15.0,
        "required": True,
        "when_hint": "A retune that swings the score more than this needs a review.",
    },
    {
        "gate_id": "no_unexpected_level_flip",
        "label": "Risk band did not change",
        "metric": "level_flip",
        "operator": "eq",
        "threshold": False,
        "required": True,
        "when_hint": "A band change re-routes every downstream control.",
    },
    {
        "gate_id": "bounded_rule_churn",
        "label": "Few rules changed",
        "metric": "affected_rule_count",
        "operator": "lte",
        "threshold": 3,
        "required": False,
        "when_hint": "Advisory: a wide edit is harder to review and to roll back.",
    },
    {
        "gate_id": "no_fired_rule_added",
        "label": "No previously-silent rule starts firing",
        "metric": "rules_added",
        "operator": "eq",
        "threshold": 0,
        "required": True,
        "when_hint": "A new firing rule means previously-clean traffic now escalates.",
    },
    {
        "gate_id": "no_fired_rule_removed",
        "label": "No previously-firing rule stops firing",
        "metric": "rules_removed",
        "operator": "eq",
        "threshold": 0,
        "required": True,
        "when_hint": "A dropped rule is a silent loss of detection.",
    },
]

_GATE_OPS: dict[str, Any] = {
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Expansion: modifier packs -----------------------------------------------


def get_modifier(modifier_id: str) -> dict[str, Any]:
    """Resolve a modifier pack by id (copy — config is never handed out)."""
    for modifier in SIMULATION_MODIFIERS:
        if modifier["modifier_id"] == modifier_id:
            return dict(modifier)
    raise ValueError(
        f"unknown simulation modifier: {modifier_id!r} "
        f"(expected one of {[m['modifier_id'] for m in SIMULATION_MODIFIERS]})"
    )


def get_scenario(scenario_id: str) -> dict[str, Any]:
    """Resolve a named scenario from the library (copy)."""
    for scenario in SCENARIO_LIBRARY:
        if scenario["scenario_id"] == scenario_id:
            return dict(scenario)
    raise ValueError(
        f"unknown scenario: {scenario_id!r} "
        f"(expected one of {[s['scenario_id'] for s in SCENARIO_LIBRARY]})"
    )


def _resolve_weight_deltas(
    deltas: dict[str, Any],
    base_rules: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, float]:
    """Turn a modifier's ``weight_deltas`` into absolute target weights.

    A delta value may be a number (absolute target) or an ``=formula`` string
    evaluated against ``params`` plus ``base`` (the rule's current weight), so
    "double the device rule" is expressible without hardcoding a number that
    drifts the moment someone tunes the table.
    """
    from app import rule_engine  # local import: rule_engine is dependency-free

    current = {rule["rule_id"]: rule.get("weight", 0) for rule in base_rules}
    resolved: dict[str, float] = {}
    for rule_id, value in (deltas or {}).items():
        if isinstance(value, str) and value.startswith("="):
            resolved[rule_id] = float(
                rule_engine.evaluate_expression(
                    value[1:], {**params, "base": current.get(rule_id, 0)}
                )
            )
        else:
            resolved[rule_id] = float(value)
    return resolved


def _retention_override_for(
    overrides: dict[str, Any],
    policy_id: str,
    baseline: int,
    *,
    params: dict[str, Any] | None = None,
) -> int:
    """Resolve one policy's retention window: specific id > wildcard > baseline.

    Single source of truth for the precedence, shared by `resolve_modifier` and
    `simulate_retention_whatif` so the two cannot drift: a wildcard that meant
    "every policy" in the pack expander but "nothing" in the simulator would
    reintroduce exactly the silent no-op this precedence exists to prevent.

    An ``=formula`` value is evaluated with ``base`` bound to *that policy's* own
    baseline, so ``=base`` stays a per-policy no-op and ``=retention_days``
    applies the same absolute window everywhere. Formulas need `params`; a
    direct call to `simulate_retention_whatif` has no pack to draw them from, so
    the error names the remedy rather than surfacing as a bare ``int()`` failure.
    """
    if policy_id in overrides:
        raw: Any = overrides[policy_id]
    elif RETENTION_OVERRIDE_WILDCARD in overrides:
        raw = overrides[RETENTION_OVERRIDE_WILDCARD]
    else:
        return baseline
    if isinstance(raw, str) and raw.startswith("="):
        if params is None:
            raise ValueError(
                f"retention override for policy {policy_id!r} is the formula "
                f"{raw!r}, which needs a modifier pack to supply params; pass an "
                f"absolute day count, or resolve the pack with resolve_modifier()"
            )
        from app import rule_engine  # local import: rule_engine is dependency-free

        raw = rule_engine.evaluate_expression(raw[1:], {**params, "base": baseline})
    return int(raw)


def unmatched_retention_overrides(
    overrides: dict[str, Any] | None = None,
    policies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Which retention-override keys actually reach a policy.

    An override key that names no policy is *inert*, not invalid: the lookup is
    `overrides.get(policy_id)` per real policy, so a key nobody matches is
    dropped without an error. That makes a placeholder key indistinguishable
    from a working one at the call site, which is how `short_retention` shipped
    promising a 30-day cut while cutting nothing.

    This reports the residual so the config is checked against the live policy
    set rather than trusted. Advisory, like the schema-gap reports: it names
    dead config, it does not raise, because an advisory what-if tool that
    hard-fails on a stale key is a tool nobody runs.
    """
    overrides = dict(overrides or {})
    policies = [dict(p) for p in policies] if policies is not None else [dict(p) for p in PARTITION_POLICIES]
    known = {str(p["policy_id"]) for p in policies if p.get("policy_id")}
    matched = sorted(key for key in overrides if key in known)
    unmatched = sorted(
        key
        for key in overrides
        if key not in known and key != RETENTION_OVERRIDE_WILDCARD
    )
    return {
        "override_count": len(overrides),
        "policy_count": len(known),
        "policies": sorted(known),
        "matched": matched,
        "unmatched": unmatched,
        "wildcard": RETENTION_OVERRIDE_WILDCARD in overrides,
        "inert": bool(unmatched),
        "note": (
            "an unmatched key is dropped silently at lookup time, so it is dead "
            "config rather than an error; the wildcard applies to every policy and "
            "is overridden by any specific id"
        ),
    }


def resolve_modifier(
    modifier_id: str | dict[str, Any],
    *,
    kind: str = "risk",
    base_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Expand a modifier pack into concrete simulator kwargs.

    Returns a dict shaped for ``run_simulation(kind, **kwargs)``: resolved
    absolute weights, disabled rules, level maxima, and retention overrides.
    """
    pack = dict(modifier_id) if isinstance(modifier_id, dict) else get_modifier(modifier_id)
    params = dict(pack.get("params") or {})
    rules = base_rules if base_rules is not None else effective_risk_rules()
    overrides = _resolve_weight_deltas(pack.get("weight_deltas") or {}, rules, params)

    if kind == "retention":
        policies = [dict(p) for p in PARTITION_POLICIES]
        overrides = dict(pack.get("retention_overrides") or {})
        retention = {
            str(policy["policy_id"]): _retention_override_for(
                overrides,
                str(policy["policy_id"]),
                int(policy.get("retention_days", 90)),
                params=params,
            )
            for policy in policies
        }
        return {
            "kind": "retention",
            "modifier_id": pack.get("modifier_id", ""),
            "label": pack.get("label", ""),
            "retention_overrides": retention,
            "policies": policies,
            "override_audit": unmatched_retention_overrides(overrides, policies),
        }

    return {
        "kind": "risk",
        "modifier_id": pack.get("modifier_id", ""),
        "label": pack.get("label", ""),
        "weight_overrides": overrides,
        "disabled_rules": list(pack.get("disabled_rules") or []),
        "level_max_overrides": {
            str(k): int(v) for k, v in (pack.get("level_max_overrides") or {}).items()
        },
        "params": params,
    }


def run_named_scenario(
    scenario_id: str,
    *,
    context: dict[str, Any] | None = None,
    active_partitions: list[dict[str, Any]] | None = None,
    policies: list[dict[str, Any]] | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """Run a scenario from ``SCENARIO_LIBRARY`` (modifier pack + its inputs)."""
    scenario = get_scenario(scenario_id)
    pack_id = scenario["modifier_id"]
    pack = get_modifier(pack_id)
    kind = "retention" if pack.get("retention_overrides") else "risk"
    resolved = resolve_modifier(pack, kind=kind)
    merged_context = {**scenario.get("context", {}), **dict(context or {})}
    if kind == "risk":
        report = simulate_risk_whatif(
            merged_context,
            weight_overrides=resolved["weight_overrides"] or None,
            disabled_rules=resolved["disabled_rules"] or None,
            level_max_overrides=resolved["level_max_overrides"] or None,
            label=scenario_id,
        )
    else:
        report = simulate_retention_whatif(
            policies,
            active_partitions,
            retention_overrides=resolved["retention_overrides"] or None,
            label=scenario_id,
        )
    report["scenario_id"] = scenario_id
    report["modifier_id"] = pack_id
    report["expect"] = scenario.get("expect", "")
    report["verdict"] = safety_verdict(report)
    if record:
        record_decision(
            decision_type="simulation",
            entity_ref=f"scenario:{scenario_id}",
            outcome=report["summary"],
            score=report.get("variant", {}).get("score"),
            detail={"kind": kind, "scenario": scenario_id, "modifier": pack_id},
        )
    return report


# --- Expansion: analysis over a finished report ------------------------------


def _report_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Flatten a report into the scalar metrics ``PROMOTION_GATES`` reads."""
    changed = report.get("changed_factors") or []
    fired_before = set(report.get("baseline", {}).get("fired_rules") or [])
    fired_after = set(report.get("variant", {}).get("fired_rules") or [])
    return {
        "score_delta": report.get("delta", {}).get("score", 0),
        "abs_score_delta": abs(report.get("delta", {}).get("score", 0) or 0),
        "level_flip": bool(report.get("delta", {}).get("level_flip", False)),
        "affected_rule_count": len(changed),
        "rules_added": len(fired_after - fired_before),
        "rules_removed": len(fired_before - fired_after),
        "changed_count": report.get("changed_count", 0),
    }


def safety_verdict(report: dict[str, Any] | None) -> dict[str, Any]:
    """Turn a simulation report into a promote/hold decision.

    Pure: reads the report, evaluates ``PROMOTION_GATES`` by name, and returns
    the failing gates so a human sees *which* policy stopped the promotion
    rather than a bare "no".
    """
    if not report:
        return {"allowed": False, "reason": "no report", "gates": []}
    metrics = _report_metrics(report)
    results: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for gate in PROMOTION_GATES:
        observed = metrics.get(gate["metric"])
        op = _GATE_OPS.get(str(gate["operator"]))
        passed = bool(op(observed, gate["threshold"])) if op and observed is not None else False
        entry = {
            "gate_id": gate["gate_id"],
            "label": gate["label"],
            "metric": gate["metric"],
            "operator": gate["operator"],
            "threshold": gate["threshold"],
            "observed": observed,
            "passed": passed,
            "required": bool(gate.get("required", True)),
        }
        results.append(entry)
        if not passed and entry["required"]:
            blocking.append(entry)
    return {
        "allowed": not blocking,
        "verdict": "promote" if not blocking else "hold",
        "blocking_gates": [g["gate_id"] for g in blocking],
        "gates": results,
        "metrics": metrics,
    }


def sweep_sensitivity(
    context: dict[str, Any],
    *,
    base_rules: list[dict[str, Any]] | None = None,
    parameter: str = "weight",
    rule_id: str | None = None,
    multipliers: list[float] | None = None,
    level_max_overrides: dict[str, int] | None = None,
    label: str = "sensitivity",
) -> dict[str, Any]:
    """One-at-a-time sensitivity: which knob actually moves the outcome?

    Runs the context under a ladder of multipliers on a single rule's weight
    and reports the score curve, the band each step lands in, and the marginal
    effect per step. A knob whose curve is flat is one you can tune without
    risk — which is exactly the question a sweep exists to answer.
    """
    base = [dict(rule) for rule in (base_rules if base_rules is not None else effective_risk_rules())]
    if not base:
        raise ValueError("sweep_sensitivity needs at least one rule")
    target = rule_id or base[0]["rule_id"]
    if all(rule["rule_id"] != target for rule in base):
        raise ValueError(f"unknown rule for sensitivity sweep: {target!r}")
    ladder = [float(m) for m in (multipliers or [0.0, 0.5, 1.0, 1.5, 2.0])]
    current = {rule["rule_id"]: rule.get("weight", 0) for rule in base}
    base_weight = current.get(target, 0)
    steps: list[dict[str, Any]] = []
    previous_score: float | None = None
    for multiplier in ladder:
        variant = [dict(rule) for rule in base]
        for rule in variant:
            if rule["rule_id"] == target:
                rule["weight"] = base_weight * multiplier
        result = score_with_config(
            variant, context, levels=[dict(b) for b in RISK_LEVELS]
        )
        steps.append(
            {
                "multiplier": multiplier,
                "weight": round(base_weight * multiplier, 4),
                "score": result["score"],
                "level": result["level"],
                "marginal": (
                    None if previous_score is None else result["score"] - previous_score
                ),
                "fired": bool(
                    next(
                        (
                            f["present"]
                            for f in result["factors"]
                            if f["rule_id"] == target
                        ),
                        False,
                    )
                ),
            }
        )
        previous_score = result["score"]
    scores = [step["score"] for step in steps]
    return {
        "scenario": label,
        "parameter": parameter,
        "rule_id": target,
        "base_weight": base_weight,
        "base_level": steps[ladder.index(1.0)]["level"] if 1.0 in ladder else None,
        "steps": steps,
        "score_range": (max(scores) - min(scores)) if scores else 0,
        "levels_touched": sorted({step["level"] for step in steps}),
        "sensitive": (max(scores) - min(scores)) > 0 if scores else False,
        "summary": {
            "rule_id": target,
            "min_score": min(scores) if scores else None,
            "max_score": max(scores) if scores else None,
            "levels_touched": sorted({step["level"] for step in steps}),
            "sensitive": (max(scores) - min(scores)) > 0 if scores else False,
        },
    }


def simulate_population(
    contexts: list[dict[str, Any]],
    *,
    base_rules: list[dict[str, Any]] | None = None,
    weight_overrides: dict[str, float] | None = None,
    disabled_rules: list[str] | None = None,
    level_max_overrides: dict[str, int] | None = None,
    label: str = "population",
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Replay a batch of real contexts and report the blast radius.

    One row is not evidence: a retune that leaves the single example alone can
    still flip 8% of live traffic. This replays the batch, reports the level
    transition matrix, the flip rate, and a bounded sample of the biggest
    movers so they can be inspected individually.
    """
    rows: list[dict[str, Any]] = []
    for index, context in enumerate(contexts or []):
        report = simulate_risk_whatif(
            dict(context),
            base_rules=base_rules,
            weight_overrides=weight_overrides,
            disabled_rules=disabled_rules,
            level_max_overrides=level_max_overrides,
            label=f"{label}#{index}",
        )
        rows.append(
            {
                "index": index,
                "baseline_level": report["baseline"]["level"],
                "variant_level": report["variant"]["level"],
                "baseline_score": report["baseline"]["score"],
                "variant_score": report["variant"]["score"],
                "score_delta": report["delta"]["score"],
                "level_flip": report["delta"]["level_flip"],
            }
        )
    matrix: dict[str, int] = {}
    for row in rows:
        key = f'{row["baseline_level"]}->{row["variant_level"]}'
        matrix[key] = matrix.get(key, 0) + 1
    flips = [row for row in rows if row["level_flip"]]
    movers = sorted(rows, key=lambda r: abs(r["score_delta"]), reverse=True)
    return {
        "scenario": label,
        "generated_at": _now_iso(),
        "population_size": len(rows),
        "transition_matrix": dict(sorted(matrix.items())),
        "flip_count": len(flips),
        "flip_rate": round(len(flips) / len(rows), 4) if rows else 0.0,
        "mean_score_delta": (
            round(sum(r["score_delta"] for r in rows) / len(rows), 4) if rows else 0.0
        ),
        "largest_movers": movers[: max(1, int(sample_limit))],
        "rows": rows,
        "summary": {
            "population_size": len(rows),
            "flip_count": len(flips),
            "flip_rate": round(len(flips) / len(rows), 4) if rows else 0.0,
            "levels_touched": sorted(
                {r["variant_level"] for r in rows} | {r["baseline_level"] for r in rows}
            ),
        },
    }


def compare_reports(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any]:
    """Diff two simulation reports so two candidate retunes can be ranked."""
    if not left or not right:
        return {"comparable": False, "reason": "one or both reports missing"}
    left_metrics = _report_metrics(left)
    right_metrics = _report_metrics(right)
    metric_diff = {
        metric: {"left": left_metrics[metric], "right": right_metrics[metric]}
        for metric in sorted(left_metrics)
        if left_metrics[metric] != right_metrics[metric]
    }
    left_verdict = safety_verdict(left)
    right_verdict = safety_verdict(right)
    return {
        "comparable": True,
        "left_scenario": left.get("scenario"),
        "right_scenario": right.get("scenario"),
        "metric_diff": metric_diff,
        "left_verdict": left_verdict["verdict"],
        "right_verdict": right_verdict["verdict"],
        "left_blocking_gates": left_verdict["blocking_gates"],
        "right_blocking_gates": right_verdict["blocking_gates"],
        "recommendation": (
            "right"
            if right_verdict["allowed"] and not left_verdict["allowed"]
            else "left"
            if left_verdict["allowed"] and not right_verdict["allowed"]
            else "both_blocked"
            if not left_verdict["allowed"] and not right_verdict["allowed"]
            else "either"
        ),
    }


# --- risk what-if ------------------------------------------------------------


def _apply_level_overrides(
    levels: list[dict[str, Any]],
    level_max_overrides: dict[str, int] | None,
) -> list[dict[str, Any]]:
    if not level_max_overrides:
        return list(levels)
    maxima = {bucket["level"]: bucket["max"] for bucket in levels}
    maxima.update({str(k): int(v) for k, v in level_max_overrides.items()})
    rebuilt = [
        {"level": bucket["level"], "max": maxima[bucket["level"]]}
        for bucket in levels
    ]
    return sorted(rebuilt, key=lambda bucket: bucket["max"])


def _factor_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["rule_id"]: f for f in result["factors"]}


def _changed_factors(
    baseline: dict[str, Any], variant: dict[str, Any]
) -> list[dict[str, Any]]:
    base_f = _factor_map(baseline)
    var_f = _factor_map(variant)
    changed: list[dict[str, Any]] = []
    for rule_id in sorted(set(base_f) | set(var_f)):
        b = base_f.get(rule_id)
        v = var_f.get(rule_id)
        if b is None or v is None:
            changed.append(
                {
                    "rule_id": rule_id,
                    "baseline": b,
                    "variant": v,
                    "change": "rule_removed" if v is None else "rule_added",
                }
            )
            continue
        if b["present"] != v["present"]:
            change = "fired" if v["present"] else "stopped_firing"
            changed.append(
                {"rule_id": rule_id, "baseline": b, "variant": v, "change": change}
            )
        elif b["weight"] != v["weight"]:
            changed.append(
                {
                    "rule_id": rule_id,
                    "baseline": b,
                    "variant": v,
                    "change": f"weight {b['weight']} -> {v['weight']}",
                }
            )
    return changed


def simulate_risk_whatif(
    context: dict[str, Any],
    *,
    base_rules: list[dict[str, Any]] | None = None,
    weight_overrides: dict[str, float] | None = None,
    disabled_rules: list[str] | None = None,
    level_max_overrides: dict[str, int] | None = None,
    label: str = "risk-what-if",
) -> dict[str, Any]:
    """Re-score a context under a modified copy of the rule table.

    `base_rules` defaults to the effective risk table (config + learned
    weights). Overrides are applied only to the variant table; the live weight
    state and config are never touched.
    """
    base = (
        [dict(rule) for rule in base_rules]
        if base_rules is not None
        else effective_risk_rules()
    )
    levels = [dict(bucket) for bucket in RISK_LEVELS]
    disabled = set(disabled_rules or [])
    overrides = weight_overrides or {}
    variant: list[dict[str, Any]] = []
    for rule in base:
        rule_id = rule["rule_id"]
        if rule_id in disabled:
            continue
        copy = dict(rule)
        if rule_id in overrides:
            copy["weight"] = float(overrides[rule_id])
        variant.append(copy)

    variant_levels = _apply_level_overrides(levels, level_max_overrides)
    baseline = score_with_config(base, context, levels=levels)
    variant_result = score_with_config(variant, context, levels=variant_levels)
    changed = _changed_factors(baseline, variant_result)
    score_delta = variant_result["score"] - baseline["score"]
    return {
        "scenario": label,
        "decision_type": "risk_score",
        "generated_at": _now_iso(),
        "baseline": {
            "score": baseline["score"],
            "level": baseline["level"],
            "fired_rules": [f["rule_id"] for f in baseline["factors"] if f["present"]],
        },
        "variant": {
            "score": variant_result["score"],
            "level": variant_result["level"],
            "fired_rules": [
                f["rule_id"] for f in variant_result["factors"] if f["present"]
            ],
        },
        "delta": {"score": score_delta, "level_flip": baseline["level"] != variant_result["level"]},
        "changed_factors": changed,
        "summary": {
            "score_delta": score_delta,
            "level_flip": baseline["level"] != variant_result["level"],
            "affected_rule_count": len(changed),
        },
    }


# --- retention what-if -------------------------------------------------------


def simulate_retention_whatif(
    policies: list[dict[str, Any]] | None = None,
    active_partitions: list[dict[str, Any]] | None = None,
    *,
    moment: datetime | None = None,
    retention_overrides: dict[str, int] | None = None,
    label: str = "retention-what-if",
) -> dict[str, Any]:
    """Replay partition drop/keep decisions under modified retention windows.

    Mirrors the drop rule used by the partition lifecycle
    (``age > retention -> drop``) but executes nothing: no DDL, no registry
    mutation. Partitions whose action flips are reported as ``changed``.
    """
    policies = [dict(p) for p in policies] if policies is not None else [dict(p) for p in PARTITION_POLICIES]
    partitions = list(active_partitions or [])
    moment = moment or datetime.now(timezone.utc)
    overrides = dict(retention_overrides or {})
    policy_by_id = {p["policy_id"]: p for p in policies}

    policy_rows: list[dict[str, Any]] = []
    partition_rows: list[dict[str, Any]] = []
    for policy in policies:
        base_ret = int(policy.get("retention_days", 90))
        var_ret = _retention_override_for(overrides, str(policy["policy_id"]), base_ret)
        policy_rows.append(
            {
                "policy_id": policy["policy_id"],
                "interval": policy.get("interval"),
                "table": policy.get("table"),
                "enabled": bool(policy.get("enabled", True)),
                "baseline_retention_days": base_ret,
                "variant_retention_days": var_ret,
                "retention_changed": var_ret != base_ret,
            }
        )

    for partition in partitions:
        policy = policy_by_id.get(partition.get("policy_id"))
        if policy is None or not policy.get("enabled", True):
            continue
        key = partition.get("key")
        interval = partition.get("interval") or policy.get("interval")
        end = period_end(interval, key)
        age_days = (moment - end).total_seconds() / 86400.0
        base_ret = int(policy.get("retention_days", 90))
        var_ret = _retention_override_for(overrides, str(policy["policy_id"]), base_ret)
        baseline_action = "drop" if age_days > base_ret else "keep"
        variant_action = "drop" if age_days > var_ret else "keep"
        changed = baseline_action != variant_action
        partition_rows.append(
            {
                "policy_id": policy["policy_id"],
                "partition": partition.get("partition") or f'{partition.get("table")}__{key}',
                "key": key,
                "interval": interval,
                "age_days": round(age_days, 2),
                "baseline_retention_days": base_ret,
                "variant_retention_days": var_ret,
                "baseline_action": baseline_action,
                "variant_action": variant_action,
                "changed": changed,
            }
        )

    changed = [row for row in partition_rows if row["changed"]]
    return {
        "scenario": label,
        "decision_type": "retention_action",
        "generated_at": _now_iso(),
        "moment": moment.isoformat(),
        "policies": policy_rows,
        "partitions": partition_rows,
        "changed_count": len(changed),
        "changed_partitions": changed,
        "summary": {
            "partition_count": len(partition_rows),
            "changed_count": len(changed),
            "would_drop": sum(1 for row in partition_rows if row["variant_action"] == "drop"),
        },
    }


# --- dispatch + tracing ------------------------------------------------------


def run_simulation(
    kind: str,
    *,
    label: str = "simulation",
    record: bool = True,
    modifier_id: str | dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a what-if simulation by kind and (optionally) record its trace.

    ``kind`` is ``risk`` or ``retention``; remaining kwargs flow to the
    matching simulator. ``modifier_id`` names a pack from
    ``SIMULATION_MODIFIERS``; explicit kwargs still win, so a caller can start
    from a pack and override one field. Returns the simulation report, with a
    ``verdict`` attached from the promotion gates.
    """
    if kind == "risk":
        pack: dict[str, Any] = {}
        if modifier_id is not None:
            pack = resolve_modifier(modifier_id, kind="risk")
        report = simulate_risk_whatif(
            dict(kwargs.get("context") or {}),
            base_rules=kwargs.get("base_rules"),
            weight_overrides={**pack.get("weight_overrides", {}), **(kwargs.get("weight_overrides") or {})} or None,
            disabled_rules=kwargs.get("disabled_rules") or pack.get("disabled_rules") or None,
            level_max_overrides=kwargs.get("level_max_overrides") or pack.get("level_max_overrides") or None,
            label=label,
        )
    elif kind == "retention":
        pack = {}
        if modifier_id is not None:
            pack = resolve_modifier(modifier_id, kind="retention")
        report = simulate_retention_whatif(
            kwargs.get("policies"),
            kwargs.get("active_partitions"),
            moment=kwargs.get("moment"),
            retention_overrides={**pack.get("retention_overrides", {}), **(kwargs.get("retention_overrides") or {})} or None,
            label=label,
        )
    else:
        raise ValueError(f"unknown simulation kind: {kind!r} (expected risk|retention)")

    report["verdict"] = safety_verdict(report)
    if pack.get("modifier_id"):
        report["modifier_id"] = pack["modifier_id"]

    if record:
        record_decision(
            decision_type="simulation",
            entity_ref=f"simulation:{kind}",
            outcome=report["summary"],
            score=report.get("variant", {}).get("score"),
            detail={"kind": kind, "scenario": label, "modifier": pack.get("modifier_id", "")},
        )
    return report


def build_simulation_engine_catalog() -> dict[str, Any]:
    """Introspectable contract for ``/meta/decisions`` and ``/meta/scoring-catalog``.

    ``kinds`` keeps its exact two-element list — it is a pinned public contract.
    The thin-group expansion is therefore surfaced under its own keys.
    """
    return {
        "kinds": list(SIMULATION_KINDS),
        "scenarios": {
            "risk": ["weight_override", "disable_rule", "level_retune"],
            "retention": ["retention_override"],
        },
        "retention_overrides": {
            "wildcard": RETENTION_OVERRIDE_WILDCARD,
            "precedence": ["specific_policy_id", "wildcard", "policy_baseline"],
            "formula_syntax": "=expression, evaluated with base = that policy's own retention_days",
            "match_against": "the live PARTITION_POLICIES ids",
            "helper": "unmatched_retention_overrides(overrides, policies) -> dead-config report",
            "note": (
                "an override key that names no policy is dropped silently, so a "
                "placeholder key looks exactly like a working one; check the helper "
                "rather than trusting the pack"
            ),
        },
        "inputs": {
            "risk": ["context", "weight_overrides", "disabled_rules", "level_max_overrides"],
            "retention": ["policies", "active_partitions", "retention_overrides", "moment"],
        },
        "note": "pure simulation — never mutates live config, weight state, or partitions",
        "modifiers": [dict(modifier) for modifier in SIMULATION_MODIFIERS],
        "scenario_library": [dict(scenario) for scenario in SCENARIO_LIBRARY],
        "promotion_gates": [dict(gate) for gate in PROMOTION_GATES],
        "analyses": {
            "run_named_scenario": "run_named_scenario(id) -> report + promotion verdict",
            "sweep_sensitivity": "sweep_sensitivity(context, rule_id=...) -> score curve",
            "simulate_population": "simulate_population(contexts) -> flip rate + movers",
            "compare_reports": "compare_reports(a, b) -> ranked diff of two retunes",
            "safety_verdict": "safety_verdict(report) -> promote | hold + failing gates",
            "resolve_modifier": "resolve_modifier(id) -> concrete simulator kwargs",
            "unmatched_retention_overrides": (
                "unmatched_retention_overrides(overrides) -> override keys that reach no policy"
            ),
        },
        "purity": "every function above is pure; only run_simulation records a trace",
    }