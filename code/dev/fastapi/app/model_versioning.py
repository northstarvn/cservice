"""Formal model versioning + shadow-mode canary (S-03).

Scoring and policy decisions are produced by *models* — a named, immutable
config snapshot (e.g. the effective risk-rule table). Changing a model live is
risky: a bad snapshot degrades every decision at once. This module formalizes
the lifecycle:

- ``ModelVersionRegistry`` — named models with immutable version snapshots.
  Active-state transitions (``promote`` / ``rollback``) run through an
  ``OptimisticLock`` so a concurrent promotion cannot clobber a winner
  (M-03 applied where it matters).
- ``CanaryRunner`` — runs the *candidate* model in shadow mode against the
  active model on the same context and records divergence/confidence. Served
  decisions keep coming from the active model until promotion — shadow-mode
  scoring, live. Auto-promotion exists behind ``CSERVICE_CANARY_AUTOPROMOTE``
  (default off); otherwise promotion is an explicit, guarded call.

The default registry is seeded with two models derived from live configuration:
``risk_rules`` (the effective risk table) and ``partition_policies`` (the
partition lifecycle policies), so canary/simulation tooling works out of the
box with zero setup.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

from app.explainability import (
    DecisionTrace,
    get_default_trace_store,
)
from app.optimistic_locking import VersionedRecord
from app.partition_manager import INTERVAL_FORMATS, PARTITION_POLICIES
from app.risk_evaluator import RISK_LEVELS, effective_risk_rules, score_with_config

CANARY_AUTOPROMOTE = os.getenv("CSERVICE_CANARY_AUTOPROMOTE", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DIVERGENCE_TOLERANCE = 0.15  # fraction of served score allowed before flagging
PROMOTE_THRESHOLD = 0.9  # minimum within-tolerance share to auto-promote
MIN_CANARY_RUNS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ModelEntry:
    def __init__(self, name: str, initial_config: dict[str, Any], label: str = ""):
        self.name = name
        self.snapshots: dict[int, dict[str, Any]] = {
            1: {
                "version": 1,
                "label": label or "initial",
                "state": "active",
                "created_at": _now_iso(),
                "config": dict(initial_config),
            }
        }
        self._next_version = 2
        self.active: int = 1
        self.canary: int | None = None
        self.lock = VersionedRecord(f"model:{name}", {"generation": 0})
        self._mutex = threading.Lock()

    def allocate_version(self, config: dict[str, Any], label: str = "") -> int:
        with self._mutex:
            version = self._next_version
            self._next_version += 1
            state = "canary"
            self.snapshots[version] = {
                "version": version,
                "label": label or f"v{version}",
                "state": state,
                "created_at": _now_iso(),
                "config": dict(config),
            }
            if self.canary is not None and self.canary != version:
                self.snapshots[self.canary]["state"] = "archived"
            self.canary = version
            return version

    def snapshot_config(self, version: int) -> dict[str, Any]:
        return dict(self.snapshots[version]["config"])

    def model_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "active_version": self.active,
            "canary_version": self.canary,
            "guard_version": self.lock.version,
            "versions": [
                {
                    "version": snap["version"],
                    "label": snap["label"],
                    "state": snap["state"],
                    "created_at": snap["created_at"],
                }
                for snap in sorted(self.snapshots.values(), key=lambda s: s["version"])
            ],
        }


class ModelVersionRegistry:
    """Named models with immutable snapshots and guarded promotion."""

    def __init__(self, models: dict[str, dict[str, Any]] | None = None):
        self._entries: dict[str, _ModelEntry] = {}
        for name, config in (models or {}).items():
            self.register(name, config)

    def models(self) -> list[str]:
        return sorted(self._entries)

    def register(
        self,
        name: str,
        initial_config: dict[str, Any] | None = None,
        *,
        label: str = "",
    ) -> dict[str, Any]:
        if name in self._entries:
            raise ValueError(f"model already registered: {name}")
        entry = _ModelEntry(name, initial_config or {}, label=label)
        self._entries[name] = entry
        return entry.model_info()

    def require_model(self, name: str) -> _ModelEntry:
        if name not in self._entries:
            raise ValueError(f"unknown model: {name}")
        return self._entries[name]

    def add_version(
        self,
        name: str,
        config: dict[str, Any],
        *,
        label: str = "",
    ) -> dict[str, Any]:
        """Register an immutable candidate snapshot; becomes the canary candidate.

        Appends a version — the current active config is never rewritten.
        """
        entry = self.require_model(name)
        entry.allocate_version(config, label=label)
        return entry.model_info()

    def active_snapshot(self, name: str) -> dict[str, Any]:
        entry = self.require_model(name)
        return entry.snapshot_config(entry.active)

    def canary_snapshot(self, name: str) -> dict[str, Any] | None:
        entry = self.require_model(name)
        if entry.canary is None:
            return None
        return entry.snapshot_config(entry.canary)

    def guard_version(self, name: str) -> int:
        return self.require_model(name).lock.version

    def promote(
        self,
        name: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Promote the canary candidate to active under an optimistic lock.

        ``expected_version`` defaults to the current guard version; callers that
        read it earlier get a ``StaleVersionError`` on conflict instead of a
        silent clobber. The former active version is archived.
        """
        entry = self.require_model(name)
        expected = (
            expected_version
            if expected_version is not None
            else entry.lock.version
        )
        old_active = entry.active

        def _promote(_data: dict[str, Any]) -> None:
            if entry.canary is None:
                raise ValueError(f"no canary candidate to promote for model {name!r}")
            promoted_version = entry.canary
            entry.snapshots[old_active]["state"] = "archived"
            entry.snapshots[promoted_version]["state"] = "active"
            entry.snapshots[promoted_version]["promoted_at"] = _now_iso()
            entry.active = promoted_version
            entry.canary = None

        entry.lock.update(expected, _promote)
        return {
            "model_name": name,
            "action": "promote",
            "from_version": old_active,
            "to_version": entry.active,
            "promoted_at": entry.snapshots[entry.active].get("promoted_at"),
            "guard_version": entry.lock.version,
        }

    def rollback(
        self,
        name: str,
        to_version: int,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Switch the active model back to a previously archived snapshot."""
        entry = self.require_model(name)
        if to_version not in entry.snapshots:
            raise ValueError(f"no snapshot v{to_version} for model {name!r}")
        if to_version == entry.active:
            raise ValueError(f"model {name!r} is already on v{to_version}")
        expected = (
            expected_version
            if expected_version is not None
            else entry.lock.version
        )
        old_active = entry.active

        def _rollback(_data: dict[str, Any]) -> None:
            entry.snapshots[old_active]["state"] = "archived"
            if entry.canary == to_version:
                entry.canary = None
            entry.snapshots[to_version]["state"] = "active"
            entry.snapshots[to_version]["promoted_at"] = _now_iso()
            entry.active = to_version

        entry.lock.update(expected, _rollback)
        return {
            "model_name": name,
            "action": "rollback",
            "from_version": old_active,
            "to_version": entry.active,
            "guard_version": entry.lock.version,
        }

    def model(self, name: str) -> dict[str, Any]:
        return self.require_model(name).model_info()

    def list_models(self) -> list[dict[str, Any]]:
        return [self._entries[name].model_info() for name in self.models()]


def seed_default_registry() -> ModelVersionRegistry:
    """Registry pre-seeded from live config: risk rules + partition policies."""
    registry = ModelVersionRegistry()
    registry.register(
        "risk_rules",
        {"rules": effective_risk_rules(), "levels": list(RISK_LEVELS)},
        label="effective risk rule table (config + learned weights)",
    )
    registry.register(
        "partition_policies",
        {
            "policies": [dict(p) for p in PARTITION_POLICIES],
            "intervals": list(INTERVAL_FORMATS),
        },
        label="partition lifecycle policies",
    )
    return registry


_default_registry: ModelVersionRegistry | None = None


def get_default_registry() -> ModelVersionRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = seed_default_registry()
    return _default_registry


def set_default_registry(registry: ModelVersionRegistry | None) -> None:
    global _default_registry
    _default_registry = registry


class CanaryRunner:
    """Shadow-mode scoring: candidate model vs active, divergence + confidence."""

    def __init__(
        self,
        registry: ModelVersionRegistry | None = None,
        *,
        divergence_tolerance: float = DIVERGENCE_TOLERANCE,
        promote_threshold: float = PROMOTE_THRESHOLD,
        min_runs: int = MIN_CANARY_RUNS,
        auto_promote: bool = CANARY_AUTOPROMOTE,
        history_capacity: int = 200,
    ):
        self.registry = registry if registry is not None else get_default_registry()
        self.divergence_tolerance = float(divergence_tolerance)
        self.promote_threshold = float(promote_threshold)
        self.min_runs = max(1, int(min_runs))
        self.auto_promote = bool(auto_promote)
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._history_capacity = max(1, int(history_capacity))

    def _config_rules(self, model_name: str, config: dict[str, Any]) -> list[dict[str, Any]]:
        if "rules" not in config:
            raise ValueError(f"model {model_name!r} is not scoreable (no 'rules')")
        return config["rules"]

    def shadow_score(
        self,
        model_name: str,
        context: dict[str, Any],
        *,
        entity_ref: str = "",
        record_trace: bool = True,
    ) -> dict[str, Any]:
        """Score with the canary candidate in shadow; serve from the active model.

        Returns the served (active) decision, the shadow (candidate) decision,
        the delta, whether the level flipped, tolerance status, and running
        canary confidence. Never mutates the active model; may auto-promote only
        when ``auto_promote`` is enabled and confidence clears the bar.
        """
        self.registry.require_model(model_name)
        canary_config = self.registry.canary_snapshot(model_name)
        if canary_config is None:
            return {
                "model_name": model_name,
                "canary": None,
                "reason": "no_canary_candidate",
            }
        active_config = self.registry.active_snapshot(model_name)
        served = score_with_config(
            self._config_rules(model_name, active_config),
            context,
            levels=active_config.get("levels"),
        )
        shadow = score_with_config(
            self._config_rules(model_name, canary_config),
            context,
            levels=canary_config.get("levels"),
        )
        delta = shadow["score"] - served["score"]
        level_flip = served["level"] != shadow["level"]
        within_tolerance = abs(delta) <= self.divergence_tolerance * max(
            1, served["score"]
        )
        active_version = self.registry.model(model_name)["active_version"]
        canary_version = self.registry.model(model_name)["canary_version"]

        self._history.setdefault(model_name, deque(maxlen=self._history_capacity))
        self._history[model_name].append(
            {
                "active_version": active_version,
                "canary_version": canary_version,
                "delta": delta,
                "level_flip": level_flip,
                "within_tolerance": within_tolerance,
                "at": _now_iso(),
            }
        )

        if record_trace:
            self._record_shadow_trace(
                model_name, context, entity_ref, active_version, canary_version,
                served, shadow, delta, level_flip, within_tolerance,
            )

        result: dict[str, Any] = {
            "model_name": model_name,
            "active_version": active_version,
            "canary_version": canary_version,
            "served": served,
            "shadow": shadow,
            "delta": delta,
            "level_flip": level_flip,
            "within_tolerance": within_tolerance,
            "divergence_tolerance": self.divergence_tolerance,
            "confidence": self.canary_confidence(model_name),
            "runs": len(self._history[model_name]),
        }

        if self.auto_promote and self._should_auto_promote(model_name):
            result["promoted"] = True
            result["promotion"] = self.registry.promote(model_name)
        return result

    def _record_shadow_trace(
        self,
        model_name: str,
        context: dict[str, Any],
        entity_ref: str,
        active_version: int,
        canary_version: int,
        served: dict[str, Any],
        shadow: dict[str, Any],
        delta: int,
        level_flip: bool,
        within_tolerance: bool,
    ) -> None:
        trace = DecisionTrace(
            decision_type="canary_score",
            entity_ref=entity_ref or f"model:{model_name}",
            outcome="within_tolerance" if within_tolerance else "divergent",
            score=shadow["score"],
            threshold=served["score"],
            model_version=f"active:v{active_version}/canary:v{canary_version}",
            factors=list(shadow.get("factors", [])),
            inputs=dict(context),
            detail={
                "delta": delta,
                "level_flip": level_flip,
                "served_level": served["level"],
                "shadow_level": shadow["level"],
            },
        )
        get_default_trace_store().record(trace)

    def _should_auto_promote(self, model_name: str) -> bool:
        history = self._history.get(model_name, [])
        if len(history) < self.min_runs:
            return False
        confidence = self.canary_confidence(model_name)
        return (confidence or 0.0) >= self.promote_threshold

    def canary_confidence(self, model_name: str) -> float | None:
        history = self._history.get(model_name, [])
        if not history:
            return None
        return sum(1 for h in history if h["within_tolerance"]) / len(history)

    def canary_stats(self, model_name: str) -> dict[str, Any]:
        history = self._history.get(model_name, [])
        return {
            "model_name": model_name,
            "runs": len(history),
            "within_tolerance_runs": sum(1 for h in history if h["within_tolerance"]),
            "confidence": self.canary_confidence(model_name),
            "total_delta": round(sum(h["delta"] for h in history), 2),
            "level_flips": sum(1 for h in history if h["level_flip"]),
        }


_default_runner: CanaryRunner | None = None


def get_default_runner() -> CanaryRunner:
    global _default_runner
    if _default_runner is None:
        _default_runner = CanaryRunner()
    # Re-bind to the current default registry so swapped registries take effect.
    _default_runner.registry = get_default_registry()
    return _default_runner


def set_default_runner(runner: CanaryRunner | None) -> None:
    global _default_runner
    _default_runner = runner


def build_canary_catalog() -> dict[str, Any]:
    runner = get_default_runner()
    registry = get_default_registry()
    stats = {
        name: runner.canary_stats(name)
        for name in registry.models()
    }
    return {
        "enabled": True,
        "auto_promote": runner.auto_promote,
        "divergence_tolerance": runner.divergence_tolerance,
        "promote_threshold": runner.promote_threshold,
        "min_runs": runner.min_runs,
        "prerequisite_env": "CSERVICE_CANARY_AUTOPROMOTE",
        "stats": stats,
    }


def build_model_versioning_catalog() -> dict[str, Any]:
    registry = get_default_registry()
    return {
        "models": registry.list_models(),
        "canary": build_canary_catalog(),
    }