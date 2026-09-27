"""Audit trail service.

Records operator and system actions so the backend can explain *who did what,
on which entity, and why*. The service is config-driven in the same spirit as
the other rule engines: allowed actions are a catalog, severity values are
constrained, and the writers are small helpers around the persisted
``AuditLogEntry`` model.

Beyond the original record/list/rollup trio the service now also answers the
harder questions a mature trail has to answer, all as pure functions or thin
config tables so nothing here needs a migration:

* **What is this action?** ``AUDIT_ACTION_SPECS`` composes family defaults with
  per-action overrides, and ``normalize_action`` resolves aliases so callers may
  write ``user.login`` and still land on ``auth.login``.
* **Did we write down too much?** ``redact_detail`` scrubs credential-shaped
  keys before anything is persisted, with depth/size guards so a hostile payload
  cannot blow the stack.
* **What changed?** ``audit_diff`` produces a flat, path-addressed change list,
  so an override can carry a before/after rather than a prose summary.
* **Was it tampered with?** ``seal_entries`` / ``verify_seal_chain`` hash-chain
  the trail so deletions and edits are detectable without extra columns.
* **Is anything unusual?** ``detect_audit_anomalies`` runs a rule table (bursts,
  after-hours criticals, unjustified sensitive actions) over fetched entries.
* **How do I get it out? / how long do I keep it?** ``export_audit_trail``
  streams NDJSON or CSV pages, and ``plan_retention`` applies per-action and
  per-severity windows.

The original five entry points are untouched; everything here is additive.
"""
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import io
import json
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

ALLOWED_SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(ALLOWED_SEVERITIES)}

# Actions the backend treats as auditable, grouped by domain family.
AUDIT_ACTION_CATALOG = {
    "auth": ["auth.login", "auth.register", "auth.password_change"],
    "booking": [
        "booking.create",
        "booking.update",
        "booking.cancel",
        "booking.assign",
        "booking.reassign",
    ],
    "policy": ["policy.override", "policy.restrict", "policy.block"],
    "communication": [
        "communication.override_set",
        "communication.override_clear",
    ],
    "payments": [
        "arrears.open",
        "arrears.settle",
        "arrears.waive_interest",
        "points.exchange",
        "points.adjust",
    ],
    "admin": ["admin.action"],
    "system": ["system.startup", "system.config_change"],
}
# Flat lookup for quick validation.
AUDIT_ACTIONS = {
    action
    for family in AUDIT_ACTION_CATALOG.values()
    for action in family
}

# Family-level defaults, as a flat tuple so a new family is a single line.
# Order: (default_severity, retention_days, requires_justification, immutable,
#         category)
AUDIT_SPEC_FIELDS = (
    "default_severity",
    "retention_days",
    "requires_justification",
    "immutable",
    "category",
)
AUDIT_FAMILY_ROWS: dict[str, tuple[Any, ...]] = {
    # family        severity  retention  justify  immutable  category
    "auth": ("info", 400, False, True, "authentication"),
    "booking": ("info", 730, False, True, "operations"),
    "policy": ("warning", 2555, True, True, "governance"),
    "communication": ("warning", 730, True, True, "governance"),
    "payments": ("warning", 2555, True, True, "financial"),
    "admin": ("warning", 2555, True, True, "governance"),
    "system": ("info", 365, False, True, "platform"),
    "custom": ("info", 90, False, False, "custom"),
}

# Per-action refinements layered on top of the family row. ``None`` inherits.
AUDIT_ACTION_ROWS: dict[str, tuple[Any, ...]] = {
    # action                    severity  retention  justify  immutable  category
    "policy.block": ("critical", None, None, None, None),
    "auth.password_change": (None, 730, None, None, None),
    "arrears.waive_interest": ("critical", None, None, None, None),
    "points.adjust": ("critical", None, None, None, None),
    "booking.reassign": (None, None, True, None, None),
    "system.config_change": (None, None, True, None, None),
}


def _rows_to_specs(rows: Mapping[str, tuple[Any, ...]]) -> dict[str, dict[str, Any]]:
    return {
        key: dict(zip(AUDIT_SPEC_FIELDS, values)) for key, values in rows.items()
    }


# Family-level defaults. Every action inherits from its family, so widening a
# family to a new verb is a one-line change rather than a new spec per action.
AUDIT_FAMILY_DEFAULTS: dict[str, dict[str, Any]] = _rows_to_specs(AUDIT_FAMILY_ROWS)

# Per-action refinements layered on top of the family defaults.
AUDIT_ACTION_OVERRIDES: dict[str, dict[str, Any]] = {
    action: {
        field: value
        for field, value in zip(AUDIT_SPEC_FIELDS, values)
        if value is not None
    }
    for action, values in AUDIT_ACTION_ROWS.items()
}

# Alternate spellings that resolve to a canonical action. Built into
# ``ACTION_ALIASES``; keep them lowercase and collision-free.
AUDIT_ACTION_ALIASES: dict[str, str] = {
    "login": "auth.login",
    "user.login": "auth.login",
    "logout": "auth.login",
    "signup": "auth.register",
    "password.change": "auth.password_change",
    "booking.create": "booking.create",
    "booking.rebook": "booking.update",
    "policy.restrict_topic": "policy.restrict",
    "policy.block_topic": "policy.block",
    "comm.override": "communication.override_set",
    "arrears.settle_all": "arrears.settle",
    "points.swap": "points.exchange",
    "admin.override": "admin.action",
    "system.boot": "system.startup",
}

# Detail keys that satisfy a ``requires_justification`` action.
JUSTIFICATION_KEYS = ("reason", "justification", "rationale", "ticket", "case_id")

# Detail keys redacted by exact (case-insensitive) name.
AUDIT_REDACT_KEYS = frozenset(
    """password passwd new_password old_password secret token access_token
    refresh_token id_token api_key apikey authorization cookie session signature
    private_key card_number pan cvv ssn national_id date_of_birth""".split()
)
# ... plus keys redacted when the fragment appears anywhere in the name.
AUDIT_REDACT_SUBSTRINGS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
)
REDACTED_PLACEHOLDER = "***redacted***"
AUDIT_REDACT_MAX_DEPTH = 8
AUDIT_REDACT_MAX_ITEMS = 200

# Columns emitted by the CSV exporter, in order.
AUDIT_EXPORT_COLUMNS = (
    "id",
    "actor_user_id",
    "action",
    "entity_type",
    "entity_id",
    "summary",
    "detail",
    "severity",
    "source",
    "created_at",
)
EXPORT_FORMATS = ("ndjson", "csv")

# Fields that participate in the tamper-evident seal. Deliberately excludes
# anything mutable-after-write so the chain stays reproducible.
AUDIT_SEALED_FIELDS = (
    "id",
    "action",
    "actor_user_id",
    "entity_type",
    "entity_id",
    "summary",
    "detail",
    "severity",
    "source",
    "created_at",
)
AUDIT_SEAL_ALGO = "sha256"
SEAL_DETAIL_KEY = "_integrity"

AUDIT_RETENTION_BY_SEVERITY = {"info": 365, "warning": 730, "critical": 2555}
AUDIT_RETENTION_MINIMUM_DAYS = 90

