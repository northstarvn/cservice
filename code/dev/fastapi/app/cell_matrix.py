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

Expansion notes (layer 5 — obligations, and the second matrix):

The layers above answer *may this actor touch this cell, right now*. A program
that has to defend an access decision needs three more things, all of which are
policy questions rather than permission questions:

- *May they do it for this reason?* ``CELL_PURPOSES`` / ``CELL_PURPOSE_TAGS``
  bind a cell to the purposes it may be read for. A support agent with a
  legitimate ``read`` grant still cannot read a government id without a
  ``fraud_investigation`` purpose. This is layer 5 and it is narrowing-only.
- *How often?* ``CELL_BUDGETS`` caps reads of a sensitive cell per role per
  window, so a compromised credential cannot exfiltrate a whole column.
- *Is the masking still right?* ``MASKING_PROFILES`` makes redaction a named
  strategy per cell rather than one hardcoded function.
- *Which cells does nobody look after?* ``CELL_SENSITIVITY`` classifies cells
  so a review can ask "which sensitive cells have no owner and no purpose tag".
- ``review_access_programs`` — the standing-question report across all of it.
- ``build_cell_matrix_policy`` — the layer-5 catalog, kept separate from
  ``build_cell_matrix_catalog`` whose key set is a pinned contract.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
import hashlib
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

# --- Layer 5: purpose limitation ------------------------------------------------
#
# ``read`` answers "may this role see this field". It does not answer "may they
# see it *for this*". A support agent who may read ``government_id`` still may
# not read it while servicing a billing question, and the difference is only
# expressible if the request declares a purpose.
#
# Config table: purpose name -> {description, requires_justification}.
CELL_PURPOSES: dict[str, dict[str, Any]] = {
    "service_delivery": {
        "description": "performing the booking the customer asked for",
        "requires_justification": False,
    },
    "billing": {
        "description": "invoicing, payment collection, arrears handling",
        "requires_justification": False,
    },
    "support": {
        "description": "answering a customer support request",
        "requires_justification": False,
    },
    "fraud_investigation": {
        "description": "investigating a fraud or security signal",
        "requires_justification": True,
    },
    "statutory_audit": {
        "description": "meeting a regulatory or internal audit obligation",
        "requires_justification": True,
    },
    "incident_response": {
        "description": "responding to a live security incident",
        "requires_justification": True,
    },
}

# Config table: resource -> cell -> allowed purposes. A missing cell entry means
# "any purpose" (the pre-layer-5 behaviour). ``"*"`` inside a list means the same
# thing, for a cell that otherwise enumerates.
CELL_PURPOSE_TAGS: dict[str, dict[str, list[str]]] = {
    "customer_profile": {
        "government_id": ["statutory_audit", "fraud_investigation"],
        "biometric_status": ["incident_response", "fraud_investigation", "statutory_audit"],
        "risk_score": ["support", "fraud_investigation", "statutory_audit"],
    },
    "payments": {
        "full_instrument": ["billing", "fraud_investigation", "incident_response"],
        "arrears_terms": ["billing", "support"],
    },
    "booking": {
        "internal_notes": ["support", "fraud_investigation"],
    },
}

# Request-context key that carries the declared purpose.
PURPOSE_CONTEXT_FIELD = "purpose"
# The reason codes purpose evaluation can return, so a caller branches on a
# closed set rather than on a string it re-derives.
PURPOSE_REASONS = ("ok", "purpose_not_declared", "purpose_unknown", "purpose_not_permitted")

# --- Layer 5: read budgets -----------------------------------------------------
#
# An RBAC grant answers "may", never "how often". A single stolen credential with
# a legitimate grant can read a whole column before anyone notices. Budgets cap
# it: a role may read a sensitive cell at most N times per window.
#
# Config table: each row is {roles, resource, cell, max_reads, window_seconds,
# enabled}. ``cell: "*"`` budgets the whole resource.
CELL_BUDGETS: list[dict[str, Any]] = [
    {
        "budget_id": "agent_government_id",
        "roles": ["agent"],
        "resource": "customer_profile",
        "cell": "government_id",
        "max_reads": 20,
        "window_seconds": 3600,
        "enabled": True,
    },
    {
        "budget_id": "agent_full_instrument",
        "roles": ["agent"],
        "resource": "payments",
        "cell": "full_instrument",
        "max_reads": 10,
        "window_seconds": 3600,
        "enabled": True,
    },
    {
        "budget_id": "auditor_audit_trail",
        "roles": ["auditor"],
        "resource": "audit_trail",
        "cell": "*",
        "max_reads": 500,
        "window_seconds": 3600,
        "enabled": True,
    },
]

