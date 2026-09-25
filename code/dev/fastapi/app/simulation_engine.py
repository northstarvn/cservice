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
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.explainability import record_decision
from app.partition_manager import PARTITION_POLICIES, period_end
from app.risk_evaluator import RISK_LEVELS, effective_risk_rules, score_with_config

SIMULATION_KINDS = ("risk", "retention")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    overrides = retention_overrides or {}
    policy_by_id = {p["policy_id"]: p for p in policies}

    policy_rows: list[dict[str, Any]] = []
    partition_rows: list[dict[str, Any]] = []
    for policy in policies:
        base_ret = int(policy.get("retention_days", 90))
        var_ret = int(overrides.get(policy["policy_id"], base_ret))
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
        var_ret = int(overrides.get(policy["policy_id"], base_ret))
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
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a what-if simulation by kind and (optionally) record its trace.

    ``kind`` is ``risk`` or ``retention``; remaining kwargs flow to the
    matching simulator. Returns the simulation report.
    """
    if kind == "risk":
        report = simulate_risk_whatif(
            dict(kwargs.get("context") or {}),
            base_rules=kwargs.get("base_rules"),
            weight_overrides=kwargs.get("weight_overrides"),
            disabled_rules=kwargs.get("disabled_rules"),
            level_max_overrides=kwargs.get("level_max_overrides"),
            label=label,
        )
    elif kind == "retention":
        report = simulate_retention_whatif(
            kwargs.get("policies"),
            kwargs.get("active_partitions"),
            moment=kwargs.get("moment"),
            retention_overrides=kwargs.get("retention_overrides"),
            label=label,
        )
    else:
        raise ValueError(f"unknown simulation kind: {kind!r} (expected risk|retention)")

    if record:
        record_decision(
            decision_type="simulation",
            entity_ref=f"simulation:{kind}",
            outcome=report["summary"],
            score=report.get("variant", {}).get("score"),
            detail={"kind": kind, "scenario": label},
        )
    return report


def build_simulation_engine_catalog() -> dict[str, Any]:
    return {
        "kinds": list(SIMULATION_KINDS),
        "scenarios": {
            "risk": ["weight_override", "disable_rule", "level_retune"],
            "retention": ["retention_override"],
        },
        "inputs": {
            "risk": ["context", "weight_overrides", "disabled_rules", "level_max_overrides"],
            "retention": ["policies", "active_partitions", "retention_overrides", "moment"],
        },
        "note": "pure simulation — never mutates live config, weight state, or partitions",
    }