# Anomaly rule table. Each rule names the signal, not the code.
AUDIT_ANOMALY_RULES = (
    {
        "id": "actor_burst",
        "severity": "warning",
        "message": "actor produced an unusual burst of entries",
    },
    {
        "id": "sensitive_action_burst",
        "severity": "critical",
        "message": "actor produced many sensitive/governance actions",
    },
    {
        "id": "after_hours_critical",
        "severity": "warning",
        "message": "critical entry recorded outside business hours",
    },
    {
        "id": "unjustified_sensitive_action",
        "severity": "warning",
        "message": "sensitive action recorded without a justification",
    },
    {
        "id": "uncatalogued_action",
        "severity": "info",
        "message": "action is not in the audit action catalog",
    },
)
AUDIT_ANOMALY_DEFAULTS = {
    "window_minutes": 10,
    "burst_threshold": 20,
    "sensitive_burst_threshold": 5,
    "business_hours_start": 6,
    "business_hours_end": 22,
}
AUDIT_SENSITIVE_CATEGORIES = ("governance", "financial")

# Actions that should always justify themselves regardless of spec drift.
DEFAULT_BUSINESS_HOURS = (6, 22)

# --- Read profiles and integrity gates ---------------------------------------
#
# Two config tables for the *read* side of the trail. A trail that is correct but
# unreadable (or readable in full by anyone with admin) is still a problem, so:
#
# * ``AUDIT_VIEW_PROFILES`` says how much of an entry a given audience needs.
#   ``/audit/logs`` keeps returning everything; the profile view is a separate,
#   explicitly requested projection.
# * ``AUDIT_INTEGRITY_GATES`` turns "is this trail trustworthy right now" into a
#   pass/fail table over derived metrics, instead of a boolean from one hash
#   check. The single most useful gate is ``unredacted_credentials``: it finds
#   entries written through the ungated ``/audit/log`` path, which does not run
#   ``redact_detail``.

#: Operator vocabulary shared by the other config-driven gate tables. Declared
#: locally so the audit service keeps no decision-intelligence dependency.
AUDIT_GATE_OPS: dict[str, str] = {
    "gte": "metric >= threshold",
    "gt": "metric > threshold",
    "lte": "metric <= threshold",
    "lt": "metric < threshold",
    "eq": "metric == threshold",
    "neq": "metric != threshold",
    "in": "metric is one of threshold (a list)",
    "not_in": "metric is not one of threshold (a list)",
    "is_true": "metric is truthy; threshold ignored",
    "is_false": "metric is falsy; threshold ignored",
    "is_none": "metric is missing or None",
    "present": "the metric key exists at all",
}
AUDIT_GATE_SEVERITIES: tuple[str, ...] = ("advisory", "review", "reject")
AUDIT_GATE_SEVERITY_RANK: dict[str, int] = {
    name: rank for rank, name in enumerate(AUDIT_GATE_SEVERITIES)
}

#: How much of an entry a profile reveals. ``detail`` selects the projection:
#: ``omit`` (no payload at all), ``shape`` (keys and types, no values) or
#: ``full`` (values, still credential-redacted at write time).
AUDIT_VIEW_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "profile": "digest",
        "detail": "omit",
        "fields": (
            "id",
            "action",
            "severity",
            "entity_type",
            "entity_id",
            "actor_user_id",
            "created_at",
        ),
        "max_summary_chars": 120,
        "include_seal": False,
        "note": "the shape of an event without its content; for dashboards and lists",
    },
    {
        "profile": "operations",
        "detail": "shape",
        "fields": (
            "id",
            "action",
            "severity",
            "entity_type",
            "entity_id",
            "actor_user_id",
            "source",
            "summary",
            "created_at",
        ),
        "max_summary_chars": 400,
        "include_seal": True,
        "note": "which keys a payload carries, not what they say; for triage",
    },
    {
        "profile": "investigation",
        "detail": "full",
        "fields": (
            "id",
            "action",
            "severity",
            "entity_type",
            "entity_id",
            "actor_user_id",
            "source",
            "summary",
            "detail",
            "created_at",
            "updated_at",
        ),
        "max_summary_chars": None,
        "include_seal": True,
        "note": "full payloads for an incident; credential redaction still applies",
    },
)
DEFAULT_AUDIT_VIEW_PROFILE = "digest"
AUDIT_VIEW_PROFILE_BY_ID: dict[str, dict[str, Any]] = {
    str(row["profile"]): dict(row) for row in AUDIT_VIEW_PROFILES
}
AUDIT_DETAIL_MODES: tuple[str, ...] = ("omit", "shape", "full")
AUDIT_VIEW_SHAPE_MAX_DEPTH = 3

#: Gates over the whole trail. ``metrics`` are derived by
#: :func:`audit_integrity_metrics`; a missing metric fails its gate closed, so a
#: metric that stops being derivable can never read as a pass.
AUDIT_INTEGRITY_GATES: tuple[dict[str, Any], ...] = (
    {
        "gate_id": "trail_not_empty",
        "metric": "entries",
        "op": "gte",
        "threshold": 1,
        "severity": "advisory",
        "rationale": "an empty trail satisfies every other gate trivially, so it proves nothing",
    },
    {
        "gate_id": "seal_chain_valid",
        "metric": "chain_valid",
        "op": "is_true",
        "severity": "reject",
        "threshold": True,
        "rationale": "a broken seal chain means entries were removed or edited",
    },
    {
        "gate_id": "seal_coverage_complete",
        "metric": "sealed_ratio",
        "op": "gte",
        "threshold": 1.0,
        "severity": "review",
        "rationale": "unsealed entries are the ones an edit would leave invisible",
    },
    {
        "gate_id": "no_unredacted_credentials",
        "metric": "unredacted_credential_entries",
        "op": "lte",
        "threshold": 0,
        "severity": "reject",
        "rationale": "the ungated write path does not scrub detail, so its entries can still hold a credential",
    },
    {
        "gate_id": "sensitive_actions_justified",
        "metric": "unjustified_sensitive_entries",
        "op": "lte",
        "threshold": 0,
        "severity": "reject",
        "rationale": "a sensitive action without a reason cannot be defended in review",
    },
    {
        "gate_id": "actors_attributed",
        "metric": "actor_coverage",
        "op": "gte",
        "threshold": 0.9,
        "severity": "review",
        "rationale": "an unattributed action cannot be held to anyone",
    },
    {
        "gate_id": "retention_backlog_clear",
        "metric": "retention_expired_entries",
        "op": "lte",
        "threshold": 0,
        "severity": "advisory",
        "rationale": "entries past their window are a data-minimisation debt, not a tamper signal",
    },
)
AUDIT_INTEGRITY_GATE_BY_ID: dict[str, dict[str, Any]] = {
    str(row["gate_id"]): dict(row) for row in AUDIT_INTEGRITY_GATES
}



