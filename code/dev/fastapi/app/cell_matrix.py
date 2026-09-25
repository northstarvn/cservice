"""Fine-grained data-cell access matrices.

Beyond role-level permissions, some data is sensitive *per cell*: an agent may
read a customer's name and email but not their phone or biometric status; an
auditor sees risk scores but not payment instruments. This module encodes that
as a config matrix and answers cell-granular questions:

- ``evaluate_cell_matrix(resource, roles)`` — every cell of a resource with the
  effective access for a principal's role set.
- ``cell_access`` / ``can_read_cell`` / ``can_write_cell`` — point queries.
- ``ROLE_GROUPS`` — role expansion (e.g. ``admin`` implies agent+auditor+owner
  unless the matrix overrides), and ``ACCESS_LEVELS`` ranks none < read < write.

The matrix (``CELL_MATRIX``) is a config table: tightening/loosening a single
cell is a data change, not a code change.
"""
from __future__ import annotations

from typing import Any

ACCESS_LEVELS = ("none", "read", "write")
ACCESS_RANK = {"none": 0, "read": 1, "write": 2}

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


def resolve_roles(roles: list[str] | tuple[str, ...] | str) -> set[str]:
    """Expand role groups into the raw roles they imply."""
    if isinstance(roles, str):
        roles = (roles,)
    resolved: set[str] = set()
    for role in roles:
        resolved.add(role)
        resolved.update(ROLE_GROUPS.get(role, ()))
    return resolved or {"none"}


def cell_access(resource: str, cell: str, roles: list[str] | tuple[str, ...] | str) -> str:
    """Effective access level (none/read/write) for a single data cell."""
    grants = CELL_MATRIX.get(resource, {}).get(cell, {})
    effective = max(
        (ACCESS_RANK.get(grants.get(role, "none"), 0) for role in resolve_roles(roles)),
    )
    return ACCESS_LEVELS[effective]


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


def build_cell_matrix_catalog() -> dict[str, object]:
    summary: dict[str, Any] = {}
    for resource, cells in sorted(CELL_MATRIX.items()):
        summary[resource] = {
            cell: dict(grants) for cell, grants in sorted(cells.items())
        }
    return {
        "access_levels": list(ACCESS_LEVELS),
        "role_groups": {group: list(roles) for group, roles in ROLE_GROUPS.items()},
        "resources": sorted(CELL_MATRIX),
        "cells": summary,
    }