BUDGET_REASONS = ("ok", "budget_exhausted")

# --- Layer 5: masking profiles -------------------------------------------------
#
# ``_mask_value`` is one strategy applied to every cell. Masking is a data-
# classification decision, so it lives in a table: which strategy a cell uses,
# and what each strategy means.
#
# Recognised strategy keys:
#   last4        keep the trailing 4 characters (the historical behaviour)
#   first1       keep the leading character
#   email_domain keep the domain, redact the local part
#   hash         stable non-reversible token for correlation
#   drop         omit the value entirely
#   fixed        a constant, for a cell whose shape must never be inferred
MASKING_PROFILES: dict[str, dict[str, Any]] = {
    "last4": {"strategy": "last4", "description": "trailing four characters"},
    "first1": {"strategy": "first1", "description": "leading character only"},
    "email_domain": {"strategy": "email_domain", "description": "local part redacted"},
    "hash": {"strategy": "hash", "description": "stable sha256 prefix for correlation"},
    "drop": {"strategy": "drop", "description": "value withheld entirely"},
    "fixed": {"strategy": "fixed", "description": "constant placeholder"},
}
DEFAULT_MASKING_PROFILE = "last4"
# Config table: resource -> cell -> profile name. Absent means
# ``DEFAULT_MASKING_PROFILE``, which reproduces the historical masking exactly.
CELL_MASKING_PROFILES: dict[str, dict[str, str]] = {
    "customer_profile": {
        "email": "email_domain",
        "phone": "last4",
        "government_id": "fixed",
    },
    "payments": {
        "full_instrument": "last4",
    },
}

# --- Layer 5: cell sensitivity classification ----------------------------------
#
# Lets a review ask questions the matrix cannot: which cells are sensitive, which
# sensitive cells have no purpose tag (so *any* reason will do), and which have
# no budget at all.
CELL_SENSITIVITY: dict[str, dict[str, str]] = {
    "customer_profile": {
        "full_name": "pii",
        "email": "pii",
        "phone": "pii",
        "preferred_language": "internal",
        "risk_score": "derived_sensitive",
        "biometric_status": "biometric",
        "government_id": "restricted_pii",
    },
    "booking": {
        "id": "internal",
        "service_type": "internal",
        "scheduled_date": "internal",
        "details": "pii",
        "internal_notes": "confidential",
        "assignment_history": "internal",
    },
    "payments": {
        "amount": "financial",
        "instrument_last4": "financial",
        "full_instrument": "restricted_financial",
        "arrears_terms": "financial",
        "refund_eligibility": "financial",
    },
    "audit_trail": {
        "actor": "confidential",
        "action": "confidential",
        "detail_json": "confidential",
        "source": "internal",
    },
}
# Sensitivities above this rank are "restricted" and are expected to carry a
# purpose tag; a cell at or below it is expected to carry a budget.
SENSITIVITY_RANK: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "pii": 2,
    "financial": 3,
    "confidential": 3,
    "derived_sensitive": 3,
    "biometric": 4,
    "restricted_pii": 5,
    "restricted_financial": 5,
}
# Budgets are only required at or above this rank — an internal code needs no
# rate limit to be safe to read.
BUDGET_REQUIRED_FROM = 2


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


def masking_profile_for(resource: str | None, cell: str) -> str:
    """The masking profile configured for a cell, defaulting to ``last4``."""
    profile = (
        CELL_MASKING_PROFILES.get(str(resource), {}).get(str(cell))
        if resource is not None
        else None
    ) or DEFAULT_MASKING_PROFILE
    # An unknown profile name falls back rather than raising: a typo in config
    # must not turn every masked read in a request path into an error.
    return profile if profile in MASKING_PROFILES else DEFAULT_MASKING_PROFILE


