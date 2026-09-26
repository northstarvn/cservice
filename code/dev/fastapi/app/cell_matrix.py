"""Fine-grained data-cell access matrices (RBAC + ABAC).

Beyond role-level permissions, some data is sensitive *per cell*: an agent may
read a customer's name and email but not their phone or biometric status; an
auditor sees risk scores but not payment instruments. The original module
encoded that as a static two-level config table. That answers "may this role
touch this field?" but not the questions a real program actually asks, which
are all conditional:

- *When* may it? A break-glass grant that expires at 03:00, or a write window
  outside business hours.
- *Under which context*? Only for your own tenant, only for records flagged
  ``sensitive=false``, only when the actor's control posture is trusted.
- *What do you get back*? A masked value (``****1234``) is not the same as
  nothing, and "read" and "full read" are different permissions.
- *Why* should the actor have to say? Some cells are legitimate but auditable,
  so a grant can require a justification ticket.
- *What would change*? Before flipping a policy, show the blast radius.

The two-level matrix stays exactly as it was — it is the base layer and every
historical answer is unchanged. Everything above it is additive:

Layers, lowest precedence first:

1. ``CELL_MATRIX`` — the original ``resource -> cell -> {role: access}`` table.
2. ``CELL_OVERRIDES`` — per-cell/role refinements: ``deny`` (deny-overrides
   everything), ``mask`` (degrade a read to a redacted one), extra conditions,
   and a justification requirement.
3. ``ROW_SCOPES`` — row-level predicates limiting *which* records a role sees
   within a readable resource.
4. ``TEMPORARY_GRANTS`` — time-boxed escalations, the break-glass path.

Public surfaces:

- ``cell_access`` / ``can_read_cell`` / ``can_write_cell`` / ``evaluate_cell_matrix``
  / ``resolve_roles`` — unchanged base answers.
- ``evaluate_access`` — the full ``CellDecision``: level, mask, justification,
  conditions, row scope, and the winning layer.
- ``plan_resource_access`` — classify every cell of a resource as
  ``visible`` / ``masked`` / ``hidden``.
- ``compare_role_sets`` — the blast radius of a role change.
- ``simulate_overrides`` — dry-run an override set without mutating config.
- ``grant_temporary_access`` — mint a scoped, expiring elevation.
- ``build_cell_matrix_catalog`` — introspection for tooling.

The tables are config: tightening or loosening a cell is a data change, not a
code change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
import threading
import uuid

# Base access levels, ordered least- to most-privileged.
ACCESS_LEVELS = ("none", "read", "write")
ACCESS_RANK = {"none": 0, "read": 1, "write": 2}
# Masked reads are a *degraded* read: the value is returned redacted, not
# withheld. They sit above ``none`` and below a full ``read``.
MASK_LEVEL = "masked"
MASKED_RANK = 0.5

# Group -> implied raw roles (lowest common denominator for cell lookup).
ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "customer": ("owner",),
    "agent": ("agent",),
    "auditor": ("auditor",),
    "admin": ("admin", "agent", "auditor", "owner"),
}

# Config table: resource -> cell -> {role: access}. Roles absent from a cell
# default to "none". Adding a cell/role grant is config-only.
CELL_MATRIX: dict[str, dict[str, dict[str, str]]] = {
    "customer_profile": {
        "full_name": {"owner": "write", "agent": "read", "admin": "write", "auditor": "read"},
        "email": {"owner": "write", "agent": "read", "admin": "write", "auditor": "read"},
        "phone": {"owner": "write", "agent": "read", "admin": "write", "auditor": "none"},
        "preferred_language": {"owner": "write", "agent": "read", "admin": "write", "auditor": "read"},
        "risk_score": {"owner": "read", "agent": "none", "admin": "read", "auditor": "read"},
        "biometric_status": {"owner": "read", "agent": "none", "admin": "read", "auditor": "none"},
        "government_id": {"owner": "write", "agent": "none", "admin": "write", "auditor": "read"},
    },
    "booking": {
        "id": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
        "service_type": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
        "scheduled_date": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
        "details": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
        "internal_notes": {"owner": "none", "agent": "write", "admin": "write", "auditor": "none"},
        "assignment_history": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
    },
    "payments": {
        "amount": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
        "instrument_last4": {"owner": "read", "agent": "read", "admin": "write", "auditor": "none"},
        "full_instrument": {"owner": "read", "agent": "none", "admin": "write", "auditor": "none"},
        "arrears_terms": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
        "refund_eligibility": {"owner": "read", "agent": "read", "admin": "write", "auditor": "read"},
    },
    "audit_trail": {
        "actor": {"owner": "none", "agent": "none", "admin": "read", "auditor": "read"},
        "action": {"owner": "none", "agent": "none", "admin": "read", "auditor": "read"},
        "detail_json": {"owner": "none", "agent": "none", "admin": "read", "auditor": "read"},
        "source": {"owner": "none", "agent": "none", "admin": "read", "auditor": "read"},
    },
}

# --- Layer 2: conditional overrides -------------------------------------------
#
# ``resource -> cell -> role -> override``. Recognised keys:
#
# - ``deny``            (bool)  hard deny; wins over every other layer.
# - ``mask``            (bool)  degrade a read to a redacted one.
# - ``requires_justification`` (bool) access is allowed but must be logged
#   with a reason; surfaced on the decision, never enforced here.
# - ``conditions``      (dict)  all keys must match the request context.
# - ``expires_at``      (str)   ISO-8601; the override stops applying after it.
#
# Every entry below is deliberately *narrowing* (deny, mask, conditions) — an
# override can remove access but never invent it, so a typo in this table can
# only ever fail closed.
CELL_OVERRIDES: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
    "customer_profile": {
        # Auditors work from email domains, not full addresses, and only in
        # working hours: a masked read is enough and leaks far less.
        "email": {
            "auditor": {
                "mask": True,
                "requires_justification": True,
                "conditions": {"business_hours": True},
            }
        },
        # Minimise PII in the audit function: auditors no longer see gov IDs.
        "government_id": {"auditor": {"deny": True}},
        # Admins may read biometric status, but the read must be attributable.
        "biometric_status": {
            "admin": {"requires_justification": True},
        },
    },
    "payments": {
        # Dual control on raw payment instruments: admin write requires a
        # step-up (a fresh re-auth), and is always justified.
        "full_instrument": {
            "admin": {"requires_justification": True, "conditions": {"step_up": True}},
        },
    },
}

# --- Layer 3: row-level scopes ------------------------------------------------
#
# ``resource -> role -> {field: allowed_values}``. All listed fields must
# satisfy the predicate for a row to be in scope; a row failing any predicate
# is out of scope even though the *cell* is readable. ``"*"`` as a value means
# "any non-null" (i.e. the row must exist and be owned by someone).
ROW_SCOPES: dict[str, dict[str, dict[str, Any]]] = {
    "booking": {
        "owner": {"customer_user_id": "*"},
        "auditor": {"archived": False},
    },
    "payments": {
        "owner": {"customer_user_id": "*"},
    },
    "audit_trail": {
        "auditor": {"severity": ["info", "warning", "critical"]},
    },
}

# --- Context keys that condition overrides can test ----------------------------
#
# Kept explicit so an unknown key is a typo an operator can spot in the
# catalog rather than a silently-ignored rule.
KNOWN_CONTEXT_FIELDS = (
    "business_hours",
    "step_up",
    "same_tenant",
    "record_sensitive",
    "control_posture",
    "ticket",
    "environment",
)

# Postures that satisfy a "trusted actor" condition out of the box.
TRUSTED_POSTURES = ("high_trust", "customer_trusted")


# --- Role resolution ------------------------------------------------------------


def resolve_roles(roles: list[str] | tuple[str, ...] | str) -> set[str]:
    """Expand role groups into the raw roles they imply."""
    if isinstance(roles, str):
        roles = (roles,)
    resolved: set[str] = set()
    for role in roles:
        resolved.add(role)
        resolved.update(ROLE_GROUPS.get(role, ()))
    return resolved or {"none"}


# --- Base layer queries (unchanged contracts) ----------------------------------


def _base_access(resource: str, cell: str, roles: Iterable[str]) -> tuple[int, str]:
    """Highest-ranked base grant among ``roles`` -> ``(rank, level)``."""
    grants = CELL_MATRIX.get(resource, {}).get(cell, {})
    rank = max((ACCESS_RANK.get(grants.get(role, "none"), 0) for role in roles), default=0)
    return rank, ACCESS_LEVELS[rank]


def cell_access(resource: str, cell: str, roles: list[str] | tuple[str, ...] | str) -> str:
    """Effective access level (none/read/write) for a single data cell.

    This is the original two-level answer and deliberately ignores the
    override layer. Use :func:`evaluate_access` when context matters.
    """
    _, level = _base_access(resource, cell, resolve_roles(roles))
    return level


def can_read_cell(resource: str, cell: str, roles: list[str] | tuple[str, ...] | str) -> bool:
    return ACCESS_RANK[cell_access(resource, cell, roles)] >= ACCESS_RANK["read"]


def can_write_cell(resource: str, cell: str, roles: list[str] | tuple[str, ...] | str) -> bool:
    return ACCESS_RANK[cell_access(resource, cell, roles)] >= ACCESS_RANK["write"]


def evaluate_cell_matrix(
    resource: str,
    roles: list[str] | tuple[str, ...] | str,
) -> dict[str, str]:
    """Per-cell access map for a resource (every defined cell)."""
    matrix = CELL_MATRIX.get(resource, {})
    return {cell: cell_access(resource, cell, roles) for cell in sorted(matrix)}


# --- Layer 2/3/4 evaluation ---------------------------------------------------


def _parse_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _match_conditions(conditions: dict[str, Any], context: dict[str, Any]) -> tuple[bool, list[str]]:
    """Every condition must hold. Returns ``(matched, unmet_keys)``."""
    unmet: list[str] = []
    for key, expected in conditions.items():
        actual = context.get(key)
        if key == "control_posture" and expected is True:
            if actual not in TRUSTED_POSTURES:
                unmet.append(key)
            continue
        if key == "environment" and isinstance(expected, (list, tuple)):
            if actual not in expected:
                unmet.append(key)
            continue
        if actual != expected:
            unmet.append(key)
    return (not unmet), unmet


def _scope_matches(scope: dict[str, Any], record: dict[str, Any] | None) -> bool:
    """Row-level predicate: every scoped field must satisfy its predicate."""
    if not scope:
        return True
    if record is None:
        return False
    for field, expected in scope.items():
        if field not in record:
            return False
        actual = record[field]
        if expected == "*":
            if actual in (None, ""):
                return False
            continue
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
            continue
        if actual != expected:
            return False
    return True


def _mask_value(value: Any, cell: str) -> Any:
    """Redact a value for a masked read.

    Strings keep only the last 4 characters, digits keep the last 4, everything
    else is replaced wholesale — a masked read should never leak more than the
    minimum needed to correlate a record.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return f"***{value[-4:]}" if len(value) > 4 else "***"
    if isinstance(value, int):
        return int(str(value)[-4:]) if len(str(value)) > 4 else 0
    if isinstance(value, (list, tuple, dict, set)):
        return None
    return "***"


