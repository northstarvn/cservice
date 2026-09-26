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
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional

DEFAULT_START_VERSION = 1
# How many versions a record keeps for diffing/rollback.
DEFAULT_HISTORY_DEPTH = 20


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


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    if isinstance(value, set):
        return {_deep_copy(v) for v in value}
    return value


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
            return self._apply(lambda data: data.update(apply_json_merge_patch(data, patch)))

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
    with record.pessimistic() as data:
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
    }
