"""Optimistic concurrency for mutable decision/config entities.

The decision-intelligence modules mutate registered state — model version
promotions, rule weights, policy tables. A plain read-modify-write silently
loses a concurrent update; the last writer clobbers the winner. This module
adds a tiny version-stamped guard so a stale write is rejected with a clear
``StaleVersionError`` instead of silently overwriting.

Design (deliberately minimal):

- ``VersionedRecord`` — the lockable unit: a dict of data + an integer version.
  ``update`` is compare-and-swap: it only applies the mutator when the caller's
  ``expected_version`` still matches, then bumps the version. A per-record
  re-entrant lock keeps concurrent writers from corrupting the record, so the
  last *successful* writer wins and every loser gets a deterministic conflict.
- ``compare_and_swap`` — convenience for the common "merge these fields" case.
- ``StaleVersionError`` — carries entity + expected/current versions so callers
  (or API layers) can map it to a 409 Conflict instead of guessing.

There is deliberately no global state here: callers own their records, which
keeps the utility testable and dependency-free.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

DEFAULT_START_VERSION = 1


class StaleVersionError(Exception):
    """Raised when a mutation targets a version that is no longer current."""

    def __init__(self, entity: str, expected: int, current: int):
        self.entity = entity
        self.expected = expected
        self.current = current
        super().__init__(
            f"stale write on {entity}: expected version {expected}, current {current}"
        )


class VersionedRecord:
    """A dict-backed mutable record with an immutable version stamp (CAS target)."""

    def __init__(
        self,
        entity: str,
        data: dict[str, Any] | None = None,
        version: int = DEFAULT_START_VERSION,
    ):
        self.entity = entity
        self._data: dict[str, Any] = dict(data or {})
        self._version = int(version)
        self._lock = threading.RLock()

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
                raise StaleVersionError(
                    self.entity, expected_version, self._version
                )
            mutator(self._data)
            self._version += 1
            return dict(self._data)


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


def build_optimistic_locking_catalog() -> dict[str, Any]:
    """Introspectable contract for the concurrency guard (meta tooling)."""
    return {
        "strategy": "optimistic-concurrency",
        "mechanism": "compare-and-swap with per-record version stamp",
        "conflict_policy": {
            "mode": "error",
            "exception": "StaleVersionError",
            "expected_status": 409,
        },
        "default_start_version": DEFAULT_START_VERSION,
    }