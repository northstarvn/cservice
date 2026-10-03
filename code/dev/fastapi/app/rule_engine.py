"""Shared `when`-DSL rule engine core (Stage 1: Business Rule Hyper-Flexibility).

Four service modules (loyalty_journey, communication_strategy, arrears_payments,
points_exchange) originally copy-pasted the same condition DSL: a pure AND walk
over a ``when`` dict with numeric operators, list membership, equality and bool
checks. This module unifies that core and extends it, so richer rules are a
*single* engine that every consumer delegates to:

Field-level DSL (backward compatible with the original per-module engines)
-----------------------------------------------------------------------
- scalar equality:          ``{"stage": "new"}``
- bool:                     ``{"has_history": False}``
- list membership (both directions):
  context value in rule list / rule value in context list
- numeric-operator dict:    ``{"gte": 1, "lt": 5}``
- date-window-operator dict (new): ``{"between_dates": ["2026-06-01", "2026-08-31"]}``,
  ``{"month_in": [6, 7, 8]}``, ``{"weekday_in": ["sat", "sun"]}``,
  ``{"on_date": "2026-12-25"}``, ``{"within_days": 7}``

Whole-rule combinators (new)
---------------------------
- ``{"any": [...]}``  at least one nested rule matches
- ``{"all": [...]}``  every nested rule matches
- ``{"not": ...}``    the nested rule must NOT match

Date evaluation
---------------
- A rule may reference the reserved key ``"_date"`` to bind the *evaluation
  date* (``effective_date`` if provided, otherwise the current UTC date):
  ``{"_date": {"between_dates": ["2026-06-01", "2026-08-31"]}}`` — used by
  seasonal campaign and regional calendar rules.
- Date-window operators applied to ordinary context fields (e.g. a booking
  date) are evaluated against the same evaluation date.

Expression params (new)
-----------------------
Config params may carry safe arithmetic formulas as strings prefixed with
``=``. ``resolve_params`` evaluates each such value against the rule context
plus math constants (``pi``/``e``) and an allow-list of callables
(``abs``/``round``/``min``/``max``/``pow``/``floor``/``ceil``/``sqrt``).
Arbitrary code (imports, attributes, subscripts) is rejected at parse time —
the AST walker only accepts constants, names, arithmetic/comparison/boolean
operators, ternary ``x if c else y``, list literals, and the allow-listed calls.

Expansion (thin-group pass)
---------------------------
The DSL above is *deliberately* small, and small is what makes it safe to run
over tenant-authored config. It is also small enough that real rules hit its
edges constantly: "starts with a prefix", "at least 3 of these tags", "is this
field empty", "why did my rule not fire". None of those can be expressed today,
and the usual workaround — computing a derived field upstream — puts rule
logic back in Python where it cannot be reviewed or overridden.

This module therefore grows *alongside* the v1 engine rather than inside it:

- ``STRING_OPS`` / ``COLLECTION_OPS`` / ``NULL_OPS`` — three new operator
  families, declared as tables. ``matches_field`` and ``evaluate_when`` are
  **byte-for-byte unchanged** and still reject every one of these names; the
  new operators are reachable only through ``evaluate_when_extended``, so no
  pinned verdict can move and a typo can never silently become a passing rule.
- ``explain_when`` — per-field trace with a human reason for each pass/fail,
  including *which* field broke an ``any``/``all``/``not`` branch.
- ``validate_when`` — static checking of a rule before it is saved: unknown
  operators, reserved-key misuse, malformed thresholds, bad ``=`` expressions.
- ``RULE_PACKS`` + ``merge_rule_packs`` / ``export_rule_pack`` / ``diff_when`` /
  ``select_rules`` — promote a rule set from staging to production and see
  exactly what changed, which is the operation that silently drops conditions.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import math
import operator
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

_NUMERIC_OPS: dict[str, Any] = {
    "gte": operator.ge,
    "gt": operator.gt,
    "lte": operator.le,
    "lt": operator.lt,
    "eq": operator.eq,
    "ne": operator.ne,
}

#: The subset of ``_NUMERIC_OPS`` whose threshold must be a number.
#:
#: ``eq`` and ``ne`` are excluded on purpose: they are evaluated by
#: ``operator.eq``/``operator.ne``, which succeed against any pair of objects, so
#: requiring a float of them rejects working rules. See ``_validate_threshold``,
#: which is where this split is load-bearing. Kept as a name rather than inlined
#: so the test that pins the split has something to point at.
_ORDERING_OPS: tuple[str, ...] = ("gt", "gte", "lt", "lte")

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_DATE_OPS: dict[str, str] = {
    "within_days": "date is within N days (inclusive) of the evaluation date",
    "between_dates": "date is inside the inclusive [start, end] ISO date range",
    "month_in": "month (1-12) is in the given list of months",
    "weekday_in": "weekday (mon..sun) is in the given list",
    "on_date": "date equals the given ISO date",
}

_COMBINATORS = ("any", "all", "not")

# Public alias — the reserved `_date` key in a `when` dict binds the evaluation
# date (effective_date if provided, else the current UTC date).
RESERVED_DATE_KEY = "_date"
_RESERVED_DATE_KEY = RESERVED_DATE_KEY

_MATH_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}
_SAFE_CALLS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
}
_COMPARE_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# ---------------------------------------------------------------------------
# Expansion config tables
#
# These operator families are *not* consulted by `matches_field` /
# `evaluate_when` — the v1 matchers keep rejecting any operator they do not
# already know, which is what makes an unrecognised key a safe failure instead
# of a silently-true condition. They are reachable via `evaluate_when_extended`.
# ---------------------------------------------------------------------------

STRING_OPS: list[dict[str, Any]] = [
    {"op": "starts_with", "arity": "str, str", "description": "Value begins with the prefix (case-sensitive)."},
    {"op": "ends_with", "arity": "str, str", "description": "Value ends with the suffix (case-sensitive)."},
    {"op": "contains", "arity": "str, str", "description": "Substring anywhere in the value."},
    {"op": "not_contains", "arity": "str, str", "description": "Substring appears nowhere in the value."},
    {"op": "equals_ignore_case", "arity": "str, str", "description": "Case-insensitive equality after stripping."},
    {"op": "starts_with_any", "arity": "str, [str]", "description": "Value begins with any listed prefix."},
    {"op": "in_phrase", "arity": "str, [str]", "description": "Whole-word match of any listed phrase."},
    {"op": "matches", "arity": "str, str", "description": "Regex match under REGEX_POLICY (search, then full)."},
    {"op": "word_count_gte", "arity": "str, int", "description": "At least N whitespace-separated words."},
    {"op": "length_between", "arity": "str, [int, int]", "description": "Character length inside the inclusive range."},
    {"op": "slug_equals", "arity": "str, str", "description": "Casefolded, non-alphanumerics collapsed to '-'."},
]

COLLECTION_OPS: list[dict[str, Any]] = [
    {"op": "len_eq", "arity": "list|str, int", "description": "Exactly N elements."},
    {"op": "len_gte", "arity": "list|str, int", "description": "At least N elements."},
    {"op": "len_lte", "arity": "list|str, int", "description": "At most N elements."},
    {"op": "is_empty", "arity": "list|str", "description": "Empty container or empty/whitespace string."},
    {"op": "intersects", "arity": "list, list", "description": "At least one element in common."},
    {"op": "disjoint", "arity": "list, list", "description": "No elements in common."},
    {"op": "contains_all", "arity": "list, list", "description": "Value contains every listed element."},
    {"op": "contains_any", "arity": "list, list", "description": "Value contains at least one listed element."},
    {"op": "subset_of", "arity": "list, list", "description": "Every element of the value is in the threshold."},
    {"op": "distinct_count_gte", "arity": "list, int", "description": "At least N distinct elements."},
    {"op": "sum_gte", "arity": "list, num", "description": "Sum of numeric elements is at least N."},
    {"op": "avg_gte", "arity": "list, num", "description": "Mean of numeric elements is at least N (empty = False)."},
    {"op": "max_lte", "arity": "list, num", "description": "Largest numeric element is at most N."},
    {"op": "min_gte", "arity": "list, num", "description": "Smallest numeric element is at least N."},
]

NULL_OPS: list[dict[str, Any]] = [
    {"op": "is_null", "arity": "any", "description": "Value is None."},
    {"op": "not_null", "arity": "any", "description": "Value is not None."},
    {"op": "is_blank", "arity": "any", "description": "None, empty string, or whitespace only."},
    {"op": "not_blank", "arity": "any", "description": "Has a meaningful value."},
    {"op": "coalesce_eq", "arity": "any, any", "description": "Equals the threshold after falling back through the `default chain."},
    {"op": "default_to", "arity": "any, any", "description": "The value the rule should treat as this when it is blank."},
]

# External enrichment operators — only usable in async/background rules.
# These are NOT evaluated by matches_field / evaluate_when (v1 engine).
# They are reachable only through evaluate_when_external, which is called
# by the pipeline enrichment stage, never by the sync request path.
EXTERNAL_OPS: list[dict[str, Any]] = [
    {"op": "enriched_field", "arity": "str, any", "description": "Compare an enriched field (from enrichment stage) against a threshold."},
    {"op": "enriched_present", "arity": "str", "description": "True if the enrichment stage produced a value for this field."},
    {"op": "enriched_absent", "arity": "str", "description": "True if the enrichment stage did not produce a value for this field."},
    {"op": "webhook_received", "arity": "str", "description": "True if a webhook event from this provider was received in the last N hours."},
    {"op": "external_api_ok", "arity": "str", "description": "True if the external API health check passed in the last N minutes."},
]

OPERATOR_FAMILIES: dict[str, str] = {
    **{entry["op"]: "string" for entry in STRING_OPS},
    **{entry["op"]: "collection" for entry in COLLECTION_OPS},
    **{entry["op"]: "null" for entry in NULL_OPS},
    **{entry["op"]: "external" for entry in EXTERNAL_OPS},
    **{name: "numeric" for name in _NUMERIC_OPS},
    **{name: "date" for name in _DATE_OPS},
}

# `matches` is the only operator that hands tenant config to the regex engine,
# so it is fenced: bounded pattern length, no inline flags, a compiled-pattern
# cache, and a step budget so a pathological pattern cannot hang a request.
REGEX_POLICY: dict[str, Any] = {
    "max_pattern_length": 256,
    "max_subject_length": 4096,
    "match_mode": "search",
    "allow_inline_flags": False,
    "reject_nested_quantifiers": True,
    "cache_size": 128,
    "step_budget": 20000,
    "description": (
        "A tenant regex is data, not code, but it is still the one place in this "
        "module where a pathological pattern can cost real CPU. Patterns are "
        "length-capped, stripped of inline flags, screened for catastrophic "
        "backtracking, and scanned under a match-attempt budget. A full-string "
        "match is expressed by anchoring the pattern with ^...$."
    ),
}
_REGEX_CACHE: dict[str, Any] = {}

# Rule packs: named, versioned bundles of `when` + `params` rules that are
# promoted between environments as a unit. `priority` breaks ties; higher wins.
RULE_PACKS: list[dict[str, Any]] = [
    {
        "pack": "loyalty_retention",
        "version": 2,
        "domain": "loyalty_journey",
        "priority": 100,
        "description": "Retention nudges: at-risk members and dormant accounts.",
        "rules": [
            {
                "id": "retention_high_risk",
                "enabled": True,
                "priority": 90,
                "effective_from": "2026-01-01",
                "effective_to": None,
                # `any`, not `{"in": [...]}`. There is no membership operator in
                # this DSL -- `validate_when` rejects `in` as
                # `unknown_operator`, and it was rejecting it here too; the
                # clause just never met the validator, because `validate_when`
                # runs on tenant-authored rules and nothing ran it over
                # `RULE_PACKS`. So the rule shipped in the catalog with a clause
                # the engine cannot evaluate, and could never fire for anybody.
                # A rule nobody calls, containing an operator nobody implements:
                # two independent reasons for the same green.
                "when": {"churn_risk": "high", "any": [{"stage": "engaged"}, {"stage": "at_risk"}]},
                "params": {"nudge_channel": "email", "discount_pct": "=15 + 5 if stage == 'at_risk' else 0"},
            },
            {
                "id": "retention_dormant",
                "enabled": True,
                "priority": 50,
                "effective_from": "2026-01-01",
                "effective_to": None,
                "when": {"days_since_login": {"gte": 45}, "at_risk": True},
                "params": {"nudge_channel": "sms", "discount_pct": 10},
            },
            {
                "id": "retention_negative_sentiment",
                "enabled": False,
                "priority": 70,
                "effective_from": "2026-04-01",
                "effective_to": None,
                "when": {"sentiment_label": "negative"},
                "params": {"nudge_channel": "none", "discount_pct": 0},
            },
        ],
    },
    {
        "pack": "communication_suppression",
        "version": 1,
        "domain": "communication_strategy",
        "priority": 80,
        "description": "Suppress outreach that would land badly.",
        "rules": [
            {
                "id": "suppress_negative_sentiment",
                "enabled": True,
                "priority": 95,
                "effective_from": "2026-01-01",
                "effective_to": None,
                "when": {"sentiment_label": "negative", "churn_risk": {"ne": "low"}},
                "params": {"suppress": True, "reason": "recent negative sentiment"},
            },
            {
                "id": "suppress_recent_complaint",
                "enabled": True,
                "priority": 85,
                "effective_from": "2026-01-01",
                "effective_to": None,
                "when": {"complaints_last_30d": {"gte": 2}},
                "params": {"suppress": True, "reason": "multiple recent complaints"},
            },
        ],
    },
    {
        "pack": "seasonal_campaigns",
        "version": 1,
        "domain": "points_exchange",
        "priority": 60,
        "description": "Date-anchored campaign eligibility.",
        "rules": [
            {
                "id": "summer_boost",
                "enabled": True,
                "priority": 70,
                "effective_from": "2026-06-01",
                "effective_to": "2026-08-31",
                "when": {"_date": {"between_dates": ["2026-06-01", "2026-08-31"]}, "value_tier": "premium"},
                "params": {"multiplier": 1.5},
            },
        ],
    },
]
RULE_PACK_BY_NAME = {entry["pack"]: dict(entry) for entry in RULE_PACKS}

# How a merge resolves two packs that define the same rule id. `error` refuses
# the promotion outright: a colliding id is exactly how a rule gets lost.
PACK_MERGE_STRATEGIES = ("later_wins", "first_wins", "union", "error")
RULE_PACK_EXPORT_FORMATS = ("json", "jsonl", "csv", "python")


def _json_safe(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return value


# ---------------------------------------------------------------------------
# Date coercion
# ---------------------------------------------------------------------------


def _coerce_date(value: Any) -> Optional[date]:
    """Normalize a datetime/date/ISO-8601 string (with optional Z / tz) to a date."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.date()
    return None


