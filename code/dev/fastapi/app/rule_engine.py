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
"""

from __future__ import annotations

import ast
import math
import operator
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
    """
    source = str(expr or "").strip()
    if not source:
        raise ValueError("empty rule expression")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid rule expression {source!r}: {exc}") from exc
    return _eval_node(tree, dict(scope or {}))


def _eval_node(node: Any, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, scope)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or isinstance(node.value, (int, float)):
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
    }