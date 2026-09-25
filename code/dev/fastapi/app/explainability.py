"""Explainability surfaces for scoring / policy / retention decisions.

Every decision the backend makes — a risk score, a retention action, a canary
shadow run, a simulation — can be recorded as a ``DecisionTrace``: the inputs
it saw, the factors that actually contributed (with weights), the threshold it
was compared against, the model version that produced it, and the outcome.
Traces are queryable by id (``explain``), filterable by entity/type, and stored
in bounded ring buffers so the surface stays cheap at any request volume.

Design:

- ``DecisionTrace`` — immutable record of one decision. ``from_risk_result``
  converts an ``evaluate_risk``/``score_with_config`` payload into a trace with
  a full factor trail, so the risk engine does not need to know about
  explainability at all.
- ``DecisionTraceStore`` — bounded store (oldest evicted past capacity).
- ``record_decision`` / ``record_risk_trace`` — module-level convenience that
  writes to the default store (swappable via ``set_default_trace_store`` like
  the pipeline/transaction-log singletons).
"""
from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

DECISION_TYPES = (
    "risk_score",
    "retention_action",
    "model_version",
    "canary_score",
    "simulation",
    "policy_decision",
)

_TRACE_FIELDS = (
    "decision_id",
    "decision_type",
    "entity_ref",
    "generated_at",
    "inputs",
    "factors",
    "score",
    "threshold",
    "outcome",
    "model_version",
    "detail",
)

DEFAULT_CAPACITY = 1000


class DecisionTrace:
    """One recorded decision, immutable after construction."""

    def __init__(
        self,
        *,
        decision_type: str,
        entity_ref: str = "",
        outcome: str = "",
        score: Any = None,
        threshold: Any = None,
        model_version: str = "",
        factors: list[dict[str, Any]] | None = None,
        inputs: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        decision_id: str | None = None,
    ) -> None:
        if decision_type not in DECISION_TYPES:
            raise ValueError(f"unknown decision type: {decision_type}")
        self.decision_type = decision_type
        self.entity_ref = entity_ref or ""
        self.outcome = outcome
        self.score = score
        self.threshold = threshold
        self.model_version = model_version
        self.factors = list(factors or [])
        self.inputs = dict(inputs or {})
        self.detail = dict(detail or {})
        self.decision_id = decision_id or uuid.uuid4().hex
        self.generated_at = datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_risk_result(
        cls,
        context: dict[str, Any],
        result: dict[str, Any],
        *,
        entity_ref: str = "",
        model_version: str = "",
        decision_type: str = "risk_score",
        outcome: str = "",
    ) -> "DecisionTrace":
        """Build a trace from an ``evaluate_risk``-shaped result (factor trail)."""
        factors = [
            {
                "rule_id": f.get("rule_id"),
                "weight": f.get("weight"),
                "present": f.get("present", False),
                "reason": f.get("reason", ""),
            }
            for f in result.get("factors", [])
        ]
        return cls(
            decision_type=decision_type,
            entity_ref=entity_ref,
            outcome=outcome or result.get("level", ""),
            score=result.get("score"),
            model_version=model_version or str(result.get("weight_version", "")),
            factors=factors,
            inputs=dict(context),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision_type": self.decision_type,
            "entity_ref": self.entity_ref,
            "generated_at": self.generated_at,
            "inputs": dict(self.inputs),
            "factors": [dict(f) for f in self.factors],
            "score": self.score,
            "threshold": self.threshold,
            "outcome": self.outcome,
            "model_version": self.model_version,
            "detail": dict(self.detail),
        }


class DecisionTraceStore:
    """Bounded trace store with id lookup + filterable listing."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self._capacity = max(1, int(capacity))
        self._order: deque[str] = deque()
        self._traces: dict[str, DecisionTrace] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, trace: DecisionTrace) -> str:
        """Store a trace (evicting oldest past capacity) and return its id."""
        did = trace.decision_id
        if did in self._traces:
            self._order.remove(did)
        self._traces[did] = trace
        self._order.append(did)
        while len(self._order) > self._capacity:
            oldest = self._order.popleft()
            self._traces.pop(oldest, None)
        return did

    def get(self, decision_id: str) -> dict[str, Any] | None:
        trace = self._traces.get(decision_id)
        return trace.to_dict() if trace is not None else None

    def explain(self, decision_id: str) -> dict[str, Any]:
        """Full explanation for a decision id; ``None``-safe lookup helper."""
        trace = self._traces.get(decision_id)
        if trace is None:
            raise KeyError(f"no decision trace with id {decision_id!r}")
        return trace.to_dict()

    def list(
        self,
        *,
        entity_ref: str | None = None,
        decision_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Newest-first traces, optionally filtered by entity/type."""
        limit = max(1, min(int(limit), 500))
        matching: list[dict[str, Any]] = []
        for did in reversed(self._order):
            trace = self._traces[did]
            if entity_ref and trace.entity_ref != entity_ref:
                continue
            if decision_type and trace.decision_type != decision_type:
                continue
            matching.append(trace.to_dict())
            if len(matching) >= limit:
                break
        return matching

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for trace in self._traces.values():
            by_type[trace.decision_type] = by_type.get(trace.decision_type, 0) + 1
        return {
            "capacity": self._capacity,
            "total": len(self._traces),
            "by_type": dict(sorted(by_type.items())),
        }

    def clear(self) -> None:
        self._order.clear()
        self._traces.clear()


_default_store: DecisionTraceStore | None = None


def get_default_trace_store() -> DecisionTraceStore:
    global _default_store
    if _default_store is None:
        _default_store = DecisionTraceStore()
    return _default_store


def set_default_trace_store(store: DecisionTraceStore | None) -> None:
    global _default_store
    _default_store = store


def record_decision(
    *,
    decision_type: str,
    entity_ref: str = "",
    outcome: str = "",
    score: Any = None,
    threshold: Any = None,
    model_version: str = "",
    factors: list[dict[str, Any]] | None = None,
    inputs: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    store: DecisionTraceStore | None = None,
) -> str:
    """Record a decision on a store (default store unless overridden)."""
    trace = DecisionTrace(
        decision_type=decision_type,
        entity_ref=entity_ref,
        outcome=outcome,
        score=score,
        threshold=threshold,
        model_version=model_version,
        factors=factors,
        inputs=inputs,
        detail=detail,
    )
    return (store or get_default_trace_store()).record(trace)


def record_risk_trace(
    context: dict[str, Any],
    result: dict[str, Any],
    *,
    entity_ref: str = "",
    model_version: str = "",
    store: DecisionTraceStore | None = None,
) -> str:
    """Record an ``evaluate_risk``-shaped result as a decision trace."""
    trace = DecisionTrace.from_risk_result(
        context, result, entity_ref=entity_ref, model_version=model_version
    )
    return (store or get_default_trace_store()).record(trace)


def build_explainability_catalog(
    store: DecisionTraceStore | None = None,
) -> dict[str, Any]:
    store = store or get_default_trace_store()
    return {
        "decision_types": list(DECISION_TYPES),
        "store": store.stats(),
        "trace_fields": list(_TRACE_FIELDS),
        "default_capacity": DEFAULT_CAPACITY,
    }