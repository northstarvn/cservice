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

Around that core this module is *config-driven*: the lifecycle decisions an
operator normally hardcodes (when a candidate may go live, how far a rollback
may travel, when an archived snapshot becomes obsolete, when history may be
dropped, how much traffic a candidate may serve) live in the declarative tables
below — ``PROMOTION_GATES``, ``ROLLBACK_POLICIES``, ``DEPRECATION_POLICIES``,
``PRUNE_POLICIES``, ``TRAFFIC_SPLITS`` — and the pure helpers evaluate them.
Nothing here is implicit: every gate names the metric it reads, the operator it
applies, its threshold, and a severity, so a verdict can always explain itself.

A note on severity, because it is the whole point of the gate table:

- ``blocking`` — the promotion must not happen at all (no candidate, no
  rollback target left behind).
- ``auto`` — the promotion is *legitimate* but must be a human call; this is
  what makes the verdict ``manual`` rather than ``auto``.
- ``advisory`` — reported, never decisive.

The v1 behaviour above is untouched: ``promote``/``rollback`` keep their exact
guards and ``shadow_score`` keeps serving from active. The tables only decide
what a caller is *told*.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from app.explainability import (
    DecisionTrace,
    get_default_trace_store,
)
from app.optimistic_locking import (
    DEFAULT_DIFF_DEPTH,
    VersionedRecord,
    deep_diff,
    diff_paths,
)
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

# --- lifecycle vocabulary ---------------------------------------------------

#: Risk severity ordering, straight from the live level table, so "the shadow
#: scored *worse* than the served model" is answerable without hardcoding words.
LEVEL_ORDER: tuple[str, ...] = tuple(row["level"] for row in RISK_LEVELS)
LEVEL_RANK: dict[str, int] = {level: rank for rank, level in enumerate(LEVEL_ORDER)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- declarative lifecycle config -------------------------------------------

#: The three states actually stored on a snapshot, plus three *derived* states
#: this module can report without ever writing them back to the registry.
VERSION_STATES: tuple[dict[str, Any], ...] = (
    {
        "state": "active",
        "stored": True,
        "serving": True,
        "rollback_target": False,
        "prunable": False,
        "description": "the snapshot every served decision is scored from",
    },
    {
        "state": "canary",
        "stored": True,
        "serving": False,
        "rollback_target": False,
        "prunable": False,
        "description": "the candidate, compared in shadow mode against active",
    },
    {
        "state": "archived",
        "stored": True,
        "serving": False,
        "rollback_target": True,
        "prunable": True,
        "description": "immutable history; the pool a rollback may draw from",
    },
    {
        "state": "deprecated",
        "stored": False,
        "serving": False,
        "rollback_target": True,
        "prunable": False,
        "description": "derived: archived but still inside the notice window",
    },
    {
        "state": "obsolete",
        "stored": False,
        "serving": False,
        "rollback_target": False,
        "prunable": True,
        "description": "derived: archived past the notice window; rolling back to it needs a permissive policy",
    },
    {
        "state": "pruned",
        "stored": False,
        "serving": False,
        "rollback_target": False,
        "prunable": False,
        "description": "derived: dropped from the registry; the changelog is the only remaining record",
    },
)
VERSION_STATE_BY_NAME: dict[str, dict[str, Any]] = {
    row["state"]: dict(row) for row in VERSION_STATES
}
STORED_VERSION_STATES: tuple[str, ...] = tuple(
    row["state"] for row in VERSION_STATES if row["stored"]
)
DERIVED_VERSION_STATES: tuple[str, ...] = tuple(
    row["state"] for row in VERSION_STATES if not row["stored"]
)

#: Operators a gate may use. Declared (rather than computed from ``operator``)
#: so a typo in a gate row is a visible unknown name, not a runtime surprise.
GATE_OPS: dict[str, str] = {
    "gte": "metric >= threshold",
    "gt": "metric > threshold",
    "lte": "metric <= threshold",
    "lt": "metric < threshold",
    "eq": "metric == threshold",
    "neq": "metric != threshold",
    "abs_lte": "abs(metric) <= threshold; for signed drift metrics",
    "in": "metric is one of threshold (a list)",
    "not_in": "metric is not one of threshold (a list)",
    "is_true": "metric is truthy; threshold ignored",
    "is_false": "metric is falsy; threshold ignored",
    "is_none": "metric is missing or None",
    "present": "the metric key exists at all",
}
GATE_OP_NAMES: tuple[str, ...] = tuple(GATE_OPS)

#: Ordered weakest -> strongest, so a verdict can be computed by max().
GATE_SEVERITIES: tuple[str, ...] = ("advisory", "auto", "blocking")
GATE_SEVERITY_RANK: dict[str, int] = {
    name: rank for rank, name in enumerate(GATE_SEVERITIES)
}

#: What a candidate must clear before promotion. ``models: ["*"]`` matches every
#: model; a list narrows the row to those models. Every row carries a rationale
#: because a gate nobody can explain is a gate nobody will trust.
PROMOTION_GATES: tuple[dict[str, Any], ...] = (
    {
        "gate_id": "candidate_present",
        "models": ("*",),
        "metric": "has_canary",
        "op": "is_true",
        "threshold": True,
        "severity": "blocking",
        "rationale": "there is nothing to promote; promote() would raise",
    },
    {
        "gate_id": "min_shadow_runs",
        "models": ("*",),
        "metric": "runs",
        "op": "gte",
        "threshold": MIN_CANARY_RUNS,
        "severity": "auto",
        "rationale": "confidence over one or two runs is noise, not evidence; this is the same floor CanaryRunner applies to itself",
    },
    {
        "gate_id": "rollback_target_available",
        "models": ("*",),
        "metric": "has_rollback_target",
        "op": "is_true",
        "severity": "auto",
        "threshold": True,
        "rationale": "promoting with no archived snapshot leaves no way back",
    },
    {
        "gate_id": "auto_promote_enabled",
        "models": ("*",),
        "metric": "auto_promote_enabled",
        "op": "is_true",
        "severity": "auto",
        "threshold": True,
        "rationale": "auto-promotion is opt-in via CSERVICE_CANARY_AUTOPROMOTE; the verdict can never be 'auto' while it is off",
    },
    {
        "gate_id": "min_confidence",
        "models": ("*",),
        "metric": "confidence",
        "op": "gte",
        "threshold": PROMOTE_THRESHOLD,
        "severity": "auto",
        "rationale": "the share of shadow runs that stayed inside the divergence tolerance",
    },
    {
        "gate_id": "max_level_flip_rate",
        "models": ("*",),
        "metric": "level_flip_rate",
        "op": "lte",
        "threshold": 0.10,
        "severity": "auto",
        "rationale": "a candidate that keeps re-banding the same context is not a small change",
    },
    {
        "gate_id": "max_escalation_rate",
        "models": ("*",),
        "metric": "escalation_rate",
        "op": "lte",
        "threshold": 0.05,
        "severity": "auto",
        "rationale": "fraction of runs where the shadow banded *riskier* than the served model",
    },
    {
        "gate_id": "max_mean_abs_delta",
        "models": ("*",),
        "metric": "mean_abs_delta",
        "op": "lte",
        "threshold": 5.0,
        "severity": "auto",
        "rationale": "average absolute score drift, in raw score points",
    },
    {
        "gate_id": "max_config_diff_paths",
        "models": ("*",),
        "metric": "config_diff_paths",
        "op": "lte",
        "threshold": 12,
        "severity": "advisory",
        "rationale": "a wide config diff is a review signal, not a stop; the reviewer is the gate",
    },
    {
        "gate_id": "max_single_weight_move",
        "models": ("risk_rules",),
        "metric": "max_weight_move",
        "op": "abs_lte",
        "threshold": 15.0,
        "severity": "advisory",
        "rationale": "largest single-rule weight change; only meaningful for a rules table",
    },
    {
        "gate_id": "no_unknown_rule_ids",
        "models": ("risk_rules",),
        "metric": "unknown_rule_ids",
        "op": "eq",
        "threshold": 0,
        "severity": "blocking",
        "rationale": "a candidate that drops or invents rule ids is a different model, not a new version",
    },
)
PROMOTION_GATE_BY_ID: dict[str, dict[str, Any]] = {
    row["gate_id"]: dict(row) for row in PROMOTION_GATES
}

#: How far a rollback may travel and how soon after a promotion.
ROLLBACK_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "policy_id": "default",
        "max_distance": None,  # no ceiling: any archived version is reachable
        "older_only": True,
        "require_archived": True,
        "cooldown_seconds": 0,  # a rollback may follow a promotion immediately
        "min_keep_archived": 1,  # always leave at least this many rollback targets behind
        "description": "the unguarded default; policy is advisory, rollback() itself stays unguarded",
    },
    {
        "policy_id": "conservative",
        "max_distance": 1,
        "older_only": True,
        "require_archived": True,
        "cooldown_seconds": 300,
        "min_keep_archived": 2,
        "description": "one version back, five minutes after a promotion, two targets retained",
    },
    {
        "policy_id": "permissive",
        "max_distance": None,
        "older_only": False,
        "require_archived": False,
        "cooldown_seconds": 0,
        "min_keep_archived": 0,
        "description": "any snapshot, including obsolete ones and forward to the canary",
    },
)
DEFAULT_ROLLBACK_POLICY = "default"
ROLLBACK_POLICY_BY_ID: dict[str, dict[str, Any]] = {
    row["policy_id"]: dict(row) for row in ROLLBACK_POLICIES
}