def _evaluation_date(*, effective_date: Any = None, now: Any = None) -> Optional[date]:
    """The anchor date used by date-window operators (effective_date > now > today)."""
    if effective_date is not None:
        return _coerce_date(effective_date)
    if now is not None:
        return _coerce_date(now)
    return datetime.now(timezone.utc).date()


def _date_op_holds(op_name: str, value: date, threshold: Any, evaluation: date) -> bool:
    if op_name == "within_days":
        try:
            days = max(1, abs(int(threshold)))
        except (TypeError, ValueError):
            return False
        return abs((evaluation - value).days) <= days
    if op_name == "between_dates":
        if not isinstance(threshold, (list, tuple)) or len(threshold) != 2:
            return False
        start = _coerce_date(threshold[0])
        end = _coerce_date(threshold[1])
        if start is None or end is None:
            return False
        return start <= value <= end
    if op_name == "month_in":
        if not isinstance(threshold, (list, tuple, set)):
            return False
        try:
            months = {int(month) for month in threshold}
        except (TypeError, ValueError):
            return False
        return value.month in months
    if op_name == "weekday_in":
        if not isinstance(threshold, (list, tuple, set)):
            return False
        names = {str(day).lower()[:3] for day in threshold}
        return _WEEKDAYS[value.weekday()] in names
    if op_name == "on_date":
        target = _coerce_date(threshold)
        return target is not None and value == target
    return False


# ---------------------------------------------------------------------------
# Field-level matcher
# ---------------------------------------------------------------------------


def matches_field(
    rule_value: Any,
    context_value: Any,
    *,
    effective_date: Any = None,
    now: Any = None,
) -> bool:
    """Match a single context field against a rule value (the field-level DSL).

    Behavior is a strict superset of the original ``_matches_rule`` helpers:
    every input the old engines accepted still evaluates identically, and the
    date-window operators add new capabilities for the old inputs.
    """
    evaluation = _evaluation_date(effective_date=effective_date, now=now)
    if isinstance(rule_value, dict):
        for op_name, threshold in rule_value.items():
            if op_name in _DATE_OPS:
                value = _coerce_date(context_value)
                if value is None or evaluation is None:
                    return False
                if not _date_op_holds(op_name, value, threshold, evaluation):
                    return False
                continue
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


# ---------------------------------------------------------------------------
# Whole-rule evaluator
# ---------------------------------------------------------------------------


def _evaluate_combinator(
    name: str,
    spec: Any,
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    now: Any = None,
) -> tuple[bool, dict[str, Any]]:
    entries = spec if isinstance(spec, list) else [spec]
    if name == "any":
        merged: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ok, fields = evaluate_when(entry, context, effective_date=effective_date, now=now)
            if ok:
                merged.update(fields)
                return True, merged
        return False, {}
    if name == "all":
        merged = {}
        for entry in entries:
            if not isinstance(entry, dict):
                return False, {}
            ok, fields = evaluate_when(entry, context, effective_date=effective_date, now=now)
            if not ok:
                return False, {}
            merged.update(fields)
        return True, merged
    # not: none of the nested rules may match
    for entry in entries:
        if isinstance(entry, dict):
            ok, _fields = evaluate_when(entry, context, effective_date=effective_date, now=now)
            if ok:
                return False, {}
    return True, {}