def apply_masking(value: Any, profile: str) -> Any:
    """Apply a named masking profile to a value.

    Every strategy returns ``None`` for a non-scalar, because a masked read of a
    list must never leak its length or contents. An unrecognised profile name
    degrades to ``last4`` rather than raising.
    """
    strategy = (MASKING_PROFILES.get(str(profile)) or {}).get("strategy") or "last4"
    if value is None or isinstance(value, (bool, list, tuple, dict, set)):
        return None
    text = value if isinstance(value, str) else str(value)
    if strategy == "drop":
        return None
    if strategy == "fixed":
        return "***"
    if strategy == "hash":
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    if strategy == "first1":
        return f"{text[:1]}***" if text else "***"
    if strategy == "email_domain":
        if "@" in text:
            return f"***@{text.partition('@')[2]}"
        return f"***{text[-4:]}" if len(text) > 4 else "***"
    # ``last4`` -- the historical default, kept character-for-character.
    return f"***{text[-4:]}" if len(text) > 4 else "***"


def _mask_value(value: Any, cell: str, resource: str | None = None) -> Any:
    """Redact a value for a masked read using the cell's configured profile.

    With no profile configured this is exactly the historical ``last4``
    behaviour. A profile can only ever redact *more* than that, so configuring
    one tightens a cell and never widens it.
    """
    return apply_masking(value, masking_profile_for(resource, cell))


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
            result[cell] = _mask_value(value, cell, resource)
        else:
            result[cell] = value
    return result


# --- Layer 5: purpose limitation ------------------------------------------------


def purposes_for(resource: str, cell: str) -> list[str] | None:
    """Purposes a cell may be read for, or ``None`` for "any purpose".

    A missing tag is the pre-layer-5 answer (no purpose check at all) and is
    reported as ``None`` rather than as an empty list, so a caller can tell
    "unrestricted" apart from "restricted to nothing".
    """
    return CELL_PURPOSE_TAGS.get(str(resource), {}).get(str(cell))


