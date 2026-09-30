"""Optimistic concurrency for mutable decision/config entities.

The decision-intelligence modules mutate registered state — model version
promotions, rule weights, policy tables. A plain read-modify-write silently
loses a concurrent update; the last writer clobbers the winner. This module
adds a tiny version-stamped guard so a stale write is rejected with a clear
``StaleVersionError`` instead of silently overwriting.

The original module could compare-and-swap a record you already held a
reference to. That is only half the problem: in practice nobody holds a
long-lived object, everybody re-reads by key, retries on conflict, and needs to
know *which fields* collided so a 409 response can tell a human what to merge.
So the module now also provides:

- ``VersionedRecord.patch`` — RFC 7396 JSON merge-patch, so a client can send
  ``{"weights": {"risk": 3}}`` and merge it into the current body without
  clobbering sibling keys.
- ``conflicting_fields`` on the error — the concrete divergence between the
  caller's view and the winner's, not just "somewhere, a version moved".
- ``VersionedStore`` — a keyed registry (tenant- and entity-scoped) so callers
  re-read by key instead of retaining object references, with history and
  rollback.
- ``update_with_retry`` — re-read, re-apply, and retry up to N times. The right
  default for *idempotent* mutations.
- ``update_serialized`` — try the CAS, and on conflict take a pessimistic lock
  and apply anyway. The right default for *must-not-lose* mutations where a
  bounded retry is worse than a short wait.
- ``LockSet`` — acquire several records in a deterministic order so a
  multi-entity transaction cannot deadlock against another one.

There is deliberately no import-time global state: callers own their records
or their store, which keeps the utility testable and dependency-free.

Expansion (thin-group pass)
---------------------------
A 409 that says "re-read and retry" is correct and useless to the caller that
just lost a race on a field the winner never touched. The winner changed
``weights.risk``; the loser was writing ``weights.stage``. Nothing actually
collided, and forcing a human to re-merge is how update conflicts turn into
lost work. The gaps this pass closes:

- ``CONFLICT_STRATEGIES`` / ``CONFLICT_GATES`` — *what to do* about a conflict is
  a per-entity-class decision, so it is a table: an audit append must never be
  dropped (``force``), a weight edit can be auto-merged (``auto_merge``), a
  hand-tuned policy should make a human decide (``error``).
- ``three_way_merge`` / ``merge_with_strategy`` — the auto-merge itself, at
  *leaf* granularity, so ``weights.risk`` conflicting does not block
  ``weights.stage``. Every genuinely divergent leaf is reported, not resolved.
- ``deep_diff`` / ``diff_paths`` — ``diff_fields`` is deliberately shallow and
  stays that way; this is the recursive structural differ for tooling.
- ``FIELD_GUARDS`` — per-field invariants (monotonic, immutable, required) that
  are checked *before* an apply, so a bad write is rejected with a reason
  instead of landing and being discovered later.
- ``LockSet.acquire_until`` — a bounded wait, so a caller cannot block forever
  behind a stuck writer.
- ``VersionedStore.conflicts`` — conflict telemetry: which entities conflict,
  how often, and on which fields.

``update``, ``patch``, ``compare_and_swap``, ``update_with_retry``,
``update_serialized``, ``LockSet.commit`` and the ``conflict_policy`` catalog
block are unchanged.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator, Optional

DEFAULT_START_VERSION = 1
# How many versions a record keeps for diffing/rollback.
DEFAULT_HISTORY_DEPTH = 20

# ---------------------------------------------------------------------------
# Expansion config tables
# ---------------------------------------------------------------------------

# What to do when a CAS fails. `error` is the v1 behaviour and stays the
# default; the others exist because "always 409" is the wrong answer for some
# entities and the right answer for others.
CONFLICT_STRATEGIES: list[dict[str, Any]] = [
    {
        "strategy": "error",
        "label": "Reject with 409",
        "auto_merge": False,
        "force": False,
        "max_attempts": 0,
        "requires_human": True,
        "when_hint": "Default. The caller's view is materially different; a human must merge.",
    },
    {
        "strategy": "auto_merge",
        "label": "Three-way merge the non-conflicting leaves",
        "auto_merge": True,
        "force": False,
        "max_attempts": 1,
        "requires_human": False,
        "when_hint": "Caller and winner edited disjoint leaves; merge, and report any true conflict.",
    },
    {
        "strategy": "retry",
        "label": "Re-read and replay the intent",
        "auto_merge": False,
        "force": False,
        "max_attempts": 3,
        "requires_human": False,
        "when_hint": "The mutator is idempotent and written against intent, not a snapshot.",
    },
    {
        "strategy": "force",
        "label": "Take the pessimistic lock and apply anyway",
        "auto_merge": False,
        "force": True,
        "max_attempts": 0,
        "requires_human": False,
        "when_hint": "Must-not-lose writes (audit appends, counters). A bounded wait beats dropping the write.",
    },
]
CONFLICT_STRATEGY_NAMES = tuple(entry["strategy"] for entry in CONFLICT_STRATEGIES)
STRATEGY_BY_NAME = {entry["strategy"]: dict(entry) for entry in CONFLICT_STRATEGIES}
DEFAULT_CONFLICT_STRATEGY = "error"

# Per-entity-class routing. `entity_prefix` matches the record's entity name;
# the most specific (longest) prefix wins, so a specific entry can override a
# broad one. `lock_timeout_seconds` bounds the force/retry wait.
CONFLICT_GATES: list[dict[str, Any]] = [
    {
        "entity_prefix": "audit",
        "strategy": "force",
        "lock_timeout_seconds": 5.0,
        "field_guards": "none",
        "when_hint": "Audit trails are append-only evidence; never reject, never drop.",
    },
    {
        "entity_prefix": "metric",
        "strategy": "auto_merge",
        "lock_timeout_seconds": 2.0,
        "field_guards": "monotonic",
        "when_hint": "Counter and gauge updates touch disjoint keys; merge them.",
    },
    {
        "entity_prefix": "model_version",
        "strategy": "auto_merge",
        "lock_timeout_seconds": 2.0,
        "field_guards": "required",
        "when_hint": "Promotion metadata is disjoint; but the version number itself must be present.",
    },
    {
        "entity_prefix": "rule_weights",
        "strategy": "error",
        "lock_timeout_seconds": 0.0,
        "field_guards": "monotonic",
        "when_hint": "Hand-tuned risk weights: a human decides, because a merge can change the score.",
    },
    {
        "entity_prefix": "",
        "strategy": "error",
        "lock_timeout_seconds": 0.0,
        "field_guards": "none",
        "when_hint": "Catch-all: the v1 behaviour for anything not routed above.",
    },
]

# Field invariants checked before an apply. `fields` names the guarded paths;
# a violation rejects the write with a reason instead of landing it.
FIELD_GUARDS: list[dict[str, Any]] = [
    {
        "guard": "monotonic",
        "applies_to": "rule_weights.weight",
        "fields": ["weight", "priority", "version", "sequence", "count", "revision"],
        "direction": "non_decreasing",
        "allow_equal": True,
        "when_hint": "These only ever move up, so a lower value means a stale write.",
    },
    {
        "guard": "immutable",
        "applies_to": "audit.entry_id",
        "fields": ["id", "entry_id", "uuid", "created_at", "tx_id", "signature"],
        "direction": "never_changes",
        "allow_equal": True,
        "when_hint": "Identity and creation facts are write-once.",
    },
    {
        "guard": "required",
        "applies_to": "model_version.version",
        "fields": ["version", "model_id", "entity_type"],
        "direction": "must_be_present",
        "allow_equal": False,
        "when_hint": "Absent rather than null: clearing these keys corrupts the record.",
    },
]

# Diff depth for merge planning. Deeper than 2 is rarely what a 409 body wants.
DEFAULT_DIFF_DEPTH = 4
MAX_AUTO_MERGE_LEAVES = 500


class StaleVersionError(Exception):
    """Raised when a mutation targets a version that is no longer current."""

    def __init__(
        self,
        entity: str,
        expected: int,
        current: int,
        conflicting_fields: Iterable[str] = (),
    ):
        self.entity = entity
        self.expected = expected
        self.current = current
        self.conflicting_fields = sorted(conflicting_fields)
        message = f"stale write on {entity}: expected version {expected}, current {current}"
        if self.conflicting_fields:
            message += f" (conflicting: {', '.join(self.conflicting_fields)})"
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        """API-shaped 409 body so callers do not re-derive the fields."""
        return {
            "error": "stale_version",
            "entity": self.entity,
            "expected_version": self.expected,
            "current_version": self.current,
            "conflicting_fields": self.conflicting_fields,
            "hint": "re-read the record, merge the listed fields, and retry",
        }


class FieldGuardError(Exception):
    """Raised when a proposed write violates a ``FIELD_GUARDS`` invariant.

    Separate from :class:`StaleVersionError` on purpose: a guard violation is
    never a concurrency problem, so retrying cannot help and answering 409 would
    send the caller looking for a conflict that does not exist. The write is
    rejected before it lands, so the record's version does not move.
    """

    def __init__(self, entity: str, violations: Iterable[dict[str, Any]]):
        self.entity = entity
        self.violations = list(violations)
        summary = "; ".join(str(item.get("message", item)) for item in self.violations)
        super().__init__(f"field guard rejected write on {entity}: {summary}")

    def to_payload(self) -> dict[str, Any]:
        """API-shaped 422 body: the write was well-formed but not permitted."""
        return {
            "error": "field_guard_violation",
            "entity": self.entity,
            "violations": self.violations,
            "hint": "the proposed body breaks a declared field invariant; nothing was written",
        }


def diff_fields(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Top-level keys where two bodies disagree.

    Deliberately shallow: the caller is a human-facing 409 that wants to say
    "``weights`` moved", not a recursive structural differ.
    """
    keys = set(expected or {}) | set(actual or {})
    return sorted(key for key in keys if (expected or {}).get(key) != (actual or {}).get(key))