def evaluate_when(
    when: dict[str, object],
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    now: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Evaluate every condition in `when` against `context`.

    Returns ``(matched, matched_fields)`` — the same contract as the original
    per-module engines (AND over field conditions; unknown fields fail the whole
    rule). Extensions: ``any``/``all``/``not`` combinators, the reserved
    ``_date`` key bound to the evaluation date, and date-window operators.
    """
    evaluation = _evaluation_date(effective_date=effective_date, now=now)
    matched_fields: dict[str, Any] = {}
    for field_name, rule_value in (when or {}).items():
        if field_name in _COMBINATORS and field_name not in context:
            ok, fields = _evaluate_combinator(
                field_name, rule_value, context, effective_date=effective_date, now=now
            )
            if not ok:
                return False, {}
            matched_fields[field_name] = fields
            continue
        if field_name == _RESERVED_DATE_KEY:
            if evaluation is None or not matches_field(rule_value, evaluation, effective_date=effective_date, now=now):
                return False, {}
            matched_fields[field_name] = evaluation.isoformat()
            continue
        if field_name not in context:
            return False, {}
        if not matches_field(rule_value, context[field_name], effective_date=effective_date, now=now):
            return False, {}
        matched_fields[field_name] = _json_safe(context[field_name])
    return True, matched_fields


# ---------------------------------------------------------------------------
# Safe expression params
# ---------------------------------------------------------------------------


def evaluate_expression(expr: str, scope: Optional[dict[str, Any]] = None) -> Any:
    """Evaluate a safe arithmetic/boolean formula against `scope`.

    Only constants, names (resolved from the scope or the math constants),
    arithmetic (``+ - * / // % **``), unary (``+ - not``), comparisons
    (``== != < <= > >=``), boolean ``and``/``or``, ternary ``x if c else y``,
    list literals, and allow-listed calls are accepted. Anything else raises
    ``ValueError`` — this is a formula calculator, not a Python sandbox.

    **String constants are allowed, and were not, which broke a shipped rule.**
    The grammar above has always claimed ``==`` and ``!=``; it could only deliver
    them over numbers, because ``_eval_node`` rejected ``ast.Constant`` unless it
    was a ``bool``/``int``/``float``. ``loyalty_retention.retention_high_risk``
    ships ``discount_pct: "=15 + 5 if stage == 'at_risk' else 0"``, which parses
    fine and then raised ``unsupported constant 'at_risk'`` from
    ``resolve_params`` -- inside ``select_rules``, on a rule whose ``when`` had
    already matched. So the pack contained a rule that crashed its own caller
    the moment it did its job, which is a worse outcome than never firing.

    Allowing the literal does not make this a string language: no string method
    is reachable, no attribute access is reachable, no indexing is reachable,
    and the only calls are the numeric allow-list. What it buys is the ability to
    *name* a vocabulary value, which is what a rule keying off an enum stage
    needs. ``TypeError`` is translated to ``ValueError`` so the "anything else
    raises ``ValueError``" promise above holds even when the grammar accepts a
    combination that has no meaning, such as ``"a" - 1``.
    """
    source = str(expr or "").strip()
    if not source:
        raise ValueError("empty rule expression")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid rule expression {source!r}: {exc}") from exc
    try:
        return _eval_node(tree, dict(scope or {}))
    except ValueError:
        raise
    except (TypeError, ArithmeticError, IndexError, KeyError) as exc:
        # Well-formed but meaningless: `"a" - 1`, `round('x')`, `1 / 0`.
        raise ValueError(f"rule expression {source!r} does not evaluate: {exc}") from exc


def _eval_node(node: Any, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, scope)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int, float, str)) or node.value is None:
            return node.value
        raise ValueError(f"unsupported constant {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        if node.id in _MATH_CONSTANTS:
            return _MATH_CONSTANTS[node.id]
        raise ValueError(f"unknown symbol {node.id!r}")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, scope)
        right = _eval_node(node.right, scope)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            return left ** right
        raise ValueError("unsupported binary operator")
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, scope)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.Not):
            return not value
        raise ValueError("unsupported unary operator")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, scope)
        for op_node, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, scope)
            compare = _COMPARE_OPS.get(type(op_node))
            if compare is None:
                raise ValueError("unsupported comparison operator")
            if not compare(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not _eval_node(value, scope):
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for value in node.values:
                if _eval_node(value, scope):
                    return True
            return False
        raise ValueError("unsupported boolean operator")
    if isinstance(node, ast.IfExp):
        return _eval_node(node.body, scope) if _eval_node(node.test, scope) else _eval_node(node.orelse, scope)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_CALLS:
        args = [_eval_node(arg, scope) for arg in node.args]
        return _SAFE_CALLS[node.func.id](*args)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(elt, scope) for elt in node.elts]
    raise ValueError(f"unsupported node in rule expression: {type(node).__name__}")


def _resolve_param(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("="):
        return evaluate_expression(value[1:], scope)
    if isinstance(value, dict):
        return {key: _resolve_param(item, scope) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve_param(item, scope) for item in value]
    return value


def resolve_params(params: Optional[dict[str, Any]], context: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Resolve ``=expr`` string values inside a params dict against `context`."""
    return {key: _resolve_param(value, dict(context or {})) for key, value in (params or {}).items()}


# ---------------------------------------------------------------------------
# Expansion: extended operators, explanation, validation, rule packs
# ---------------------------------------------------------------------------


def _regex_risky(source: str) -> bool:
    """Screen for catastrophic-backtracking shapes before compiling.

    ``(a+)+`` and friends blow up exponentially on a non-matching input. Python's
    ``re`` has no timeout, so the only defence available to a request handler is
    to refuse the pattern outright.
    """
    return bool(
        re.search(r"\((?:[^()]*[+*][^()]*)\)[+*]", source)          # (a+)+
        or re.search(r"\((?:[^()|]*\|[^()|]*)\)[+*]", source)     # (a|a)+
        or re.search(r"\{\d+,\d*\}\s*\{", source)                  # {n,}{m,}
    )


def _regex_search(pattern: str, subject: str) -> bool:
    """Run a tenant regex under ``REGEX_POLICY`` (cached, length-capped, bounded).

    Matching is a manual scan over start offsets rather than ``Pattern.search``,
    purely so the number of match attempts is bounded by ``step_budget`` — the
    dominant cost of a scan and the one an attacker can inflate with a long
    subject. Exceeding the budget is reported as "no match", never as an error.
    """
    text = str(subject or "")
    source = str(pattern or "")
    if not source or len(source) > int(REGEX_POLICY["max_pattern_length"]):
        return False
    if len(text) > int(REGEX_POLICY["max_subject_length"]):
        return False
    if not REGEX_POLICY["allow_inline_flags"] and re.search(r"\(\?[aiLmsux]+\)", source):
        return False
    if _regex_risky(source):
        return False
    compiled = _REGEX_CACHE.get(source)
    if compiled is None:
        try:
            compiled = re.compile(source)
        except re.error:
            _REGEX_CACHE[source] = False
            return False
        if len(_REGEX_CACHE) >= int(REGEX_POLICY["cache_size"]):
            _REGEX_CACHE.clear()
        _REGEX_CACHE[source] = compiled
    if compiled is False:
        return False
    budget = int(REGEX_POLICY["step_budget"])
    for offset in range(len(text) + 1):
        budget -= 1
        if budget <= 0:
            return False
        if compiled.match(text, offset) is not None:
            return True
    return False


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if value is None:
        return []
    return [value]


def _numbers(value: Any) -> list[float]:
    out: list[float] = []
    for item in _as_list(value):
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _string_op_holds(op_name: str, value: Any, threshold: Any) -> bool:
    text = "" if value is None else str(value)
    if op_name == "starts_with":
        return text.startswith(str(threshold))
    if op_name == "ends_with":
        return text.endswith(str(threshold))
    if op_name == "contains":
        return str(threshold) in text
    if op_name == "not_contains":
        return str(threshold) not in text
    if op_name == "equals_ignore_case":
        return text.strip().casefold() == str(threshold).strip().casefold()
    if op_name == "starts_with_any":
        return any(text.startswith(str(item)) for item in _as_list(threshold))
    if op_name == "in_phrase":
        needles = {str(item).strip().casefold() for item in _as_list(threshold)}
        words = {word.strip(".,!?;:'\"()[]").casefold() for word in text.split()}
        return bool(needles & words)
    if op_name == "matches":
        return _regex_search(str(threshold), text)
    if op_name == "word_count_gte":
        try:
            return len(text.split()) >= int(threshold)
        except (TypeError, ValueError):
            return False
    if op_name == "length_between":
        bounds = _as_list(threshold)
        if len(bounds) != 2:
            return False
        try:
            return int(bounds[0]) <= len(text) <= int(bounds[1])
        except (TypeError, ValueError):
            return False
    if op_name == "slug_equals":
        return _slug(text) == _slug(threshold)
    return False


def _collection_op_holds(op_name: str, value: Any, threshold: Any) -> bool:
    items = _as_list(value) if not isinstance(value, str) else list(value)
    if op_name in ("len_eq", "len_gte", "len_lte"):
        try:
            size = len(items)
            if op_name == "len_eq":
                return size == int(threshold)
            if op_name == "len_gte":
                return size >= int(threshold)
            return size <= int(threshold)
        except (TypeError, ValueError):
            return False
    if op_name == "is_empty":
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return len(_as_list(value)) == 0
    if op_name in ("intersects", "disjoint", "contains_all", "contains_any", "subset_of"):
        left = {_json_safe_key(item) for item in _as_list(value)}
        right = {_json_safe_key(item) for item in _as_list(threshold)}
        if op_name == "intersects":
            return bool(left & right)
        if op_name == "disjoint":
            return not (left & right)
        if op_name == "contains_all":
            return right.issubset(left)
        if op_name == "contains_any":
            return bool(left & right)
        return left.issubset(right)
    if op_name == "distinct_count_gte":
        try:
            return len({_json_safe_key(item) for item in items}) >= int(threshold)
        except (TypeError, ValueError):
            return False
    numbers = _numbers(value)
    try:
        if op_name == "sum_gte":
            return bool(numbers) and sum(numbers) >= float(threshold)
        if op_name == "avg_gte":
            return bool(numbers) and (sum(numbers) / len(numbers)) >= float(threshold)
        if op_name == "max_lte":
            return bool(numbers) and max(numbers) <= float(threshold)
        if op_name == "min_gte":
            return bool(numbers) and min(numbers) >= float(threshold)
    except (TypeError, ValueError):
        return False
    return False


def _json_safe_key(value: Any) -> Any:
    """Hashable projection of an element so set ops work on dicts too."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _null_op_holds(op_name: str, value: Any, threshold: Any) -> bool:
    if op_name == "is_null":
        return value is None
    if op_name == "not_null":
        return value is not None
    if op_name == "is_blank":
        return value is None or (isinstance(value, str) and not value.strip())
    if op_name == "not_blank":
        return not (value is None or (isinstance(value, str) and not value.strip()))
    if op_name in ("coalesce_eq", "default_to"):
        chain = _as_list(threshold)
        for candidate in chain:
            if candidate is not None and not (isinstance(candidate, str) and not candidate.strip()):
                if op_name == "default_to":
                    return _null_op_holds("is_blank", value, None)
                return _json_safe_key(value) == _json_safe_key(candidate) if value is not None else False
        return False
    return False


def matches_field_extended(
    rule_value: Any,
    context_value: Any,
    *,
    effective_date: Any = None,
    now: Any = None,
) -> bool:
    """``matches_field`` plus the string/collection/null operator families.

    Operators are partitioned by family, and the partition is the whole point:
    the three families handled here are applied inline, while *every other*
    family -- ``numeric``, ``date``, ``external``, and anything unrecognised --
    is delegated to ``matches_field``.

    The previous version computed ``legacy`` as "operators not in
    ``OPERATOR_FAMILIES``", but ``OPERATOR_FAMILIES`` contains the numeric
    operators too, so ``legacy`` was empty for exactly the rules that needed it
    and the inline loop matched none of the remaining families. Every rule
    containing only numeric operators therefore matched unconditionally --
    ``{"days_since_login": {"gte": 45}}`` fired on ``days_since_login == 0``.
    Because ``select_rules`` and ``explain_when`` both route through here, that
    made every numeric rule in ``RULE_PACKS`` fire regardless of its threshold,
    and the trace reported ``reason: "satisfied"`` for the failures.

    Delegating the non-inline families also keeps the two fail-closed
    properties intact: an unknown operator is unknown to ``matches_field`` and
    returns False, and an ``external`` operator is likewise unknown to it, so
    ``evaluate_when_external`` still sees the False it relies on to hand off to
    the enrichment stage.
    """
    if not isinstance(rule_value, dict):
        return matches_field(rule_value, context_value, effective_date=effective_date, now=now)
    inline = {"string", "collection", "null"}
    delegated = {key: value for key, value in rule_value.items() if OPERATOR_FAMILIES.get(key) not in inline}
    if delegated and not matches_field(delegated, context_value, effective_date=effective_date, now=now):
        return False
    for op_name, threshold in rule_value.items():
        family = OPERATOR_FAMILIES.get(op_name)
        if family == "string":
            if not _string_op_holds(op_name, context_value, threshold):
                return False
        elif family == "collection":
            if not _collection_op_holds(op_name, context_value, threshold):
                return False
        elif family == "null":
            if not _null_op_holds(op_name, context_value, threshold):
                return False
    return True


def evaluate_when_extended(
    when: dict[str, object],
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    now: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """``evaluate_when`` with the extended operator families enabled.

    Same ``(matched, matched_fields)`` contract, same AND-over-fields and
    same ``_date`` / ``any`` / ``all`` / ``not`` semantics. The only difference
    is that string/collection/null operators are recognised.
    """
    evaluation = _evaluation_date(effective_date=effective_date, now=now)
    matched_fields: dict[str, Any] = {}
    for field_name, rule_value in (when or {}).items():
        if field_name in _COMBINATORS and field_name not in context:
            ok, fields = _evaluate_combinator_extended(
                field_name, rule_value, context, effective_date=effective_date, now=now
            )
            if not ok:
                return False, {}
            matched_fields[field_name] = fields
            continue
        if field_name == _RESERVED_DATE_KEY:
            if evaluation is None or not matches_field_extended(
                rule_value, evaluation, effective_date=effective_date, now=now
            ):
                return False, {}
            matched_fields[field_name] = evaluation.isoformat()
            continue
        if field_name not in context:
            return False, {}
        if not matches_field_extended(
            rule_value, context[field_name], effective_date=effective_date, now=now
        ):
            return False, {}
        matched_fields[field_name] = _json_safe(context[field_name])
    return True, matched_fields


def _evaluate_combinator_extended(
    name: str,
    spec: Any,
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    now: Any = None,
) -> tuple[bool, dict[str, Any]]:
    entries = spec if isinstance(spec, list) else [spec]
    if name == "any":
        merged: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ok, fields = evaluate_when_extended(entry, context, effective_date=effective_date, now=now)
            if ok:
                merged.update(fields)
                return True, merged
        return False, {}
    if name == "all":
        merged = {}
        for entry in entries:
            if not isinstance(entry, dict):
                return False, {}
            ok, fields = evaluate_when_extended(entry, context, effective_date=effective_date, now=now)
            if not ok:
                return False, {}
            merged.update(fields)
        return True, merged
    for entry in entries:
        if isinstance(entry, dict):
            ok, _fields = evaluate_when_extended(entry, context, effective_date=effective_date, now=now)
            if ok:
                return False, {}
    return True, {}


def explain_when(
    when: dict[str, object],
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    now: Any = None,
    extended: bool = True,
) -> dict[str, Any]:
    """Why did this rule match or not? A per-field trace with reasons.

    ``evaluate_when`` returns a bool and stops at the first failure, which is
    fast but useless for a tenant asking "my rule stopped working". This walks
    every field, records the actual value beside the expected one, and names the
    first field that broke the rule. A rule with no failures reports the branch
    that *would* have decided an ``any``.
    """
    evaluator = evaluate_when_extended if extended else evaluate_when
    evaluation = _evaluation_date(effective_date=effective_date, now=now)
    trace: list[dict[str, Any]] = []
    failed_field: Optional[str] = None
    for field_name, rule_value in (when or {}).items():
        entry: dict[str, Any] = {"field": field_name, "expected": rule_value}
        if field_name in _COMBINATORS and field_name not in context:
            branch_results = []
            branches = rule_value if isinstance(rule_value, list) else [rule_value]
            for index, branch in enumerate(branches):
                if not isinstance(branch, dict):
                    branch_results.append({"index": index, "matched": False, "reason": "branch is not a rule dict"})
                    continue
                ok, _fields = evaluator(branch, context, effective_date=effective_date, now=now)
                branch_results.append({"index": index, "matched": ok, "rule": branch})
            if field_name == "not":
                hits = [item["index"] for item in branch_results if item["matched"]]
                entry.update(
                    {
                        "combinator": field_name,
                        "matched": not hits,
                        "reason": (
                            f"no branch matched ({len(branches)} checked)"
                            if not hits
                            else f"branch(es) {hits} matched, which `not` forbids"
                        ),
                    }
                )
            else:
                hits = [item["index"] for item in branch_results if item["matched"]]
                entry.update(
                    {
                        "combinator": field_name,
                        "matched": bool(hits),
                        "reason": (
                            f"branch(es) {hits} matched"
                            if hits
                            else f"no branch matched ({len(branches)} checked)"
                        ),
                        "branches": branch_results,
                    }
                )
            trace.append(entry)
            if not entry["matched"] and failed_field is None:
                failed_field = field_name
            continue
        if field_name == _RESERVED_DATE_KEY:
            actual = evaluation
            ok = actual is not None and evaluator(
                {field_name: rule_value}, {}, effective_date=effective_date, now=now
            )[0]
            entry.update(
                {
                    "actual": actual.isoformat() if actual else None,
                    "matched": ok,
                    "reason": "reserved evaluation date"
                    if ok
                    else f"evaluation date {actual} does not satisfy {rule_value}",
                }
            )
            trace.append(entry)
            if not ok and failed_field is None:
                failed_field = field_name
            continue
        if field_name not in context:
            entry.update(
                {
                    "actual": None,
                    "matched": False,
                    "reason": f"field {field_name!r} is absent from the context (unknown fields fail closed)",
                }
            )
            trace.append(entry)
            if failed_field is None:
                failed_field = field_name
            continue
        actual = context[field_name]
        ok = matches_field_extended(
            rule_value, actual, effective_date=effective_date, now=now
        )
        entry.update(
            {
                "actual": _json_safe(actual),
                "matched": ok,
                "reason": "satisfied" if ok else f"{actual!r} does not satisfy {rule_value!r}",
            }
        )
        trace.append(entry)
        if not ok and failed_field is None:
            failed_field = field_name
    matched = failed_field is None
    return {
        "matched": matched,
        "engine": "extended" if extended else "v1",
        "evaluation_date": evaluation.isoformat() if evaluation else None,
        "fields_checked": len(trace),
        "failed_field": failed_field,
        "reason": (
            "all conditions satisfied"
            if matched
            else f"first failing condition: {failed_field!r}"
        ),
        "trace": trace,
    }


def validate_when(
    when: Any,
    *,
    known_fields: Optional[set[str]] = None,
    context_hint: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Statically check a `when` DSL before it is stored or promoted.

    Catches the four failures that make a rule silently inert: an operator
    nobody implements, a reserved key used as an ordinary field, a threshold of
    the wrong shape, and a ``=expr`` that does not parse. Errors block; warnings
    are advisory (an unknown *field* is a warning unless the caller declares
    ``known_fields``, because field names are tenant-owned).
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    operators_used: list[str] = []
    fields_referenced: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            errors.append({"path": path, "code": "not_a_dict", "message": f"expected a rule dict, got {type(node).__name__}"})
            return
        for field_name, rule_value in node.items():
            here = f"{path}.{field_name}" if path else str(field_name)
            if field_name in _COMBINATORS:
                branches = rule_value if isinstance(rule_value, list) else [rule_value]
                if not branches:
                    errors.append({"path": here, "code": "empty_combinator", "message": f"{field_name!r} has no branches"})
                for index, branch in enumerate(branches):
                    _walk(branch, f"{here}[{index}]")
                continue
            fields_referenced.append(str(field_name))
            if field_name == _RESERVED_DATE_KEY and not isinstance(rule_value, dict):
                warnings.append({"path": here, "code": "date_key_scalar", "message": f"{_RESERVED_DATE_KEY!r} is a date; scalar rules should use a date operator dict"})
            if isinstance(rule_value, dict):
                if not rule_value:
                    errors.append({"path": here, "code": "empty_operator_dict", "message": "operator dict is empty, so the condition is always true"})
                for op_name, threshold in rule_value.items():
                    operators_used.append(str(op_name))
                    if op_name not in OPERATOR_FAMILIES:
                        errors.append(
                            {
                                "path": f"{here}.{op_name}",
                                "code": "unknown_operator",
                                "message": f"{op_name!r} is not implemented by any operator family",
                            }
                        )
                        continue
                    for problem in _validate_threshold(str(op_name), threshold):
                        problems = dict(problem)
                        problems["path"] = f"{here}.{op_name}"
                        errors.append(problems)
            elif isinstance(rule_value, str) and rule_value.startswith("="):
                try:
                    evaluate_expression(rule_value[1:], dict(context_hint or {}))
                except ValueError as exc:
                    errors.append({"path": here, "code": "bad_expression", "message": str(exc)})
    _walk(when, "")
    for field_name in dict.fromkeys(fields_referenced):
        if known_fields is not None and field_name not in known_fields and field_name != _RESERVED_DATE_KEY:
            warnings.append(
                {
                    "path": field_name,
                    "code": "unknown_field",
                    "message": f"{field_name!r} is not in the declared context; it will fail closed at evaluation time",
                }
            )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "operators_used": sorted(set(operators_used)),
        "families_used": sorted({OPERATOR_FAMILIES[op] for op in operators_used if op in OPERATOR_FAMILIES}),
        "fields_referenced": sorted(set(fields_referenced)),
    }


def _validate_threshold(op_name: str, threshold: Any) -> list[dict[str, str]]:
    """Shape checks per operator; a wrong-shaped threshold is a silent False.

    **Only the ordering operators get the numeric check, and that split is the
    fix for a false positive that shipped.** ``_NUMERIC_OPS`` groups ``eq``/``ne``
    with ``lt``/``lte``/``gt``/``gte`` because ``matches_field`` applies them all
    through ``operator``, so ``if op in _NUMERIC_OPS: float(threshold)`` looked
    correct. It is not: ``matches_field`` evaluates ``ne(context_value,
    threshold)`` and only a ``TypeError`` turns that into a False, so
    ``{"churn_risk": {"ne": "low"}}`` evaluates exactly as written. The
    validator rejected it as ``'ne' needs a number, got 'low'`` -- an error, so
    ``validate_when`` said ``valid: False`` about a rule that works.

    That is the worse direction for this function to be wrong in. Its job is to
    stop a tenant saving a rule that will silently never match; calling a working
    rule invalid trains people to ignore it, and a warning nobody reads is the
    same as no check. ``communication_suppression.suppress_negative_sentiment``
    carried that clause, so the one pack with a production caller held a rule its
    own validator disowned.

    Equality comparisons are left entirely alone: ``==`` and ``!=`` succeed
    against any value, so there is no wrong-shaped threshold to reject.
    Ordering against a string threshold still errors, because
    ``{"days_since_login": {"lt": "45"}}`` succeeding lexicographically is
    almost never what the author meant.
    """
    problems: list[dict[str, str]] = []
    if op_name in _ORDERING_OPS:
        try:
            float(threshold)
        except (TypeError, ValueError):
            problems.append(
                {
                    "code": "bad_threshold",
                    "message": (
                        f"{op_name!r} orders against a number, got {threshold!r}. "
                        "Equality comparisons accept any value, so use "
                        f"{{{op_name!r}: ...}} with eq/ne, or compare against a "
                        "number if this really is an ordering test."
                    ),
                }
            )
    if op_name in ("between_dates", "length_between"):
        bounds = _as_list(threshold)
        if len(bounds) != 2:
            problems.append({"code": "bad_threshold", "message": f"{op_name!r} needs exactly two bounds, got {threshold!r}"})
    if op_name in _DATE_OPS and op_name in ("between_dates", "on_date"):
        for item in (_as_list(threshold) if op_name == "between_dates" else [threshold]):
            if _coerce_date(item) is None:
                problems.append({"code": "bad_threshold", "message": f"{op_name!r} needs ISO dates, got {item!r}"})
    if op_name in ("intersects", "disjoint", "contains_all", "contains_any", "subset_of", "starts_with_any", "in_phrase"):
        if not isinstance(threshold, (list, tuple, set, frozenset)):
            problems.append({"code": "bad_threshold", "message": f"{op_name!r} needs a list, got {threshold!r}"})
    if op_name == "matches":
        if not isinstance(threshold, str) or len(threshold) > int(REGEX_POLICY["max_pattern_length"]):
            problems.append({"code": "bad_threshold", "message": f"pattern must be a string of at most {REGEX_POLICY['max_pattern_length']} characters"})
        elif not _regex_search(threshold, ""):
            try:
                re.compile(threshold)
            except re.error as exc:
                problems.append({"code": "bad_threshold", "message": f"invalid regex: {exc}"})
    if op_name in ("len_eq", "len_gte", "len_lte", "word_count_gte", "distinct_count_gte"):
        try:
            int(threshold)
        except (TypeError, ValueError):
            problems.append({"code": "bad_threshold", "message": f"{op_name!r} needs an integer, got {threshold!r}"})
    if op_name in ("sum_gte", "avg_gte", "max_lte", "min_gte"):
        try:
            float(threshold)
        except (TypeError, ValueError):
            problems.append({"code": "bad_threshold", "message": f"{op_name!r} needs a number, got {threshold!r}"})
    return problems


# --- external enrichment operators ------------------------------------------
#
# These operators are NOT evaluated by matches_field / evaluate_when (v1).
# They are reachable only through evaluate_when_external, which is called
# by the pipeline enrichment stage or by async/background rule evaluation.
# The sync request path never blocks on a provider.


def _external_op_holds(
    op_name: str,
    value: Any,
    threshold: Any,
    enriched_context: dict[str, Any] | None = None,
) -> bool:
    """Evaluate an external enrichment operator.

    ``enriched_context`` is the dict produced by the pipeline enrichment
    stage (``EnrichmentClient.enrich_event``). When it is None, every
    external operator fails closed — a rule that references an enriched
    field without the enrichment stage running is a configuration error,
    not a passing condition.
    """
    if enriched_context is None:
        return False
    if op_name == "enriched_field":
        # value = enriched field name, threshold = operator dict
        if not isinstance(threshold, dict):
            return False
        field_value = enriched_context.get(value)
        if field_value is None:
            return False
        for op, compare_val in threshold.items():
            numeric_op = _NUMERIC_OPS.get(op)
            if numeric_op is None:
                return False
            try:
                if not numeric_op(field_value, compare_val):
                    return False
            except TypeError:
                return False
        return True
    if op_name == "enriched_present":
        return value in enriched_context and enriched_context[value] is not None
    if op_name == "enriched_absent":
        return value not in enriched_context or enriched_context[value] is None
    if op_name == "webhook_received":
        # value = provider name, threshold = hours
        # Check the webhook event log for recent events from this provider
        # This is a placeholder — the actual implementation would query
        # the pipeline's event store or a dedicated webhook_events table
        return False
    if op_name == "external_api_ok":
        # value = provider name, threshold = minutes
        # Check the enrichment client's circuit breaker state
        from app.enrichment import get_enrichment_client
        client = get_enrichment_client()
        stats = client.stats()
        provider_stats = stats.get("providers", {}).get(value, {})
        return provider_stats.get("circuit_breaker") == "closed"
    return False


def evaluate_when_external(
    when: dict[str, object],
    context: dict[str, Any],
    *,
    enriched_context: dict[str, Any] | None = None,
    effective_date: Any = None,
    now: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """``evaluate_when_extended`` with external enrichment operators enabled.

    Same ``(matched, matched_fields)`` contract. The only difference from
    ``evaluate_when_extended`` is that ``EXTERNAL_OPS`` are recognised.
    External operators require ``enriched_context`` (the dict produced by
    the pipeline enrichment stage); without it they fail closed.
    """
    evaluation = _evaluation_date(effective_date=effective_date, now=now)
    matched_fields: dict[str, Any] = {}
    for field_name, rule_value in (when or {}).items():
        if field_name in _COMBINATORS and field_name not in context:
            ok, fields = _evaluate_combinator_extended(
                field_name, rule_value, context, effective_date=effective_date, now=now
            )
            if not ok:
                return False, {}
            matched_fields[field_name] = fields
            continue
        if field_name == _RESERVED_DATE_KEY:
            if evaluation is None or not matches_field_extended(
                rule_value, evaluation, effective_date=effective_date, now=now
            ):
                return False, {}
            matched_fields[field_name] = evaluation.isoformat()
            continue
        if field_name not in context:
            return False, {}
        if not matches_field_extended(
            rule_value, context[field_name], effective_date=effective_date, now=now
        ):
            # Check if this is an external operator dict
            if isinstance(rule_value, dict):
                is_external = any(
                    op in OPERATOR_FAMILIES and OPERATOR_FAMILIES[op] == "external"
                    for op in rule_value
                )
                if is_external:
                    if not _external_op_holds(
                        field_name, context.get(field_name), rule_value, enriched_context
                    ):
                        return False, {}
                    matched_fields[field_name] = _json_safe(context.get(field_name))
                    continue
            return False, {}
        matched_fields[field_name] = _json_safe(context[field_name])
    return True, matched_fields


# --- rule packs -------------------------------------------------------------


def get_rule_pack(name: str) -> dict[str, Any]:
    for entry in RULE_PACKS:
        if str(entry["pack"]) == str(name):
            return entry
    raise KeyError(f"unknown rule pack {name!r} (known: {sorted(RULE_PACK_BY_NAME)})")


def select_rules(
    pack: Any,
    context: dict[str, Any],
    *,
    effective_date: Any = None,
    now: Any = None,
    include_disabled: bool = False,
) -> dict[str, Any]:
    """Evaluate every rule in a pack and report which ones fired.

    Rules are returned in firing order (priority descending, then pack order), so
    the first entry is the one a caller should act on. Each fired rule carries
    its resolved ``params`` and an explanation, which is what makes a pack
    reviewable rather than a black box.

    **A rule whose ``when`` matches but whose ``params`` will not resolve is
    skipped, not raised.** ``params`` are resolved *after* the match, so a rule
    carrying an unevaluable ``=expr`` used to raise out of the middle of this
    function -- taking the whole selection down with it, including the rules that
    had already been decided. Every caller of ``select_rules`` in this tree sits
    inside a request or a playbook: one tenant's typo in one ``params`` value took
    out a complaint escalation and a recovery outreach at the same time.

    The failure lands in ``skipped`` with its own reason, so it is still visible
    in the payload a caller already reads, and it is distinguishable from a rule
    that did not match. Note which direction that is: the rule is *not* fired
    with partial or empty params. A rule that cannot compute its own discount
    must not quietly fire and apply nothing.
    """
    resolved = pack if isinstance(pack, dict) and "rules" in pack else get_rule_pack(pack)
    anchor = _evaluation_date(effective_date=effective_date, now=now)
    fired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, rule in enumerate(resolved.get("rules", [])):
        rule_id = str(rule.get("id", f"#{index}"))
        if not rule.get("enabled", True) and not include_disabled:
            skipped.append({"id": rule_id, "reason": "disabled"})
            continue
        if not _rule_in_window(rule, anchor):
            skipped.append({"id": rule_id, "reason": "outside effective window"})
            continue
        verdict = explain_when(
            rule.get("when", {}), context, effective_date=effective_date, now=now
        )
        if not verdict["matched"]:
            skipped.append({"id": rule_id, "reason": verdict["reason"]})
            continue
        try:
            resolved_params = resolve_params(rule.get("params"), context)
        except ValueError as exc:
            skipped.append(
                {
                    "id": rule_id,
                    "reason": f"params did not resolve: {exc}",
                    "matched": True,
                }
            )
            continue
        fired.append(
            {
                "id": rule_id,
                "priority": int(rule.get("priority", 0) or 0),
                "order": index,
                "params": resolved_params,
                "explanation": verdict,
            }
        )
    fired.sort(key=lambda item: (-item["priority"], item["order"]))
    return {
        "pack": resolved.get("pack"),
        "version": resolved.get("version"),
        "domain": resolved.get("domain"),
        "fired": fired,
        "fired_ids": [item["id"] for item in fired],
        "skipped": skipped,
        "context_fields_used": sorted(
            {str(name) for rule in resolved.get("rules", []) for name in (rule.get("when") or {})}
        ),
    }


def pack_reachability_report(app_root: Optional[Any] = None) -> dict[str, Any]:
    """Which registered packs does production code actually select?

    A pack is data, so it can be shipped complete and inert. Every validator in
    this module checks that a pack is *well formed*; nothing checked that
    anything *asks* for it, and on this tree two of the three registered packs
    were selected by no call site outside this module and its own catalog.

    That is worth reporting rather than inferring, because the failure reads as
    coverage. ``loyalty_retention`` carries two enabled rules with priorities, a
    discount expression and a description; a reviewer reading the catalog believes
    a retention nudge system is in place.

    Found by source inspection rather than by behaviour, deliberately. A
    behavioural reachability probe has to supply a context, and the context it
    supplies is its own construction -- which is how ``retention_dormant``
    survived: it reads ``days_since_login`` while the only context built for it
    anywhere carried ``days_since_last_activity``. Asking "who calls this" cannot
    be fooled that way.

    ``app_root`` is injectable so a test can point at a fixture tree; it defaults
    to the ``app`` package next to this module.
    """
    import ast as _ast
    import pathlib as _pathlib

    root = _pathlib.Path(app_root) if app_root is not None else _pathlib.Path(__file__).resolve().parent

    selected: dict[str, list[str]] = {}
    this_file = _pathlib.Path(__file__).name
    for path in sorted(root.rglob("*.py")):
        if path.name == this_file:
            continue
        try:
            tree = _ast.parse(path.read_text())
        except (OSError, SyntaxError):  # pragma: no cover - unreadable file
            continue
        for node in _ast.walk(tree):
            if not (
                isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "select_rules"
                and node.args
            ):
                continue
            first = node.args[0]
            if isinstance(first, _ast.Constant) and isinstance(first.value, str):
                selected.setdefault(str(first.value), []).append(path.name)

    registered = sorted(str(entry.get("pack")) for entry in RULE_PACKS)
    reachable = [name for name in registered if name in selected]
    unreachable = [name for name in registered if name not in selected]

    reasons: dict[str, str] = {}
    for name in unreachable:
        pack = get_rule_pack(name)
        enabled = [r for r in pack.get("rules", ()) if r.get("enabled", True)]
        # Descend into combinators, same reason as in `catalog_validation_report`:
        # a rule that reads `stage` only inside an `any` branch still needs
        # `stage`, and listing `any` as the field it needs would be nonsense.
        needed: set[str] = set()
        for rule in enabled:
            _collect_when_fields(rule.get("when") or {}, needed)
        reasons[name] = (
            f"{len(enabled)} enabled rule(s) read {sorted(needed) or ['nothing']} and no "
            "call site outside rule_engine selects this pack. Either wire it up or "
            "delete it: an authored rule nobody evaluates is indistinguishable "
            "from a safeguard that works, to everyone who reads the catalog."
        )

    return {
        "registered_packs": registered,
        "reachable_packs": reachable,
        "unreachable_packs": unreachable,
        "selected_by": {name: sorted(set(files)) for name, files in selected.items()},
        "reasons": reasons,
        "note": (
            "reachability is about call sites, not about whether a rule would "
            "match: a reachable pack can still be mis-fed, which is a separate "
            "finding and a separate check"
        ),
    }


def _collect_when_fields(node: Any, into: set[str]) -> None:
    """Every field a ``when`` clause reads, descending into the combinators.

    Shared by ``catalog_validation_report`` (to decide which names a rule's
    ``params`` expressions may bind) and ``pack_reachability_report`` (to say
    what an unreachable pack would need). Both need the deep answer, and two
    shallow copies of this collector already disagreed about the same rule --
    which is the failure mode a shared helper exists to remove.
    """
    if not isinstance(node, dict):
        return
    for name, value in node.items():
        if str(name) in _COMBINATORS:
            for branch in value if isinstance(value, list) else [value]:
                _collect_when_fields(branch, into)
            continue
        into.add(str(name))


def expression_free_names(expr: str, available: Any = ()) -> tuple[set[str], list[str]]:
    """``(names, problems)`` for a ``params`` expression, without a context.

    The free names are what the expression expects its caller to have supplied.
    Anything outside ``RULE_MATH_CONSTANTS``/``RULE_SAFE_CALLS``/``available``
    is either a typo or a name nothing will ever bind, and both make the rule
    unevaluable at selection time. ``available`` is the rule's own
    ``fields_referenced`` -- the fields its ``when`` reads, and therefore the ones
    the caller that evaluated that ``when`` must have had.

    **Checked as names rather than by evaluating, and the reason is that
    evaluation is the wrong test.** The first version of this called
    ``evaluate_expression`` with no scope and reported ``unknown symbol
    'stage'`` on the shipped ``loyalty_retention`` rule -- which is *correct
    behaviour of the evaluator* and a false positive here, because ``stage`` is a
    context field the rule's own ``when`` reads and the caller does supply. To
    make evaluation work you must invent values for those fields, and then the
    verdict depends on the values you invented: ``1 / days`` passes with 0 and
    fails with a real day count. A static name check has no such dependence.

    Node types are left to ``evaluate_expression`` -- it already refuses
    attribute access, subscripting and non-allow-listed calls, and duplicating
    that here would be a second implementation to keep in step with the first.
    """
    names: set[str] = set()
    problems: list[str] = []
    source = str(expr or "").strip()
    if not source:
        return names, ["empty expression"]
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        return names, [f"does not parse: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
    unbound = sorted(
        name
        for name in names
        if name not in _MATH_CONSTANTS and name not in _SAFE_CALLS and name not in set(available)
    )
    if unbound:
        problems.append(
            f"reads {unbound}, which is neither a rule math constant "
            f"({sorted(_MATH_CONSTANTS)}) nor a field this rule's own `when` "
            "reads, so nothing will bind it at selection time"
        )
    return names, problems


def catalog_validation_report() -> dict[str, Any]:
    """Run ``validate_when`` over every rule the product ships.

    **The gap this closes was found by accident, which is how to know it was
    real.** ``loyalty_retention.retention_high_risk`` was written with
    ``{"stage": {"in": ["engaged", "at_risk"]}}``. There is no ``in`` operator in
    this DSL. ``validate_when`` had been rejecting it as ``unknown_operator`` for
    as long as it had existed -- and rejecting nothing here, because every one of
    its callers validates a *tenant-authored* rule arriving from the admin
    surface. The built-in catalog in ``RULE_PACKS`` was the one set of rules that
    had never met the validator that exists to keep it honest.

    That combination produced the quietest failure this module can produce: a
    rule in a published catalog, with a priority and a discount expression,
    carrying a clause that fails closed for every context forever. It is also
    *unreachable*, so nothing would have surfaced it either way -- and that is
    worth naming, because the two defects hid each other. A pack nothing calls
    is invisible; a clause nothing implements inside it is also invisible; the
    pair looks exactly like correct configuration.

    So this validates shape (operators, thresholds, expressions) and separately
    reports reachability, because those are different questions and a green on
    one must not be read as a green on the other.
    """
    packs: list[dict[str, Any]] = []
    invalid_rules: list[dict[str, Any]] = []
    for entry in RULE_PACKS:
        name = str(entry.get("pack"))
        rule_reports: list[dict[str, Any]] = []
        for rule in entry.get("rules", ()):
            check = validate_when(rule.get("when") or {})
            rule_id = str(rule.get("id"))
            problems = list(check["errors"])
            # `params` are validated too, and they used not to be. Every
            # pre-existing caller of `validate_when` validates a `when`; the
            # `=expr` params were resolved at *selection* time by
            # `resolve_params`, unguarded, so an unevaluable expression shipped
            # fine and then raised from inside `select_rules` the first time its
            # rule matched. Validating them here means the catalog cannot contain
            # a rule that crashes its own caller.
            #
            # The names an expression reads are checked against *this rule's* own
            # fields, which means the field collector has to descend into the
            # `all`/`any`/`not` combinators: `retention_high_risk` reads `stage`
            # only inside an `any` branch, and a shallow collector would call
            # every expression on that rule unbound.
            available = set(check["fields_referenced"])
            _collect_when_fields(rule.get("when") or {}, available)
            for param, value in (rule.get("params") or {}).items():
                if not (isinstance(value, str) and value.startswith("=")):
                    continue
                _, complaints = expression_free_names(value[1:], available)
                for complaint in complaints:
                    problems.append(
                        {
                            "path": f"params.{param}",
                            "code": "bad_expression",
                            "message": f"{complaint} (params are resolved eagerly on a match)",
                        }
                    )
                if not complaints:
                    try:
                        evaluate_expression(value[1:], {name: 0 for name in available})
                    except ValueError as exc:
                        problems.append(
                            {
                                "path": f"params.{param}",
                                "code": "bad_expression",
                                "message": f"{exc} (params are resolved eagerly on a match)",
                            }
                        )
            rule_reports.append(
                {
                    "id": rule_id,
                    "enabled": bool(rule.get("enabled", True)),
                    "valid": not problems,
                    "errors": problems,
                    "fields_referenced": list(check["fields_referenced"]),
                    "warnings": list(check["warnings"]),
                }
            )
            if problems:
                invalid_rules.append({"pack": name, "rule_id": rule_id, "errors": problems})
        packs.append({"pack": name, "valid": all(row["valid"] for row in rule_reports), "rules": rule_reports})

    return {
        "valid": not invalid_rules,
        "packs": packs,
        "invalid_rules": invalid_rules,
        "validated": sum(len(pack["rules"]) for pack in packs),
        "note": (
            "shape only. A rule can be well formed and still never fire, because "
            "the context its caller builds does not carry the fields it reads; "
            "pair with pack_reachability_report() and neither green stands alone"
        ),
    }


def _rule_in_window(rule: dict[str, Any], anchor: Optional[date]) -> bool:
    if anchor is None:
        return True
    start = _coerce_date(rule.get("effective_from")) if rule.get("effective_from") else None
    end = _coerce_date(rule.get("effective_to")) if rule.get("effective_to") else None
    if start is not None and anchor < start:
        return False
    if end is not None and anchor > end:
        return False
    return True


def merge_rule_packs(
    packs: list[Any],
    *,
    strategy: str = "later_wins",
) -> dict[str, Any]:
    """Merge packs by rule id under a declared conflict strategy.

    Promoting rules between environments is where conditions go missing, so the
    merge is explicit about what it did: every rule it dropped or replaced is
    listed in ``conflicts`` rather than being silently overwritten.
    """
    chosen = str(strategy)
    if chosen not in PACK_MERGE_STRATEGIES:
        raise ValueError(f"unknown merge strategy {strategy!r} (expected one of {list(PACK_MERGE_STRATEGIES)})")
    resolved_packs = [pack if isinstance(pack, dict) and "rules" in pack else get_rule_pack(pack) for pack in packs]
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for pack in resolved_packs:
        for index, rule in enumerate(pack.get("rules", [])):
            rule_id = str(rule.get("id", f"#{index}"))
            if rule_id not in merged:
                merged[rule_id] = {**rule, "origin_pack": pack.get("pack")}
                order.append(rule_id)
                continue
            existing = merged[rule_id]
            if chosen == "error":
                raise ValueError(
                    f"rule id {rule_id!r} is defined in both "
                    f"{existing.get('origin_pack')!r} and {pack.get('pack')!r}; "
                    "resolve the collision or choose a different strategy"
                )
            if chosen == "first_wins":
                conflicts.append({"id": rule_id, "resolution": "kept_first", "kept_from": existing.get("origin_pack")})
                continue
            if chosen == "union":
                existing.setdefault("params", {})
                existing["params"] = {**(rule.get("params") or {}), **existing.get("params", {})}
                conflicts.append({"id": rule_id, "resolution": "union", "merged_from": [existing.get("origin_pack"), pack.get("pack")]})
                continue
            diff = diff_when(existing.get("when", {}), rule.get("when", {}))
            conflicts.append(
                {
                    "id": rule_id,
                    "resolution": "replaced",
                    "replaced_from": existing.get("origin_pack"),
                    "replaced_by": pack.get("pack"),
                    "when_diff": diff,
                }
            )
            merged[rule_id] = {**rule, "origin_pack": pack.get("pack")}
    rules = [merged[rule_id] for rule_id in order]
    return {
        "strategy": chosen,
        "packs": [pack.get("pack") for pack in resolved_packs],
        "rules": rules,
        "rule_ids": order,
        "conflicts": conflicts,
        "versions": {str(pack.get("pack")): pack.get("version") for pack in resolved_packs},
    }


def diff_when(left: Any, right: Any, *, path: str = "") -> dict[str, Any]:
    """Structural diff of two `when` dicts.

    Reports conditions added, removed, and changed at the leaf level, plus
    changed operator thresholds. A diff that only compared whole fields would
    call ``{"gte": 1}`` -> ``{"gte": 2}`` "changed" without saying which
    condition moved, which is exactly the change reviewers need to see.
    """
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    if not isinstance(left, dict) or not isinstance(right, dict):
        if left != right:
            changed.append({"path": path or "when", "from": left, "to": right})
        return {"added": added, "removed": removed, "changed": changed, "identical": not (added or removed or changed)}
    for key in right:
        if key not in left:
            added.append({"path": f"{path}.{key}" if path else str(key), "value": right[key]})
        else:
            here = f"{path}.{key}" if path else str(key)
            if isinstance(left[key], dict) and isinstance(right[key], dict):
                nested = diff_when(left[key], right[key], path=here)
                added.extend(nested["added"])
                removed.extend(nested["removed"])
                changed.extend(nested["changed"])
            elif left[key] != right[key]:
                changed.append({"path": here, "from": left[key], "to": right[key]})
    for key in left:
        if key not in right:
            removed.append({"path": f"{path}.{key}" if path else str(key), "value": left[key]})
    return {"added": added, "removed": removed, "changed": changed, "identical": not (added or removed or changed)}


def export_rule_pack(pack: Any, *, fmt: str = "json", include_explanations: bool = False) -> str:
    """Serialise a pack for review, diffing, or a Git-tracked rules file."""
    out_fmt = str(fmt)
    if out_fmt not in RULE_PACK_EXPORT_FORMATS:
        raise ValueError(f"unknown export format {fmt!r} (expected one of {list(RULE_PACK_EXPORT_FORMATS)})")
    resolved = pack if isinstance(pack, dict) and "rules" in pack else get_rule_pack(pack)
    if out_fmt == "json":
        return json.dumps(resolved, indent=2, sort_keys=True, default=str)
    if out_fmt == "jsonl":
        lines = [json.dumps({"pack": resolved.get("pack"), "version": resolved.get("version"), "domain": resolved.get("domain")}, sort_keys=True)]
        lines += [json.dumps(rule, sort_keys=True, default=str) for rule in resolved.get("rules", [])]
        return "\n".join(lines) + "\n"
    if out_fmt == "python":
        body = ",\n".join(f"        {json.dumps(rule, sort_keys=True, default=str)}" for rule in resolved.get("rules", []))
        return (
            "RULE_PACKS.append(\n"
            "    {\n"
            f"        \"pack\": {json.dumps(resolved.get('pack'))},\n"
            f"        \"version\": {json.dumps(resolved.get('version'))},\n"
            f"        \"domain\": {json.dumps(resolved.get('domain'))},\n"
            f"        \"priority\": {json.dumps(resolved.get('priority'))},\n"
            "        \"rules\": [\n"
            f"{body}\n"
            "        ],\n"
            "    }\n"
            ")\n"
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["id", "enabled", "priority", "effective_from", "effective_to", "when", "params"])
    for rule in resolved.get("rules", []):
        writer.writerow(
            [
                rule.get("id", ""),
                "true" if rule.get("enabled", True) else "false",
                int(rule.get("priority", 0) or 0),
                rule.get("effective_from") or "",
                rule.get("effective_to") or "",
                json.dumps(rule.get("when", {}), sort_keys=True, default=str),
                json.dumps(rule.get("params", {}), sort_keys=True, default=str),
            ]
        )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def build_rule_engine_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog`."""
    return {
        "catalog_version": "rule_engine_v1",
        "combine_operators": list(_COMBINATORS),
        "field_operators": sorted(_NUMERIC_OPS),
        "date_window_operators": dict(_DATE_OPS),
        "reserved_date_key": _RESERVED_DATE_KEY,
        "evaluation": "effective_date if provided, else current UTC date",
        "expression_params": {
            "prefix": "=",
            "arity": "arithmetic + comparisons + and/or + ternary + allow-listed calls",
            "safe_calls": sorted(_SAFE_CALLS),
            "safe_symbols": ["context fields", "pi", "e"],
            "note": "Arbitrary code (imports, attributes, subscripts) is rejected at parse time.",
        },
        "consumers": [
            "loyalty_journey",
            "communication_strategy",
            "arrears_payments",
            "points_exchange",
            "regional_policy",
        ],
        "examples": [
            {"any": [{"stage": "new"}, {"stage": "engaged"}]},
            {"_date": {"between_dates": ["2026-06-01", "2026-08-31"]}, "value_tier": {"ne": "new"}},
            {"not": {"churn_risk": "high"}},
            {"param": "=base * tier_multiplier * campaign_multiplier"},
        ],
        "extended_operators": {
            "string": [dict(entry) for entry in STRING_OPS],
            "collection": [dict(entry) for entry in COLLECTION_OPS],
            "null": [dict(entry) for entry in NULL_OPS],
            "families": {name: family for name, family in sorted(OPERATOR_FAMILIES.items())},
            "opt_in": True,
            "policy": (
                "matches_field/evaluate_when do NOT implement these operators and still fail "
                "closed on them; use evaluate_when_extended to enable them. An operator that no "
                "family declares is never silently true."
            ),
            "entry_points": [
                "matches_field_extended",
                "evaluate_when_extended",
                "explain_when",
            ],
        },
        "regex_policy": dict(REGEX_POLICY),
        "explanation": {
            "helper": "explain_when(when, context, effective_date=None, now=None, extended=True)",
            "returns": "per-field trace with actual vs expected, the first failing field, and combinator branch results",
            "why": "evaluate_when stops at the first failure, which cannot answer 'why did my rule stop firing'",
        },
        "validation": {
            "helper": "validate_when(when, known_fields=None, context_hint=None)",
            "checks": [
                "unknown operator (error)",
                "empty operator dict, i.e. an always-true condition (error)",
                "wrong-shaped threshold (error)",
                "unparseable =expr (error)",
                "unknown context field (warning unless known_fields is declared)",
                "scalar _date (warning)",
            ],
            "blocking": "errors only; warnings are advisory",
        },
        "rule_packs": {
            "table": [
                {
                    "pack": entry["pack"],
                    "version": entry["version"],
                    "domain": entry["domain"],
                    "priority": entry["priority"],
                    "rules": [str(rule.get("id")) for rule in entry.get("rules", [])],
                    "description": entry.get("description", ""),
                }
                for entry in RULE_PACKS
            ],
            "merge_strategies": list(PACK_MERGE_STRATEGIES),
            "export_formats": list(RULE_PACK_EXPORT_FORMATS),
            "policy": "merge_rule_packs reports every dropped or replaced rule in `conflicts`",
            "helpers": [
                "get_rule_pack",
                "select_rules",
                "merge_rule_packs",
                "diff_when",
                "export_rule_pack",
            ],
        },
        "advanced_examples": [
            {"note": "string family", "when": {"email": {"ends_with": "@blocked.example"}}},
            {"note": "collection family", "when": {"tags": {"contains_all": ["vip", "beta"]}}},
            {"note": "null family", "when": {"consent_at": {"is_null": True}}},
            {"note": "extended evaluator required", "when": {"days_since_login": {"gte": 45}, "tags": {"len_gte": 3}}},
        ],
    }