def check_purpose(
    resource: str,
    cell: str,
    purpose: str | None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether a declared purpose permits reading one cell.

    Pure, and deliberately *narrowing only*: it can withhold a read that the
    matrix allowed, never grant one. An undeclared purpose on an untagged cell
    is ``ok`` — that is the historical behaviour — while an undeclared purpose
    on a tagged cell is ``purpose_not_declared``.
    """
    allowed = purposes_for(resource, cell)
    declared = str(purpose) if purpose not in (None, "") else None
    if declared is None and context is not None:
        raw = context.get(PURPOSE_CONTEXT_FIELD)
        declared = str(raw) if raw not in (None, "") else None
    if allowed is None:
        return {"resource": resource, "cell": cell, "permitted": True, "reason": "ok",
                "purpose": declared, "allowed_purposes": None}
    if "*" in allowed:
        return {"resource": resource, "cell": cell, "permitted": True, "reason": "ok",
                "purpose": declared, "allowed_purposes": list(allowed)}
    if declared is None:
        return {"resource": resource, "cell": cell, "permitted": False,
                "reason": "purpose_not_declared", "purpose": None,
                "allowed_purposes": list(allowed)}
    if declared not in CELL_PURPOSES:
        return {"resource": resource, "cell": cell, "permitted": False,
                "reason": "purpose_unknown", "purpose": declared,
                "allowed_purposes": list(allowed)}
    if declared not in allowed:
        return {"resource": resource, "cell": cell, "permitted": False,
                "reason": "purpose_not_permitted", "purpose": declared,
                "allowed_purposes": list(allowed)}
    return {"resource": resource, "cell": cell, "permitted": True, "reason": "ok",
            "purpose": declared, "allowed_purposes": list(allowed)}


def plan_purposes(
    resource: str,
    roles: list[str] | tuple[str, ...] | str,
    *,
    purpose: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-run a resource plan with purpose limitation applied on top.

    The layer-5 view of :func:`plan_resource_access`: a cell the matrix would
    show becomes ``hidden`` when the declared purpose does not cover it. Cells
    the matrix already hid stay hidden, so this can only reduce visibility.
    """
    base = plan_resource_access(resource, roles, context=context)
    per_cell: dict[str, dict[str, Any]] = {}
    for cell, state in base["cells"].items():
        verdict = check_purpose(resource, cell, purpose, context=context)
        per_cell[cell] = verdict
        if state != "hidden" and not verdict["permitted"]:
            base["cells"][cell] = "hidden"
    base["purpose"] = {
        "declared": next(
            (v["purpose"] for v in per_cell.values() if v["purpose"]), None
        ),
        "reasons": sorted({v["reason"] for v in per_cell.values()}),
        "cells": per_cell,
    }
    base["visible"] = sorted(c for c, k in base["cells"].items() if k == "visible")
    base["masked"] = sorted(c for c, k in base["cells"].items() if k == "masked")
    base["hidden"] = sorted(c for c, k in base["cells"].items() if k == "hidden")
    return base


# --- Layer 5: read budgets -----------------------------------------------------


def budgets_for(resource: str, cell: str, roles: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Enabled budget rows that cover ``(resource, cell)`` for these roles."""
    resolved = resolve_roles(list(roles) if roles is not None else [])
    out: list[dict[str, Any]] = []
    for budget in CELL_BUDGETS:
        if not budget.get("enabled", True):
            continue
        if budget.get("resource") != resource:
            continue
        if budget.get("cell") not in (cell, "*"):
            continue
        if resolved and not (set(budget.get("roles") or ()) & resolved):
            continue
        out.append(dict(budget))
    return out


class ReadBudgetStore:
    """Thread-safe sliding-window counters for :data:`CELL_BUDGETS`.

    Budgets answer "how often", which the matrix cannot. A grant that is
    legitimate and unlimited is exactly what makes a stolen credential useful,
    so the cap is where the blast radius of a compromise actually stops.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reads: dict[tuple[str, str, str], deque] = {}

    @staticmethod
    def _key(budget_id: str, subject: str, cell: str) -> tuple[str, str, str]:
        return (str(budget_id), str(subject), str(cell))

    def _window(self, key: tuple[str, str, str], window_seconds: int) -> deque:
        bucket = self._reads.get(key)
        if bucket is None or bucket.maxlen != max(1, int(window_seconds)):
            bucket = deque(maxlen=max(1, int(window_seconds)))
            self._reads[key] = bucket
        return bucket

    def record(
        self, budget: dict[str, Any], *, subject: str, cell: str, now: datetime | None = None
    ) -> dict[str, Any]:
        """Count one read against a budget and report whether it is allowed.

        The read is counted whether or not it is allowed, so a client hammering
        a refused cell still cannot be served by waiting for the window to roll.
        """
        moment = now or datetime.now(timezone.utc)
        key = self._key(budget["budget_id"], subject, cell)
        window_seconds = max(1, int(budget.get("window_seconds", 60)))
        with self._lock:
            bucket = self._window(key, window_seconds)
            bucket.append(moment.timestamp())
            used = len(bucket)
            allowed = used <= int(budget.get("max_reads", 0) or 0)
        return {
            "budget_id": budget["budget_id"],
            "subject": subject,
            "cell": cell,
            "used": used,
            "max_reads": int(budget.get("max_reads", 0) or 0),
            "window_seconds": window_seconds,
            "permitted": allowed,
            "reason": "ok" if allowed else "budget_exhausted",
        }

    def peek(
        self, budget: dict[str, Any], *, subject: str, cell: str, now: datetime | None = None
    ) -> dict[str, Any]:
        """Current usage without counting a read (for a pre-flight check)."""
        moment = now or datetime.now(timezone.utc)
        key = self._key(budget["budget_id"], subject, cell)
        window_seconds = max(1, int(budget.get("window_seconds", 60)))
        cutoff = moment.timestamp() - window_seconds
        with self._lock:
            bucket = self._reads.get(key) or deque(maxlen=window_seconds)
            used = sum(1 for stamp in bucket if stamp > cutoff)
        return {
            "budget_id": budget["budget_id"],
            "subject": subject,
            "cell": cell,
            "used": used,
            "max_reads": int(budget.get("max_reads", 0) or 0),
            "window_seconds": window_seconds,
            "permitted": used < int(budget.get("max_reads", 0) or 0),
            "reason": "ok" if used < int(budget.get("max_reads", 0) or 0) else "budget_exhausted",
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                f"{budget_id}:{subject}:{cell}": len(bucket)
                for (budget_id, subject, cell), bucket in sorted(self._reads.items())
            }

    def reset(self) -> None:
        with self._lock:
            self._reads.clear()


READ_BUDGETS = ReadBudgetStore()


# --- Layer 5: standing review questions ----------------------------------------


def review_access_programs() -> dict[str, Any]:
    """The questions a periodic access review has to answer, answered.

    Three gaps, all derived from config rather than from a manual spreadsheet:

    - restricted cells with **no purpose tag** — anything will do as a reason;
    - cells at or above the budget threshold with **no budget** — unbounded;
    - cells no role can read — dead configuration nobody has revisited.
    """
    unrestricted: list[dict[str, Any]] = []
    unbounded: list[dict[str, Any]] = []
    for resource, cells in sorted(CELL_MATRIX.items()):
        for cell in sorted(cells):
            rank = SENSITIVITY_RANK.get(CELL_SENSITIVITY.get(resource, {}).get(cell, "internal"), 1)
            sensitivity = CELL_SENSITIVITY.get(resource, {}).get(cell, "internal")
            if purposes_for(resource, cell) is None:
                unrestricted.append(
                    {
                        "resource": resource,
                        "cell": cell,
                        "sensitivity": sensitivity,
                        "reason": "no_purpose_tag",
                    }
                )
            covered = any(
                budget.get("resource") == resource and budget.get("cell") in (cell, "*")
                for budget in CELL_BUDGETS
            )
            if rank >= BUDGET_REQUIRED_FROM and not covered:
                unbounded.append(
                    {
                        "resource": resource,
                        "cell": cell,
                        "sensitivity": sensitivity,
                        "rank": rank,
                        "reason": "no_read_budget",
                    }
                )
    dead_cells = [
        {"resource": resource, "cell": cell, "reason": "no_role_has_access"}
        for resource, cells in sorted(CELL_MATRIX.items())
        for cell in sorted(cells)
        if not any(
            ACCESS_RANK.get(grants.get(role, "none"), 0) > 0
            for grants in cells[cell].values()
            for role in ROLE_GROUPS
            if role in grants
        )
    ]
    findings = unrestricted + unbounded + dead_cells
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sensitivity": {
            resource: {cell: CELL_SENSITIVITY.get(resource, {}).get(cell, "internal")
                       for cell in sorted(cells)}
            for resource, cells in sorted(CELL_MATRIX.items())
        },
        "purposes": sorted(CELL_PURPOSES),
        "purpose_tags": {
            resource: {cell: list(values) for cell, values in sorted(cells.items())}
            for resource, cells in sorted(CELL_PURPOSE_TAGS.items())
        },
        "budgets": [dict(budget) for budget in CELL_BUDGETS],
        "budget_usage": READ_BUDGETS.state(),
        "unrestricted_cells": unrestricted,
        "unbounded_cells": unbounded,
        "dead_cells": dead_cells,
        "findings": findings,
        "finding_count": len(findings),
        "reviewed": not findings,
    }