def apply_json_merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """RFC 7396 JSON merge-patch against a copy of ``target``.

    ``null`` deletes a key, nested objects merge recursively, and everything
    else replaces. Deep-copied so the patch never aliases live record data.
    """
    result = _deep_copy(target or {})
    for key, value in (patch or {}).items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            base = result.get(key)
            result[key] = apply_json_merge_patch(base if isinstance(base, dict) else {}, value)
        else:
            result[key] = _deep_copy(value)
    return result


def _merge_patch_into(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """Apply a merge-patch to ``target`` *in place*, honouring deletion.

    ``apply_json_merge_patch`` returns a whole new body, so folding it back with
    ``target.update(...)`` silently drops the deletion half of RFC 7396: a
    ``null`` in the patch removed the key from the *result* and then
    ``update()`` put the old value straight back. Replacing the contents keeps
    ``null`` meaning "delete", which is what the docstring promises and what a
    client sending ``{"field": null}`` actually means.
    """
    patched = apply_json_merge_patch(target, patch)
    target.clear()
    target.update(patched)


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    if isinstance(value, set):
        return {_deep_copy(v) for v in value}
    return value


# ---------------------------------------------------------------------------
# Expansion: structural diff, three-way merge, guards, strategy routing
# ---------------------------------------------------------------------------


class _Missing:
    """Sentinel distinguishing "absent" from an explicit ``null``.

    A three-way merge that cannot tell those apart will happily resurrect a key
    the winner deliberately deleted, which is the one outcome a merge must never
    produce silently.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def _get(body: Any, key: str) -> Any:
    if isinstance(body, dict) and key in body:
        return body[key]
    return MISSING


def deep_diff(
    expected: Any,
    actual: Any,
    *,
    prefix: str = "",
    depth: int = DEFAULT_DIFF_DEPTH,
) -> list[dict[str, Any]]:
    """Recursive structural diff, reported as dotted leaf paths.

    ``diff_fields`` above is intentionally shallow ("``weights`` moved"); this
    is the other half ("``weights.risk`` went 2 -> 3"), which is what an
    auto-merge or a review UI needs. Recursion stops at ``depth`` and at any
    value that is not a dict, so a list of 10k rows diffs as one leaf.
    """
    changes: list[dict[str, Any]] = []
    if isinstance(expected, dict) and isinstance(actual, dict) and depth > 0:
        for key in sorted(set(expected) | set(actual)):
            here = f"{prefix}.{key}" if prefix else str(key)
            left = _get(expected, key)
            right = _get(actual, key)
            if left is MISSING and right is MISSING:
                continue
            if left is MISSING or right is MISSING:
                changes.append(
                    {
                        "path": here,
                        "from": None if left is MISSING else left,
                        "to": None if right is MISSING else right,
                        "change": "added" if left is MISSING else "removed",
                    }
                )
                continue
            if left == right:
                continue
            if isinstance(left, dict) and isinstance(right, dict):
                changes.extend(deep_diff(left, right, prefix=here, depth=depth - 1))
            else:
                changes.append({"path": here, "from": left, "to": right, "change": "changed"})
        return changes
    if expected is MISSING and actual is MISSING:
        return changes
    if expected != actual:
        changes.append(
            {
                "path": prefix or "$",
                "from": None if expected is MISSING else expected,
                "to": None if actual is MISSING else actual,
                "change": "changed",
            }
        )
    return changes


def diff_paths(expected: Any, actual: Any, *, depth: int = DEFAULT_DIFF_DEPTH) -> list[str]:
    """Just the dotted paths from :func:`deep_diff` (handy as a merge filter)."""
    return [change["path"] for change in deep_diff(expected, actual, depth=depth)]


def three_way_merge(
    base: Optional[dict[str, Any]],
    mine: Optional[dict[str, Any]],
    theirs: Optional[dict[str, Any]],
    *,
    path: str = "",
) -> dict[str, Any]:
    """Merge two edits made against a common ``base``.

    ``base`` is what both sides last read, ``mine`` is the caller's proposal and
    ``theirs`` is the winner's current body. At every leaf:

    - only one side changed it -> that side wins;
    - both changed it to the same value -> converged, take it;
    - both changed it differently -> **conflict**: the winner's value is kept
      and the divergence is reported.

    Conflicting leaves are never resolved silently and never dropped: the caller
    gets ``conflicts`` and decides. Nested dicts recurse, so an edit to
    ``weights.risk`` does not collide with an unrelated edit to ``weights.stage``.
    Lists are compared whole — a structural list merge is a different feature and
    guessing at one is how merge bugs are born.
    """
    base_body = dict(base or {})
    my_body = dict(mine or {})
    their_body = dict(theirs or {})
    conflicts: list[dict[str, Any]] = []
    applied: list[str] = []

    def _recurse(here: str, b: dict, m: dict, t: dict) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in sorted(set(b) | set(m) | set(t)):
            dotted = f"{here}.{key}" if here else str(key)
            left, mid, right = _get(b, key), _get(m, key), _get(t, key)
            mine_changed = mid is not MISSING and mid != left
            theirs_changed = right is not MISSING and right != left
            mine_deleted = mid is MISSING and left is not MISSING
            theirs_deleted = right is MISSING and left is not MISSING
            if isinstance(left, dict) and isinstance(mid, dict) and isinstance(right, dict):
                out[key] = _recurse(dotted, left, mid, right)
                continue
            if mine_changed and theirs_changed and mid != right:
                conflicts.append({"path": dotted, "base": None if left is MISSING else left, "mine": mid, "theirs": right})
                out[key] = right
                continue
            if mine_deleted and theirs_changed:
                conflicts.append({"path": dotted, "base": left, "mine": None, "theirs": right, "kind": "delete_vs_edit"})
                continue
            if theirs_deleted and mine_changed:
                conflicts.append({"path": dotted, "base": left, "mine": mid, "theirs": None, "kind": "edit_vs_delete"})
                continue
            if mine_deleted and not theirs_changed:
                continue  # honour the delete
            if theirs_deleted and not mine_changed:
                continue  # honour the winner's delete
            if mine_changed and theirs_changed:
                out[key] = _deep_copy(right)
                if mid == right:
                    applied.append(dotted)  # both sides landed on the same value
                else:
                    conflicts.append(
                        {
                            "path": dotted,
                            "base": None if left is MISSING else left,
                            "mine": mid,
                            "theirs": right,
                        }
                    )
                continue
            if mine_changed:
                out[key] = _deep_copy(mid)
                applied.append(dotted)
                continue
            if theirs_changed:
                # Only the winner moved this leaf: the caller's copy is stale
                # here, so the winner's value is what must survive.
                out[key] = _deep_copy(right)
                applied.append(dotted)
                continue
            if right is not MISSING:
                out[key] = _deep_copy(right)
        return out

    merged = _recurse(path, base_body, my_body, their_body)
    return {
        "merged": merged,
        "conflicts": conflicts,
        "conflict_paths": [item["path"] for item in conflicts],
        "applied_paths": applied,
        "clean": not conflicts,
        "policy": "conflicting leaves keep the winner's value and are reported for a human",
    }


def field_guard_violations(
    before: dict[str, Any], after: dict[str, Any], *, guards: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Check ``FIELD_GUARDS`` invariants for a proposed body.

    Returns one entry per violation. A guard is a *precondition*, so this is
    called before the apply and a non-empty result means the write is rejected.
    """
    wanted = set(guards) if guards is not None else {entry["guard"] for entry in FIELD_GUARDS}
    violations: list[dict[str, Any]] = []
    for spec in FIELD_GUARDS:
        if spec["guard"] not in wanted:
            continue
        for dotted in spec["fields"]:
            old = _dig(before, dotted)
            new = _dig(after, dotted)
            if spec["guard"] == "monotonic":
                if old is not MISSING and new is not MISSING:
                    before_num, after_num = _numeric(old), _numeric(new)
                    if before_num is not None and after_num is not None and after_num < before_num:
                        violations.append(
                            {
                                "guard": "monotonic",
                                "field": dotted,
                                "before": old,
                                "after": new,
                                "message": f"{dotted} may not decrease ({old} -> {new})",
                            }
                        )
            elif spec["guard"] == "immutable":
                if old is not MISSING and new is not MISSING and old != new:
                    violations.append({"guard": "immutable", "field": dotted, "before": old, "after": new, "message": f"{dotted} is write-once ({old!r} -> {new!r})"})
            elif spec["guard"] == "required":
                if new is MISSING and old is not MISSING:
                    violations.append({"guard": "required", "field": dotted, "before": old, "after": None, "message": f"{dotted} may not be removed"})
    return violations


def _dig(body: Any, dotted: str) -> Any:
    node: Any = body
    for part in str(dotted).split("."):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def _numeric(value: Any) -> Optional[float]:
    try:
        if isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def gate_for(entity: str) -> dict[str, Any]:
    """The ``CONFLICT_GATES`` row for an entity name (longest prefix wins)."""
    name = str(entity)
    best: Optional[dict[str, Any]] = None
    best_len = -1
    for row in CONFLICT_GATES:
        prefix = str(row.get("entity_prefix", ""))
        if name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = row, len(prefix)
    return dict(best or CONFLICT_GATES[-1])


def resolve_conflict(
    entity: str,
    base: Optional[dict[str, Any]],
    mine: Optional[dict[str, Any]],
    theirs: dict[str, Any],
    *,
    strategy: str | None = None,
) -> dict[str, Any]:
    """Decide and *describe* how to resolve a conflict.

    Returns a plan, never a silent outcome: ``apply`` is what the caller should
    write, ``conflicts`` are the leaves a human still has to decide, and
    ``requires_human`` says plainly whether this is safe to do unattended.
    """
    gate = gate_for(entity)
    chosen = str(strategy or gate["strategy"])
    if chosen not in STRATEGY_BY_NAME:
        raise ValueError(f"unknown conflict strategy {chosen!r} (expected one of {list(CONFLICT_STRATEGY_NAMES)})")
    spec = STRATEGY_BY_NAME[chosen]
    merge = three_way_merge(base, mine, theirs)
    plan: dict[str, Any] = {
        "entity": entity,
        "strategy": chosen,
        "gate": gate,
        "strategy_spec": spec,
        "auto_merge": bool(spec["auto_merge"]),
        "requires_human": bool(spec["requires_human"]),
        "lock_timeout_seconds": float(gate.get("lock_timeout_seconds", 0.0) or 0.0),
        "field_guards": str(gate.get("field_guards", "none")),
        "conflicts": merge["conflicts"],
        "conflict_paths": merge["conflict_paths"],
        "deep_diff": deep_diff(mine or {}, theirs),
        "reason": (
            f"gated as {chosen!r} by entity prefix {gate.get('entity_prefix', '')!r}"
            if strategy is None
            else f"strategy {chosen!r} chosen explicitly"
        ),
    }
    if spec["auto_merge"] and merge["clean"]:
        plan.update({"resolution": "auto_merge", "apply": merge["merged"], "applied_paths": merge["applied_paths"]})
    elif spec["auto_merge"]:
        plan.update({"resolution": "partial_merge", "apply": merge["merged"], "applied_paths": merge["applied_paths"]})
    elif spec["force"]:
        plan.update({"resolution": "force", "apply": dict(mine or {}), "applied_paths": []})
    else:
        plan.update({"resolution": "reject", "apply": None, "applied_paths": []})
    return plan



class VersionedRecord:
    """A dict-backed mutable record with an immutable version stamp (CAS target).

    Deliberately not a dataclass: the internal state is lock- and
    history-coupled, and a hand-written ``__init__`` keeps the public
    constructor signature stable regardless of the private fields.
    """

    def __init__(
        self,
        entity: str,
        data: dict[str, Any] | None = None,
        version: int = DEFAULT_START_VERSION,
        history_depth: int = DEFAULT_HISTORY_DEPTH,
    ):
        self.entity = entity
        self._data: dict[str, Any] = dict(data or {})
        self._version = int(version)
        self._lock = threading.RLock()
        self._history = []
        self._conflicts: list[dict[str, Any]] = []
        self.history_depth = max(int(history_depth), 0)

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def read(self) -> tuple[int, dict[str, Any]]:
        """Return ``(current_version, data_copy)`` — the caller's expected version."""
        with self._lock:
            return self._version, dict(self._data)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entity": self.entity,
                "version": self._version,
                "data": dict(self._data),
            }

    def history(self) -> list[dict[str, Any]]:
        """Recent prior versions, oldest first (bounded by ``history_depth``)."""
        with self._lock:
            return [dict(h) for h in self._history]

    def _remember(self) -> None:
        if self.history_depth:
            self._history.append({"version": self._version, "data": dict(self._data)})
            del self._history[: max(len(self._history) - self.history_depth, 0)]

    def update(
        self,
        expected_version: int,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Compare-and-swap update.

        Applies ``mutator(self._data)`` only when ``expected_version`` matches
        the current version, then bumps the version. On mismatch raises
        ``StaleVersionError`` and leaves the record untouched. A raising
        mutator also leaves the record untouched (no partial writes).
        """
        with self._lock:
            if expected_version != self._version:
                raise StaleVersionError(self.entity, expected_version, self._version)
            return self._apply(mutator)

    def patch(
        self,
        expected_version: int,
        patch: dict[str, Any],
        *,
        base: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """RFC 7396 merge-patch under a CAS guard.

        ``base`` is the caller's view of the record; on a version conflict the
        fields where that view diverges from the winner are reported on the
        raised :class:`StaleVersionError` so a 409 can name them.
        """
        with self._lock:
            if expected_version != self._version:
                raise StaleVersionError(
                    self.entity,
                    expected_version,
                    self._version,
                    diff_fields(base or {}, self._data),
                )
            return self._apply(lambda data: _merge_patch_into(data, patch))

    def merge(
        self,
        expected_version: int,
        patch: dict[str, Any],
        *,
        base: dict[str, Any] | None = None,
        strategy: str | None = None,
        guards: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """CAS with a table-driven conflict strategy instead of a bare 409.

        On a version match this is :meth:`patch`. On a conflict it consults
        ``CONFLICT_GATES`` (or an explicit ``strategy``) and either

        - auto-merges the non-conflicting leaves and applies them,
        - forces the write under the pessimistic lock, or
        - raises :class:`StaleVersionError` carrying every conflicting leaf.

        Field guards are checked before any apply, so a guard violation never
        lands and never bumps the version.
        """
        with self._lock:
            if expected_version == self._version:
                return self._guarded_apply(
                    lambda data: _merge_patch_into(data, patch),
                    guards=guards,
                )
            plan = resolve_conflict(self.entity, base, apply_json_merge_patch(base or {}, patch), self._data, strategy=strategy)
            self._conflicts.append(
                {
                    "entity": self.entity,
                    "expected_version": expected_version,
                    "current_version": self._version,
                    "strategy": plan["strategy"],
                    "resolution": plan["resolution"],
                    "conflict_paths": plan["conflict_paths"],
                }
            )
            if plan["resolution"] in ("auto_merge", "partial_merge") and plan["apply"] is not None:
                return self._guarded_apply(
                    lambda data: data.update(plan["apply"]), guards=guards
                )
            if plan["resolution"] == "force":
                return self._guarded_apply(
                    lambda data: _merge_patch_into(data, patch), guards=guards
                )
            raise StaleVersionError(
                self.entity, expected_version, self._version, plan["conflict_paths"]
            )

    def _guarded_apply(
        self, mutator: Callable[[dict[str, Any]], None], *, guards: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """Run a mutation through the field guards, then apply it atomically.

        The guard runs against a *candidate* copy so a rejected write leaves the
        record — and its version — exactly as it was.
        """
        candidate = _deep_copy(self._data)
        mutator(candidate)
        violations = field_guard_violations(self._data, candidate, guards=guards)
        if violations:
            raise FieldGuardError(self.entity, violations)
        return self._apply(lambda data: data.update(candidate))

    @property
    def conflicts(self) -> list[dict[str, Any]]:
        """Conflicts this record has seen, newest last (diagnostics only)."""
        with self._lock:
            return [dict(item) for item in self._conflicts]

    def rollback(
        self, expected_version: int, target_version: int
    ) -> dict[str, Any]:
        """Restore the body captured at ``target_version`` under a CAS guard."""
        with self._lock:
            if expected_version != self._version:
                raise StaleVersionError(self.entity, expected_version, self._version)
            for snapshot in reversed(self._history):
                if snapshot["version"] == target_version:
                    restored = dict(snapshot["data"])
                    return self._apply(lambda _data: _data.update(restored))
            raise ValueError(f"no history entry at version {target_version} for {self.entity}")

    def _apply(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Mutate in place. Callers must already hold ``self._lock``."""
        self._remember()
        mutator(self._data)
        self._version += 1
        return dict(self._data)

    @contextmanager
    def pessimistic(self) -> Iterator[dict[str, Any]]:
        """Hold the record's mutex and mutate without a version check.

        The escape hatch for "apply this no matter what" — a late write that
        still cannot interleave with another writer mid-mutation.
        """
        with self._lock:
            yield self._data


def compare_and_swap(
    record: VersionedRecord,
    expected_version: int,
    new_values: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``new_values`` into the record under a CAS guard. Returns new data."""
    return record.update(
        expected_version,
        lambda data: data.update(new_values),
    )


def update_with_retry(
    record: VersionedRecord,
    mutator: Callable[[dict[str, Any]], None],
    *,
    attempts: int = 3,
    delay_seconds: float = 0.0,
) -> tuple[int, dict[str, Any]]:
    """Re-read, re-apply, retry. For mutations that are safe to repeat.

    ``mutator`` receives the *fresh* body on each attempt, so it must be
    written in terms of intent ("set ``enabled`` to True") rather than of a
    captured snapshot ("set ``enabled`` to whatever I read earlier").

    Returns ``(version, data)``; raises the last :class:`StaleVersionError`
    when every attempt is exhausted.
    """
    last_error: Optional[StaleVersionError] = None
    for attempt in range(1, max(attempts, 1) + 1):
        version, _data = record.read()
        try:
            data = record.update(version, mutator)
            return record.version, data
        except StaleVersionError as exc:
            last_error = exc
            if attempt < attempts and delay_seconds:
                time.sleep(delay_seconds * attempt)
    assert last_error is not None
    raise last_error


def update_serialized(
    record: VersionedRecord,
    mutator: Callable[[dict[str, Any]], None],
    *,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Try the CAS; on conflict take the pessimistic lock and apply anyway.

    For mutations that must not be dropped (e.g. an audit append) and where a
    bounded retry is worse than briefly blocking the winner. The version still
    advances, so a subsequent CAS reader still sees the change.
    """
    if expected_version is not None:
        try:
            return record.update(expected_version, mutator)
        except StaleVersionError:
            pass
    # The context manager is the point; its yielded value was never read.
    with record.pessimistic():
        return record._apply(mutator)


class LockSet:
    """Ordered multi-record acquisition, so multi-entity writes cannot deadlock.

    Two transactions touching ``{a, b}`` and ``{b, a}`` in different orders is
    the classic deadlock. Sorting the keys by entity name makes acquisition
    order globally consistent, so one transaction always waits for the other
    rather than each holding what the other needs.
    """

    def __init__(self, records: Iterable[VersionedRecord]):
        self._records = sorted(records, key=lambda r: r.entity)
        self._names = [r.entity for r in self._records]

    @property
    def entities(self) -> list[str]:
        return list(self._names)

    @contextmanager
    def acquire(self) -> Iterator[dict[str, Any]]:
        """Acquire every record in name order and yield them by entity name."""
        acquired: list[VersionedRecord] = []
        try:
            for record in self._records:
                record._lock.acquire()
                acquired.append(record)
            yield {record.entity: record for record in self._records}
        finally:
            for record in reversed(acquired):
                record._lock.release()

    @contextmanager
    def acquire_until(self, timeout: float) -> Iterator[Optional[dict[str, VersionedRecord]]]:
        """Bounded variant of :meth:`acquire`.

        Yields ``None`` instead of blocking forever when the deadline passes, so
        a caller behind a stuck writer gets a decision it can act on rather than
        a thread parked until the process is restarted. Partial acquisition is
        always released before yielding.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        acquired: list[VersionedRecord] = []
        for record in self._records:
            remaining = deadline - time.monotonic()
            if not record._lock.acquire(timeout=max(0.0, remaining)):
                for held in reversed(acquired):
                    held._lock.release()
                yield None
                return
            acquired.append(record)
        try:
            yield {record.entity: record for record in self._records}
        finally:
            for record in reversed(acquired):
                record._lock.release()

    def commit(self, mutations: dict[str, Callable[[dict[str, Any]], None]]) -> dict[str, dict[str, Any]]:
        """Apply one mutator per record, all-or-nothing, in ordered lock scope.

        A raising mutator restores the prior body *and* version of every record
        already applied, so a partial multi-entity write cannot survive.
        """
        with self.acquire():
            results: dict[str, dict[str, Any]] = {}
            undo: list[tuple[VersionedRecord, dict[str, Any], int]] = []
            try:
                for record in self._records:
                    mutator = mutations.get(record.entity)
                    if mutator is None:
                        continue
                    undo.append((record, dict(record._data), record._version))
                    results[record.entity] = record._apply(mutator)
                return results
            except Exception:
                for record, prior_data, prior_version in reversed(undo):
                    record._data = prior_data
                    record._version = prior_version
                    if record._history:
                        record._history.pop()
                raise


class VersionedStore:
    """Keyed registry of versioned records, with history and rollback.

    Callers normally want "load ``risk_rules`` for tenant ``acme``" rather
    than a retained object reference, so the store owns the records and hands
    out copies. Keys are ``(scope, entity)``: ``scope`` is typically a tenant
    id, which keeps two tenants' identical entity names from colliding.
    """

    def __init__(self, history_depth: int = DEFAULT_HISTORY_DEPTH):
        self.history_depth = history_depth
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], VersionedRecord] = {}

    @staticmethod
    def key(entity: str, scope: str = "default") -> tuple[str, str]:
        return (scope, entity)

    def register(
        self, entity: str, data: dict[str, Any] | None = None, *, scope: str = "default"
    ) -> VersionedRecord:
        """Create the record if absent, and return it. Idempotent."""
        with self._lock:
            key = self.key(entity, scope)
            if key not in self._records:
                self._records[key] = VersionedRecord(
                    entity=entity,
                    data=data,
                    history_depth=self.history_depth,
                )
            return self._records[key]

    def get(self, entity: str, *, scope: str = "default") -> Optional[VersionedRecord]:
        with self._lock:
            return self._records.get(self.key(entity, scope))

    def read(self, entity: str, *, scope: str = "default") -> tuple[int, dict[str, Any]]:
        """``(version, data)`` for a key, registering an empty record if unseen."""
        return self.register(entity, scope=scope).read()

    def update(
        self, entity: str, expected_version: int, mutator: Callable[[dict[str, Any]], None], *, scope: str = "default"
    ) -> dict[str, Any]:
        return self.register(entity, scope=scope).update(expected_version, mutator)

    def patch(
        self, entity: str, expected_version: int, patch: dict[str, Any], *, scope: str = "default", base: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.register(entity, scope=scope).patch(expected_version, patch, base=base)

    def list_entities(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "entity": record.entity,
                    "scope": key_scope,
                    "version": record.version,
                    "keys": sorted(record.read()[1]),
                }
                for (key_scope, _entity), record in sorted(self._records.items())
                if scope is None or key_scope == scope
            ]

    def conflicts(self, *, scope: str | None = None) -> dict[str, Any]:
        """Conflict telemetry across the store: where and on what.

        Aggregated rather than raw, because the actionable question is "which
        entity is contended and on which leaves", not "show me every event".
        """
        with self._lock:
            by_entity: dict[str, dict[str, Any]] = {}
            total = 0
            for (key_scope, entity), record in sorted(self._records.items()):
                if scope is not None and key_scope != scope:
                    continue
                for item in record.conflicts:
                    total += 1
                    entry = by_entity.setdefault(
                        entity,
                        {"entity": entity, "scope": key_scope, "count": 0, "strategies": {}, "paths": {}},
                    )
                    entry["count"] += 1
                    strategy = str(item.get("strategy", "?"))
                    entry["strategies"][strategy] = entry["strategies"].get(strategy, 0) + 1
                    for path in item.get("conflict_paths", []):
                        entry["paths"][path] = entry["paths"].get(path, 0) + 1
            for entry in by_entity.values():
                entry["strategies"] = dict(sorted(entry["strategies"].items()))
                entry["top_paths"] = [
                    path for path, _ in sorted(entry["paths"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
                ]
            return {
                "total": total,
                "by_entity": sorted(by_entity.values(), key=lambda item: (-item["count"], item["entity"])),
                "hot_entities": [item["entity"] for item in sorted(by_entity.values(), key=lambda i: (-i["count"], i["entity"]))[:3]],
            }

    def merge(
        self,
        entity: str,
        expected_version: int,
        patch: dict[str, Any],
        *,
        scope: str = "default",
        base: dict[str, Any] | None = None,
        strategy: str | None = None,
        guards: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Store-level :meth:`VersionedRecord.merge` (registers the key if unseen)."""
        return self.register(entity, scope=scope).merge(
            expected_version, patch, base=base, strategy=strategy, guards=guards
        )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            scopes: dict[str, int] = {}
            for key_scope, _ in self._records:
                scopes[key_scope] = scopes.get(key_scope, 0) + 1
            return {
                "records": len(self._records),
                "scopes": dict(sorted(scopes.items())),
                "history_depth": self.history_depth,
            }

    def reset(self) -> None:
        with self._lock:
            self._records.clear()


def build_optimistic_locking_catalog() -> dict[str, Any]:
    """Introspectable contract for the concurrency guard (meta tooling)."""
    return {
        "strategy": "optimistic-concurrency",
        "mechanism": "compare-and-swap with per-record version stamp",
        "conflict_policy": {
            "mode": "error",
            "exception": "StaleVersionError",
            "expected_status": 409,
            "reports_conflicting_fields": True,
        },
        "default_start_version": DEFAULT_START_VERSION,
        "mutators": {
            "compare_and_swap": "merge fields under CAS",
            "patch": "RFC 7396 JSON merge-patch under CAS",
            "update_with_retry": "re-read and replay; for idempotent mutations",
            "update_serialized": "pessimistic fallback; for must-not-lose mutations",
            "rollback": "restore a prior version body under CAS",
            "LockSet.commit": "ordered multi-record commit, all-or-nothing",
        },
        "deadlock_avoidance": "LockSet acquires records in entity-name order",
        "registry": {
            "key": "(scope, entity)",
            "scope_semantics": "tenant id; isolates identical entity names",
            "history_depth": DEFAULT_HISTORY_DEPTH,
        },
        "conflict_strategies": {
            "table": [dict(entry) for entry in CONFLICT_STRATEGIES],
            "default": DEFAULT_CONFLICT_STRATEGY,
            "policy": (
                "update()/patch() keep raising StaleVersionError; the strategies are reached "
                "through merge()/merge_with_strategy, or by an explicit strategy= argument."
            ),
            "helpers": ["resolve_conflict", "three_way_merge", "merge_with_strategy"],
        },
        "conflict_gates": {
            "table": [dict(entry) for entry in CONFLICT_GATES],
            "routing": "longest matching entity_prefix wins; the '' row is the catch-all",
            "helper": "gate_for",
        },
        "three_way_merge": {
            "inputs": ["base (what both sides last read)", "mine (caller proposal)", "theirs (winner)"],
            "granularity": "recursive to the leaf",
            "lists": "compared whole; a structural list merge is deliberately not attempted",
            "on_conflict": "the winner's value is kept and the divergence is reported",
            "deletion": "absent vs null are distinguished; a one-sided delete is honoured",
            "helpers": ["three_way_merge", "deep_diff", "diff_paths"],
        },
        "diff_depth": {
            "default": DEFAULT_DIFF_DEPTH,
            "shallow": "diff_fields (top-level keys, unchanged; used for the 409 body)",
            "deep": "deep_diff / diff_paths (dotted leaf paths)",
        },
        "field_guards": {
            "table": [dict(entry) for entry in FIELD_GUARDS],
            "checked": "before the apply, against a candidate copy",
            "on_violation": "FieldGuardError (422-shaped); nothing is written and the version does not move",
            "note": "a guard violation is not a conflict, so it must not be reported as 409",
            "helper": "field_guard_violations",
        },
        "bounded_wait": {
            "helper": "LockSet.acquire_until(timeout)",
            "yields": "None on timeout after releasing any partial acquisition",
            "why": "an unbounded acquire can park a request thread until restart",
        },
        "telemetry": {
            "helper": "VersionedStore.conflicts(scope=None) / VersionedRecord.conflicts",
            "shape": "counts per entity, per strategy, and the hottest conflicting leaf paths",
        },
    }


def merge_with_strategy(
    record: "VersionedRecord",
    expected_version: int,
    patch: dict[str, Any],
    *,
    base: dict[str, Any] | None = None,
    strategy: str | None = None,
    guards: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Functional form of :meth:`VersionedRecord.merge` (no store needed)."""
    return record.merge(
        expected_version, patch, base=base, strategy=strategy, guards=guards
    )