def mask_payload(resource: str, roles: Any, payload: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Redact every cell of ``payload`` the caller may not fully read.

    The counterpart to :func:`evaluate_access`: given a decision per cell, apply
    it. Cells with no read access are dropped, masked reads are redacted, and
    everything else passes through untouched.
    """
    ctx = context or {}
    result: dict[str, Any] = {}
    for cell, value in payload.items():
        decision = evaluate_access(resource, cell, roles, context=ctx)
        level = decision["level"]
        if level == "none":
            continue  # hidden: omit from the response entirely
        if level == MASK_LEVEL:
            result[cell] = _mask_value(value, cell)
        else:
            result[cell] = value
    return result


# --- Temporary / break-glass grants --------------------------------------------


class TemporaryGrantStore:
    """Thread-safe registry of scoped, expiring access elevations.

    A break-glass grant is the escape hatch for "I legitimately need this
    field, right now": it is explicit, time-boxed, attributed, and auditable.
    Each grant names the roles it applies to, the exact cell it unlocks, and
    when it dies.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._grants: dict[str, dict[str, Any]] = {}

    def grant(
        self,
        *,
        subject: str,
        roles: Iterable[str],
        resource: str,
        cell: str,
        level: str = "read",
        ttl_minutes: int = 30,
        justification: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if level not in ACCESS_LEVELS:
            raise ValueError(f"temporary grant level must be one of {', '.join(ACCESS_LEVELS)}")
        moment = now or datetime.now(timezone.utc)
        grant_id = uuid.uuid4().hex[:16]
        record = {
            "grant_id": grant_id,
            "subject": subject,
            "roles": sorted({str(r) for r in roles}),
            "resource": resource,
            "cell": cell,
            "level": level,
            "justification": justification,
            "granted_at": moment.isoformat(),
            "expires_at": (moment + timedelta(minutes=max(ttl_minutes, 1))).isoformat(),
            "uses": 0,
        }
        with self._lock:
            self._grants[grant_id] = record
        return dict(record)

    def active_grants(
        self, *, subject: str | None = None, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Unexpired grants, optionally narrowed to one subject."""
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            rows = [
                dict(record)
                for record in self._grants.values()
                if _parse_moment(record["expires_at"]) > moment
                and (subject is None or record["subject"] == subject)
            ]
        return sorted(rows, key=lambda r: r["expires_at"])

    def lookup(
        self, resource: str, cell: str, roles: Iterable[str], now: datetime | None = None
    ) -> Optional[dict[str, Any]]:
        """Best active grant covering ``(resource, cell)`` for ``roles``."""
        resolved = resolve_roles(roles)
        best: Optional[dict[str, Any]] = None
        for record in self.active_grants(now=now):
            if record["resource"] != resource or record["cell"] != cell:
                continue
            if not (set(record["roles"]) & resolved):
                continue
            if best is None or ACCESS_RANK[record["level"]] > ACCESS_RANK[best["level"]]:
                best = record
        return best

    def consume(self, grant_id: str) -> bool:
        """Retire a grant early (operator revoked it)."""
        with self._lock:
            return self._grants.pop(grant_id, None) is not None

    def prune(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            stale = [gid for gid, r in self._grants.items() if _parse_moment(r["expires_at"]) <= moment]
            for gid in stale:
                self._grants.pop(gid)
            return len(stale)

    def reset(self) -> None:
        with self._lock:
            self._grants.clear()


TEMPORARY_GRANTS = TemporaryGrantStore()


# --- The full decision ----------------------------------------------------------


def evaluate_access(
    resource: str,
    cell: str,
    roles: list[str] | tuple[str, ...] | str,
    *,
    context: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
    now: datetime | None = None,
    use_temporary_grants: bool = True,
    overrides: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Resolve one cell to a full access decision.

    Returns a dict with ``level`` (one of ``none``/``masked``/``read``/
    ``write``), the winning ``source`` layer, whether a ``justification`` is
    required, the ``row_scope`` in force, the ``grant_id`` that applied, and a
    human-readable ``reason``.

    Precedence, highest first:

    1. ``deny`` overrides and row-scope misses -> ``none``
    2. active temporary grant (break-glass)
    3. base matrix grant, then ``mask`` / condition refinement

    ``overrides`` substitutes a candidate table for :data:`CELL_OVERRIDES` for
    this call only, which is what makes :func:`simulate_overrides` safe to run
    concurrently against live traffic.
    """
    ctx = dict(context or {})
    resolved = resolve_roles(roles)
    moment = now or datetime.now(timezone.utc)
    override_table = CELL_OVERRIDES if overrides is None else overrides

    def _decision(level: str, source: str, reason: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "resource": resource,
            "cell": cell,
            "roles": sorted(resolved),
            "level": level,
            "base_level": _base_access(resource, cell, resolved)[1],
            "source": source,
            "reason": reason,
            "requires_justification": False,
            "grant_id": None,
            "row_scope": ROW_SCOPES.get(resource, {}),
        }
        payload.update(extra)
        return payload

    # --- 1. hard deny + row scope (both can only ever reduce access) ---
    overrides = override_table.get(resource, {}).get(cell, {})
    for role in sorted(resolved):
        if overrides.get(role, {}).get("deny"):
            return _decision("none", "override_deny", f"explicit deny for role {role}")

    scopes = {
        role: ROW_SCOPES.get(resource, {}).get(role)
        for role in resolved
        if ROW_SCOPES.get(resource, {}).get(role)
    }
    # Row scope is only decidable against an actual row. With no record
    # supplied (a schema-level "may this role touch this field at all?"
    # question) it must not silently deny — the cell-level answer stands.
    if scopes and record is not None:
        in_scope = any(_scope_matches(scope, record) for scope in scopes.values())
        if not in_scope:
            return _decision(
                "none",
                "row_scope",
                "record is outside this role's row scope",
                row_scope=scopes,
            )

    # --- 2. break-glass elevation ---
    if use_temporary_grants:
        grant = TEMPORARY_GRANTS.lookup(resource, cell, roles, now=moment)
        if grant is not None:
            return _decision(
                grant["level"],
                "temporary_grant",
                f"break-glass grant {grant['grant_id']}",
                requires_justification=bool(grant.get("justification")),
                grant_id=grant["grant_id"],
            )

    base_rank, base_level = _base_access(resource, cell, resolved)
    if base_rank == 0:
        return _decision("none", "base_matrix", "no grant in the base matrix")

    # --- 3. conditions + mask refinement ---
    level = base_level
    source = "base_matrix"
    reason = "granted by the base matrix"
    justification = False
    unmet: list[str] = []

    for role in sorted(resolved):
        override = overrides.get(role)
        if not override:
            continue
        justification = justification or bool(override.get("requires_justification"))

        if override.get("expires_at"):
            expiry = _parse_moment(override["expires_at"])
            if expiry is not None and expiry <= moment:
                continue

        conditions = override.get("conditions") or {}
        if conditions:
            matched, role_unmet = _match_conditions(conditions, ctx)
            if not matched:
                unmet.extend(role_unmet)
                continue
            source = "override_conditions"
            reason = "conditions satisfied"

        if override.get("mask") and base_rank == ACCESS_RANK["read"] and level == "read":
            level = MASK_LEVEL
            source = "override_mask"
            reason = "read downgraded to a masked value"

    if unmet:
        # Every grant that mattered was conditional and none of them matched.
        return _decision(
            "none" if level == MASK_LEVEL else "none",
            "conditions_unmet",
            f"unmet conditions: {', '.join(sorted(set(unmet)))}",
            requires_justification=justification,
            unmet_conditions=sorted(set(unmet)),
        )

    return _decision(level, source, reason, requires_justification=justification)


def plan_resource_access(
    resource: str,
    roles: list[str] | tuple[str, ...] | str,
    *,
    context: dict[str, Any] | None = None,
    overrides: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Classify every cell of a resource for a principal.

    ``visible`` cells can be read or written, ``masked`` cells come back
    redacted, ``hidden`` cells are omitted from the response entirely. This is
    the shape a serializer or a Pydantic response model should be driven by.
    """
    cells = sorted(CELL_MATRIX.get(resource, {}))
    classification: dict[str, str] = {}
    detail: dict[str, dict[str, Any]] = {}
    for cell in cells:
        decision = evaluate_access(
            resource, cell, roles, context=context, overrides=overrides
        )
        level = decision["level"]
        classification[cell] = {
            "none": "hidden",
            MASK_LEVEL: "masked",
        }.get(level, "visible")
        detail[cell] = decision
    return {
        "resource": resource,
        "roles": sorted(resolve_roles(roles)),
        "cells": classification,
        "visible": sorted(c for c, k in classification.items() if k == "visible"),
        "masked": sorted(c for c, k in classification.items() if k == "masked"),
        "hidden": sorted(c for c, k in classification.items() if k == "hidden"),
        "decisions": detail,
    }


def compare_role_sets(
    resource: str,
    base_roles: list[str] | tuple[str, ...] | str,
    candidate_roles: list[str] | tuple[str, ...] | str,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Blast radius of moving from ``base_roles`` to ``candidate_roles``.

    Answers "if I grant this role, what does it start/stop being able to see?"
    without touching any configuration.
    """
    before = plan_resource_access(resource, base_roles, context=context)
    after = plan_resource_access(resource, candidate_roles, context=context)
    gained: list[str] = []
    lost: list[str] = []
    changed: list[str] = []
    for cell, base_state in before["cells"].items():
        cand_state = after["cells"].get(cell, "hidden")
        if base_state == "hidden" and cand_state != "hidden":
            gained.append(cell)
        elif base_state != "hidden" and cand_state == "hidden":
            lost.append(cell)
        elif base_state != cand_state:
            changed.append(cell)
    return {
        "resource": resource,
        "base_roles": sorted(resolve_roles(base_roles)),
        "candidate_roles": sorted(resolve_roles(candidate_roles)),
        "gained": sorted(gained),
        "lost": sorted(lost),
        "changed": sorted(changed),
        "net": len(gained) - len(lost),
        "before": before["cells"],
        "after": after["cells"],
    }


def simulate_overrides(
    resource: str,
    cell: str,
    roles: list[str] | tuple[str, ...] | str,
    overrides: dict[str, dict[str, dict[str, Any]]],
    *,
    context: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dry-run a *hypothetical* override table against one cell.

    The candidate table is passed through, never installed, so an operator can
    answer "if I add this deny, who loses what?" with zero risk of mutating
    the running policy and no interference with concurrent requests.
    """
    return evaluate_access(
        resource,
        cell,
        roles,
        context=context,
        record=record,
        use_temporary_grants=False,
        overrides=overrides,
    )


def set_cell_override(
    resource: str, cell: str, role: str, override: dict[str, Any] | None
) -> dict[str, Any]:
    """Install (or clear) one override. Config-only policy change at runtime."""
    if override is None:
        CELL_OVERRIDES.get(resource, {}).get(cell, {}).pop(role, None)
        return {"resource": resource, "cell": cell, "role": role, "cleared": True}
    CELL_OVERRIDES.setdefault(resource, {}).setdefault(cell, {})[role] = dict(override)
    return {"resource": resource, "cell": cell, "role": role, "cleared": False, "override": dict(override)}


def set_row_scope(
    resource: str, role: str, scope: dict[str, Any] | None
) -> dict[str, Any]:
    """Install (or clear) a row-level scope for a role."""
    if scope is None:
        ROW_SCOPES.get(resource, {}).pop(role, None)
        return {"resource": resource, "role": role, "cleared": True}
    ROW_SCOPES.setdefault(resource, {})[role] = dict(scope)
    return {"resource": resource, "role": role, "cleared": False, "scope": dict(scope)}


def build_cell_matrix_catalog() -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for resource, cells in sorted(CELL_MATRIX.items()):
        summary[resource] = {
            cell: dict(grants) for cell, grants in sorted(cells.items())
        }
    return {
        "access_levels": list(ACCESS_LEVELS),
        "masked_level": MASK_LEVEL,
        "role_groups": {group: list(roles) for group, roles in ROLE_GROUPS.items()},
        "resources": sorted(CELL_MATRIX),
        "cells": summary,
        "overrides": {
            resource: {
                cell: {role: dict(spec) for role, spec in sorted(roles.items())}
                for cell, roles in sorted(cells.items())
            }
            for resource, cells in sorted(CELL_OVERRIDES.items())
        },
        "row_scopes": {
            resource: {role: dict(scope) for role, scope in sorted(roles.items())}
            for resource, roles in sorted(ROW_SCOPES.items())
        },
        "known_context_fields": list(KNOWN_CONTEXT_FIELDS),
        "trusted_postures": list(TRUSTED_POSTURES),
        "temporary_grants": {
            "active": len(TEMPORARY_GRANTS.active_grants()),
            "mechanism": "scoped, time-boxed, subject-attributed escalation",
        },
        "precedence": [
            "row_scope / override_deny (reduce only)",
            "temporary_grant",
            "base_matrix",
            "override_conditions",
            "override_mask",
        ],
    }