def build_cell_matrix_policy() -> dict[str, Any]:
    """The layer-5 catalog: purposes, budgets, masking, sensitivity, review.

    Separate from :func:`build_cell_matrix_catalog` because that function's key
    set is a pinned contract — new capability lives here instead of beside it.
    """
    return {
        "layer": 5,
        "purposes": {name: dict(config) for name, config in sorted(CELL_PURPOSES.items())},
        "purpose_reasons": list(PURPOSE_REASONS),
        "purpose_context_field": PURPOSE_CONTEXT_FIELD,
        "purpose_tags": review_access_programs()["purpose_tags"],
        "budget_reasons": list(BUDGET_REASONS),
        "budgets": [dict(budget) for budget in CELL_BUDGETS],
        "budget_state": READ_BUDGETS.state(),
        "masking_profiles": {
            name: dict(config) for name, config in sorted(MASKING_PROFILES.items())
        },
        "default_masking_profile": DEFAULT_MASKING_PROFILE,
        "cell_masking": {
            resource: dict(sorted(cells.items()))
            for resource, cells in sorted(CELL_MASKING_PROFILES.items())
        },
        "sensitivity_rank": dict(sorted(SENSITIVITY_RANK.items())),
        "budget_required_from": BUDGET_REQUIRED_FROM,
        "review": review_access_programs(),
        "note": (
            "layer 5 is narrowing-only: purpose tags and read budgets can withhold "
            "a read the matrix allowed, never grant one, and a masking profile can "
            "only redact more than the default"
        ),
    }


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
            "purpose_limitation (reduce only)",
            "read_budget (reduce only)",
            "temporary_grant",
            "base_matrix",
            "override_conditions",
            "override_mask",
        ],
        # Layer 5 lives in its own catalog because this key set is pinned.
        "policy": {
            "catalog": "build_cell_matrix_policy",
            "purposes": sorted(CELL_PURPOSES),
            "budget_count": len(CELL_BUDGETS),
            "masking_profile_default": DEFAULT_MASKING_PROFILE,
        },
    }