#: How long an archived snapshot stays useful before it is called obsolete.
DEPRECATION_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "policy_id": "default",
        "notice_versions": 2,  # newest archived versions that stay rollback-eligible
        "obsolete_after_distance": 3,  # versions behind active before "obsolete"
        "min_keep_archived": 2,
        "description": "two archived versions stay one rollback away; anything three behind is obsolete",
    },
    {
        "policy_id": "strict",
        "notice_versions": 1,
        "obsolete_after_distance": 2,
        "min_keep_archived": 3,
        "description": "history is short and mostly obsolete; favours a small registry",
    },
    {
        "policy_id": "permissive",
        "notice_versions": 5,
        "obsolete_after_distance": 10,
        "description": "long-lived rollback reach; favours safety over registry size",
    },
)
DEFAULT_DEPRECATION_POLICY = "default"
DEPRECATION_POLICY_BY_ID: dict[str, dict[str, Any]] = {
    row["policy_id"]: dict(row) for row in DEPRECATION_POLICIES
}

#: When archived history may actually be dropped. A version survives unless it
#: is in ``prune_states`` *and* outside every retention bound, so each bound
#: alone can never delete the last rollback target.
PRUNE_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "policy_id": "default",
        "deprecation_policy": "default",
        "prune_states": ("obsolete",),
        "keep_last": 5,
        "keep_archive_days": 30,
        "min_keep_archived": 2,
        "description": "drop only obsolete snapshots older than 30 days, keeping the newest five",
    },
    {
        "policy_id": "aggressive",
        "deprecation_policy": "strict",
        "prune_states": ("deprecated", "obsolete"),
        "keep_last": 3,
        "keep_archive_days": 7,
        "min_keep_archived": 1,
        "description": "short history; prunes as soon as a snapshot leaves the notice window",
    },
    {
        "policy_id": "append_only",
        "deprecation_policy": "default",
        "prune_states": (),
        "keep_last": None,
        "keep_archive_days": None,
        "min_keep_archived": None,
        "description": "never prunes; the registry is the audit trail",
    },
)
DEFAULT_PRUNE_POLICY = "default"
PRUNE_POLICY_BY_ID: dict[str, dict[str, Any]] = {
    row["policy_id"]: dict(row) for row in PRUNE_POLICIES
}

#: Staged rollout, per model. ``canary_weight`` is the share of subjects the
#: candidate serves; active keeps the remainder. ``max_step`` is the widest
#: single increase :func:`next_stage` will allow without inserting a hold.
DEFAULT_BUCKET_SALT = "cservice.model_versioning"
BUCKET_RESOLUTION = 10_000  # weights are expressed in units of 1/10000
TRAFFIC_SPLITS: tuple[dict[str, Any], ...] = (
    {
        "split_id": "risk_rules_ramp",
        "model": "risk_rules",
        "strategy": "ramp",
        "bucketing": "stable-hash",
        "salt": DEFAULT_BUCKET_SALT,
        "max_step": 0.25,
        "stages": (
            {"stage": "shadow", "canary_weight": 0.0, "description": "score in shadow, serve from active"},
            {"stage": "canary", "canary_weight": 0.05, "description": "first real traffic"},
            {"stage": "quarter", "canary_weight": 0.25, "description": "a quarter of subjects"},
            {"stage": "half", "canary_weight": 0.5, "description": "even split"},
            {"stage": "full", "canary_weight": 1.0, "description": "candidate is the only snapshot served"},
        ),
    },
    {
        "split_id": "partition_policies_holdback",
        "model": "partition_policies",
        "strategy": "holdback",
        "bucketing": "stable-hash",
        "salt": DEFAULT_BUCKET_SALT,
        "max_step": 0.1,
        "stages": (
            {"stage": "shadow", "canary_weight": 0.0, "description": "score in shadow, serve from active"},
            {"stage": "holdback", "canary_weight": 0.02, "description": "2% canary, 98% control"},
            {"stage": "full", "canary_weight": 1.0, "description": "candidate is the only snapshot served"},
        ),
    },
)
DEFAULT_TRAFFIC_SPLIT: dict[str, Any] = {
    "split_id": "default_ramp",
    "model": "*",
    "strategy": "ramp",
    "bucketing": "stable-hash",
    "salt": DEFAULT_BUCKET_SALT,
    "max_step": 0.5,
    "stages": (
        {"stage": "shadow", "canary_weight": 0.0},
        {"stage": "half", "canary_weight": 0.5},
        {"stage": "full", "canary_weight": 1.0},
    ),
}
TRAFFIC_SPLIT_BY_MODEL: dict[str, dict[str, Any]] = {
    row["model"]: dict(row) for row in TRAFFIC_SPLITS
}

#: Version labels. "plain" renders exactly the registry's implicit default
#: (``v{version}``), so adopting a template cannot change existing labels.
LABEL_TEMPLATES: tuple[dict[str, Any], ...] = (
    {"template_id": "plain", "pattern": "v{version}", "description": "matches the registry's implicit default"},
    {"template_id": "dated", "pattern": "v{version}-{date}", "description": "v3-20260926"},
    {"template_id": "dated_label", "pattern": "{label}-v{version}-{date}", "description": "tightened-v3-20260926"},
    {"template_id": "sha", "pattern": "v{version}-sha:{digest}", "description": "digest is supplied by the caller"},
)
DEFAULT_LABEL_TEMPLATE = "plain"
LABEL_TEMPLATE_BY_ID: dict[str, dict[str, Any]] = {
    row["template_id"]: dict(row) for row in LABEL_TEMPLATES
}

