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

Expansion (thin-group pass): a recorded trace is only useful if it can be
*read* by three different audiences, so the explanation surface is now
config-driven rather than hardcoded:

- ``EXPLANATION_AUDIENCES`` — per-audience projection table. An engineer sees
  raw inputs, an auditor sees redacted inputs plus the full factor trail, a
  customer sees a plain narrative with no internal signal names. Adding an
  audience is a config row, not a new code path.
- ``FACTOR_PHRASING`` — rule-id/reason -> human phrase table, so a narrative
  reads "signed in from an unrecognised device" rather than "rule_id=device_new".
- ``NARRATIVE_TEMPLATES`` — decision_type -> template string, resolved by
  ``narrate`` against a controlled scope (only ``{placeholders}`` and safe
  literals; no attribute access, no imports).
- ``ATTRIBUTION_BUCKETS`` — groups factor contributions into named buckets
  (identity / device / behaviour / context / temporal) so an operator sees
  *what kind* of thing drove the score, not 40 rule ids.
- ``redact_for_audience`` / ``attribute_factors`` / ``narrate`` /
  ``counterfactual_factors`` / ``compare_traces`` / ``store.export`` are the
  pure readers over an already-recorded trace. Nothing here mutates a decision.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable

DECISION_TYPES = (
    "risk_score",
    "retention_action",
    "model_version",
    "canary_score",
    "simulation",
    "policy_decision",
    "recovery_action",
    "topic_selection",
    "booking_decision",
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

# --- Expansion: audience projection config ---------------------------------
#
# `detail` selects how much of a trace an audience may see. `redact_inputs` is
# an explicit deny-list (fail-closed: a key that is not listed is shown), so an
# audience can only ever get *less* than the engineer view, never more.

EXPLANATION_AUDIENCES: list[dict[str, Any]] = [
    {
        "audience": "engineer",
        "label": "Engineering (full signal set)",
        "detail": "full",
        "redact_inputs": [],
        "factor_limit": 0,  # 0 = no cap
        "show_weights": True,
        "show_inputs": True,
        "when_hint": "Raw context and every factor; used for incident response.",
    },
    {
        "audience": "auditor",
        "label": "Compliance / audit",
        "detail": "redacted",
        "redact_inputs": [
            "email",
            "phone",
            "device_fingerprint",
            "ip_address",
            "raw_message",
            "hashed_password",
        ],
        "factor_limit": 0,
        "show_weights": True,
        "show_inputs": True,
        "when_hint": "Same trail with PII values masked; keys still named for evidence.",
    },
    {
        "audience": "operator",
        "label": "Support operator",
        "detail": "summary",
        "redact_inputs": [
            "email",
            "phone",
            "device_fingerprint",
            "ip_address",
            "raw_message",
            "hashed_password",
            "user_agent",
        ],
        "factor_limit": 5,
        "show_weights": False,
        "show_inputs": False,
        "when_hint": "Attribution buckets plus a narrative; no raw signals, no weights.",
    },
    {
        "audience": "customer",
        "label": "Customer-facing explanation",
        "detail": "narrative",
        "redact_inputs": [
            "email",
            "phone",
            "device_fingerprint",
            "ip_address",
            "raw_message",
            "hashed_password",
            "user_agent",
            "internal_score",
        ],
        "factor_limit": 3,
        "show_weights": False,
        "show_inputs": False,
        "when_hint": "Plain-language narrative only; no rule ids, weights, or signals.",
    },
]

DEFAULT_AUDIENCE = "auditor"

# rule_id prefix -> human phrase. Longest prefix wins; `*` is the fallback.
# Prefixes mirror the live ``app.risk_evaluator.RISK_RULES`` ids so every shipped
# rule resolves to a real phrase; an unrecognised id falls through to `*`
# rather than leaking a raw rule name into a customer-facing narrative.
FACTOR_PHRASING: list[dict[str, str]] = [
    {"match": "new_device", "phrase": "the device was not recognised"},
    {"match": "biometric_", "phrase": "the biometric check did not match"},
    {"match": "unverified_hsm", "phrase": "the security module was not verified"},
    {"match": "geo_velocity", "phrase": "the location changed implausibly fast"},
    {"match": "auth_velocity", "phrase": "sign-in attempts came unusually fast"},
    {"match": "poor_ip", "phrase": "the network address has a poor reputation"},
    {"match": "off_hours", "phrase": "the activity happened outside normal hours"},
    {"match": "sensitive_target", "phrase": "a protected resource was targeted"},
    {"match": "churn", "phrase": "the customer shows churn signals"},
    {"match": "dissatisfaction", "phrase": "the customer recently expressed dissatisfaction"},
    {"match": "stage", "phrase": "the customer is early in their journey"},
    {"match": "value_tier", "phrase": "the customer's value tier affects the handling"},
    {"match": "tax", "phrase": "regional tax treatment applies"},
    {"match": "labor", "phrase": "regional labor-compliance limits apply"},
    {"match": "working_day", "phrase": "the requested date is not a working day"},
    {"match": "*", "phrase": "a recorded rule contributed to this decision"},
]

# decision_type -> narrative template. `{placeholders}` are filled from a
# controlled scope by `narrate`; no attribute access, no calls.
NARRATIVE_TEMPLATES: dict[str, str] = {
    "risk_score": (
        "This interaction was scored {score} out of 100, which placed it in the "
        "{outcome} band. {drivers}"
    ),
    "canary_score": (
        "A candidate model was scored in shadow against the live model. The "
        "candidate produced {score} versus {threshold} from the live model, so the "
        "outcome was {outcome}."
    ),
    "simulation": (
        "A what-if simulation reported {summary}."
    ),
    "policy_decision": (
        "The access policy resolved to {outcome}. {drivers}"
    ),
    "retention_action": (
        "A retention action was selected ({outcome}). {drivers}"
    ),
    "recovery_action": (
        "An automated recovery action ran and finished as {outcome}. {drivers}"
    ),
    "model_version": "A model version transition was recorded as {outcome}.",
    "topic_selection": "The topic was selected as {outcome}.",
    "booking_decision": "The booking decision resolved to {outcome}. {drivers}",
    "default": "The decision resolved to {outcome}.",
}

# rule_id prefix -> attribution bucket. Longest prefix wins; `*` is the fallback.
ATTRIBUTION_BUCKETS: list[dict[str, str]] = [
    {"match": "new_device", "bucket": "device"},
    {"match": "unverified_hsm", "bucket": "device"},
    {"match": "biometric_", "bucket": "identity"},
    {"match": "auth_velocity", "bucket": "identity"},
    {"match": "geo_velocity", "bucket": "location"},
    {"match": "poor_ip", "bucket": "location"},
    {"match": "off_hours", "bucket": "temporal"},
    {"match": "sensitive_target", "bucket": "access"},
    {"match": "churn", "bucket": "behaviour"},
    {"match": "dissatisfaction", "bucket": "behaviour"},
    {"match": "stage", "bucket": "lifecycle"},
    {"match": "value_tier", "bucket": "lifecycle"},
    {"match": "tax", "bucket": "regional"},
    {"match": "labor", "bucket": "regional"},
    {"match": "working_day", "bucket": "regional"},
    {"match": "*", "bucket": "other"},
]

MASK = "***"
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


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

    def query(
        self,
        *,
        entity_ref: str | None = None,
        decision_type: str | None = None,
        outcome: str | None = None,
        model_version: str | None = None,
        since: str | None = None,
        before: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        scored_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Filtered + paged read over the ring buffer.

        ``since``/``before`` are inclusive ISO-8601 bounds on ``generated_at``.
        ``scored_only`` drops traces that carry no numeric score, which is how
        you exclude simulations and promotions from a score report. Returns a
        page envelope rather than a bare list so callers never have to guess
        whether more rows exist.
        """
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        matches: list[dict[str, Any]] = []
        for did in reversed(self._order):
            trace = self._traces[did]
            if entity_ref and trace.entity_ref != entity_ref:
                continue
            if decision_type and trace.decision_type != decision_type:
                continue
            if outcome and trace.outcome != outcome:
                continue
            if model_version and trace.model_version != model_version:
                continue
            if (since or before) and not _within_window(trace.generated_at, since, before):
                continue
            if scored_only and not isinstance(trace.score, (int, float)):
                continue
            if min_score is not None and not _score_at_least(trace.score, min_score):
                continue
            if max_score is not None and not _score_at_most(trace.score, max_score):
                continue
            matches.append(trace.to_dict())
        page = matches[offset : offset + limit]
        return {
            "matched": len(matches),
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "has_more": offset + len(page) < len(matches),
            "traces": page,
        }

    def export(
        self,
        *,
        fmt: str = "ndjson",
        limit: int = 500,
        audience: str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """Serialize a filtered page out as NDJSON or CSV.

        ``audience`` applies the projection table first, so an exported file can
        be handed to an audience that is not allowed to see raw signals.
        """
        fmt = str(fmt or "ndjson").lower()
        if fmt not in {"ndjson", "csv"}:
            raise ValueError(f"unsupported export format: {fmt!r} (expected ndjson|csv)")
        page = self.query(limit=limit, **filters)
        rows = [
            project_trace(trace, audience) if audience else trace
            for trace in page["traces"]
        ]
        if fmt == "ndjson":
            content = "\n".join(json.dumps(row, sort_keys=True, default=str) for row in rows)
            columns: list[str] = []
        else:
            columns = sorted({key for row in rows for key in row})
            lines = [",".join(columns)]
            for row in rows:
                lines.append(
                    ",".join(_csv_cell(row.get(column)) for column in columns)
                )
            content = "\n".join(lines)
        return {
            "format": fmt,
            "audience": audience,
            "row_count": len(rows),
            "matched": page["matched"],
            "truncated": page["has_more"],
            "columns": columns,
            "content": content,
        }

    def drop_before(self, cutoff: str) -> int:
        """Evict traces generated before ``cutoff`` (ISO-8601). Returns count.

        Useful for compliance windows where old explanations must be destroyed
        on a schedule rather than whenever the ring happens to wrap.
        """
        removed = 0
        for did in list(self._order):
            trace = self._traces.get(did)
            if trace is None:
                continue
            if not _at_or_after(trace.generated_at, cutoff):
                self._order.remove(did)
                self._traces.pop(did, None)
                removed += 1
        return removed

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
        "audiences": [dict(entry) for entry in EXPLANATION_AUDIENCES],
        "default_audience": DEFAULT_AUDIENCE,
        "attribution_buckets": [dict(entry) for entry in ATTRIBUTION_BUCKETS],
        "narrative_templates": sorted(NARRATIVE_TEMPLATES),
        "readers": {
            "project_trace": "project_trace(trace, audience) -> audience-shaped dict",
            "redact_inputs": "redact_inputs(inputs, audience) -> masked copy",
            "attribute_factors": "attribute_factors(trace) -> contribution shares",
            "top_contributors": "top_contributors(trace, n) -> biggest drivers",
            "narrate": "narrate(trace) -> plain-language explanation",
            "counterfactual_factors": "counterfactual_factors(trace) -> what if removed",
            "compare_traces": "compare_traces(left, right) -> field/factor diff",
            "store.query": "filtered + paged read (outcome, window, score range)",
            "store.export": "ndjson | csv export, optionally audience-projected",
            "store.drop_before": "evict traces older than a compliance cutoff",
        },
        "mask": MASK,
    }


# --- Expansion: projection / attribution / narrative readers -----------------
#
# Everything below is a pure function over an already-recorded trace dict. None
# of it can change a decision; if you need a different decision, record a new
# trace with the new inputs.


def _first_match(table: Iterable[dict[str, Any]], value: str, key: str = "match") -> str:
    """Longest-prefix match of ``value`` against a config table; ``*`` fallback.

    Longest-prefix (not first-match) so a table can be listed in any order and a
    specific entry can still override a broader one.
    """
    text = str(value or "").lower()
    fallback = ""
    best: str = ""
    best_len = -1
    for row in table:
        needle = str(row.get(key, ""))
        if needle == "*":
            fallback = str(row.get("phrase") or row.get("bucket") or "")
            continue
        if text.startswith(needle) and len(needle) > best_len:
            best = str(row.get("phrase") or row.get("bucket") or "")
            best_len = len(needle)
    return best or fallback or str(value or "")


def audience_config(audience: str | None = None) -> dict[str, Any]:
    """Resolve an audience row, falling back to ``DEFAULT_AUDIENCE``."""
    for entry in EXPLANATION_AUDIENCES:
        if entry["audience"] == (audience or DEFAULT_AUDIENCE):
            return dict(entry)
    raise ValueError(
        f"unknown explanation audience: {audience!r} "
        f"(expected one of {[e['audience'] for e in EXPLANATION_AUDIENCES]})"
    )


def redact_inputs(
    inputs: dict[str, Any] | None,
    audience: str | None = None,
) -> dict[str, Any]:
    """Mask the keys an audience may not see. Keys are preserved, values masked.

    Deny-list semantics: anything not in ``redact_inputs`` is shown, so a new
    signal field is visible to the engineer view by default rather than
    silently hidden from everyone.
    """
    config = audience_config(audience)
    blocked = set(config.get("redact_inputs") or ())
    return {
        key: (MASK if key in blocked else value)
        for key, value in dict(inputs or {}).items()
    }


def project_trace(
    trace: dict[str, Any] | None,
    audience: str | None = None,
) -> dict[str, Any]:
    """Project a trace dict into the shape ``audience`` is allowed to see.

    Always returns the decision id/type/outcome (an explanation with no
    identity is useless), then layers on as much as the audience permits:
    inputs (redacted), factors (capped, weights stripped unless allowed), and a
    narrative. ``detail`` is only ever reduced, never expanded.
    """
    if not trace:
        return {}
    config = audience_config(audience)
    limit = int(config.get("factor_limit") or 0)
    factors = [dict(f) for f in trace.get("factors", [])]
    if limit > 0:
        factors = factors[:limit]
    if not config.get("show_weights"):
        for factor in factors:
            factor.pop("weight", None)
    projected: dict[str, Any] = {
        "decision_id": trace.get("decision_id"),
        "decision_type": trace.get("decision_type"),
        "entity_ref": trace.get("entity_ref"),
        "generated_at": trace.get("generated_at"),
        "score": trace.get("score"),
        "threshold": trace.get("threshold"),
        "outcome": trace.get("outcome"),
        "model_version": trace.get("model_version"),
        "audience": config["audience"],
        "detail_level": config.get("detail"),
    }
    if config.get("show_inputs"):
        projected["inputs"] = redact_inputs(trace.get("inputs"), audience)
    if config.get("detail") in {"full", "redacted", "summary"}:
        projected["factors"] = factors
    if config.get("detail") in {"full", "redacted"}:
        projected["detail"] = dict(trace.get("detail") or {})
    if config.get("detail") in {"summary", "narrative"}:
        projected["attribution"] = attribute_factors(trace)["buckets"]
    if config.get("detail") == "narrative":
        projected.pop("factors", None)
        projected["narrative"] = narrate(trace)
    return projected


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def attribute_factors(trace: dict[str, Any] | None) -> dict[str, Any]:
    """Attribute a trace's score to named buckets.

    Only factors that actually *fired* contribute, and only when they carry a
    numeric weight — an unfired rule explains the decision negatively (it is
    listed under ``absent``) but must not dilute the share of the ones that
    did. ``score`` is the denominator when it is numeric so shares add to 1.0
    against the final score rather than against the raw weight sum.
    """
    if not trace:
        return {"score": None, "total_weight": 0.0, "buckets": [], "factors": [], "absent": []}
    fired: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    total_weight = 0.0
    for raw in trace.get("factors", []):
        factor = dict(raw)
        rule_id = str(factor.get("rule_id") or "")
        if not factor.get("present", False):
            absent.append({"rule_id": rule_id, "reason": factor.get("reason", "")})
            continue
        weight = _numeric(factor.get("weight"))
        if weight is None:
            absent.append({"rule_id": rule_id, "reason": factor.get("reason", "")})
            continue
        total_weight += weight
        fired.append(
            {
                "rule_id": rule_id,
                "weight": weight,
                "reason": factor.get("reason", ""),
                "phrase": _first_match(FACTOR_PHRASING, rule_id),
                "bucket": _first_match(ATTRIBUTION_BUCKETS, rule_id),
            }
        )
    denominator = _numeric(trace.get("score"))
    if not denominator:
        denominator = total_weight or None
    contributions: list[dict[str, Any]] = []
    for factor in fired:
        share = (factor["weight"] / denominator) if denominator else 0.0
        contributions.append({**factor, "share": round(share, 4)})
    bucket_totals: dict[str, float] = {}
    for factor in contributions:
        bucket_totals[factor["bucket"]] = bucket_totals.get(factor["bucket"], 0.0) + factor["share"]
    buckets = [
        {"bucket": bucket, "share": round(share, 4)}
        for bucket, share in sorted(bucket_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    contributions.sort(key=lambda f: f["weight"], reverse=True)
    return {
        "score": trace.get("score"),
        "denominator": denominator,
        "total_weight": round(total_weight, 4),
        "attributed_share": round(sum(bucket_totals.values()), 4),
        "buckets": buckets,
        "factors": contributions,
        "absent": absent,
    }


def top_contributors(trace: dict[str, Any] | None, n: int = 3) -> list[dict[str, Any]]:
    """The ``n`` largest fired factors, biggest first."""
    n = max(0, int(n))
    if not n:
        return []
    return attribute_factors(trace)["factors"][:n]


def _drivers_clause(trace: dict[str, Any]) -> str:
    factors = top_contributors(trace, 3)
    if not factors:
        return "No individual rule contributed; the outcome came from the base configuration."
    phrases = [f["phrase"] for f in factors]
    if len(phrases) == 1:
        return f"This was mainly because {phrases[0]}."
    return "This was mainly because " + ", ".join(phrases[:-1]) + f" and {phrases[-1]}."


def narrate(trace: dict[str, Any] | None) -> str:
    """Plain-language explanation rendered from ``NARRATIVE_TEMPLATES``.

    Only literal text and ``{placeholder}`` substitution is supported: a
    template can never reach into the trace object, so a config typo degrades
    to a visible ``{placeholder}`` rather than an exception or a data leak.
    """
    if not trace:
        return ""
    template = NARRATIVE_TEMPLATES.get(
        str(trace.get("decision_type") or ""), NARRATIVE_TEMPLATES["default"]
    )
    summary = trace.get("detail", {}) if isinstance(trace.get("detail"), dict) else {}
    scope = {
        "score": trace.get("score"),
        "threshold": trace.get("threshold"),
        "outcome": trace.get("outcome") or "unknown",
        "decision_type": trace.get("decision_type"),
        "entity_ref": trace.get("entity_ref"),
        "model_version": trace.get("model_version"),
        "summary": ", ".join(f"{k}={v}" for k, v in sorted(summary.items())) or "no summary",
    }
    text = _PLACEHOLDER.sub(
        lambda m: str(scope.get(m.group(1), m.group(0))), template
    )
    if "{drivers}" in template:
        text = text.replace("{drivers}", _drivers_clause(trace))
    return text


def counterfactual_factors(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the score would have been with each single factor removed.

    Pure arithmetic on the recorded weights (score minus that factor's
    contribution, clamped at 0). Useful for "what would have had to be
    different" questions without re-running the engine — and, unlike a
    re-score, it cannot drift from what was actually recorded.
    """
    if not trace:
        return []
    score = _numeric(trace.get("score"))
    if score is None:
        return []
    rows: list[dict[str, Any]] = []
    for factor in attribute_factors(trace)["factors"]:
        without = max(0.0, score - factor["weight"])
        rows.append(
            {
                "rule_id": factor["rule_id"],
                "phrase": factor["phrase"],
                "bucket": factor["bucket"],
                "weight": factor["weight"],
                "score_without": round(without, 4),
                "reduction": round(score - without, 4),
            }
        )
    rows.sort(key=lambda row: row["reduction"], reverse=True)
    return rows


def compare_traces(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any]:
    """Diff two recorded decisions field-by-field and factor-by-factor.

    Answers "the same request produced a different answer yesterday" — the
    question a canary or a retune is really asking.
    """
    if not left or not right:
        missing = "left" if not left else "right"
        return {"comparable": False, "reason": f"{missing} trace not found"}
    changed_fields = {
        field: {"left": left.get(field), "right": right.get(field)}
        for field in sorted(set(_TRACE_FIELDS))
        if left.get(field) != right.get(field)
    }
    left_factors = {f.get("rule_id"): dict(f) for f in left.get("factors", [])}
    right_factors = {f.get("rule_id"): dict(f) for f in right.get("factors", [])}
    factor_changes: list[dict[str, Any]] = []
    for rule_id in sorted(set(left_factors) | set(right_factors)):
        before = left_factors.get(rule_id)
        after = right_factors.get(rule_id)
        if before is None:
            factor_changes.append(
                {"rule_id": rule_id, "change": "added", "left": None, "right": after}
            )
        elif after is None:
            factor_changes.append(
                {"rule_id": rule_id, "change": "removed", "left": before, "right": None}
            )
        elif before.get("present") != after.get("present") or before.get("weight") != after.get("weight"):
            factor_changes.append(
                {
                    "rule_id": rule_id,
                    "change": "changed",
                    "left": {"present": before.get("present"), "weight": before.get("weight")},
                    "right": {"present": after.get("present"), "weight": after.get("weight")},
                }
            )
    left_score = _numeric(left.get("score"))
    right_score = _numeric(right.get("score"))
    return {
        "comparable": True,
        "left_decision_id": left.get("decision_id"),
        "right_decision_id": right.get("decision_id"),
        "same_decision_type": left.get("decision_type") == right.get("decision_type"),
        "same_entity": left.get("entity_ref") == right.get("entity_ref"),
        "changed_fields": changed_fields,
        "changed_field_count": len(changed_fields),
        "factor_changes": factor_changes,
        "score_delta": (
            round(right_score - left_score, 4)
            if left_score is not None and right_score is not None
            else None
        ),
        "outcome_changed": left.get("outcome") != right.get("outcome"),
    }


# --- Small value helpers (kept private, shared by query/export) --------------


def _parse_moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _within_window(generated_at: str, since: str | None, before: str | None) -> bool:
    moment = _parse_moment(generated_at)
    if moment is None:
        return False
    lower = _parse_moment(since)
    upper = _parse_moment(before)
    if lower is not None and moment < lower:
        return False
    if upper is not None and moment > upper:
        return False
    return True


def _at_or_after(generated_at: str, cutoff: str) -> bool:
    moment = _parse_moment(generated_at)
    bound = _parse_moment(cutoff)
    if moment is None or bound is None:
        return True
    return moment >= bound


def _score_at_least(score: Any, minimum: float) -> bool:
    value = _numeric(score)
    return value is not None and value >= float(minimum)


def _score_at_most(score: Any, maximum: float) -> bool:
    value = _numeric(score)
    return value is not None and value <= float(maximum)


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text