def build_action_specs(
    extra: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compose the action spec table: family defaults + per-action overrides.

    ``extra`` adds or widens individual actions at runtime, which is how a
    deployment adds a bespoke verb without forking the module.
    """
    specs: dict[str, dict[str, Any]] = {}
    for family, actions in AUDIT_ACTION_CATALOG.items():
        base = AUDIT_FAMILY_DEFAULTS.get(family, AUDIT_FAMILY_DEFAULTS["custom"])
        for action in actions:
            spec = dict(base)
            spec["family"] = family
            spec.update(AUDIT_ACTION_OVERRIDES.get(action, {}))
            spec["aliases"] = sorted(
                alias
                for alias, target in AUDIT_ACTION_ALIASES.items()
                if target == action
            )
            specs[action] = spec
    for action, override in (extra or {}).items():
        spec = dict(specs.get(action, AUDIT_FAMILY_DEFAULTS["custom"]))
        spec.setdefault("family", "custom")
        spec.setdefault("aliases", [])
        spec.update(override)
        specs[action] = spec
    return specs


AUDIT_ACTION_SPECS = build_action_specs()


def build_action_aliases(
    specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """Flat alias -> canonical action map, including the canonical names."""
    aliases: dict[str, str] = {action: action for action in (specs or AUDIT_ACTION_SPECS)}
    for action, spec in (specs or AUDIT_ACTION_SPECS).items():
        for alias in spec.get("aliases") or ():
            aliases[str(alias)] = action
    for alias, target in AUDIT_ACTION_ALIASES.items():
        aliases.setdefault(alias, target)
    return aliases


ACTION_ALIASES = build_action_aliases()


def validate_severity(severity: str) -> str:
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(
            f"severity must be one of {', '.join(ALLOWED_SEVERITIES)}"
        )
    return severity


def severity_rank(severity: str | None) -> int:
    """Sortable rank for a severity (``-1`` when unknown)."""
    return SEVERITY_RANK.get(severity or "", -1)


def max_severity(*severities: str | None) -> str:
    """Highest severity passed, defaulting to ``info``."""
    best = "info"
    for candidate in severities:
        if severity_rank(candidate) > severity_rank(best):
            best = candidate or "info"
    return best


def normalize_action(
    action: str, *, specs: Mapping[str, Mapping[str, Any]] | None = None
) -> str:
    """Resolve ``action`` to its canonical spelling via the alias table.

    Alias chains are walked defensively (bounded) so a cyclic alias
    configuration degrades to the last resolved name instead of hanging.
    """
    canonical = str(action or "").strip().lower()
    if not canonical:
        raise ValueError("action must be a non-empty string")
    aliases = build_action_aliases(specs) if specs else ACTION_ALIASES
    seen = {canonical}
    for _ in range(len(aliases) + 1):
        target = aliases.get(canonical)
        if target is None or target == canonical:
            return canonical
        if target in seen:
            return target
        seen.add(target)
        canonical = target
    return canonical


def validate_action(
    action: str,
    *,
    strict: bool = True,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Normalize ``action`` and optionally require it to be catalogued."""
    canonical = normalize_action(action, specs=specs)
    known = (specs or AUDIT_ACTION_SPECS)
    if strict and canonical not in known:
        families = ", ".join(sorted(AUDIT_ACTION_CATALOG))
        raise ValueError(
            f"unknown audit action {canonical!r}; expected one of the catalogued "
            f"actions (families: {families})"
        )
    return canonical


def action_spec(
    action: str, *, specs: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Spec for ``action``, synthesized for anything not catalogued."""
    table = specs or AUDIT_ACTION_SPECS
    canonical = normalize_action(action, specs=table)
    if canonical in table:
        return dict(table[canonical])
    synthesized = dict(AUDIT_FAMILY_DEFAULTS["custom"])
    synthesized["family"] = "custom"
    synthesized["aliases"] = []
    return synthesized


def default_severity_for(
    action: str, *, specs: Mapping[str, Mapping[str, Any]] | None = None
) -> str:
    """Severity an action records at when the caller does not choose one."""
    return validate_severity(action_spec(action, specs=specs)["default_severity"])


def requires_justification(
    action: str, *, specs: Mapping[str, Mapping[str, Any]] | None = None
) -> bool:
    return bool(action_spec(action, specs=specs).get("requires_justification"))


def has_justification(detail: Mapping[str, Any] | None) -> bool:
    """True when ``detail`` carries any of :data:`JUSTIFICATION_KEYS`."""
    for key in JUSTIFICATION_KEYS:
        value = (detail or {}).get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def _is_sensitive_key(
    key: Any,
    *,
    exact: frozenset[str] = AUDIT_REDACT_KEYS,
    substrings: tuple[str, ...] = AUDIT_REDACT_SUBSTRINGS,
) -> bool:
    lowered = str(key).strip().lower()
    if lowered in exact:
        return True
    return any(fragment in lowered for fragment in substrings)


def redact_detail(
    detail: Any,
    *,
    extra_keys: Iterable[str] = (),
    placeholder: str = REDACTED_PLACEHOLDER,
    max_depth: int = AUDIT_REDACT_MAX_DEPTH,
    max_items: int = AUDIT_REDACT_MAX_ITEMS,
) -> Any:
    """Return a copy of ``detail`` with credential-shaped values masked.

    Depth- and size-bounded, so a deeply nested or huge payload cannot turn a
    redaction into a denial of service.
    """
    exact = AUDIT_REDACT_KEYS | {str(k).strip().lower() for k in extra_keys}

    def _walk(node: Any, depth: int) -> Any:
        if depth > max_depth:
            return {"_truncated": "max_depth_exceeded"}
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if _is_sensitive_key(key, exact=exact):
                    out[str(key)] = placeholder
                else:
                    out[str(key)] = _walk(value, depth + 1)
            return out
        if isinstance(node, (list, tuple)):
            items = list(node)
            kept = [_walk(item, depth + 1) for item in items[:max_items]]
            if len(items) > max_items:
                kept.append({"_truncated": f"{len(items) - max_items}_more_items"})
            return kept
        return node

    if not isinstance(detail, (Mapping, list, tuple)):
        return detail
    return _walk(detail, 0)


def _detail_json(detail: dict | None) -> str:
    try:
        return json.dumps(detail or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _load_json(raw: str | None) -> Any:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def _canonical_json(payload: Any) -> str:
    """Stable serialization used for sealing (sorted keys, no whitespace)."""
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):
        return json.dumps({"_unserializable": True}, sort_keys=True)


def entry_to_payload(entry: models.AuditLogEntry) -> dict:
    return {
        "id": entry.id,
        "actor_user_id": entry.actor_user_id,
        "action": entry.action,
        "entity_type": entry.entity_type,
        "entity_id": str(entry.entity_id or ""),
        "summary": entry.summary,
        "detail": _load_json(entry.detail_json),
        "severity": entry.severity,
        "source": entry.source,
        "created_at": entry.created_at,
    }


def coerce_entry(entry: Any) -> dict[str, Any]:
    """Payload dict for either a persisted model or an already-shaped mapping.

    Lets the pure helpers below (diff, seal, anomalies, export) be reused on
    hand-built fixtures without going through the ORM.
    """
    if isinstance(entry, Mapping):
        payload = dict(entry)
        if "detail" not in payload and "detail_json" in payload:
            payload["detail"] = _load_json(payload["detail_json"])
        payload.setdefault("detail", {})
        payload["entity_id"] = str(payload.get("entity_id") or "")
        return payload
    return entry_to_payload(entry)


# --- change tracking ----------------------------------------------------------


def audit_diff(before: Any, after: Any, *, path: str = "") -> list[dict[str, Any]]:
    """Flat, path-addressed diff of two JSON-able values.

    Returns ``{"op": add|remove|replace, "path": ..., "before": ..., "after": ...}``
    records. Containers recurse, so the result is stable enough to diff twice.
    """
    changes: list[dict[str, Any]] = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in after:
                changes.append(
                    {"op": "remove", "path": child, "before": before[key], "after": None}
                )
            elif key not in before:
                changes.append(
                    {"op": "add", "path": child, "before": None, "after": after[key]}
                )
            else:
                changes.extend(audit_diff(before[key], after[key], path=child))
        return changes
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        before_list, after_list = list(before), list(after)
        for index in range(max(len(before_list), len(after_list))):
            child = f"{path}[{index}]"
            if index >= len(after_list):
                changes.append(
                    {
                        "op": "remove",
                        "path": child,
                        "before": before_list[index],
                        "after": None,
                    }
                )
            elif index >= len(before_list):
                changes.append(
                    {
                        "op": "add",
                        "path": child,
                        "before": None,
                        "after": after_list[index],
                    }
                )
            else:
                changes.extend(
                    audit_diff(before_list[index], after_list[index], path=child)
                )
        return changes
    if before != after:
        changes.append(
            {"op": "replace", "path": path or "$", "before": before, "after": after}
        )
    return changes


def build_change_detail(
    before: Any, after: Any, *, max_items: int = 50
) -> dict[str, Any]:
    """Diff summary suitable for stashing in an entry's ``detail``."""
    diff = audit_diff(before, after)
    return {
        "changed": bool(diff),
        "change_count": len(diff),
        "diff": diff[:max_items],
        "truncated": len(diff) > max_items,
    }


# --- tamper-evident sealing ---------------------------------------------------


def seal_payload(entry: Any) -> dict[str, Any]:
    """The subset of an entry that participates in the seal."""
    payload = coerce_entry(entry)
    detail = payload.get("detail")
    if isinstance(detail, Mapping) and SEAL_DETAIL_KEY in detail:
        # The seal cannot cover itself; drop the previously written integrity
        # block so re-sealing a re-read entry is idempotent.
        detail = {k: v for k, v in detail.items() if k != SEAL_DETAIL_KEY}
    sealed: dict[str, Any] = {}
    for field in AUDIT_SEALED_FIELDS:
        value = payload.get(field)
        if field == "detail" and detail is not None:
            value = detail
        sealed[field] = value.isoformat() if isinstance(value, datetime) else value
    return sealed


def compute_seal(payload: Any, *, prev_seal: str = "") -> str:
    """Hash-chain link for one entry: ``sha256(prev_seal | canonical(payload))``."""
    material = f"{prev_seal}|{_canonical_json(payload)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def entry_seal(entry: Any, *, prev_seal: str = "") -> str:
    """Seal for a single entry chained onto ``prev_seal``."""
    return compute_seal(seal_payload(entry), prev_seal=prev_seal)


def seal_entries(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Hash-chain ``entries`` (oldest first) into seal records."""
    seals: list[dict[str, Any]] = []
    prev_seal = ""
    for entry in entries:
        payload = seal_payload(entry)
        seal = compute_seal(payload, prev_seal=prev_seal)
        seals.append(
            {
                "id": payload.get("id"),
                "action": payload.get("action"),
                "seal": seal,
                "prev_seal": prev_seal,
            }
        )
        prev_seal = seal
    return seals


def verify_seal_chain(seals: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Check that seal ``n`` was chained onto seal ``n-1``."""
    broken_at = None
    previous = ""
    count = 0
    for index, record in enumerate(seals):
        count = index + 1
        if record.get("prev_seal", "") != previous:
            broken_at = index
            break
        previous = str(record.get("seal") or "")
    return {
        "generated_at": datetime.now(timezone.utc),
        "valid": broken_at is None,
        "entries": count,
        "broken_at": broken_at,
        "algo": AUDIT_SEAL_ALGO,
        "policy": "hash-chained over sealed fields; edits break the link",
        "sealed_fields": list(AUDIT_SEALED_FIELDS),
    }


class SealChain:
    """In-memory append-only chain, so writes seal as they happen.

    Mirrors the persisted ``detail_json`` integrity block but keeps the running
    head available without a database round-trip.
    """

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def extend(self, entry: Any) -> str:
        """Seal ``entry`` onto the chain and return the new head digest."""
        payload = seal_payload(entry)
        prev_seal = self._records[-1]["seal"] if self._records else ""
        seal = compute_seal(payload, prev_seal=prev_seal)
        self._records.append(
            {
                "id": payload.get("id"),
                "action": payload.get("action"),
                "seal": seal,
                "prev_seal": prev_seal,
            }
        )
        return seal

    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def head(self) -> str:
        return self._records[-1]["seal"] if self._records else ""

    def total(self) -> int:
        return len(self._records)

    def verify(self) -> dict[str, Any]:
        return verify_seal_chain(self._records)

    def reset(self) -> None:
        self._records.clear()


SEAL_CHAIN = SealChain()


# --- rollups over fetched entries --------------------------------------------


#: The two ways a stored ``_integrity`` block can stop matching the entry it
#: describes. ``link`` means the entry is not chained onto its predecessor;
#: ``content`` means the chained-on hash no longer covers the entry's fields,
#: which is what an edit looks like.
AUDIT_SEAL_FAILURE_REASONS: dict[str, str] = {
    "link": "the stored prev_seal does not match the seal of the preceding entry",
    "content": "the stored seal does not match a recomputed seal over the entry's sealed fields",
    "malformed": "the _integrity block is missing a seal or prev_seal",
}


def verify_entry_seal_chain(entries: Iterable[Any]) -> dict[str, Any]:
    """Recompute the seal chain over real trail entries and compare it.

    :func:`verify_seal_chain` checks that a list of *seal records* links up. This
    is the other direction: given entries that each carry a persisted
    ``_integrity`` block, re-derive each seal and say whether the stored hashes
    still describe what the entries now say. An unsealed entry does not
    participate in the chain (it is reported as coverage, not as tampering), so
    the running ``prev_seal`` only advances across sealed entries.
    """
    broken_at: int | None = None
    broken_reason: str | None = None
    previous = ""
    checked = 0
    sealed = 0
    for index, entry in enumerate(entries):
        payload = coerce_entry(entry)
        detail = payload.get("detail")
        if not isinstance(detail, Mapping) or SEAL_DETAIL_KEY not in detail:
            continue
        block = detail.get(SEAL_DETAIL_KEY)
        if not isinstance(block, Mapping):
            broken_at, broken_reason = index, "malformed"
            break
        stored_seal = str(block.get("seal") or "")
        stored_prev = str(block.get("prev_seal") or "")
        if not stored_seal:
            broken_at, broken_reason = index, "malformed"
            break
        sealed += 1
        if stored_prev != previous:
            broken_at, broken_reason = index, "link"
            break
        recomputed = entry_seal(payload, prev_seal=previous)
        if recomputed != stored_seal:
            broken_at, broken_reason = index, "content"
            break
        checked += 1
        previous = stored_seal
    return {
        "generated_at": datetime.now(timezone.utc),
        "valid": broken_at is None,
        "sealed_entries": sealed,
        "verified_entries": checked,
        "broken_at": broken_at,
        "broken_reason": broken_reason,
        "broken_explanation": AUDIT_SEAL_FAILURE_REASONS.get(str(broken_reason or ""), ""),
        "algo": AUDIT_SEAL_ALGO,
        "sealed_fields": list(AUDIT_SEALED_FIELDS),
        "policy": (
            "recomputed per entry and compared to the persisted _integrity block; "
            "unsealed entries are coverage, not tampering"
        ),
    }


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def summarize_actor_activity(entries: Iterable[Any]) -> dict[str, Any]:
    """Per-actor rollup: volume, span, and the severity/action mix."""
    actors: dict[Any, dict[str, Any]] = {}
    for raw in entries:
        payload = coerce_entry(raw)
        key = payload.get("actor_user_id")
        bucket = actors.setdefault(
            key,
            {
                "actor_user_id": key,
                "count": 0,
                "by_action": {},
                "by_severity": {},
                "entities": set(),
                "first_seen": None,
                "last_seen": None,
            },
        )
        bucket["count"] += 1
        action = str(payload.get("action") or "")
        bucket["by_action"][action] = bucket["by_action"].get(action, 0) + 1
        severity = str(payload.get("severity") or "info")
        bucket["by_severity"][severity] = bucket["by_severity"].get(severity, 0) + 1
        entity = f"{payload.get('entity_type', '')}:{payload.get('entity_id', '')}"
        if payload.get("entity_type"):
            bucket["entities"].add(entity)
        occurred = _as_datetime(payload.get("created_at"))
        if occurred is not None:
            if bucket["first_seen"] is None or occurred < bucket["first_seen"]:
                bucket["first_seen"] = occurred
            if bucket["last_seen"] is None or occurred > bucket["last_seen"]:
                bucket["last_seen"] = occurred

    rows = []
    for bucket in actors.values():
        rows.append(
            {
                "actor_user_id": bucket["actor_user_id"],
                "count": bucket["count"],
                "distinct_entities": len(bucket["entities"]),
                "by_action": dict(sorted(bucket["by_action"].items())),
                "by_severity": dict(sorted(bucket["by_severity"].items())),
                "peak_severity": max_severity(*bucket["by_severity"]),
                "first_seen": bucket["first_seen"],
                "last_seen": bucket["last_seen"],
            }
        )
    rows.sort(key=lambda row: (-row["count"], str(row["actor_user_id"])))
    return {
        "generated_at": datetime.now(timezone.utc),
        "actor_count": len(rows),
        "actors": rows,
    }


def build_entity_timeline(
    entries: Iterable[Any],
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Chronological history for one entity (or every entity when unfiltered)."""
    events: list[dict[str, Any]] = []
    for raw in entries:
        payload = coerce_entry(raw)
        if entity_type and payload.get("entity_type") != entity_type:
            continue
        if entity_id is not None and str(payload.get("entity_id") or "") != str(entity_id):
            continue
        detail = payload.get("detail")
        change = detail.get("_change") if isinstance(detail, Mapping) else None
        events.append(
            {
                "id": payload.get("id"),
                "action": payload.get("action"),
                "actor_user_id": payload.get("actor_user_id"),
                "severity": payload.get("severity"),
                "summary": payload.get("summary"),
                "source": payload.get("source"),
                "occurred_at": payload.get("created_at"),
                "changed": bool((change or {}).get("changed")) if change else None,
            }
        )
    events.sort(key=lambda event: (str(event["occurred_at"] or ""), str(event["id"] or "")))
    return events


# --- anomaly detection --------------------------------------------------------


def _is_sensitive_action(action: str, *, specs: Mapping[str, Mapping[str, Any]]) -> bool:
    return action_spec(action, specs=specs).get("category") in AUDIT_SENSITIVE_CATEGORIES


def detect_audit_anomalies(
    entries: Iterable[Any],
    *,
    thresholds: Mapping[str, Any] | None = None,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run the anomaly rule table over ``entries`` (oldest order not required)."""
    config = {**AUDIT_ANOMALY_DEFAULTS, **dict(thresholds or {})}
    table = specs or AUDIT_ACTION_SPECS
    window = timedelta(minutes=int(config["window_minutes"]))
    burst_threshold = int(config["burst_threshold"])
    sensitive_threshold = int(config["sensitive_burst_threshold"])
    hours_start = int(config["business_hours_start"])
    hours_end = int(config["business_hours_end"])

    payloads = [coerce_entry(raw) for raw in entries]
    findings: list[dict[str, Any]] = []

    # actor_burst + sensitive_action_burst
    by_actor: dict[Any, list[dict[str, Any]]] = {}
    for payload in payloads:
        by_actor.setdefault(payload.get("actor_user_id"), []).append(payload)
    for actor, rows in by_actor.items():
        if actor is None:
            continue
        stamps = sorted(
            stamp
            for stamp in (_as_datetime(row.get("created_at")) for row in rows)
            if stamp is not None
        )
        for index, start in enumerate(stamps):
            in_window = [s for s in stamps[index:] if s - start <= window]
            if len(in_window) > burst_threshold:
                findings.append(
                    {
                        "rule": "actor_burst",
                        "severity": "warning",
                        "subject": f"actor:{actor}",
                        "message": (
                            f"{len(in_window)} entries in "
                            f"{config['window_minutes']}m (threshold {burst_threshold})"
                        ),
                        "count": len(in_window),
                        "entry_ids": [
                            row.get("id")
                            for row in rows
                            if _as_datetime(row.get("created_at")) in set(in_window)
                        ],
                        "observed_at": start,
                    }
                )
                break
        sensitive = [
            row for row in rows if _is_sensitive_action(str(row.get("action") or ""), specs=table)
        ]
        if len(sensitive) > sensitive_threshold:
            findings.append(
                {
                    "rule": "sensitive_action_burst",
                    "severity": "critical",
                    "subject": f"actor:{actor}",
                    "message": (
                        f"{len(sensitive)} governance/financial actions "
                        f"(threshold {sensitive_threshold})"
                    ),
                    "count": len(sensitive),
                    "entry_ids": [row.get("id") for row in sensitive],
                    "observed_at": _as_datetime(
                        sensitive[-1].get("created_at")
                    ),
                }
            )

    # after_hours_critical
    for payload in payloads:
        if severity_rank(payload.get("severity")) < SEVERITY_RANK["critical"]:
            continue
        occurred = _as_datetime(payload.get("created_at"))
        if occurred is None:
            continue
        hour = occurred.hour
        in_hours = hours_start <= hour < hours_end
        if in_hours:
            continue
        findings.append(
            {
                "rule": "after_hours_critical",
                "severity": "warning",
                "subject": f"action:{payload.get('action')}",
                "message": f"critical entry at {hour:02d}:00 local (business hours {hours_start:02d}-{hours_end:02d})",
                "count": 1,
                "entry_ids": [payload.get("id")],
                "observed_at": occurred,
            }
        )

    # unjustified_sensitive_action + uncatalogued_action
    for payload in payloads:
        action = str(payload.get("action") or "")
        spec = action_spec(action, specs=table)
        detail = payload.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        if spec.get("requires_justification") and not has_justification(detail):
            findings.append(
                {
                    "rule": "unjustified_sensitive_action",
                    "severity": "warning",
                    "subject": f"action:{action}",
                    "message": "sensitive action has no justification detail",
                    "count": 1,
                    "entry_ids": [payload.get("id")],
                    "observed_at": _as_datetime(payload.get("created_at")),
                }
            )
        if action not in table:
            findings.append(
                {
                    "rule": "uncatalogued_action",
                    "severity": "info",
                    "subject": f"action:{action}",
                    "message": "action is absent from AUDIT_ACTION_CATALOG",
                    "count": 1,
                    "entry_ids": [payload.get("id")],
                    "observed_at": _as_datetime(payload.get("created_at")),
                }
            )

    findings.sort(key=lambda finding: (finding["rule"], str(finding["subject"])))
    return findings


# --- retention ----------------------------------------------------------------


def retention_days_for(
    action: str,
    *,
    severity: str = "info",
    specs: Mapping[str, Mapping[str, Any]] | None = None,
    overrides: Mapping[str, int] | None = None,
) -> int:
    """Retention window for an action, widened by severity and overrides.

    Precedence: an explicit per-action override wins, then the action's own
    spec window, then the severity floor. A ``"*"`` override raises the floor
    for every action, and the result never drops below
    :data:`AUDIT_RETENTION_MINIMUM_DAYS`.
    """
    canonical = normalize_action(action, specs=specs)
    table = dict(overrides or {})
    if canonical in table:
        return max(int(table[canonical]), AUDIT_RETENTION_MINIMUM_DAYS)
    base = int(
        action_spec(canonical, specs=specs).get("retention_days")
        or AUDIT_RETENTION_MINIMUM_DAYS
    )
    base = max(base, AUDIT_RETENTION_BY_SEVERITY.get(severity, 0))
    if "*" in table:
        base = max(base, int(table["*"]))
    return max(base, AUDIT_RETENTION_MINIMUM_DAYS)


def plan_retention(
    entries: Iterable[Any],
    *,
    now: datetime | None = None,
    overrides: Mapping[str, int] | None = None,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Split ``entries`` into the ones past their retention window and the rest."""
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    expired: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    policies: dict[str, int] = {}
    for raw in entries:
        payload = coerce_entry(raw)
        action = str(payload.get("action") or "")
        severity = str(payload.get("severity") or "info")
        window = retention_days_for(
            action, severity=severity, specs=specs, overrides=overrides
        )
        policies[action] = window
        occurred = _as_datetime(payload.get("created_at")) or reference
        age_days = max((reference - occurred).total_seconds() / 86400.0, 0.0)
        record = {
            "id": payload.get("id"),
            "action": action,
            "severity": severity,
            "age_days": round(age_days, 3),
            "retention_days": window,
            "expires_on": occurred + timedelta(days=window),
        }
        (expired if age_days > window else keep).append(record)
    return {
        "generated_at": datetime.now(timezone.utc),
        "now": reference,
        "expired_count": len(expired),
        "keep_count": len(keep),
        "expired": expired,
        "keep": keep,
        "policies": dict(sorted(policies.items())),
        "retention_by_severity": dict(AUDIT_RETENTION_BY_SEVERITY),
        "minimum_days": AUDIT_RETENTION_MINIMUM_DAYS,
    }


# --- export -------------------------------------------------------------------


def export_entries_ndjson(entries: Iterable[Any]) -> str:
    """One JSON object per line, payloads already coerced."""
    lines: list[str] = []
    for raw in entries:
        payload = coerce_entry(raw)
        if isinstance(payload.get("created_at"), datetime):
            payload["created_at"] = payload["created_at"].isoformat()
        lines.append(_canonical_json(payload))
    return "\n".join(lines)


def export_entries_csv(entries: Iterable[Any]) -> str:
    """Flat CSV over :data:`AUDIT_EXPORT_COLUMNS`; ``detail`` is compact JSON."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(AUDIT_EXPORT_COLUMNS)
    for raw in entries:
        payload = coerce_entry(raw)
        row = []
        for column in AUDIT_EXPORT_COLUMNS:
            value = payload.get(column)
            if column == "detail":
                value = _canonical_json(value if isinstance(value, Mapping) else {})
            elif column == "created_at" and isinstance(value, datetime):
                value = value.isoformat()
            row.append("" if value is None else value)
        writer.writerow(row)
    return buffer.getvalue()


async def iter_audit_pages(
    db: AsyncSession,
    *,
    page_size: int = 200,
    max_pages: int = 25,
    **filters: Any,
) -> AsyncIterator[list[dict[str, Any]]]:
    """Yield payload pages until the trail is exhausted or ``max_pages`` is hit.

    De-duplicates by entry id, so a backend that ignores ``offset`` terminates
    instead of looping forever, and holds a backend that ignores ``limit`` to
    ``page_size`` rows per page instead of over-delivering.
    """
    page_size = max(1, int(page_size))
    seen: set[Any] = set()
    carry: list[dict[str, Any]] = []
    for page in range(max(1, int(max_pages))):
        if carry:
            rows: list[Any] = carry
            carry = []
        else:
            rows = await list_audit_log_entries(
                db, limit=page_size, offset=page * page_size, **filters
            )
            if not rows:
                return
        fresh: list[dict[str, Any]] = []
        for row in rows:
            payload = coerce_entry(row)
            # Identity is read off the coerced payload so a mapping row dedupes
            # exactly like a mapped ORM row.
            identity = payload.get("id")
            if identity is not None and identity in seen:
                continue
            if len(fresh) >= page_size:
                # Surplus rows are re-queued, not dropped and not re-seen.
                carry.append(payload)
                continue
            fresh.append(payload)
            if identity is not None:
                seen.add(identity)
        if not fresh:
            return
        yield fresh
        if carry:
            continue
        if len(rows) < page_size:
            return


async def export_audit_trail(
    db: AsyncSession,
    *,
    fmt: str = "ndjson",
    page_size: int = 200,
    max_pages: int = 25,
    **filters: Any,
) -> dict[str, Any]:
    """Stream the trail into a single NDJSON or CSV document."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(EXPORT_FORMATS)}")
    entries: list[dict[str, Any]] = []
    async for page in iter_audit_pages(
        db, page_size=page_size, max_pages=max_pages, **filters
    ):
        entries.extend(page)
    renderer = {"ndjson": export_entries_ndjson, "csv": export_entries_csv}[fmt]
    return {
        "format": fmt,
        "entry_count": len(entries),
        "page_size": page_size,
        "max_pages": max_pages,
        "truncated": len(entries) >= page_size * max_pages,
        "columns": list(AUDIT_EXPORT_COLUMNS) if fmt == "csv" else None,
        "content": renderer(entries),
    }


def audit_view_profile(profile: str | None = None) -> dict[str, Any]:
    """The :data:`AUDIT_VIEW_PROFILES` row to project with."""
    key = profile or DEFAULT_AUDIT_VIEW_PROFILE
    if key not in AUDIT_VIEW_PROFILE_BY_ID:
        raise ValueError(
            f"unknown view profile {key!r}; "
            f"expected one of {sorted(AUDIT_VIEW_PROFILE_BY_ID)}"
        )
    return dict(AUDIT_VIEW_PROFILE_BY_ID[key])


def _detail_shape(value: Any, depth: int = 0) -> Any:
    """Type-only projection of a payload: keys without values."""
    if depth > AUDIT_VIEW_SHAPE_MAX_DEPTH:
        return "_depth"
    if isinstance(value, Mapping):
        return {
            str(key): _detail_shape(item, depth + 1)
            for key, item in list(value.items())[:AUDIT_REDACT_MAX_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        if not value:
            return "_empty_list"
        return [_detail_shape(value[0], depth + 1), f"_{len(value)}_items"]
    if value is None:
        return "_null"
    return type(value).__name__


def project_audit_entry(
    entry: Any,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Project one trail entry through a view profile.

    The projection is additive: ``/audit/logs`` is untouched, and a caller has to
    ask for a profile to get one. ``detail`` is resolved by mode (``omit`` /
    ``shape`` / ``full``) and the summary is truncated to the profile's budget so
    a list view cannot be turned into a bulk payload dump.
    """
    rules = audit_view_profile(profile)
    payload = coerce_entry(entry)
    detail = payload.get("detail") or {}
    mode = str(rules["detail"])
    if mode == "omit":
        projected_detail: Any = None
    elif mode == "shape":
        projected_detail = _detail_shape(detail)
    else:
        projected_detail = redact_detail(detail)
    summary = str(payload.get("summary") or "")
    budget = rules.get("max_summary_chars")
    truncated = bool(budget is not None and len(summary) > int(budget))
    if truncated:
        summary = summary[: int(budget)]
    sealed = SEAL_DETAIL_KEY in (detail if isinstance(detail, Mapping) else {})
    out: dict[str, Any] = {
        "profile": rules["profile"],
        "detail_mode": mode,
        "detail_keys": sorted(str(key) for key in detail) if isinstance(detail, Mapping) else [],
        "redacted": bool(projected_detail) and REDACTED_PLACEHOLDER in json.dumps(
            projected_detail, default=str
        ),
        "truncated": truncated,
    }
    for field in rules["fields"]:
        if field == "detail":
            out["detail"] = projected_detail
        else:
            out[field] = payload.get(field)
    if rules.get("include_seal"):
        out["sealed"] = sealed
    return out


def project_audit_entries(
    entries: Iterable[Any],
    *,
    profile: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Project a batch of entries and report what the projection cost."""
    rules = audit_view_profile(profile)
    rows = [project_audit_entry(entry, profile=rules["profile"]) for entry in entries]
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return {
        "generated_at": datetime.now(timezone.utc),
        "profile": rules["profile"],
        "detail_mode": str(rules["detail"]),
        "fields": list(rules["fields"]),
        "max_summary_chars": rules.get("max_summary_chars"),
        "include_seal": bool(rules.get("include_seal")),
        "count": len(rows),
        "redacted_count": sum(1 for row in rows if row.get("redacted")),
        "truncated_count": sum(1 for row in rows if row.get("truncated")),
        "sealed_count": sum(1 for row in rows if row.get("sealed")),
        "entries": rows,
        "note": "projection only; /audit/logs still returns the unprojected trail",
    }


def _number(value: Any) -> Optional[float]:
    """Coerce to float, or None. Booleans are never numbers."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _op_holds(value: Any, op: str, threshold: Any) -> bool:
    """Apply one gate operator. Fails closed on an unknown operator name."""
    if op not in AUDIT_GATE_OPS:
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
    if op == "eq":
        return value == threshold
    if op == "neq":
        return value != threshold
    number = _number(value)
    limit = _number(threshold)
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
    return False


def _carries_unredacted_credential(detail: Any) -> bool:
    """True when ``redact_detail`` would still change this payload.

    The write paths that go through :func:`record_auditable` always redact, so
    this is a detector for entries that bypassed them.
    """
    if not isinstance(detail, (Mapping, list, tuple)):
        return False
    return redact_detail(detail) != detail


def audit_integrity_metrics(entries: Iterable[Any]) -> dict[str, Any]:
    """Derive the metrics :data:`AUDIT_INTEGRITY_GATES` reads from the trail."""
    rows = list(entries)
    payloads = [coerce_entry(entry) for entry in rows]
    total = len(payloads)
    chain = verify_entry_seal_chain(rows)
    if not total:
        return {
            "entries": 0,
            "sealed_entries": 0,
            "sealed_ratio": None,
            "unredacted_credential_entries": 0,
            "unjustified_sensitive_entries": 0,
            "sensitive_entries": 0,
            "actor_attributed_entries": 0,
            "actor_coverage": None,
            "retention_expired_entries": 0,
            # Nothing to break, so the chain is vacuously intact -- but it proves
            # nothing either, which is what the coverage gates are for.
            "chain_valid": bool(chain.get("valid")),
            "verified_entries": 0,
            "broken_at": chain.get("broken_at"),
            "broken_reason": chain.get("broken_reason"),
        }
    sealed = 0
    unredacted = 0
    unjustified = 0
    sensitive = 0
    attributed = 0
    expired = 0
    now = datetime.now(timezone.utc)
    for payload in payloads:
        detail = payload.get("detail") or {}
        if isinstance(detail, Mapping) and SEAL_DETAIL_KEY in detail:
            sealed += 1
        if _carries_unredacted_credential(detail):
            unredacted += 1
        action = str(payload.get("action") or "")
        if _is_sensitive_action(action, specs=AUDIT_ACTION_SPECS):
            sensitive += 1
            if not has_justification(detail):
                unjustified += 1
        if payload.get("actor_user_id") is not None:
            attributed += 1
        created = _as_datetime(payload.get("created_at"))
        if created is not None:
            age_days = (now - created).total_seconds() / 86400
            if age_days > retention_days_for(
                action, severity=str(payload.get("severity") or "info")
            ):
                expired += 1
    return {
        "entries": total,
        "sealed_entries": sealed,
        "sealed_ratio": round(sealed / total, 6),
        "unredacted_credential_entries": unredacted,
        "unjustified_sensitive_entries": unjustified,
        "sensitive_entries": sensitive,
        "actor_attributed_entries": attributed,
        "actor_coverage": round(attributed / total, 6),
        "retention_expired_entries": expired,
        "chain_valid": bool(chain.get("valid")),
        "verified_entries": int(chain.get("verified_entries") or 0),
        "broken_at": chain.get("broken_at"),
        "broken_reason": chain.get("broken_reason"),
    }


def evaluate_audit_gates(
    metrics: Mapping[str, Any],
    *,
    gates: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate the integrity gates, strongest severity first."""
    results: list[dict[str, Any]] = []
    for row in gates or AUDIT_INTEGRITY_GATES:
        metric = str(row.get("metric", ""))
        op = str(row.get("op", ""))
        threshold = row.get("threshold")
        observed = metric in metrics and metrics[metric] is not None
        actual = metrics.get(metric)
        if not observed:
            holds = False
            reason = "metric_missing"
        else:
            holds = _op_holds(actual, op, threshold)
            reason = "ok" if holds else "threshold_not_met"
        severity = str(row.get("severity", "advisory"))
        if severity not in AUDIT_GATE_SEVERITY_RANK:
            severity = "advisory"
        results.append(
            {
                "gate_id": row.get("gate_id"),
                "metric": metric,
                "op": op,
                "op_meaning": AUDIT_GATE_OPS.get(op, "unknown operator"),
                "threshold": threshold,
                "actual": actual,
                "observed": observed,
                "severity": severity,
                "holds": holds,
                "reason": reason,
                "rationale": row.get("rationale", ""),
            }
        )
    results.sort(
        key=lambda result: (
            -AUDIT_GATE_SEVERITY_RANK.get(str(result["severity"]), 0),
            str(result["gate_id"]),
        )
    )
    return results


def audit_integrity_report(
    entries: Iterable[Any],
    *,
    gates: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Is this trail trustworthy right now, and which gate says otherwise.

    ``reject`` means the trail has a problem worth stopping for (a broken chain,
    a credential still in a payload, a sensitive action with no reason). It is a
    *report*, not an enforcement point: the read endpoints keep answering.
    """
    rows = list(entries)
    metrics = audit_integrity_metrics(rows)
    results = evaluate_audit_gates(metrics, gates=gates)
    failed = [result for result in results if not result["holds"]]
    rejected = [str(r["gate_id"]) for r in failed if r["severity"] == "reject"]
    review = [str(r["gate_id"]) for r in failed if r["severity"] == "review"]
    advisory = [str(r["gate_id"]) for r in failed if r["severity"] == "advisory"]
    if rejected:
        verdict = "reject"
    elif review or advisory:
        verdict = "review"
    else:
        verdict = "clean"
    return {
        "generated_at": datetime.now(timezone.utc),
        "verdict": verdict,
        "rejected_by": rejected,
        "needs_review_by": review,
        "advisory_by": advisory,
        "metrics": metrics,
        "gates": results,
        "gates_evaluated": len(results),
        "summary": (
            f"{metrics['entries']} entries, seal chain "
            f"{'valid' if metrics['chain_valid'] else 'BROKEN at ' + str(metrics['broken_at'])}"
            f"{' (' + str(metrics['broken_reason']) + ')' if metrics['broken_reason'] else ''}, "
            f"{metrics['sealed_entries']} sealed, "
            f"{metrics['unredacted_credential_entries']} with unredacted credentials, "
            f"{metrics['unjustified_sensitive_entries']} unjustified sensitive actions "
            f"-> {verdict}"
        ),
    }


def build_audit_log_catalog() -> dict[str, object]:
    return {
        "severities": list(ALLOWED_SEVERITIES),
        "actions": AUDIT_ACTION_CATALOG,
        "action_count": len(AUDIT_ACTIONS),
        # --- expansion surface -------------------------------------------------
        "families": sorted(AUDIT_ACTION_CATALOG),
        "action_specs": {
            action: {
                "family": spec["family"],
                "category": spec["category"],
                "default_severity": spec["default_severity"],
                "requires_justification": spec["requires_justification"],
                "immutable": spec["immutable"],
                "retention_days": spec["retention_days"],
                "aliases": list(spec["aliases"]),
            }
            for action, spec in sorted(AUDIT_ACTION_SPECS.items())
        },
        "alias_count": len(ACTION_ALIASES),
        "justification_keys": list(JUSTIFICATION_KEYS),
        "redaction": {
            "placeholder": REDACTED_PLACEHOLDER,
            "exact_keys": sorted(AUDIT_REDACT_KEYS),
            "substrings": list(AUDIT_REDACT_SUBSTRINGS),
            "max_depth": AUDIT_REDACT_MAX_DEPTH,
            "max_items": AUDIT_REDACT_MAX_ITEMS,
        },
        "diff_ops": ["add", "remove", "replace"],
        "seal": {
            "algo": AUDIT_SEAL_ALGO,
            "fields": list(AUDIT_SEALED_FIELDS),
            "detail_key": SEAL_DETAIL_KEY,
            "chained": len(SEAL_CHAIN.records()),
        },
        "anomaly_rules": [dict(rule) for rule in AUDIT_ANOMALY_RULES],
        "anomaly_defaults": dict(AUDIT_ANOMALY_DEFAULTS),
        "sensitive_categories": list(AUDIT_SENSITIVE_CATEGORIES),
        "retention": {
            "by_severity": dict(AUDIT_RETENTION_BY_SEVERITY),
            "minimum_days": AUDIT_RETENTION_MINIMUM_DAYS,
        },
        "export": {
            "formats": list(EXPORT_FORMATS),
            "columns": list(AUDIT_EXPORT_COLUMNS),
        },
        "view_profiles": {
            "table": [dict(row) for row in AUDIT_VIEW_PROFILES],
            "default": DEFAULT_AUDIT_VIEW_PROFILE,
            "detail_modes": list(AUDIT_DETAIL_MODES),
            "shape_max_depth": AUDIT_VIEW_SHAPE_MAX_DEPTH,
            "note": (
                "/audit/logs is unchanged; a profile is an explicit projection "
                "requested through the view endpoint"
            ),
            "helpers": ["audit_view_profile", "project_audit_entry", "project_audit_entries"],
        },
        "integrity_gates": {
            "table": [dict(row) for row in AUDIT_INTEGRITY_GATES],
            "operators": dict(AUDIT_GATE_OPS),
            "severities": list(AUDIT_GATE_SEVERITIES),
            "seal_failure_reasons": dict(AUDIT_SEAL_FAILURE_REASONS),
            "verdict": "clean | review | reject",
            "verdict_rule": "any reject failure -> reject; any review/advisory failure -> review; else clean",
            "missing_metric": "fails the gate closed (reason=metric_missing)",
            "advisory": "a report, not an enforcement point: the read endpoints keep answering",
            "notable_gate": (
                "no_unredacted_credentials finds entries written through the ungated "
                "/audit/log path, which does not run redact_detail"
            ),
            "helpers": ["audit_integrity_metrics", "evaluate_audit_gates", "audit_integrity_report"],
        },
        "note": (
            "action specs are composed from family defaults so a new verb is a "
            "one-line change; redaction, diffing, sealing, anomaly rules and "
            "retention windows are all config-driven and need no migration"
        ),
    }


async def record_audit_log_entry(
    db: AsyncSession,
    *,
    action: str,
    summary: str,
    actor_user_id: Optional[int] = None,
    entity_type: str = "system",
    entity_id: str = "",
    detail: dict | None = None,
    severity: str = "info",
    source: str = "api",
) -> models.AuditLogEntry:
    """Persist one audit trail entry and return it."""
    validate_severity(severity)
    entry = models.AuditLogEntry(
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id or ""),
        summary=summary,
        detail_json=_detail_json(detail),
        severity=severity,
        source=source,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def record_auditable(
    db: AsyncSession,
    *,
    action: str,
    summary: str,
    actor_user_id: Optional[int] = None,
    entity_type: str = "system",
    entity_id: str = "",
    detail: dict | None = None,
    severity: str | None = None,
    source: str = "api",
    before: Any = None,
    after: Any = None,
    strict: bool = False,
    require_justification: bool = True,
    seal: bool = True,
    extra_redact_keys: Iterable[str] = (),
) -> models.AuditLogEntry:
    """Record a governed entry: validate, redact, diff, then seal.

    The stricter sibling of :func:`record_audit_log_entry`. It resolves aliases,
    applies the action's default severity, requires justification for sensitive
    actions, scrubs credential-shaped detail and folds in a before/after diff.

    With ``seal=True`` (the default) the write is two-phase: the governed
    payload is inserted first, then the row's real ``id``/``created_at`` are
    known and the ``_integrity`` seal block is stamped on top and chained onto
    the running :data:`SEAL_CHAIN` head. Sealing cannot cover its own block, so
    :func:`seal_payload` drops it when re-deriving — re-sealing a re-read entry
    is therefore idempotent. ``seal=False`` keeps it to a single commit.
    """
    canonical = validate_action(action, strict=strict)
    spec = action_spec(canonical)
    resolved_severity = validate_severity(severity or spec["default_severity"])
    if (
        require_justification
        and spec.get("requires_justification")
        and not has_justification(detail)
    ):
        raise ValueError(
            f"action {canonical!r} requires a justification field "
            f"({', '.join(JUSTIFICATION_KEYS)})"
        )

    payload_detail: dict[str, Any] = redact_detail(
        dict(detail or {}), extra_keys=extra_redact_keys
    )
    if before is not None or after is not None:
        payload_detail["_change"] = build_change_detail(before, after)

    entry = models.AuditLogEntry(
        actor_user_id=actor_user_id,
        action=canonical,
        entity_type=entity_type,
        entity_id=str(entity_id or ""),
        summary=summary,
        detail_json=_detail_json(payload_detail),
        severity=resolved_severity,
        source=source,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    if seal:
        prev_seal = SEAL_CHAIN.head()
        written = dict(payload_detail)
        written[SEAL_DETAIL_KEY] = {
            "algo": AUDIT_SEAL_ALGO,
            "prev_seal": prev_seal,
            "seal": entry_seal(entry, prev_seal=prev_seal),
        }
        entry.detail_json = _detail_json(written)
        await db.commit()
        SEAL_CHAIN.extend(entry)
    return entry


async def list_audit_log_entries(
    db: AsyncSession,
    *,
    action: Optional[str] = None,
    severity: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[models.AuditLogEntry]:
    """List the trail, newest first, with optional filters."""
    query = select(models.AuditLogEntry).order_by(
        models.AuditLogEntry.created_at.desc(),
        models.AuditLogEntry.id.desc(),
    )
    if action:
        query = query.where(models.AuditLogEntry.action == action)
    if severity:
        validate_severity(severity)
        query = query.where(models.AuditLogEntry.severity == severity)
    if actor_user_id is not None:
        query = query.where(models.AuditLogEntry.actor_user_id == actor_user_id)
    if entity_type:
        query = query.where(models.AuditLogEntry.entity_type == entity_type)
    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


async def count_audit_log_entries(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(models.AuditLogEntry.id)))
    return int(result.scalar() or 0)


async def build_audit_log_summary(db: AsyncSession) -> dict:
    """Roll up the trail by action and by severity."""
    total = await count_audit_log_entries(db)

    action_rows = await db.execute(
        select(models.AuditLogEntry.action, func.count())
        .group_by(models.AuditLogEntry.action)
        .order_by(models.AuditLogEntry.action)
    )
    by_action = [{"key": key, "count": count} for key, count in action_rows.all()]

    severity_rows = await db.execute(
        select(models.AuditLogEntry.severity, func.count())
        .group_by(models.AuditLogEntry.severity)
        .order_by(models.AuditLogEntry.severity)
    )
    by_severity = [{"key": key, "count": count} for key, count in severity_rows.all()]

    return {
        "generated_at": datetime.now(timezone.utc),
        "total": total,
        "by_action": by_action,
        "by_severity": by_severity,
    }