#: Renderings available for a version diff (see :func:`render_diff`).
DIFF_FORMATS: tuple[str, ...] = ("json", "summary", "unified", "weights")


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

    def snapshot_meta(self, version: int) -> dict[str, Any]:
        """Snapshot bookkeeping *without* the config body (timestamps, label)."""
        if version not in self.snapshots:
            raise ValueError(f"no snapshot v{version} for model {self.name!r}")
        snap = self.snapshots[version]
        return {key: value for key, value in snap.items() if key != "config"}

    def drop_versions(self, versions: Any) -> list[int]:
        """Delete the given *archived* snapshots; never active or canary.

        Used by :meth:`ModelVersionRegistry.prune_versions`. Refuses to touch a
        serving snapshot so a prune policy can never strand the model.
        """
        removed: list[int] = []
        for version in sorted(int(v) for v in versions):
            if version in (self.active, self.canary):
                continue
            if version in self.snapshots:
                del self.snapshots[version]
                removed.append(version)
        return removed

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

    def version_states(self, name: str, *, policy: str | None = None) -> list[dict[str, Any]]:
        """Per-version rows with the *derived* state resolved (read-only).

        ``model_info()`` stays exactly as it was — this is the richer view that
        also reports distance, effective state and rollback reach.
        """
        entry = self.require_model(name)
        deprecation = deprecation_policy(policy)
        rows: list[dict[str, Any]] = []
        for version in sorted(entry.snapshots):
            stored = entry.snapshots[version]["state"]
            distance = entry.active - version
            effective = version_state(
                version,
                active=entry.active,
                canary=entry.canary,
                stored=stored,
                distance=distance,
                policy=deprecation,
            )
            rows.append(
                {
                    "version": version,
                    "label": entry.snapshots[version]["label"],
                    "state": stored,
                    "effective_state": effective,
                    "created_at": entry.snapshots[version]["created_at"],
                    "promoted_at": entry.snapshots[version].get("promoted_at"),
                    "distance_from_active": distance,
                    "serving": version == entry.active,
                    "rollback_reachable": VERSION_STATE_BY_NAME[effective]["rollback_target"],
                    "prunable": VERSION_STATE_BY_NAME[effective]["prunable"],
                }
            )
        return rows

    def version_config(self, name: str, version: int) -> dict[str, Any]:
        return self.require_model(name).snapshot_config(int(version))

    def prune_versions(
        self,
        name: str,
        *,
        policy: str | None = None,
        dry_run: bool = True,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Drop prunable archived snapshots. ``dry_run`` defaults to a plan only.

        The registry lifecycle itself is untouched: only snapshots the prune
        policy already calls prunable are removed, and never the active or
        canary one. Set ``dry_run=False`` to actually delete.
        """
        plan = prune_plan(name, policy=policy, registry=self, as_of=as_of)
        if dry_run:
            return plan
        removed = self.require_model(name).drop_versions(plan["prunable"])
        applied = prune_plan(name, policy=policy, registry=self, as_of=as_of)
        return {**applied, "removed": removed, "dry_run": False}

    def split_snapshot(
        self,
        name: str,
        subject_key: Any,
        *,
        stage: str | None = None,
        splits: Any = None,
    ) -> dict[str, Any]:
        """The snapshot one subject should be served from under a traffic split.

        Read-side only: the plan and the bucket decide, nothing is promoted.
        The subject key is not echoed back in the result, and the same key
        always resolves to the same version while the plan is unchanged.
        """
        plan = traffic_plan(name, stage=stage, registry=self, splits=splits)
        assignment = select_version(plan, subject_key)
        version = assignment["version"]
        if version is None:
            raise ValueError(f"model {name!r} has no active snapshot to serve")
        meta = self.require_model(name).snapshot_meta(version)
        return {
            **assignment,
            "label": meta.get("label"),
            "state": meta.get("state"),
            "config": self.version_config(name, version),
        }


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


#: The exact shape :meth:`CanaryRunner.canary_metrics` reports before any shadow
#: run exists, so "no evidence" is a value a gate can read rather than a gap.
NO_HISTORY_METRICS: dict[str, Any] = {
    "runs": 0,
    "confidence": None,
    "level_flip_rate": None,
    "escalation_rate": None,
    "mean_delta": None,
    "mean_abs_delta": None,
    "max_abs_delta": None,
    "total_delta": 0.0,
    "level_flips": 0,
    "escalations": 0,
    "de_escalations": 0,
    "level_moves": {},
}


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
                "served_level": served["level"],
                "shadow_level": shadow["level"],
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

    def canary_metrics(self, model_name: str) -> dict[str, Any]:
        """The derived rates :mod:`PROMOTION_GATES` read, from canary history.

        ``canary_stats`` above is the pinned summary and stays as it was; this
        is the extra shape the gate table needs — rates instead of raw counts,
        plus drift magnitudes and the direction of each level change (an
        *escalation* is a run where the shadow banded riskier than the served
        model, which is the failure mode that matters).
        """
        history = list(self._history.get(model_name, []))
        runs = len(history)
        if not runs:
            return dict(NO_HISTORY_METRICS)
        deltas = [float(h["delta"]) for h in history]
        flips = [h for h in history if h["level_flip"]]
        escalations = [
            h
            for h in flips
            if LEVEL_RANK.get(h.get("shadow_level", ""), 0)
            > LEVEL_RANK.get(h.get("served_level", ""), 0)
        ]
        de_escalations = [
            h
            for h in flips
            if LEVEL_RANK.get(h.get("shadow_level", ""), 0)
            < LEVEL_RANK.get(h.get("served_level", ""), 0)
        ]
        moves: dict[str, int] = {}
        for hit in flips:
            key = f"{hit.get('served_level')}->{hit.get('shadow_level')}"
            moves[key] = moves.get(key, 0) + 1
        return {
            "runs": runs,
            "confidence": self.canary_confidence(model_name),
            "level_flip_rate": round(len(flips) / runs, 6),
            "escalation_rate": round(len(escalations) / runs, 6),
            "mean_delta": round(sum(deltas) / runs, 4),
            "mean_abs_delta": round(sum(abs(d) for d in deltas) / runs, 4),
            "max_abs_delta": round(max(abs(d) for d in deltas), 4),
            "total_delta": round(sum(deltas), 2),
            "level_flips": len(flips),
            "escalations": len(escalations),
            "de_escalations": len(de_escalations),
            "level_moves": moves,
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


# --- pure helpers: gates, diffs, rollback, deprecation, prune, traffic --------
#
# Everything below is a function of (registry state, config table). Nothing here
# promotes, rolls back or deletes on its own: they answer questions, and the
# mutating registry methods stay exactly as guarded as they were in v1.


def _as_number(value: Any) -> Optional[float]:
    """Coerce to float, or None. Booleans are never numbers here."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(stamp: Any) -> Optional[datetime]:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_ref(registry: ModelVersionRegistry, name: str, ref: Any) -> dict[str, Any]:
    """Resolve a version reference: an int, or the role names active/canary."""
    if ref is None:
        version = registry.model(name)["active_version"]
    elif isinstance(ref, str):
        info = registry.model(name)
        role = ref.strip().lower()
        if role == "active":
            version = info["active_version"]
        elif role in {"canary", "candidate"}:
            version = info["canary_version"]
            if version is None:
                raise ValueError(f"model {name!r} has no canary candidate")
        else:
            version = int(ref)
    else:
        version = int(ref)
    entry = registry.require_model(name)
    if version not in entry.snapshots:
        raise ValueError(f"no snapshot v{version} for model {name!r}")
    return {
        "version": version,
        "role": "active" if version == entry.active else "candidate",
        "label": entry.snapshots[version]["label"],
    }


def _rules_of(config: Any) -> Optional[list[dict[str, Any]]]:
    """The ``rules`` list of a snapshot, if it is shaped like a rule table."""
    rules = config.get("rules") if isinstance(config, dict) else None
    if not isinstance(rules, list) or not rules:
        return None
    if not all(isinstance(rule, dict) and "rule_id" in rule for rule in rules):
        return None
    return rules


def weight_moves(
    before: Any,
    after: Any,
    *,
    id_key: str = "rule_id",
    weight_key: str = "weight",
) -> list[dict[str, Any]]:
    """Rule-level weight moves between two snapshots, paired by ``rule_id``.

    ``deep_diff`` reports a whole ``rules`` list as a single changed leaf, which
    is useless for a review ("weights moved" tells an operator nothing). This
    pairs the two tables by id and reports the per-rule delta, which is what the
    ``max_single_weight_move`` gate and the ``weights`` diff format read.
    Accepts either a snapshot config or a bare list of rules.
    """
    left = _rules_of(before) or (before if isinstance(before, list) else None)
    right = _rules_of(after) or (after if isinstance(after, list) else None)
    if left is None or right is None:
        return []
    before_by_id = {rule.get(id_key): rule for rule in left}
    after_by_id = {rule.get(id_key): rule for rule in right}
    moves: list[dict[str, Any]] = []
    for rule_id in sorted(
        set(before_by_id) | set(after_by_id), key=lambda value: (value is None, str(value))
    ):
        old = before_by_id.get(rule_id)
        new = after_by_id.get(rule_id)
        old_weight = _as_number((old or {}).get(weight_key))
        new_weight = _as_number((new or {}).get(weight_key))
        if old is None:
            moves.append(
                {
                    "rule_id": rule_id,
                    "change": "added",
                    "from": None,
                    "to": new_weight,
                    "delta": new_weight,
                }
            )
            continue
        if new is None:
            moves.append(
                {
                    "rule_id": rule_id,
                    "change": "removed",
                    "from": old_weight,
                    "to": None,
                    "delta": None if old_weight is None else -old_weight,
                }
            )
            continue
        if old_weight is None or new_weight is None:
            continue
        delta = round(new_weight - old_weight, 6)
        if delta:
            moves.append(
                {
                    "rule_id": rule_id,
                    "change": "changed",
                    "from": old_weight,
                    "to": new_weight,
                    "delta": delta,
                }
            )
    return moves


def diff_versions(
    name: str,
    left: Any,
    right: Any,
    *,
    registry: ModelVersionRegistry | None = None,
    depth: int = DEFAULT_DIFF_DEPTH,
) -> dict[str, Any]:
    """Structural diff between two snapshots of one model.

    ``left``/``right`` accept a version number, ``"active"``/``"canary"``, or
    ``None`` for active. The result is the recursive :func:`deep_diff` from
    ``app.optimistic_locking`` (dotted leaf paths) plus the rule-level weight
    moves a structural diff cannot see inside a list.
    """
    registry = registry if registry is not None else get_default_registry()
    from_ref = _resolve_ref(registry, name, left)
    to_ref = _resolve_ref(registry, name, right)
    before = registry.version_config(name, from_ref["version"])
    after = registry.version_config(name, to_ref["version"])
    changes = deep_diff(before, after, depth=depth)
    moves = weight_moves(before, after)
    summary = {
        "added": sum(1 for change in changes if change["change"] == "added"),
        "removed": sum(1 for change in changes if change["change"] == "removed"),
        "changed": sum(1 for change in changes if change["change"] == "changed"),
        "total": len(changes),
        "weight_moves": len(moves),
        "max_weight_move": max(
            (abs(move["delta"]) for move in moves if move["delta"] is not None),
            default=0.0,
        ),
    }
    return {
        "model_name": name,
        "from": from_ref,
        "to": to_ref,
        "identical": not changes,
        "changes": changes,
        "summary": summary,
        "weight_moves": moves,
    }


def diff_against_active(
    name: str,
    version: Any = None,
    *,
    registry: ModelVersionRegistry | None = None,
    depth: int = DEFAULT_DIFF_DEPTH,
) -> dict[str, Any]:
    """:func:`diff_versions` with the active snapshot as the left-hand side."""
    return diff_versions(name, "active", version, registry=registry, depth=depth)


def render_diff(diff: dict[str, Any], fmt: str = "json") -> Any:
    """Render a :func:`diff_versions` result in one of :data:`DIFF_FORMATS`.

    ``json``/``summary``/``weights`` return structures, ``unified`` returns a
    review-pasteable text block.
    """
    if fmt not in DIFF_FORMATS:
        raise ValueError(f"unknown diff format {fmt!r}; expected one of {list(DIFF_FORMATS)}")
    changes = diff.get("changes", [])
    if fmt == "json":
        return diff
    if fmt == "summary":
        return {
            "model_name": diff.get("model_name"),
            "from": (diff.get("from") or {}).get("version"),
            "to": (diff.get("to") or {}).get("version"),
            "identical": diff.get("identical"),
            "summary": diff.get("summary", {}),
            "top_paths": [change["path"] for change in changes[:10]],
        }
    if fmt == "weights":
        return diff.get("weight_moves", [])
    symbols = {"added": "+", "removed": "-", "changed": "~"}
    lines = [
        f"--- {diff.get('model_name')} "
        f"v{(diff.get('from') or {}).get('version')}",
        f"+++ {diff.get('model_name')} "
        f"v{(diff.get('to') or {}).get('version')}",
    ]
    for change in changes:
        lines.append(
            f"{symbols.get(change['change'], '?')} {change['path']}: "
            f"{change.get('from')!r} -> {change.get('to')!r}"
        )
    for move in diff.get("weight_moves", []):
        lines.append(
            f"~ weights.{move['rule_id']}: {move['from']} -> {move['to']} "
            f"({move['delta']:+g})"
        )
    return "\n".join(lines)


def version_changelog(
    name: str,
    *,
    registry: ModelVersionRegistry | None = None,
) -> list[dict[str, Any]]:
    """The event log implied by snapshot timestamps: created, then promoted.

    Derived from the immutable snapshot metadata, so it cannot drift from what
    the registry actually holds — unlike a separate audit row.
    """
    registry = registry if registry is not None else get_default_registry()
    entry = registry.require_model(name)
    events: list[dict[str, Any]] = []
    for version in sorted(entry.snapshots):
        snap = entry.snapshots[version]
        events.append(
            {
                "at": snap.get("created_at"),
                "event": "created",
                "version": version,
                "label": snap.get("label"),
                "state": snap.get("state"),
            }
        )
        if snap.get("promoted_at"):
            events.append(
                {
                    "at": snap["promoted_at"],
                    "event": "promoted",
                    "version": version,
                    "label": snap.get("label"),
                    "state": snap.get("state"),
                }
            )
    events.sort(key=lambda event: (str(event.get("at") or ""), event["version"], event["event"]))
    return events


def promotion_gates_for(
    model_name: str,
    *,
    gates: Any = None,
) -> list[dict[str, Any]]:
    """The applicable gate rows for a model (wildcard rows included)."""
    rows = list(gates) if gates is not None else list(PROMOTION_GATES)
    selected: list[dict[str, Any]] = []
    for row in rows:
        models = row.get("models") or ("*",)
        if "*" in models or model_name in models:
            selected.append(dict(row))
    return selected


def promotion_metrics(
    model_name: str,
    *,
    registry: ModelVersionRegistry | None = None,
    runner: CanaryRunner | None = None,
) -> dict[str, Any]:
    """Every metric :data:`PROMOTION_GATES` may read, computed once.

    When ``runner`` is omitted the default runner is used *only* if it already
    points at this registry, so metrics from one registry are never mixed with
    another registry's history.
    """
    registry = registry if registry is not None else get_default_registry()
    entry = registry.require_model(model_name)
    if runner is None:
        default_runner = _default_runner
        if default_runner is not None and default_runner.registry is registry:
            runner = default_runner
    metrics: dict[str, Any] = {
        "model_name": model_name,
        "active_version": entry.active,
        "canary_version": entry.canary,
        "has_canary": entry.canary is not None,
        "auto_promote_enabled": bool(runner.auto_promote) if runner else False,
        "guard_version": entry.lock.version,
        "has_rollback_target": any(
            snap["state"] == "archived" for snap in entry.snapshots.values()
        ),
        "archived_versions": sorted(
            version
            for version, snap in entry.snapshots.items()
            if snap["state"] == "archived"
        ),
    }
    if entry.canary is not None:
        metrics["distance_from_active"] = entry.canary - entry.active
    else:
        metrics["distance_from_active"] = None
    if runner is not None:
        metrics.update(runner.canary_metrics(model_name))
    else:
        metrics.update(NO_HISTORY_METRICS)
    active_config = registry.version_config(model_name, entry.active)
    canary_config = (
        registry.version_config(model_name, entry.canary)
        if entry.canary is not None
        else None
    )
    if canary_config is None:
        # No candidate: only the *would-be* diff against the next version is
        # unavailable, so report the running diff as empty rather than stale.
        metrics["config_diff_paths"] = 0
        metrics["weight_move_count"] = 0
        metrics["max_weight_move"] = 0.0
        metrics["unknown_rule_ids"] = None
    else:
        moves = weight_moves(active_config, canary_config)
        metrics["weight_move_count"] = len(moves)
        metrics["max_weight_move"] = max(
            (abs(move["delta"]) for move in moves if move["delta"] is not None),
            default=0.0,
        )
        # A ``rules`` list is one structural diff path, so the per-rule moves
        # are added here; otherwise this gate would read ~1 for any retune.
        metrics["config_diff_paths"] = len(
            diff_paths(active_config, canary_config, depth=DEFAULT_DIFF_DEPTH)
        ) + len(moves)
        left = _rules_of(active_config)
        right = _rules_of(canary_config)
        if left is not None and right is not None:
            known = {rule.get("rule_id") for rule in effective_risk_rules()}
            metrics["unknown_rule_ids"] = len(
                {rule.get("rule_id") for rule in right} ^ known
            )
        else:
            metrics["unknown_rule_ids"] = None
    return metrics


def _gate_op_holds(value: Any, op: str, threshold: Any) -> bool:
    """Apply one gate operator. Fails closed on an unknown operator name."""
    if op not in GATE_OPS:
        return False
    if op == "is_true":
        return bool(value)
    if op == "is_false":
        return not value
    if op == "is_none":
        return value is None
    if op == "present":
        return value is not None
    if op in {"in", "not_in"}:
        options = threshold if isinstance(threshold, (list, tuple, set)) else [threshold]
        inside = value in options
        return inside if op == "in" else not inside
    number = _as_number(value)
    limit = _as_number(threshold)
    if op == "eq":
        return value == threshold
    if op == "neq":
        return value != threshold
    if number is None or limit is None:
        return False
    if op == "gte":
        return number >= limit
    if op == "gt":
        return number > limit
    if op == "lte":
        return number <= limit
    if op == "lt":
        return number < limit
    if op == "abs_lte":
        return abs(number) <= limit
    return False


def evaluate_promotion_gate(
    gate: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """One gate row against a metrics mapping, with the full explanation kept."""
    metric = str(gate.get("metric", ""))
    op = str(gate.get("op", ""))
    threshold = gate.get("threshold")
    present = metric in metrics and metrics[metric] is not None
    actual = metrics.get(metric)
    if not present:
        holds = False
        reason = "metric_missing"
    else:
        holds = _gate_op_holds(actual, op, threshold)
        reason = "ok" if holds else "threshold_not_met"
    severity = str(gate.get("severity", "advisory"))
    if severity not in GATE_SEVERITY_RANK:
        severity = "advisory"
    return {
        "gate_id": gate.get("gate_id"),
        "metric": metric,
        "op": op,
        "op_meaning": GATE_OPS.get(op, "unknown operator"),
        "threshold": threshold,
        "actual": actual,
        "observed": present,
        "severity": severity,
        "holds": holds,
        "reason": reason,
        "rationale": gate.get("rationale", ""),
    }


def evaluate_promotion_gates(
    model_name: str,
    *,
    metrics: dict[str, Any] | None = None,
    gates: Any = None,
    registry: ModelVersionRegistry | None = None,
    runner: CanaryRunner | None = None,
) -> list[dict[str, Any]]:
    """Evaluate every applicable gate, strongest severity first."""
    if metrics is None:
        metrics = promotion_metrics(
            model_name, registry=registry, runner=runner
        )
    results = [
        evaluate_promotion_gate(gate, metrics)
        for gate in promotion_gates_for(model_name, gates=gates)
    ]
    results.sort(
        key=lambda result: (
            -GATE_SEVERITY_RANK.get(result["severity"], 0),
            str(result["gate_id"]),
        )
    )
    return results


def promotion_verdict(
    model_name: str,
    *,
    metrics: dict[str, Any] | None = None,
    gates: Any = None,
    registry: ModelVersionRegistry | None = None,
    runner: CanaryRunner | None = None,
) -> dict[str, Any]:
    """``auto`` / ``manual`` / ``blocked``, with the gates that decided it.

    - ``blocked`` — at least one ``blocking`` gate failed: do not promote.
    - ``manual``  — no blocking failure, but an ``auto`` or ``advisory`` gate
      did: a human may promote, automation should not.
    - ``auto``    — every applicable gate holds.

    The verdict never promotes anything; ``CanaryRunner`` still needs
    ``auto_promote`` enabled to act on its own.
    """
    if metrics is None:
        metrics = promotion_metrics(model_name, registry=registry, runner=runner)
    results = evaluate_promotion_gates(
        model_name, metrics=metrics, gates=gates
    )
    failed = [result for result in results if not result["holds"]]
    blocking = [r["gate_id"] for r in failed if r["severity"] == "blocking"]
    automatic = [r["gate_id"] for r in failed if r["severity"] == "auto"]
    advisory = [r["gate_id"] for r in failed if r["severity"] == "advisory"]
    if blocking:
        decision = "blocked"
    elif automatic or advisory:
        decision = "manual"
    else:
        decision = "auto"
    return {
        "model_name": model_name,
        "decision": decision,
        "blocking_failures": blocking,
        "auto_failures": automatic,
        "advisory_failures": advisory,
        "gates": results,
        "metrics": metrics,
        "policy_note": (
            "advisory and auto failures never block an explicit promote(); only "
            "blocking failures say a promotion should not happen at all"
        ),
    }


def rollback_policy(policy_id: str | None = None) -> dict[str, Any]:
    """The :data:`ROLLBACK_POLICIES` row to evaluate against."""
    key = policy_id or DEFAULT_ROLLBACK_POLICY
    if key not in ROLLBACK_POLICY_BY_ID:
        raise ValueError(
            f"unknown rollback policy {key!r}; expected one of {sorted(ROLLBACK_POLICY_BY_ID)}"
        )
    return dict(ROLLBACK_POLICY_BY_ID[key])


def rollback_plan(
    name: str,
    to_version: Any,
    *,
    policy: str | None = None,
    registry: ModelVersionRegistry | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Would a rollback to ``to_version`` be allowed under ``policy``?

    Pure evaluation: every clause reports separately so an operator can see
    which one objected. ``rollback()`` itself stays unguarded.
    """
    registry = registry if registry is not None else get_default_registry()
    rules = rollback_policy(policy)
    entry = registry.require_model(name)
    info = registry.model(name)
    target = int(to_version)
    reasons: list[str] = []
    distance = entry.active - target
    if target not in entry.snapshots:
        reasons.append("unknown_version")
    if target == entry.active:
        reasons.append("already_active")
    stored = entry.snapshots[target]["state"] if target in entry.snapshots else None
    effective = (
        version_state(
            target,
            active=entry.active,
            canary=entry.canary,
            stored=stored or "archived",
            distance=distance,
        )
        if target in entry.snapshots
        else None
    )
    if target in entry.snapshots and rules.get("older_only") and distance < 0:
        reasons.append("forward_rollback_not_allowed")
    if (
        target in entry.snapshots
        and rules.get("require_archived")
        and stored != "archived"
    ):
        reasons.append("target_not_archived")
    max_distance = rules.get("max_distance")
    if max_distance is not None and target in entry.snapshots and distance > max_distance:
        reasons.append("distance_exceeds_policy")
    cooldown = rules.get("cooldown_seconds") or 0
    cooldown_remaining = 0.0
    if cooldown:
        promoted_at = _parse_iso(entry.snapshots[entry.active].get("promoted_at"))
        now = _parse_iso(as_of) or datetime.now(timezone.utc)
        if promoted_at is not None:
            elapsed = (now - promoted_at).total_seconds()
            if elapsed < cooldown:
                cooldown_remaining = round(cooldown - elapsed, 3)
                reasons.append("within_cooldown")
    return {
        "model_name": name,
        "policy": rules["policy_id"],
        "from_version": info["active_version"],
        "to_version": target,
        "known": target in entry.snapshots,
        "distance": distance,
        "target_state": stored,
        "target_effective_state": effective,
        "label": entry.snapshots[target]["label"] if target in entry.snapshots else None,
        "allowed": not reasons,
        "reasons": reasons,
        "cooldown_remaining_seconds": cooldown_remaining,
        "bounds": {
            "max_distance": max_distance,
            "older_only": bool(rules.get("older_only")),
            "require_archived": bool(rules.get("require_archived")),
            "cooldown_seconds": cooldown,
        },
    }


def rollback_candidates(
    name: str,
    *,
    policy: str | None = None,
    registry: ModelVersionRegistry | None = None,
) -> dict[str, Any]:
    """Every snapshot a rollback could target, with its per-policy verdict."""
    registry = registry if registry is not None else get_default_registry()
    entry = registry.require_model(name)
    rules = rollback_policy(policy)
    candidates: list[dict[str, Any]] = []
    for version in sorted(entry.snapshots, reverse=True):
        if version == entry.active:
            continue
        plan = rollback_plan(name, version, policy=rules["policy_id"], registry=registry)
        plan["reachable"] = plan["allowed"]
        candidates.append(plan)
    return {
        "model_name": name,
        "policy": rules["policy_id"],
        "candidates": candidates,
        "allowed": [row["to_version"] for row in candidates if row["reachable"]],
        "blocked": [row["to_version"] for row in candidates if not row["reachable"]],
        "min_keep_archived": rules.get("min_keep_archived"),
    }


def deprecation_policy(policy_id: str | None = None) -> dict[str, Any]:
    """The :data:`DEPRECATION_POLICIES` row to evaluate against."""
    key = policy_id or DEFAULT_DEPRECATION_POLICY
    if key not in DEPRECATION_POLICY_BY_ID:
        raise ValueError(
            f"unknown deprecation policy {key!r}; "
            f"expected one of {sorted(DEPRECATION_POLICY_BY_ID)}"
        )
    return dict(DEPRECATION_POLICY_BY_ID[key])


def version_state(
    version: int,
    *,
    active: int,
    canary: int | None,
    stored: str,
    distance: int,
    policy: dict[str, Any] | None = None,
) -> str:
    """Resolve one version's *effective* state without writing anything back.

    ``active``/``canary``/``archived`` are stored; ``deprecated`` and
    ``obsolete`` are derived from how far behind active a snapshot has fallen.
    """
    rules = policy or deprecation_policy()
    if version == active:
        return "active"
    if canary is not None and version == canary:
        return "canary"
    if stored not in {"archived", "active", "canary"}:
        return stored
    obsolete_after = rules.get("obsolete_after_distance")
    if obsolete_after is not None and distance > obsolete_after:
        return "obsolete"
    return "deprecated"


def deprecation_status(
    name: str,
    *,
    policy: str | None = None,
    registry: ModelVersionRegistry | None = None,
) -> dict[str, Any]:
    """Per-version deprecation view plus the rollback and prune pools."""
    registry = registry if registry is not None else get_default_registry()
    rules = deprecation_policy(policy)
    rows = registry.version_states(name, policy=rules["policy_id"])
    notice = rules.get("notice_versions")
    archived = [row["version"] for row in rows if row["state"] == "archived"]
    annotated: list[dict[str, Any]] = []
    for row in rows:
        if row["state"] == "archived":
            # rank among archived snapshots, newest = 0
            rank = len(archived) - 1 - archived.index(row["version"])
            row["in_notice_window"] = notice is None or rank < notice
            row["archived_rank"] = rank
        else:
            row["in_notice_window"] = False
            row["archived_rank"] = None
        annotated.append(row)
    by_state: dict[str, list[int]] = {}
    for row in annotated:
        by_state.setdefault(row["effective_state"], []).append(row["version"])
    return {
        "model_name": name,
        "policy": rules["policy_id"],
        "active_version": registry.model(name)["active_version"],
        "canary_version": registry.model(name)["canary_version"],
        "notice_versions": notice,
        "obsolete_after_distance": rules.get("obsolete_after_distance"),
        "versions": annotated,
        "by_state": {state: sorted(versions) for state, versions in sorted(by_state.items())},
        "rollback_pool": sorted(row["version"] for row in annotated if row["rollback_reachable"]),
        "prune_pool": sorted(row["version"] for row in annotated if row["prunable"]),
        "retained_archived": len(archived),
    }


def prune_policy(policy_id: str | None = None) -> dict[str, Any]:
    """The :data:`PRUNE_POLICIES` row to evaluate against."""
    key = policy_id or DEFAULT_PRUNE_POLICY
    if key not in PRUNE_POLICY_BY_ID:
        raise ValueError(
            f"unknown prune policy {key!r}; expected one of {sorted(PRUNE_POLICY_BY_ID)}"
        )
    return dict(PRUNE_POLICY_BY_ID[key])


def prune_plan(
    name: str,
    *,
    policy: str | None = None,
    registry: ModelVersionRegistry | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Which archived snapshots a prune policy would drop, and why the rest stay.

    Retention is conjunctive — a version is prunable only when it is in
    ``prune_states`` *and* outside every bound — so no single bound can delete
    the last rollback target on its own.
    """
    registry = registry if registry is not None else get_default_registry()
    rules = prune_policy(policy)
    status = deprecation_status(
        name,
        policy=rules.get("deprecation_policy", DEFAULT_DEPRECATION_POLICY),
        registry=registry,
    )
    entry = registry.require_model(name)
    now = _parse_iso(as_of) or datetime.now(timezone.utc)
    prune_states = tuple(rules.get("prune_states") or ())
    keep_last = rules.get("keep_last")
    keep_days = rules.get("keep_archive_days")
    min_keep = rules.get("min_keep_archived")
    versions = sorted(entry.snapshots)
    keep_tail = set(versions[-keep_last:]) if keep_last else set()
    archived = [row for row in status["versions"] if row["state"] == "archived"]
    keep_archived_tail = (
        {row["version"] for row in archived[-min_keep:]} if min_keep else set()
    )
    prunable: list[int] = []
    protected: list[dict[str, Any]] = []
    for row in status["versions"]:
        version = row["version"]
        if row["state"] in {"active", "canary"}:
            protected.append({"version": version, "reason": "serving"})
            continue
        if not prune_states:
            protected.append({"version": version, "reason": "policy_never_prunes"})
            continue
        if row["effective_state"] not in prune_states:
            protected.append(
                {"version": version, "reason": f"state_{row['effective_state']}_not_prunable"}
            )
            continue
        if version in keep_tail:
            protected.append({"version": version, "reason": "within_keep_last"})
            continue
        if version in keep_archived_tail:
            protected.append({"version": version, "reason": "within_min_keep_archived"})
            continue
        if keep_days:
            created = _parse_iso(row.get("created_at"))
            if created is not None and (now - created).total_seconds() < keep_days * 86400:
                protected.append({"version": version, "reason": "younger_than_keep_archive_days"})
                continue
        prunable.append(version)
    return {
        "model_name": name,
        "policy": rules["policy_id"],
        "as_of": (as_of or _now_iso()),
        "prune_states": list(prune_states),
        "prunable": sorted(prunable),
        "protected": sorted(protected, key=lambda row: row["version"]),
        "retained": len(entry.snapshots) - len(prunable),
        "bounds": {
            "keep_last": keep_last,
            "keep_archive_days": keep_days,
            "min_keep_archived": min_keep,
        },
    }


def traffic_split_for(
    model_name: str,
    *,
    splits: Any = None,
) -> dict[str, Any]:
    """The :data:`TRAFFIC_SPLITS` row for a model, else the default ramp."""
    rows = list(splits) if splits is not None else list(TRAFFIC_SPLITS)
    for row in rows:
        if row.get("model") in {model_name, "*"}:
            return dict(row)
    return dict(DEFAULT_TRAFFIC_SPLIT)


def traffic_weights(
    model_name: str,
    stage: str,
    *,
    splits: Any = None,
) -> dict[str, Any]:
    """Resolve a stage name to ``{"active": w, "canary": w}`` for one model."""
    split = traffic_split_for(model_name, splits=splits)
    stages = split.get("stages") or ()
    for row in stages:
        if row.get("stage") == stage:
            canary_weight = _as_number(row.get("canary_weight")) or 0.0
            canary_weight = max(0.0, min(1.0, canary_weight))
            return {
                "model_name": model_name,
                "split_id": split.get("split_id"),
                "strategy": split.get("strategy"),
                "stage": stage,
                "canary_weight": canary_weight,
                "active_weight": round(1.0 - canary_weight, 6),
                "description": row.get("description", ""),
            }
    raise ValueError(
        f"unknown stage {stage!r} for model {model_name!r}; "
        f"expected one of {[row.get('stage') for row in stages]}"
    )


def assign_bucket(key: Any, *, salt: str = DEFAULT_BUCKET_SALT) -> int:
    """Stable bucket in ``[0, BUCKET_RESOLUTION)`` for a subject key.

    ``hash()`` is salted per process, so a rollout would reshuffle every
    request after a restart; blake2b is stable across processes and machines,
    which is what "the same 5% of subjects" requires.
    """
    payload = f"{salt}:{key}".encode("utf-8", "replace")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") % BUCKET_RESOLUTION


def traffic_plan(
    name: str,
    *,
    stage: str | None = None,
    registry: ModelVersionRegistry | None = None,
    splits: Any = None,
) -> dict[str, Any]:
    """Resolve a stage into concrete version assignments for one model.

    With no ``stage`` the plan starts at ``shadow`` (serve everything from
    active) unless a candidate exists, in which case the first stage that
    actually moves traffic is chosen. A stage that wants canary traffic but has
    no candidate degrades to active-only and says so in ``reasons``.
    """
    registry = registry if registry is not None else get_default_registry()
    entry = registry.require_model(name)
    split = traffic_split_for(name, splits=splits)
    stage_names = [row.get("stage") for row in split.get("stages") or ()]
    if stage is None:
        stage = "shadow" if entry.canary is None else (
            stage_names[1] if len(stage_names) > 1 else stage_names[0]
        )
    weights = traffic_weights(name, stage, splits=splits)
    reasons: list[str] = []
    canary_version = entry.canary
    canary_weight = weights["canary_weight"]
    if canary_version is None and canary_weight > 0:
        reasons.append("no_candidate")
        canary_weight = 0.0
    assignments = [
        {
            "role": "active",
            "version": entry.active,
            "weight": round(1.0 - canary_weight, 6),
        }
    ]
    if canary_version is not None:
        assignments.append(
            {"role": "canary", "version": canary_version, "weight": canary_weight}
        )
    max_step = _as_number(split.get("max_step"))
    return {
        "model_name": name,
        "split_id": split.get("split_id"),
        "strategy": split.get("strategy"),
        "bucketing": split.get("bucketing", "stable-hash"),
        "salt": split.get("salt", DEFAULT_BUCKET_SALT),
        "stage": stage,
        "stages": stage_names,
        "max_step": max_step,
        "canary_weight": canary_weight,
        "active_weight": round(1.0 - canary_weight, 6),
        "assignments": assignments,
        "reasons": reasons,
        "weight_total": round(sum(row["weight"] for row in assignments), 6),
    }


def select_version(plan: dict[str, Any], subject_key: Any) -> dict[str, Any]:
    """Pick the snapshot a single subject should be served from.

    Deterministic: the same key always lands in the same bucket, so a subject
    stays on one version for the life of the plan. The key itself is not echoed
    back — it may be a device or account identifier.
    """
    salt = plan.get("salt", DEFAULT_BUCKET_SALT)
    resolution = BUCKET_RESOLUTION
    bucket = assign_bucket(subject_key, salt=salt)
    canary_units = int(round(float(plan.get("canary_weight") or 0.0) * resolution))
    chosen = next(
        (row for row in plan.get("assignments", []) if row["role"] == "active"),
        None,
    )
    if canary_units and bucket < canary_units:
        chosen = next(
            (row for row in plan.get("assignments", []) if row["role"] == "canary"),
            chosen,
        )
    return {
        "model_name": plan.get("model_name"),
        "split_id": plan.get("split_id"),
        "stage": plan.get("stage"),
        "version": (chosen or {}).get("version"),
        "role": (chosen or {}).get("role", "active"),
        "bucket": bucket,
        "bucket_pct": round(bucket / resolution, 6),
        "bucket_resolution": resolution,
        "echoes_subject": False,
    }


def stage_progress(
    name: str,
    *,
    stage: str | None = None,
    registry: ModelVersionRegistry | None = None,
    splits: Any = None,
) -> dict[str, Any]:
    """Where a model's rollout currently stands against its split stages."""
    registry = registry if registry is not None else get_default_registry()
    plan = traffic_plan(name, stage=stage, registry=registry, splits=splits)
    stage_names = plan["stages"]
    try:
        position = stage_names.index(plan["stage"])
    except ValueError:
        position = -1
    if position > 0:
        previous = traffic_weights(name, stage_names[position - 1], splits=splits)
        previous_weight = previous["canary_weight"]
    else:
        previous_weight = 0.0
    return {
        "model_name": name,
        "split_id": plan["split_id"],
        "strategy": plan["strategy"],
        "stage": plan["stage"],
        "position": position,
        "stages": stage_names,
        "remaining": max(0, len(stage_names) - position - 1),
        "canary_weight": plan["canary_weight"],
        "previous_weight": previous_weight,
        "max_step": plan["max_step"],
        "max_step_exceeded": _step_exceeds(
            plan["max_step"], previous_weight, plan["canary_weight"]
        ),
        "has_candidate": len(plan["assignments"]) > 1,
        "reasons": plan["reasons"],
    }


def _step_exceeds(max_step: Any, previous: Any, current: Any) -> bool:
    """True when moving from ``previous`` to ``current`` breaches ``max_step``."""
    limit = _as_number(max_step)
    if limit is None:
        return False
    now = _as_number(current) or 0.0
    before = _as_number(previous) or 0.0
    return (now - before) > limit + 1e-9


def next_stage(
    name: str,
    *,
    stage: str | None = None,
    registry: ModelVersionRegistry | None = None,
    splits: Any = None,
) -> dict[str, Any]:
    """The stage to move to *after* the current candidate is promoted.

    ``exceeds_max_step`` is the interesting field: it says the next increase is
    wider than the split's configured ``max_step``, so the rollout must hold
    (more shadow runs, or an explicit override) instead of jumping.
    """
    registry = registry if registry is not None else get_default_registry()
    progress = stage_progress(name, stage=stage, registry=registry, splits=splits)
    split = traffic_split_for(name, splits=splits)
    stage_names = progress["stages"]
    position = progress["position"]
    if position < 0 or position + 1 >= len(stage_names):
        return {
            "model_name": name,
            "split_id": progress["split_id"],
            "from_stage": progress["stage"],
            "to_stage": None,
            "exceeds_max_step": False,
            "hold_required": False,
            "reason": "at_final_stage" if position >= 0 else "unknown_stage",
        }
    following = stage_names[position + 1]
    here = traffic_weights(name, progress["stage"], splits=splits)["canary_weight"]
    there = traffic_weights(name, following, splits=splits)["canary_weight"]
    exceeds = _step_exceeds(split.get("max_step"), here, there)
    return {
        "model_name": name,
        "split_id": progress["split_id"],
        "from_stage": progress["stage"],
        "to_stage": following,
        "from_weight": here,
        "to_weight": there,
        "delta": round(there - here, 6),
        "max_step": _as_number(split.get("max_step")),
        "exceeds_max_step": exceeds,
        "hold_required": exceeds,
        "reason": "step_exceeds_max_step" if exceeds else "within_max_step",
    }


def render_label(
    template_id: str | None = None,
    *,
    version: int,
    label: str = "",
    date: str | None = None,
    digest: str = "",
) -> str:
    """Render a version label from :data:`LABEL_TEMPLATES`.

    ``"plain"`` (the default) reproduces the registry's implicit ``v{version}``,
    so switching on a template never rewrites an existing label.
    """
    key = template_id or DEFAULT_LABEL_TEMPLATE
    if key not in LABEL_TEMPLATE_BY_ID:
        raise ValueError(
            f"unknown label template {key!r}; "
            f"expected one of {sorted(LABEL_TEMPLATE_BY_ID)}"
        )
    pattern = LABEL_TEMPLATE_BY_ID[key]["pattern"]
    stamp = date or datetime.now(timezone.utc).strftime("%Y%m%d")
    return pattern.format(
        version=int(version),
        label=label or "",
        date=stamp,
        digest=digest,
    )


def version_report(
    name: str,
    version: Any = None,
    *,
    registry: ModelVersionRegistry | None = None,
    gates: Any = None,
    rollback: str | None = None,
    deprecation: str | None = None,
    pruning: str | None = None,
    splits: Any = None,
    label_template: str | None = None,
) -> dict[str, Any]:
    """One review surface for a single version of a model.

    Combines the derived state, the diff against active, the weight moves, the
    promotion verdict (only meaningful for the candidate), the rollback plan
    and the prune fate — the whole "can this go live, and can we get back?"
    answer in one payload.
    """
    registry = registry if registry is not None else get_default_registry()
    entry = registry.require_model(name)
    resolved = _resolve_ref(registry, name, version)
    target = resolved["version"]
    info = registry.model(name)
    rows = registry.version_states(
        name, policy=deprecation or DEFAULT_DEPRECATION_POLICY
    )
    row = next(item for item in rows if item["version"] == target)
    diff = diff_against_active(name, target, registry=registry)
    plan = prune_plan(name, policy=pruning, registry=registry)
    is_candidate = info["canary_version"] == target
    report: dict[str, Any] = {
        "model_name": name,
        "version": target,
        "label": row["label"],
        "state": row["state"],
        "effective_state": row["effective_state"],
        "created_at": row["created_at"],
        "promoted_at": row["promoted_at"],
        "distance_from_active": row["distance_from_active"],
        "is_active": row["serving"],
        "is_candidate": is_candidate,
        "suggested_label": render_label(
            label_template,
            version=target,
            label=row["label"],
        ),
        "diff_vs_active": render_diff(diff, "summary"),
        "weight_moves": diff["weight_moves"],
        "rollback_plan": rollback_plan(
            name, target, policy=rollback, registry=registry
        ),
        "prunable": target in plan["prunable"],
        "prune_policy": plan["policy"],
        "verdict": None,
    }
    if is_candidate:
        verdict = promotion_verdict(
            name,
            gates=gates,
            registry=registry,
        )
        report["verdict"] = {
            "decision": verdict["decision"],
            "blocking_failures": verdict["blocking_failures"],
            "auto_failures": verdict["auto_failures"],
            "advisory_failures": verdict["advisory_failures"],
        }
    else:
        report["verdict_note"] = (
            "a promotion verdict only applies to the canary candidate; this "
            "version is the active or an archived snapshot"
        )
    if entry.canary is not None and target != entry.canary:
        report["traffic"] = stage_progress(
            name, registry=registry, splits=splits
        )
    return report


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
        "lifecycle": {
            "immutability": "add_version appends a candidate; the active config is never rewritten",
            "stored_states": list(STORED_VERSION_STATES),
            "derived_states": list(DERIVED_VERSION_STATES),
            "states": [dict(row) for row in VERSION_STATES],
            "note": (
                "stored states are written by the registry; derived states are "
                "reported by version_states()/deprecation_status() and are never "
                "written back"
            ),
            "transitions": {
                "register": "-> v1 active",
                "add_version": "-> new canary, previous canary archived",
                "promote": "canary -> active, former active -> archived (guarded)",
                "rollback": "any snapshot -> active, former active -> archived (guarded)",
                "prune_versions": "drops prunable archived snapshots only",
            },
            "helper": "version_report",
        },
        "promotion_gates": {
            "table": [dict(row) for row in PROMOTION_GATES],
            "operators": dict(GATE_OPS),
            "severities": list(GATE_SEVERITIES),
            "severity_meaning": {
                "blocking": "the promotion should not happen at all (structurally wrong: nothing to promote, or a different model)",
                "auto": "legitimate, but a human must make the call (thin evidence, no rollback target, auto-promote off)",
                "advisory": "reported only, never decisive",
            },
            "verdict": "auto | manual | blocked",
            "verdict_rule": "any blocking failure -> blocked; any auto/advisory failure -> manual; else auto",
            "missing_metric": "fails the gate closed (reason=metric_missing)",
            "advisory": (
                "a verdict never promotes anything; CanaryRunner still needs "
                "auto_promote (CSERVICE_CANARY_AUTOPROMOTE) to act unattended"
            ),
            "helpers": [
                "promotion_metrics",
                "evaluate_promotion_gates",
                "promotion_verdict",
            ],
            "metrics": "CanaryRunner.canary_metrics + config-diff and weight-move counts",
        },
        "version_diff": {
            "engine": "deep_diff / diff_paths from app.optimistic_locking",
            "depth": DEFAULT_DIFF_DEPTH,
            "refs": "a version number, 'active', 'canary', or None for active",
            "rule_aware": "a rules list is one diff path; weight_moves pairs them by rule_id",
            "formats": list(DIFF_FORMATS),
            "changelog": "version_changelog is derived from snapshot timestamps",
            "helpers": ["diff_versions", "diff_against_active", "weight_moves", "render_diff", "version_changelog"],
        },
        "rollback_policy": {
            "table": [dict(row) for row in ROLLBACK_POLICIES],
            "default": DEFAULT_ROLLBACK_POLICY,
            "advisory": (
                "rollback_plan() evaluates; rollback() itself stays unguarded, so a "
                "policy can never stop an incident rollback"
            ),
            "helpers": ["rollback_policy", "rollback_plan", "rollback_candidates"],
        },
        "deprecation": {
            "table": [dict(row) for row in DEPRECATION_POLICIES],
            "default": DEFAULT_DEPRECATION_POLICY,
            "rule": (
                "archived and more than obsolete_after_distance versions behind "
                "active is obsolete; the newest notice_versions archived "
                "snapshots stay in the notice window"
            ),
            "helper": "deprecation_status / version_state",
        },
        "prune": {
            "table": [dict(row) for row in PRUNE_POLICIES],
            "default": DEFAULT_PRUNE_POLICY,
            "conjunctive": (
                "a version is prunable only when it is in prune_states and outside "
                "every bound, so no single bound can drop the last rollback target"
            ),
            "dry_run_default": True,
            "never_touches": ["active", "canary"],
            "helpers": ["prune_plan", "ModelVersionRegistry.prune_versions"],
        },
        "traffic_split": {
            "table": [dict(row) for row in TRAFFIC_SPLITS],
            "default": dict(DEFAULT_TRAFFIC_SPLIT),
            "bucketing": "blake2b, stable across processes (hash() is not)",
            "bucket_resolution": BUCKET_RESOLUTION,
            "privacy": "the subject key is never echoed back in a result",
            "max_step": "next_stage flags an increase wider than the split allows",
            "helpers": [
                "traffic_split_for",
                "traffic_weights",
                "traffic_plan",
                "select_version",
                "stage_progress",
                "next_stage",
                "ModelVersionRegistry.split_snapshot",
            ],
        },
        "label_templates": {
            "table": [dict(row) for row in LABEL_TEMPLATES],
            "default": DEFAULT_LABEL_TEMPLATE,
            "note": "'plain' reproduces the registry's implicit v{version}",
            "helper": "render_label",
        },
    }