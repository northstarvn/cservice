"""Database engine, session, and resilience plumbing.

The historical surface is small and load-bearing: ``engine``, ``SessionLocal``,
``Base``, ``get_db``, ``ping_database``, ``retry_async``. Every other module
imports those names, so they are preserved exactly.

What the original module could not answer is what happens when the database is
*misbehaving* rather than merely absent. A service that only distinguishes
"connected" from "not connected" will happily hammer a connection-limited
database, hold a transaction open for the length of a slow request, and retry
a non-idempotent write three times. This module adds the pieces a real
deployment needs, all env-tunable and all off-the-shelf safe:

- ``RetryPolicy`` + ``retry_async`` — classified retry with exponential
  backoff, jitter, and an explicit *non-retryable* set. A serialization
  failure is retryable; a constraint violation is not, and retrying it just
  burns the error budget.
- ``CircuitBreaker`` — after a run of failures, fail fast instead of piling
  more load onto a database that is already down. Half-open probing lets it
  recover without a manual reset.
- ``db_health`` / ``readiness_probe`` — a liveness answer (is the process up)
  and a readiness answer (can we serve traffic), with the pool numbers that
  explain a "not ready".
- ``advisory_lock`` — a transaction-scoped Postgres advisory lock, so two
  workers cannot run the same partition/recovery cycle concurrently.
- ``transaction`` — a unit-of-work wrapper that commits on success and rolls
  back on any exception, so callers stop hand-rolling the try/except.
- ``statement_timeout`` / ``lock_timeout`` — bound how long a single request
  can hold a connection, which is the difference between a slow request and a
  connection-exhaustion outage.

The resilience layer above answers "is the database up?". It cannot answer the
questions that actually get asked once a database is up but misbehaving: *which
query is slow, did I send a write to a read replica, does the live schema still
match the models, what would change if I retuned a threshold*. A module that
only has ``SELECT 1`` makes every one of those a one-off ``psql`` session in
someone's shell history, which is to say: unanswerable at 3am. So the second
layer here is instrumentation and diagnostics, all of it config-driven, all of it
degrading to a reported reason instead of an exception:

- ``normalize_sql`` / ``fingerprint_statement`` / ``QueryRecorder`` — collapse
  literals, bind parameters, and ``IN`` lists so that a thousand executions of
  the same statement aggregate into one row, then band that row by duration.
  Without fingerprinting, "the API is slow" has no cause attached to it; with it,
  the cause is one entry in a bounded table.
- ``classify_statement`` — a read/write/ddl/transaction verdict per statement.
  This is the load-bearing input to the split below, and it resolves
  data-modifying CTEs rather than trusting the leading verb, because
  ``WITH x AS (DELETE ...) SELECT * FROM x`` starts with ``WITH``.
- ``guarded_call`` — retry *and* circuit-break in one call, with an explicit
  idempotence flag so a non-idempotent write is never replayed by the resilience
  machinery that is trying to help.
- ``read_session`` / ``get_read_engine`` / ``read_only`` — an optional
  read-replica split that degrades to the primary when ``DB_READ_URL`` is unset,
  so nothing has to know whether a replica exists.
- ``savepoint`` / ``bulk_load`` — the two write shapes that are still
  hand-rolled everywhere: a recoverable step inside a larger unit of work, and a
  chunked insert that will not pin a pooled connection for its whole duration.
- ``explain_statement`` — with ``EXPLAIN ANALYZE`` gated, because that variant
  *executes* the statement it explains.
- ``connection_diagnostics`` / ``schema_drift_report`` — one round-trip each,
  answered as structured findings, and both never raise.
- ``validate_db_ops_policy`` / ``simulate_db_ops`` / ``build_db_ops_catalog`` —
  the config is checkable, retunable by simulation, and introspectable, on the
  same terms as the service-layer policy tables.
"""
import asyncio
import hashlib
import logging
import os
import random
import re
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, AsyncIterator, Callable, Iterable, Optional

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Load environment variables from a .env file if present
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/cservice")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# Pool tuning is env-driven so operators can scale without code changes.
DB_POOL_SIZE = _int_env("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _int_env("DB_MAX_OVERFLOW", 10)
DB_POOL_TIMEOUT = _int_env("DB_POOL_TIMEOUT", 30)
# echo stays True by default to preserve the historical behavior.
DB_ECHO = _bool_env("DB_ECHO", True)
# Per-statement ceiling so one slow query cannot pin a pooled connection.
DB_STATEMENT_TIMEOUT_MS = _int_env("DB_STATEMENT_TIMEOUT_MS", 0)
DB_LOCK_TIMEOUT_MS = _int_env("DB_LOCK_TIMEOUT_MS", 0)

logger = logging.getLogger(__name__)

engine = create_async_engine(
    DATABASE_URL,
    echo=DB_ECHO,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
)
SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def ping_database(_engine=engine) -> bool:
    """Return True when a SELECT 1 succeeds; never raises."""
    try:
        async with _engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        logging.info("Database connection successful!")
        return True
    except Exception as e:
        logging.error(f"Database connection failed: {e}")
        return False


# Test connection function
async def test_connection():
    return await ping_database()


# --- Retry with classification, backoff, and jitter ---------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry, how long to wait, and what is worth retrying.

    ``retry_on``/``give_up_on`` are matched by exception *type name* rather
    than by importing driver exception classes, so this module stays free of a
    hard asyncpg dependency and still classifies correctly when one is present.
    """

    attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    multiplier: float = 2.0
    jitter: float = 0.1
    give_up_on: frozenset[str] = frozenset(
        {
            # Programming errors: retrying cannot fix them.
            "IntegrityError",
            "ProgrammingError",
            "NotNullViolation",
            "ForeignKeyViolation",
            "UniqueViolation",
            "CheckViolation",
            "InvalidRequestError",
        }
    )
    retry_on: frozenset[str] = frozenset(
        {
            # Transient infrastructure conditions.
            "OperationalError",
            "InterfaceError",
            "DBAPIError",
            "ConnectionError",
            "ConnectionResetError",
            "ConnectionRefusedError",
            "ConnectionDoesNotExist",
            "TimeoutError",
            "SerializationFailure",
            "DeadlockDetected",
            "CannotConnectNowError",
            "TooManyConnectionsError",
            "PoolTimeout",
        }
    )

    def is_retryable(self, exc: BaseException) -> bool:
        names = {type(exc).__name__}
        names.update(base.__name__ for base in type(exc).__mro__)
        if names & set(self.give_up_on):
            return False
        return bool(names & set(self.retry_on)) or not self.retry_on

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Exponential backoff for ``attempt`` (1-based), clamped and jittered."""
        raw = self.base_delay_seconds * (self.multiplier ** max(attempt - 1, 0))
        capped = min(raw, self.max_delay_seconds)
        if not self.jitter:
            return capped
        spread = capped * self.jitter
        source = rng or random
        return max(0.0, capped + source.uniform(-spread, spread))


DEFAULT_RETRY_POLICY = RetryPolicy()


async def retry_async(
    coro_factory,
    attempts: int = 3,
    delay_seconds: float = 0.5,
    logger: logging.Logger = None,
    *,
    policy: RetryPolicy | None = None,
):
    """Re-invoke ``coro_factory()`` up to ``attempts`` times on failure.

    Useful for startup probes and idempotent recovery paths where a transient
    connection failure should not abort the whole request. A ``raise`` from a
    retried call is still propagated once the attempts are exhausted.

    Without ``policy`` this is the original behavior exactly: retry *any*
    exception, sleep a flat ``delay_seconds``, re-raise the last one. Pass a
    :class:`RetryPolicy` (or ``DEFAULT_RETRY_POLICY``) to opt into
    classification, exponential backoff, and jitter.
    """
    logger = logger or logging.getLogger(__name__)
    if policy is None:
        # Legacy mode: an empty retry_on means "nothing is excluded".
        effective = RetryPolicy(
            attempts=attempts,
            base_delay_seconds=delay_seconds,
            jitter=0.0,
            retry_on=frozenset(),
            give_up_on=frozenset(),
        )
    else:
        effective = policy
    attempts = max(int(effective.attempts), 1)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if not effective.is_retryable(exc):
                logger.warning("Non-retryable failure on attempt %d/%d: %s", attempt, attempts, exc)
                raise
            if attempt < attempts:
                delay = effective.delay_for(attempt)
                if delay:
                    logger.warning(
                        "Retryable operation failed (attempt %d/%d, retry in %.3fs): %s",
                        attempt,
                        attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "Retryable operation failed (attempt %d/%d): %s", attempt, attempts, exc
                    )
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_async called with attempts <= 0")


# --- Circuit breaker -----------------------------------------------------------

CIRCUIT_STATES = ("closed", "open", "half_open")


@dataclass
class CircuitBreaker:
    """Fail-fast guard so a down database is not hammered.

    ``closed``    — traffic flows; consecutive failures are counted.
    ``open``      — calls raise immediately until ``recovery_timeout`` elapses.
    ``half_open`` — a limited number of probe calls are admitted; a success
                    closes the circuit, a failure re-opens it.
    """

    name: str = "database"
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_probes: int = 1
    state: str = "closed"
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    _lock: Any = field(default_factory=asyncio.Lock, repr=False)

    def _now(self) -> float:
        return time.monotonic()

    def allows(self) -> bool:
        """Whether a call may proceed right now."""
        if self.state == "closed":
            return True
        if self.state == "open":
            if (
                self.opened_at is not None
                and self._now() - self.opened_at >= self.recovery_timeout_seconds
            ):
                self.state = "half_open"
                self.success_count = 0
                return True
            return False
        return self.success_count < self.half_open_probes

    def record_success(self) -> None:
        if self.state == "half_open":
            self.success_count += 1
            if self.success_count >= self.half_open_probes:
                self.state = "closed"
                self.failure_count = 0
                self.opened_at = None
        else:
            self.failure_count = 0

    def record_failure(self) -> None:
        if self.state == "half_open":
            self._open()
            return
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self.state = "open"
        self.opened_at = self._now()
        self.success_count = 0

    def reset(self) -> None:
        self.state = "closed"
        self.failure_count = 0
        self.success_count = 0
        self.opened_at = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "success_count": self.success_count,
            "recovery_timeout_seconds": self.recovery_timeout_seconds,
            "opened_at": self.opened_at,
        }


class CircuitOpenError(RuntimeError):
    """Raised instead of calling a dependency whose circuit is open."""


DATABASE_CIRCUIT = CircuitBreaker(
    name="database",
    failure_threshold=_int_env("DB_CIRCUIT_FAILURE_THRESHOLD", 5),
    recovery_timeout_seconds=_float_env("DB_CIRCUIT_RECOVERY_SECONDS", 30.0),
)


def get_database_circuit() -> CircuitBreaker:
    return DATABASE_CIRCUIT


# --- Transaction / locking helpers --------------------------------------------


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Unit-of-work: commit on success, roll back on any exception.

    Centralising this is what lets callers stop writing
    ``try/except/rollback/raise`` at every call site — and stops them
    forgetting it once, which is how a half-applied write reaches production.
    """
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise


@asynccontextmanager
async def advisory_lock(session: AsyncSession, lock_id: int, *, timeout_ms: int = 5000) -> AsyncIterator[bool]:
    """Transaction-scoped Postgres advisory lock.

    Yields True when the lock was taken, False when it was not. It never
    blocks — a non-blocking try is the point: the loser of a race exits
    quietly instead of duplicating the run. Use it to make singleton
    background work (partition cycles, recovery sweeps, canary promotion)
    safe to run on N workers.

    Scope note: ``pg_try_advisory_xact_lock`` releases when the caller's
    *transaction* ends, so this helper is a no-op on release. Wrap it in
    :func:`transaction` when the lock should cover a unit of work. A
    non-Postgres backend yields True without locking, so callers need no
    dialect branch.
    """
    acquired = True
    try:
        got = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"), {"lock_id": int(lock_id)}
        )
        acquired = bool(got.scalar())
    except Exception as exc:  # unsupported dialect / driver
        logger.debug("advisory_lock unavailable (%s); proceeding unlocked", exc)
        acquired = True
    yield acquired


@asynccontextmanager
async def statement_timeouts(
    session: AsyncSession,
    *,
    statement_ms: int | None = None,
    lock_ms: int | None = None,
) -> AsyncIterator[AsyncSession]:
    """Bound how long statements and lock waits may take on this session.

    Both timeouts default to 0 (disabled) so behavior is unchanged unless an
    operator sets them.
    """
    statement = DB_STATEMENT_TIMEOUT_MS if statement_ms is None else statement_ms
    lock = DB_LOCK_TIMEOUT_MS if lock_ms is None else lock_ms
    applied = False
    if statement or lock:
        try:
            await session.execute(text(f"SET LOCAL statement_timeout = {int(statement)}"))
            if lock:
                await session.execute(text(f"SET LOCAL lock_timeout = {int(lock)}"))
            applied = True
        except Exception as exc:
            logger.debug("statement_timeouts unavailable (%s)", exc)
    try:
        yield session
    finally:
        if applied:
            try:
                await session.execute(text("SET LOCAL statement_timeout = DEFAULT"))
                await session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
            except Exception:
                pass


# --- Observability -------------------------------------------------------------


def pool_status(_engine=engine) -> dict[str, Any]:
    """Pool saturation snapshot. Never raises — an unknown pool reports None."""
    try:
        pool = _engine.pool
    except Exception:
        return {"available": False, "reason": "pool not initialized"}
    def _safe(getter: str) -> Any:
        try:
            return getattr(pool, getter)()
        except Exception:
            return None
    checked_out = _safe("checkedout")
    size = _safe("size")
    overflow = _safe("overflow")
    return {
        "available": True,
        "size": size,
        "checked_out": checked_out,
        "overflow": overflow,
        "in_use": _safe("checkedin"),
        "max_overflow_configured": DB_MAX_OVERFLOW,
        "pool_size_configured": DB_POOL_SIZE,
        "saturation": (
            round(checked_out / size, 4) if isinstance(checked_out, int) and isinstance(size, int) and size else None
        ),
    }


async def db_health(_engine=engine, *, include_pool: bool = True) -> dict[str, Any]:
    """Structured reachability + pool answer. Never raises."""
    started = time.monotonic()
    reachable = await ping_database(_engine)
    payload: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "reachable": reachable,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "circuit": DATABASE_CIRCUIT.snapshot(),
    }
    if include_pool:
        payload["pool"] = pool_status(_engine)
    return payload


async def readiness_probe(_engine=engine) -> dict[str, Any]:
    """Can we serve traffic right now?

    Readiness is stricter than liveness: an open circuit means "stop sending
    me traffic" even though the process is perfectly alive.
    """
    health = await db_health(_engine)
    circuit_open = health["circuit"]["state"] == "open"
    ready = bool(health["reachable"]) and not circuit_open
    reasons: list[str] = []
    if not health["reachable"]:
        reasons.append("database_unreachable")
    if circuit_open:
        reasons.append("circuit_open")
    return {
        "ready": ready,
        "live": True,
        "reasons": reasons,
        "checked_at": health["checked_at"],
        "latency_ms": health["latency_ms"],
        "circuit": health["circuit"]["state"],
    }


def build_db_catalog() -> dict[str, Any]:
    """Introspectable catalog of the database/resilience surface."""
    return {
        "dialect": DATABASE_URL.split("://", 1)[0],
        "pool": {
            "size": DB_POOL_SIZE,
            "max_overflow": DB_MAX_OVERFLOW,
            "timeout_seconds": DB_POOL_TIMEOUT,
            "echo": DB_ECHO,
            "current": pool_status(),
        },
        "timeouts_ms": {
            "statement_timeout": DB_STATEMENT_TIMEOUT_MS,
            "lock_timeout": DB_LOCK_TIMEOUT_MS,
        },
        "retry": {
            "policy": {
                "attempts": DEFAULT_RETRY_POLICY.attempts,
                "base_delay_seconds": DEFAULT_RETRY_POLICY.base_delay_seconds,
                "max_delay_seconds": DEFAULT_RETRY_POLICY.max_delay_seconds,
                "multiplier": DEFAULT_RETRY_POLICY.multiplier,
                "jitter": DEFAULT_RETRY_POLICY.jitter,
            },
            "classified": True,
            "give_up_on": sorted(DEFAULT_RETRY_POLICY.give_up_on),
            "retry_on": sorted(DEFAULT_RETRY_POLICY.retry_on),
        },
        "circuit_breaker": DATABASE_CIRCUIT.snapshot() | {
            "states": list(CIRCUIT_STATES),
            "error": "CircuitOpenError",
        },
        "helpers": {
            "transaction": "unit-of-work: commit on success, rollback on error",
            "advisory_lock": "pg_try_advisory_xact_lock; no-op on non-Postgres",
            "statement_timeouts": "SET LOCAL statement_timeout / lock_timeout",
            "get_db": "FastAPI session dependency",
        },
    }


# ==============================================================================
# Layer two: instrumentation, routing, and diagnostics
# ==============================================================================
#
# Same convention as the service-layer policy tables, and for the same reason: a
# threshold or a routing rule that lives inside a branch is a threshold nobody
# can find, retune, or test. Everything here is a table plus one resolver, and
# every default reproduces the behaviour the module already had.
#
# Band convention: ordered highest threshold first, first match wins, and a value
# below every ``min_ms`` takes the declared default.
#
# Deliberate duplication: ``resolve_slow_bands`` and ``classify_duration`` below
# reimplement the band fold that ``services.retention_snapshots.resolve_band``
# already provides. Importing it from here would be an import cycle (that module
# imports ``app.models``, which imports this one), and a shared private helper in
# a third module would be a new top-level code-map node for one five-line
# function. The duplication is the cheaper side of that trade, exactly as the
# audit-gate ops are declared twice rather than crossing the models/services
# boundary.

# --- Config tables -------------------------------------------------------------

# Statement verb -> query class. The first matching leading verb wins, so this is
# a prefix table and not a lookup by parsed statement type.
#
# ``mutating`` is tri-state on purpose:
#   False -> known not to write
#   True  -> known to write
#   None  -> depends on the body ("WITH"), resolved by scanning the CTE
# An unresolved ``mutating`` routes to the primary, never to a replica. Failing
# open toward the writer is the only safe direction for an unknown statement.
QUERY_VERB_RULES: list[dict[str, object]] = [
    {"verb": "WITH", "query_class": "read", "mutating": None, "resolves_body": True},
    {"verb": "SELECT", "query_class": "read", "mutating": False, "resolves_body": False},
    {"verb": "TABLE", "query_class": "read", "mutating": False, "resolves_body": False},
    {"verb": "VALUES", "query_class": "read", "mutating": False, "resolves_body": False},
    {"verb": "EXPLAIN", "query_class": "explain", "mutating": False, "resolves_body": False},
    {"verb": "SHOW", "query_class": "admin", "mutating": False, "resolves_body": False},
    {"verb": "SET", "query_class": "session", "mutating": False, "resolves_body": False},
    {"verb": "RESET", "query_class": "session", "mutating": False, "resolves_body": False},
    {"verb": "BEGIN", "query_class": "transaction", "mutating": False, "resolves_body": False},
    {"verb": "COMMIT", "query_class": "transaction", "mutating": False, "resolves_body": False},
    {"verb": "ROLLBACK", "query_class": "transaction", "mutating": False, "resolves_body": False},
    {"verb": "INSERT", "query_class": "write", "mutating": True, "resolves_body": False},
    {"verb": "UPDATE", "query_class": "write", "mutating": True, "resolves_body": False},
    {"verb": "DELETE", "query_class": "write", "mutating": True, "resolves_body": False},
    {"verb": "MERGE", "query_class": "write", "mutating": True, "resolves_body": False},
    {"verb": "CREATE", "query_class": "ddl", "mutating": True, "resolves_body": False},
    {"verb": "ALTER", "query_class": "ddl", "mutating": True, "resolves_body": False},
    {"verb": "DROP", "query_class": "ddl", "mutating": True, "resolves_body": False},
    {"verb": "TRUNCATE", "query_class": "ddl", "mutating": True, "resolves_body": False},
]
QUERY_VERBS: tuple[str, ...] = tuple(str(rule["verb"]) for rule in QUERY_VERB_RULES)
QUERY_CLASSES: tuple[str, ...] = (
    "read",
    "write",
    "ddl",
    "transaction",
    "session",
    "admin",
    "explain",
    "other",
)
# An unrecognised statement is neither known-safe nor known-writing.
QUERY_VERB_FALLBACK: dict[str, object] = {
    "verb": "UNKNOWN",
    "query_class": "other",
    "mutating": None,
    "resolves_body": False,
}
# Derived rather than declared: a verb that mutates is exactly the set a
# data-modifying CTE can hide.
MUTATING_VERBS: tuple[str, ...] = tuple(
    str(rule["verb"]) for rule in QUERY_VERB_RULES if rule["mutating"] is True
)

# Where each query class may run. "read" is the only class eligible for a
# replica, and "explain" is deliberately not: EXPLAIN ANALYZE executes.
SPLIT_TARGET_BY_QUERY_CLASS: dict[str, str] = {
    "read": "replica",
    "write": "primary",
    "ddl": "primary",
    "transaction": "primary",
    "session": "primary",
    "admin": "primary",
    "explain": "primary",
    "other": "primary",
}
SPLIT_TARGETS: tuple[str, ...] = ("primary", "replica")

# Duration bands. Each row names the tunable that supplies its threshold rather
# than repeating the number: a duplicated literal is a literal that drifts, and
# the band table and the tunable table would then disagree about the same knob.
SLOW_QUERY_BANDS: list[dict[str, object]] = [
    {"severity": "critical", "tunable": "slow_query_critical_ms", "advice": "investigate first; this is an availability risk, not a style question"},
    {"severity": "slow", "tunable": "slow_query_slow_ms", "advice": "check the plan and add the covering index this query is missing"},
    {"severity": "watch", "tunable": "slow_query_watch_ms", "advice": "cheap to fix now, expensive to find later"},
]
SLOW_QUERY_DEFAULT = "fast"
QUERY_SEVERITIES: tuple[str, ...] = ("fast", "watch", "slow", "critical")

# The masking pipeline that turns a statement into a stable fingerprint. Order is
# load-bearing and is asserted by ``validate_db_ops_policy``:
#   * bind parameters go before numbers, or ``$1`` normalizes to ``$?`` and the
#     placeholder rule can no longer match it;
#   * comments go first, or a commented-out number survives as a distinct value;
#   * the IN-list collapse goes after both, which is what makes a 5000-element
#     WHERE IN collapse to the same fingerprint as a 2-element one.
SQL_NORMALIZATION_RULES: list[dict[str, object]] = [
    {"id": "strip_comments", "pattern": r"--[^\n]*|/\*.*?\*/", "replacement": " "},
    {"id": "mask_quoted", "pattern": r"'(?:[^']|'')*'", "replacement": "?"},
    {"id": "mask_bindparams", "pattern": r"%\(\w+\)[sd]|%\(\w+\)|\:\w+|\$\d+", "replacement": "?"},
    {"id": "mask_in_list", "pattern": r"\bin\s*\([^)]*\)", "replacement": "in (?)"},
    {"id": "mask_numbers", "pattern": r"\b\d+(?:\.\d+)?\b", "replacement": "?"},
    {"id": "collapse_whitespace", "pattern": r"\s+", "replacement": " "},
]

# EXPLAIN options, and the subset that actually *runs* the statement.
EXPLAIN_OPTIONS: dict[str, str] = {
    "costs": "COSTS",
    "verbose": "VERBOSE",
    "buffers": "BUFFERS",
    "settings": "SETTINGS",
    "wal": "WAL",
    "timing": "TIMING",
    "summary": "SUMMARY",
    "analyze": "ANALYZE",
    "format_json": "FORMAT JSON",
}
EXPLAIN_MODES: tuple[str, ...] = tuple(EXPLAIN_OPTIONS)
# ANALYZE executes the statement. A mode list containing this entry cannot be
# treated as a read-only inspection option.
EXPLAIN_EXECUTING_MODES: tuple[str, ...] = ("analyze",)

# Server settings worth reading, and what a bad value means for this application.
CONNECTION_DIAGNOSTIC_SETTINGS: tuple[str, ...] = (
    "server_version_num",
    "TimeZone",
    "max_connections",
    "statement_timeout",
    "lock_timeout",
    "idle_in_transaction_session_timeout",
    "default_transaction_isolation",
)
CONNECTION_DIAGNOSTIC_CHECKS: list[dict[str, object]] = [
    {
        "check": "server_version_num",
        "format": "number",
        "concern_below": 120000,
        "severity": "warning",
        "finding": "PostgreSQL 12 or newer is required; older servers lack the lock_timeout and CTE behaviour this module relies on",
        "remedy": "upgrade the server or pin a newer image tag",
    },
    {
        "check": "max_connections",
        "format": "number",
        "concern_below": 40,
        "severity": "warning",
        "finding": "max_connections is tight against the configured pool ceiling",
        "remedy": "raise max_connections or lower DB_POOL_SIZE plus DB_MAX_OVERFLOW",
    },
    {
        "check": "TimeZone",
        "format": "text",
        "concern_not_equals": "UTC",
        "severity": "info",
        "finding": "the server timezone is not UTC; timestamptz columns are unaffected but date-bucketed reports and any server-side now() will bucket by the server's zone",
        "remedy": "set the database timezone to UTC, or make sure every bucketing query is explicit about its zone",
    },
    {
        "check": "default_transaction_isolation",
        "format": "text",
        "concern_not_equals": "read committed",
        "severity": "info",
        "finding": "a stricter default isolation than read committed means serialization failures are expected; DEFAULT_RETRY_POLICY already classifies them as retryable, but every such failure costs a round-trip",
        "remedy": "keep the default at read committed, or shorten the transactions that rely on strict isolation",
    },
    {
        "check": "lock_timeout",
        "format": "duration",
        "concern_above": 0,
        "severity": "info",
        "finding": "a server-wide lock_timeout is set and will apply on top of the per-session one",
        "remedy": "keep DB_LOCK_TIMEOUT_MS below the server-wide value so the session setting is the one that fires first",
    },
    {
        "check": "idle_in_transaction_session_timeout",
        "format": "duration",
        "concern_above": 0,
        "severity": "info",
        "finding": "an idle-in-transaction timeout is set; long scans opened inside an explicit transaction will be cancelled",
        "remedy": "keep report queries outside an explicit transaction, or raise the timeout",
    },
    {
        "check": "statement_timeout",
        "format": "duration",
        "concern_above": 0,
        "severity": "info",
        "finding": "a server-wide statement_timeout is set and will apply on top of the per-session one",
        "remedy": "keep DB_STATEMENT_TIMEOUT_MS below the server-wide value so the session setting is the one that fires first",
    },
]
DIAGNOSTIC_VERDICTS: tuple[str, ...] = ("ok", "info", "warning", "skipped")
# Exactly one comparator per check. Two would make the verdict depend on which
# one is evaluated first; zero would make the check informational by accident.
DIAGNOSTIC_COMPARATORS: tuple[str, ...] = (
    "concern_below",
    "concern_above",
    "concern_equals",
    "concern_not_equals",
)
DIAGNOSTIC_NUMERIC_COMPARATORS: tuple[str, ...] = ("concern_below", "concern_above")
# "number" and "duration" both parse via parse_postgres_value; "duration" is
# declared on its own because a bare "0" tests fine while the real "30s" does not.
DIAGNOSTIC_FORMATS: tuple[str, ...] = ("number", "duration", "text")

# Which kind of divergence between declared models and the live schema matters,
# and what to do about it. Ordered by how much it should worry an operator.
SCHEMA_DRIFT_CHECKS: list[dict[str, object]] = [
    {
        "check": "missing_table",
        "severity": "critical",
        "finding": "the model defines a table the database does not have",
        "remedy": "run the migration; the application queries a table that does not exist",
    },
    {
        "check": "missing_column",
        "severity": "critical",
        "finding": "the model selects a column the live table does not have",
        "remedy": "run the migration; every query touching this column fails until it does",
    },
    {
        "check": "type_mismatch",
        "severity": "warning",
        "finding": "the declared column type and the live column type differ",
        "remedy": "reconcile the model with the live column type; mismatches surface as cast failures, not errors",
    },
    {
        "check": "nullable_mismatch",
        "severity": "warning",
        "finding": "the model and the live column disagree about nullability",
        "remedy": "align nullability, or every write that omits the column will fail at the database",
    },
    {
        "check": "missing_index",
        "severity": "info",
        "finding": "an index declared on the model is absent from the live table",
        "remedy": "add the index the model declares, or expect the query plan to degrade to a sequential scan",
    },
    {
        "check": "unexpected_table",
        "severity": "info",
        "finding": "the database has a table no imported model declares",
        "remedy": "usually a migration that landed ahead of the model; harmless while nothing references it",
    },
]
DRIFT_SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")
DRIFT_CHECK_NAMES: tuple[str, ...] = tuple(str(c["check"]) for c in SCHEMA_DRIFT_CHECKS)
DRIFT_SEVERITY_DEFAULT = "warning"

# The knobs, declared once, with the kind of value they hold.
DB_OPS_TUNABLES: list[dict[str, object]] = [
    {"name": "slow_query_watch_ms", "kind": "scalar", "default": 50.0, "affects": ["slow_query"]},
    {"name": "slow_query_slow_ms", "kind": "scalar", "default": 200.0, "affects": ["slow_query"]},
    {"name": "slow_query_critical_ms", "kind": "scalar", "default": 1000.0, "affects": ["slow_query"]},
    {"name": "max_fingerprints", "kind": "scalar", "default": 500, "affects": ["query_recorder"]},
    {"name": "bulk_load_chunk_size", "kind": "scalar", "default": 500, "affects": ["bulk_load"]},
    {"name": "explain_timeout_ms", "kind": "scalar", "default": 5000, "affects": ["explain"]},
    {"name": "diagnostic_timeout_ms", "kind": "scalar", "default": 3000, "affects": ["diagnostics", "schema_drift"]},
    {"name": "read_replica_enabled", "kind": "flag", "default": False, "affects": ["read_write_split"]},
    {"name": "guard_idempotent_attempts", "kind": "scalar", "default": 3, "affects": ["guarded_call"]},
    {"name": "guard_non_idempotent_retries", "kind": "flag", "default": False, "affects": ["guarded_call"]},
]
TUNABLE_KINDS: tuple[str, ...] = ("scalar", "flag")
DB_OPS_TUNABLE_NAMES: tuple[str, ...] = tuple(str(t["name"]) for t in DB_OPS_TUNABLES)

# Sample corpus for the normalization invariants in ``validate_db_ops_policy``.
# A masker that is not idempotent produces two fingerprints for one statement,
# which is the one failure mode that would quietly defeat the whole table.
NORMALIZATION_CORPUS: tuple[str, ...] = (
    "SELECT * FROM users WHERE id = 42",
    "select  *   from users\n where id = 7;",
    "SELECT * FROM users WHERE email = 'a@b.com' AND id = 9",
    "SELECT * FROM users WHERE email = 'other@b.com' AND id = 12345",
    "SELECT * FROM t WHERE id IN (1, 2, 3)",
    "SELECT * FROM t WHERE id IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)",
    "SELECT * FROM t WHERE id = $1",
    "SELECT * FROM t WHERE id = %(pk)s",
    "SELECT * FROM t WHERE id = :pk",
    "SELECT id -- the user id\n FROM t",
    "/* audit */ SELECT id FROM t",
    "WITH moved AS (DELETE FROM t WHERE id = 1 RETURNING *) SELECT * FROM moved",
)


# --- Tunable resolution --------------------------------------------------------


def resolve_db_ops_tunables(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Declared defaults, with ``overrides`` applied by copy — never in place.

    An unknown override raises rather than being ignored, because a silently
    dropped threshold override is indistinguishable from one that did not take
    effect, which is the same failure this module exists to rule out.
    """
    resolved: dict[str, Any] = {}
    for tunable in DB_OPS_TUNABLES:
        name = str(tunable["name"])
        default = tunable["default"]
        resolved[name] = bool(default) if tunable["kind"] == "flag" else default
    for name, value in (overrides or {}).items():
        if name not in resolved:
            raise ValueError(
                f"unknown database-ops tunables: {name}; "
                f"known: {', '.join(DB_OPS_TUNABLE_NAMES)}"
            )
        kind = next(t for t in DB_OPS_TUNABLES if str(t["name"]) == name)["kind"]
        resolved[name] = bool(value) if kind == "flag" else value
    return resolved


def resolve_slow_bands(overrides: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The slow-query bands with their tunable thresholds substituted in.

    Returns fresh dicts, so a caller that sorts or annotates the result cannot
    corrupt the module-level table for the next caller.
    """
    tunables = resolve_db_ops_tunables(overrides)
    bands: list[dict[str, Any]] = []
    for band in SLOW_QUERY_BANDS:
        name = str(band["tunable"])
        if name not in tunables:
            continue
        bands.append(
            {
                "severity": str(band["severity"]),
                "min_ms": float(tunables[name]),
                "advice": str(band["advice"]),
            }
        )
    bands.sort(key=lambda band: float(band["min_ms"]), reverse=True)
    return bands


def classify_duration(
    duration_ms: float, *, overrides: dict[str, Any] | None = None
) -> str:
    """Band a statement duration into a severity."""
    value = float(duration_ms)
    for band in resolve_slow_bands(overrides):
        if value >= float(band["min_ms"]):
            return str(band["severity"])
    return SLOW_QUERY_DEFAULT


# --- SQL normalization and fingerprinting -------------------------------------


def _statement_text(statement: object) -> str:
    """Best-effort SQL text for a raw string or a SQLAlchemy construct.

    ``TextClause.text`` is preferred over ``str()``: on some versions the string
    form of a ``text()`` wrapper is not the statement itself, and a fingerprint
    over the wrapper would group every ``text()`` query under one key.
    """
    if isinstance(statement, str):
        return statement
    inner = getattr(statement, "text", None)
    if isinstance(inner, str) and inner.strip():
        return inner
    return str(statement)


@lru_cache(maxsize=64)
def _compiled_normalization_rule(rule_id: str, pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


def normalize_sql(
    statement: object, *, rules: list[dict[str, object]] | None = None
) -> str:
    """Collapse a statement to its stable shape: literals, params, and lists go.

    Idempotent by construction, and ``validate_db_ops_policy`` asserts that it
    actually is — which is the property that makes a fingerprint trustworthy.
    """
    table = SQL_NORMALIZATION_RULES if rules is None else rules
    normalized = _statement_text(statement)
    for rule in table:
        pattern = _compiled_normalization_rule(str(rule["id"]), str(rule["pattern"]))
        normalized = pattern.sub(str(rule["replacement"]), normalized)
    return normalized.strip().rstrip(";").strip().lower()


def fingerprint_statement(statement: object, *, length: int = 12) -> str:
    """Short, stable id for a statement's *shape*, shared across all executions."""
    digest = hashlib.blake2b(
        normalize_sql(statement).encode("utf-8"), digest_size=8
    ).hexdigest()
    return digest[: max(int(length), 4)]


def _leading_verb(normalized: str) -> str:
    token = normalized.lstrip("(").split(None, 1)
    return (token[0] if token else "UNKNOWN").upper()


def _rule_for_verb(
    verb: str, rules: list[dict[str, object]]
) -> dict[str, object]:
    for rule in rules:
        if str(rule["verb"]) == verb:
            return rule
    return dict(QUERY_VERB_FALLBACK)


def _first_mutating_verb(normalized: str, rules: list[dict[str, object]]) -> str | None:
    """First data-modifying verb inside a statement body, if any.

    Scanned rather than assumed because a leading ``WITH`` proves nothing: the
    CTE it introduces may be a plain read or a ``DELETE ... RETURNING``, and
    those two must not land in the same routing bucket.
    """
    verbs = [str(rule["verb"]) for rule in rules if rule["mutating"] is True]
    if not verbs:
        return None
    match = re.search(
        r"\b(" + "|".join(sorted(verbs, key=len, reverse=True)) + r")\b",
        normalized,
        re.IGNORECASE,
    )
    return match.group(1).upper() if match else None


def classify_statement(
    statement: object, *, rules: list[dict[str, object]] | None = None
) -> dict[str, Any]:
    """Verb, query class, mutability, and fingerprint for one statement.

    ``mutating`` is ``None`` only when the statement's body could not be
    resolved, and an unresolved statement is routed to the primary. The three
    fields a caller must not guess at — ``query_class``, ``split_target``, and
    ``mutating_resolved`` — are all derived here so they cannot disagree.
    """
    table = QUERY_VERB_RULES if rules is None else rules
    normalized = normalize_sql(statement, rules=None)
    verb = _leading_verb(normalized)
    rule = _rule_for_verb(verb, table)

    mutating = rule.get("mutating")
    body_verb: str | None = None
    if mutating is None and bool(rule.get("resolves_body")):
        body_verb = _first_mutating_verb(normalized, table)
        mutating = body_verb is not None
        if body_verb is not None:
            # A data-modifying CTE is a write wearing a read's leading verb, so
            # it inherits the write class rather than staying "read".
            rule = dict(rule)
            rule["query_class"] = _rule_for_verb(body_verb, table).get(
                "query_class", "write"
            )

    query_class = str(rule.get("query_class", QUERY_VERB_FALLBACK["query_class"]))
    resolved = mutating is not None

    target = SPLIT_TARGET_BY_QUERY_CLASS.get(query_class, "primary")
    if not resolved:
        target = "primary"

    return {
        "verb": verb,
        "query_class": query_class,
        # Stays None when the body could not be resolved. Coercing it to False
        # would claim to know the statement is read-only when all we know is
        # that we could not tell, and the split routes on this value.
        "mutating": bool(mutating) if resolved else None,
        "mutating_resolved": resolved,
        "body_verb": body_verb,
        "split_target": target,
        "fingerprint": fingerprint_statement(statement),
        "normalized": normalized,
    }


def split_target_for(
    statement: object,
    *,
    rules: list[dict[str, object]] | None = None,
    overrides: dict[str, Any] | None = None,
) -> str:
    """Which engine a statement should run on, given the replica's availability.

    Returns ``"primary"`` whenever the split is disabled, whenever the replica
    is not configured, and whenever the statement is not provably a read. The
    three conditions are deliberately the same condition: any doubt, write.
    """
    tunables = resolve_db_ops_tunables(overrides)
    if not bool(tunables["read_replica_enabled"]):
        return "primary"
    verdict = classify_statement(statement, rules=rules)
    if verdict["split_target"] != "replica":
        return "primary"
    if get_read_engine() is None:
        return "primary"
    return "replica"


# --- Query recorder ------------------------------------------------------------
#
# In-memory and bounded on purpose. A metrics backend is the right answer in a
# real deployment, but a recorder that depends on one cannot be exercised in a
# test with no network, and a slow-query table that can grow without limit is a
# memory leak wearing a diagnostic's clothes. Capacity is a tunable, and hitting
# it is *counted and reported* rather than silently evicting or silently
# ignoring: a fingerprint that vanished from the table is a query whose slowness
# you can never see again.


@dataclass
class QuerySample:
    """Aggregated executions of one statement shape."""

    fingerprint: str
    normalized: str
    query_class: str
    count: int = 0
    error_count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_ms: float = 0.0
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_error: str | None = None

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def as_dict(self, *, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "normalized": self.normalized,
            "query_class": self.query_class,
            "severity": classify_duration(self.max_ms, overrides=overrides),
            "count": self.count,
            "error_count": self.error_count,
            "total_ms": round(self.total_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "last_ms": round(self.last_ms, 3),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_error": self.last_error,
        }


class QueryRecorder:
    """Bounded fingerprint -> aggregated-duration table."""

    def __init__(self, *, max_fingerprints: int | None = None) -> None:
        self._max = (
            int(max_fingerprints)
            if max_fingerprints is not None
            else int(resolve_db_ops_tunables()["max_fingerprints"])
        )
        self._samples: dict[str, QuerySample] = {}
        self._dropped = 0
        self._dropped_ms = 0.0
        self._rejected = 0

    def record(
        self,
        statement: object,
        *,
        duration_ms: float,
        error: BaseException | None = None,
        now: float | None = None,
    ) -> QuerySample | None:
        """Fold one execution into its sample. Returns None when at capacity.

        None means "not retained", and the caller-visible counter says so; it
        never means "recorded fine".
        """
        moment = time.monotonic() if now is None else float(now)
        try:
            verdict = classify_statement(statement)
        except Exception as exc:  # a malformed statement must not break the caller
            self._rejected += 1
            logger.debug("query classification failed (%s); not recorded", exc)
            return None

        key = str(verdict["fingerprint"])
        sample = self._samples.get(key)
        if sample is None:
            if len(self._samples) >= self._max:
                self._dropped += 1
                self._dropped_ms += max(float(duration_ms), 0.0)
                return None
            sample = QuerySample(
                fingerprint=key,
                normalized=str(verdict["normalized"]),
                query_class=str(verdict["query_class"]),
                first_seen=moment,
            )
            self._samples[key] = sample
        sample.count += 1
        sample.total_ms += max(float(duration_ms), 0.0)
        sample.max_ms = max(sample.max_ms, float(duration_ms))
        sample.last_ms = float(duration_ms)
        sample.last_seen = moment
        if error is not None:
            sample.error_count += 1
            sample.last_error = f"{type(error).__name__}: {error}"
        return sample

    def samples(self) -> list[QuerySample]:
        return sorted(
            self._samples.values(), key=lambda s: (-s.max_ms, s.fingerprint)
        )

    def report(
        self,
        *,
        overrides: dict[str, Any] | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        """Slow-query report, banded, plus the drop counter.

        ``top`` bounds the per-severity lists so the report stays readable; the
        totals are always over *every* retained sample, so a truncated list
        never misreports a count.
        """
        limit = max(int(top), 1)
        samples = self.samples()
        by_severity: dict[str, list[dict[str, Any]]] = {
            severity: [] for severity in QUERY_SEVERITIES
        }
        for sample in samples:
            severity = classify_duration(sample.max_ms, overrides=overrides)
            by_severity.setdefault(severity, []).append(
                sample.as_dict(overrides=overrides)
            )
        total_ms = sum(sample.total_ms for sample in samples)
        return {
            "thresholds": {
                str(band["severity"]): band["min_ms"]
                for band in resolve_slow_bands(overrides)
            },
            "default_severity": SLOW_QUERY_DEFAULT,
            "totals": {
                "fingerprints": len(samples),
                "statements": sum(sample.count for sample in samples),
                "errors": sum(sample.error_count for sample in samples),
                "total_ms": round(total_ms, 3),
                "mean_ms": round(total_ms / max(sum(s.count for s in samples), 1), 3),
                "max_ms": round(max((s.max_ms for s in samples), default=0.0), 3),
            },
            "capacity": {
                "max_fingerprints": self._max,
                "retained": len(self._samples),
                "dropped_fingerprints": self._dropped,
                "dropped_ms": round(self._dropped_ms, 3),
                "rejected": self._rejected,
                "note": (
                    "dropped_fingerprints counts executions that arrived after the "
                    "table was full; those are not retained and cannot be reported"
                ),
            },
            "by_severity": {name: rows[:limit] for name, rows in by_severity.items()},
            "slowest": [sample.as_dict(overrides=overrides) for sample in samples[:limit]],
            "by_query_class": self._by_query_class(samples, overrides),
        }

    def _by_query_class(
        self, samples: list[QuerySample], overrides: dict[str, Any] | None
    ) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for sample in samples:
            bucket = grouped.setdefault(
                sample.query_class,
                {"statements": 0, "fingerprints": 0, "errors": 0, "total_ms": 0.0},
            )
            bucket["statements"] += sample.count
            bucket["fingerprints"] += 1
            bucket["errors"] += sample.error_count
            bucket["total_ms"] += sample.total_ms
        return {
            name: {
                "statements": bucket["statements"],
                "fingerprints": bucket["fingerprints"],
                "errors": bucket["errors"],
                "total_ms": round(float(bucket["total_ms"]), 3),
            }
            for name, bucket in sorted(grouped.items())
        }

    def reset(self) -> None:
        self._samples.clear()
        self._dropped = 0
        self._dropped_ms = 0.0
        self._rejected = 0

    def snapshot(self) -> dict[str, Any]:
        return {key: sample.as_dict() for key, sample in self._samples.items()}


QUERY_RECORDER = QueryRecorder()


def get_query_recorder() -> QueryRecorder:
    return QUERY_RECORDER


def reset_query_recorder() -> None:
    QUERY_RECORDER.reset()


async def timed_execute(
    session: AsyncSession,
    statement: Any,
    params: dict[str, Any] | None = None,
    *,
    recorder: QueryRecorder | None = None,
) -> Any:
    """``session.execute`` with the duration recorded either way.

    Errors are recorded and then re-raised unchanged: instrumentation that
    swallows a failure is worse than no instrumentation.
    """
    target = recorder if recorder is not None else QUERY_RECORDER
    started = time.monotonic()
    try:
        if params is not None:
            result = await session.execute(statement, params)
        else:
            result = await session.execute(statement)
    except BaseException as exc:
        target.record(
            statement, duration_ms=(time.monotonic() - started) * 1000.0, error=exc
        )
        raise
    # Reached by falling off the try, not by returning out of it: recording a
    # successful statement after a `return` inside the try is unreachable code.
    target.record(statement, duration_ms=(time.monotonic() - started) * 1000.0)
    return result


# --- Guarded call: retry + circuit breaker -------------------------------------


async def guarded_call(
    coro_factory: Callable[[], Any],
    *,
    breaker: CircuitBreaker | None = None,
    policy: RetryPolicy | None = None,
    idempotent: bool = True,
    operation: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Any:
    """Run ``coro_factory`` behind both the retry policy and the circuit.

    Three properties worth stating, because each is a bug this shape invites:

    1. ``idempotent=False`` runs the factory **once**. The retry machinery is
       for transient *connection* failures, and a non-idempotent write is exactly
       the case where "it did not answer" is not the same as "it did not
       happen". Replaying it is how a duplicate row gets written.
    2. The breaker records the outcome of the whole guarded call, not of each
       attempt. A call that fails twice and succeeds on the third attempt did
       not take the database down, and counting it would open the circuit during
       a recoverable blip.
    3. When no explicit ``policy`` is given, the attempt count comes from
       ``guard_idempotent_attempts`` and is baked into a copied policy. Passing
       ``DEFAULT_RETRY_POLICY`` and an attempt count separately would let
       ``retry_async`` read ``policy.attempts`` and silently ignore the count.
    """
    guard = breaker if breaker is not None else DATABASE_CIRCUIT
    label = operation or getattr(coro_factory, "__name__", "database call")
    if not guard.allows():
        raise CircuitOpenError(
            f"circuit {guard.name!r} is {guard.state}; refusing to call {label}"
        )

    tunables = resolve_db_ops_tunables(overrides)
    attempts = max(int(tunables["guard_idempotent_attempts"]), 1)
    effective: RetryPolicy | None
    if not idempotent and not bool(tunables["guard_non_idempotent_retries"]):
        attempts = 1
        effective = None
    elif policy is not None:
        # An explicit policy is the caller's business, including its attempt
        # count; a tunable must not override a deliberate choice.
        effective = policy
        attempts = max(int(policy.attempts), 1)
    else:
        effective = replace(DEFAULT_RETRY_POLICY, attempts=attempts)

    async def _once() -> Any:
        try:
            result = await retry_async(
                coro_factory, attempts=attempts, delay_seconds=0.0, policy=effective
            )
        except Exception:
            guard.record_failure()
            raise
        guard.record_success()
        return result

    return await _once()


# --- Read / write split --------------------------------------------------------
#
# Opt-in and invisible when unused: with no ``DB_READ_URL`` configured,
# ``get_read_engine`` returns None, ``split_target_for`` answers "primary" for
# every statement, and ``read_session`` hands back an ordinary primary session.
# No call site needs a dialect branch or a "does this deployment have a replica"
# flag.

READ_DATABASE_URL: Optional[str] = os.getenv("DB_READ_URL") or None
_read_engine: Any = None
_read_engine_failed = False
_read_sessionmaker: Any = None


def get_read_engine() -> Any:
    """Lazily build the replica engine, or return None. Never raises.

    Built on first use rather than at import so a deployment with a bad replica
    URL still starts, and so importing this module never opens a second pool.
    The result is cached, including the failure: retrying engine construction on
    every statement would turn a misconfiguration into a latency problem.
    """
    global _read_engine, _read_engine_failed
    if _read_engine is not None:
        return _read_engine
    if _read_engine_failed or not READ_DATABASE_URL:
        return None
    try:
        _read_engine = create_async_engine(
            READ_DATABASE_URL,
            echo=DB_ECHO,
            pool_size=DB_POOL_SIZE,
            max_overflow=DB_MAX_OVERFLOW,
            pool_timeout=DB_POOL_TIMEOUT,
        )
    except Exception as exc:
        _read_engine_failed = True
        logger.warning("read replica engine unavailable (%s); using primary", exc)
        return None
    return _read_engine


def read_replica_configured() -> bool:
    return get_read_engine() is not None


def reset_read_engine() -> None:
    """Drop the cached replica engine (tests, and a config reload)."""
    global _read_engine, _read_engine_failed, _read_sessionmaker
    _read_engine = None
    _read_engine_failed = False
    _read_sessionmaker = None


def read_session_target() -> str:
    """Which engine ``read_session`` would use right now."""
    if not bool(resolve_db_ops_tunables()["read_replica_enabled"]):
        return "primary"
    return "replica" if read_replica_configured() else "primary"


@asynccontextmanager
async def read_session(*, engine: Any = None) -> AsyncIterator[AsyncSession]:
    """A session on the replica when one is available, else on the primary.

    Yields a plain ``AsyncSession`` so callers are identical in both cases;
    ``read_session_target()`` is how a caller that cares finds out where it
    landed. Does not commit — a read session that commits is a write session
    with an extra step and a replication-lag surprise.
    """
    global _read_sessionmaker
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False) if engine is not None else None
    if factory is None and read_session_target() == "replica":
        source = get_read_engine()
        if source is not None:
            if _read_sessionmaker is None:
                _read_sessionmaker = sessionmaker(
                    source, class_=AsyncSession, expire_on_commit=False
                )
            factory = _read_sessionmaker
    session = (factory or SessionLocal)()
    try:
        yield session
    finally:
        await session.close()


@asynccontextmanager
async def read_only(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Mark the current transaction read-only, where the server supports it.

    Defence in depth for the split: routing a statement to the replica is a
    classification decision, and this turns a misclassification into a database
    error instead of a silent write to the wrong server.
    """
    applied = False
    try:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        applied = True
    except Exception as exc:  # unsupported dialect / outside a transaction
        logger.debug("read_only transaction unavailable (%s)", exc)
    try:
        yield session
    finally:
        if applied:
            try:
                await session.execute(text("SET TRANSACTION READ WRITE"))
            except Exception:
                pass


# --- Savepoints and bulk load --------------------------------------------------


@asynccontextmanager
async def savepoint(session: AsyncSession) -> AsyncIterator[Any]:
    """A nested transaction that can fail without discarding the outer work.

    ``transaction`` rolls the whole unit of work back on the first failure, which
    is right for a request and wrong for a batch: one bad row in a 5000-row
    import should cost you that row, not the other 4999. The savepoint commits
    into the enclosing transaction, so a release here is not durable until the
    outer unit of work commits.
    """
    nested = await session.begin_nested()
    try:
        yield nested
    except Exception:
        try:
            await nested.rollback()
        except Exception as exc:
            logger.debug("savepoint rollback failed (%s)", exc)
        raise
    else:
        await nested.commit()


async def run_in_savepoint(session: AsyncSession, operation: Callable[[], Any]) -> Any:
    """Run one awaitable inside a savepoint. Returns the operation's result."""
    async with savepoint(session):
        return await operation()


async def bulk_load(
    session: AsyncSession,
    items: Iterable[Any],
    *,
    mapper: Any = None,
    chunk_size: int | None = None,
    flush: bool = True,
) -> dict[str, Any]:
    """Chunked insert that never holds a pooled connection for the whole batch.

    Accepts ORM instances (added in chunks and flushed) or mappings of column
    values (inserted as one multi-row statement per chunk). Commits nothing: the
    caller's :func:`transaction` owns durability, so this composes with the rest
    of the write path instead of racing it.

    An empty batch is a no-op that still reports its shape, so a caller can
    distinguish "nothing to load" from "loaded nothing" without a special case.
    """
    from sqlalchemy import insert  # local: only this helper needs the constructor

    started = time.monotonic()
    size = max(int(chunk_size or resolve_db_ops_tunables()["bulk_load_chunk_size"]), 1)
    rows = list(items)
    mode = "mappings" if rows and isinstance(rows[0], Mapping) else "orm"
    if mode == "mappings" and mapper is None:
        raise ValueError(
            "bulk_load received mappings but no mapper; pass mapper=<Model> "
            "so the target table is known"
        )

    async def _write(chunk: list[Any]) -> None:
        if mode == "mappings":
            await session.execute(insert(mapper).values(chunk))
        else:
            session.add_all(chunk)
            if flush:
                await session.flush()

    chunks = 0
    for start in range(0, len(rows), size):
        chunks += 1
        await _write(rows[start : start + size])

    return {
        "received": len(rows),
        "written": len(rows),
        "chunks": chunks,
        "chunk_size": size,
        "mode": mode,
        "committed": False,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
    }


# --- Explain -------------------------------------------------------------------
#
# ``EXPLAIN ANALYZE`` is not an inspection option. It executes the statement it
# explains, so ``EXPLAIN ANALYZE INSERT ...`` writes a row. Both gates below are
# therefore about the same thing — *will this actually run* — and are driven by a
# single ``execute=True`` flag so there is no way to satisfy one gate and not the
# other. The answers are returned as data (``{"ok": False, "reason": ...}``) and
# never raised, because a diagnostic that raises is a diagnostic that takes down
# the thing it was diagnosing.


def explain_plan_request(statement: object, *, mode: str = "costs") -> dict[str, Any]:
    """Pure: decide whether this statement may be explained, and how.

    Returns the pieces the caller needs to build the query, plus the refusal if
    there is one. Separated from execution so the policy is testable with no
    database at all.
    """
    verdict = classify_statement(statement)
    mode_key = str(mode).strip().lower()
    base: dict[str, Any] = {
        "requested_mode": mode_key,
        "statement": verdict,
        "options": [EXPLAIN_OPTIONS[mode_key]] if mode_key in EXPLAIN_OPTIONS else [],
        "executes_statement": False,
        "ok": False,
    }
    if mode_key not in EXPLAIN_OPTIONS:
        return base | {
            "reason": (
                f"unknown explain mode {mode_key!r}; known: "
                f"{', '.join(sorted(EXPLAIN_OPTIONS))}"
            )
        }
    base["options"] = [EXPLAIN_OPTIONS[mode_key]]

    if mode_key in EXPLAIN_EXECUTING_MODES:
        base["requires_execute"] = True
    if verdict["mutating"]:
        base["requires_execute"] = True
    return base | {"ok": True}


async def explain_statement(
    session: AsyncSession,
    statement: object,
    *,
    mode: str = "costs",
    execute: bool = False,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """``EXPLAIN`` one statement. Never raises; refusals come back as ``ok: False``.

    ``execute=True`` is the explicit acknowledgement required by both
    ``ANALYZE`` and any mutating statement. It is a separate argument from
    ``mode`` on purpose: "explain this write" and "run this write" are different
    requests and should not be spelled the same way.
    """
    request = explain_plan_request(statement, mode=mode)
    if not request["ok"]:
        return request
    if request.get("requires_execute") and not execute:
        # ok goes back to False explicitly: the request was well-formed, the
        # execution was not authorised, and a refusal must not read as a success.
        return request | {
            "ok": False,
            "reason": (
                f"explain mode {request['requested_mode']!r} "
                f"{'or the statement being mutating ' if request['statement']['mutating'] else ''}"
                "would execute it; pass execute=True to acknowledge that"
            ),
        }

    sql = _statement_text(statement)
    prefix = "EXPLAIN"
    if request["options"]:
        prefix = f"EXPLAIN ({', '.join(request['options'])})"
    query = f"{prefix} {sql}"

    budget = int(
        timeout_ms if timeout_ms is not None else resolve_db_ops_tunables()["explain_timeout_ms"]
    )
    started = time.monotonic()
    try:
        async with statement_timeouts(session, statement_ms=budget, lock_ms=budget):
            result = await session.execute(text(query))
            rows = [str(row) for row in result.scalars().all()]
    except Exception as exc:
        return request | {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        }

    payload = request | {
        "ok": True,
        "executed": bool(execute and request.get("requires_execute")),
        "query": query,
        "rows": rows,
        "plan": "\n".join(rows),
        "row_count": len(rows),
        "timeout_ms": budget,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
    }
    if "FORMAT JSON" in request["options"]:
        # Postgres returns one row whose text is a JSON array of plan nodes.
        # Parsing it is what makes the plan machine-checkable rather than
        # something an operator has to read; a parse failure keeps the text.
        try:
            import json

            parsed = json.loads(rows[0]) if rows else []
        except Exception as exc:
            payload["json_error"] = f"{type(exc).__name__}: {exc}"
        else:
            payload["plan_json"] = parsed
    return payload


# --- Connection diagnostics ----------------------------------------------------


async def connection_diagnostics(
    _engine=engine,
    *,
    settings: Iterable[str] | None = None,
) -> dict[str, Any]:
    """One round-trip describing the server we are actually talking to.

    Never raises: an unreachable database reports ``available: False`` with the
    reason, because "diagnostics crashed" and "the database is down" need to be
    distinguishable by the caller and this is the wrong place to conflate them.
    """
    requested = tuple(settings) if settings is not None else CONNECTION_DIAGNOSTIC_SETTINGS
    selections = ", ".join(
        f"current_setting('{name}') AS {name}" for name in requested
    )
    query = (
        "SELECT version() AS server_version, current_database() AS database_name, "
        f"current_user AS db_user, {selections}"
    )
    started = time.monotonic()
    try:
        async with _engine.begin() as conn:
            row = (await conn.execute(text(query))).mappings().first()
    except Exception as exc:
        return {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
    if row is None:
        return {
            "available": False,
            "reason": "diagnostic query returned no row",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
    payload = dict(row)
    return {
        "available": True,
        "dialect": DATABASE_URL.split("://", 1)[0],
        "read_replica_configured": read_replica_configured(),
        "server_version": payload.pop("server_version", None),
        "database": payload.pop("database_name", None),
        "user": payload.pop("db_user", None),
        "settings": payload,
        "requested_settings": list(requested),
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
    }


def _maybe_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# Units Postgres uses when it renders a duration setting.
POSTGRES_DURATION_UNITS: dict[str, float] = {
    "us": 0.001,
    "ms": 1.0,
    "s": 1_000.0,
    "min": 60_000.0,
    "h": 3_600_000.0,
    "d": 86_400_000.0,
}
# Compound literals are real: "1h30min", "500ms", "2min". The unit alternation
# is ordered longest-first so "min" cannot be read as "m" + leftover "in".
POSTGRES_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(us|ms|min|h|d|s)")


def parse_postgres_value(value: Any) -> float | None:
    """Numeric reading of a Postgres setting, in milliseconds. None if not one.

    ``current_setting('statement_timeout')`` answers ``"0"``, ``"30s"``,
    ``"2min"``, or ``"1h30min"`` -- a number-and-unit literal, not a number.
    Calling ``float()`` on the real value raises, which would make every
    duration check resolve to ``skipped`` in production while a test that only
    ever passed ``"0"`` reported a clean bill of health. Version strings and
    connection counts come back as bare numbers and take the fast path.
    """
    number = _maybe_number(value)
    if number is not None:
        return number
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    parts = POSTGRES_DURATION_PART.findall(text)
    if not parts or POSTGRES_DURATION_PART.sub("", text).strip():
        return None
    return sum(float(amount) * POSTGRES_DURATION_UNITS[unit] for amount, unit in parts)


def diagnostic_verdict(check: dict[str, object], raw: Any) -> str:
    """Pure: apply one declared comparator. ``ok`` when the value is fine.

    A setting that will not parse numerically is compared as text, because
    ``TimeZone`` and ``default_transaction_isolation`` are strings and coercing
    them to a number would make every check on them quietly pass.
    """
    if "concern_below" in check:
        number = parse_postgres_value(raw)
        if number is None:
            return "skipped"
        return str(check["severity"]) if number < float(check["concern_below"]) else "ok"
    if "concern_above" in check:
        number = parse_postgres_value(raw)
        if number is None:
            return "skipped"
        return str(check["severity"]) if number > float(check["concern_above"]) else "ok"
    text = "" if raw is None else str(raw).strip()
    if "concern_equals" in check:
        return str(check["severity"]) if text != str(check["concern_equals"]) else "ok"
    if "concern_not_equals" in check:
        # "concern_not_equals: UTC" means *a value other than* UTC is the
        # problem, so the expected value is the healthy one.
        return "ok" if text == str(check["concern_not_equals"]) else str(check["severity"])
    return "info"


def diagnose_connection(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: turn a diagnostics payload into ranked findings.

    A check whose setting is absent, or whose numeric setting will not parse, is
    reported ``skipped`` rather than passed, because "we could not read it" and
    "it is fine" must not produce the same row in an on-call decision.
    """
    values = dict(diagnostics.get("settings") or {})
    findings: list[dict[str, Any]] = []
    for check in CONNECTION_DIAGNOSTIC_CHECKS:
        name = str(check["check"])
        severity = str(check["severity"])
        raw = values.get(name, diagnostics.get(name))
        if raw is None:
            verdict = "skipped"
        else:
            verdict = diagnostic_verdict(check, raw)
        if verdict == "ok":
            detail = f"{name}={raw}"
            finding = "within the expected range"
            remedy = None
        elif verdict == "skipped":
            detail = f"{name}={raw if raw is not None else 'absent'}"
            finding = (
                f"{name} was not present in the diagnostics payload"
                if raw is None
                else f"{name}={raw} is not comparable as a number"
            )
            remedy = str(check["remedy"])
        else:
            detail = f"{name}={raw}"
            finding = str(check["finding"])
            remedy = str(check["remedy"])
        findings.append(
            {
                "check": name,
                "verdict": verdict,
                "severity": severity,
                "comparator": next(
                    (c for c in DIAGNOSTIC_COMPARATORS if c in check), None
                ),
                "value": raw,
                "detail": detail,
                "finding": finding,
                "remedy": remedy,
            }
        )
    severity_rank = {"warning": 0, "info": 1, "skipped": 2, "ok": 3}
    findings.sort(key=lambda f: (severity_rank.get(str(f["verdict"]), 9), str(f["check"])))
    return findings


# --- Schema drift --------------------------------------------------------------
#
# The live schema is compared against whatever is registered on ``Base.metadata``
# at call time. That is "whatever has been imported", not "everything the
# application defines" — ``app.models`` cannot be imported from here without a
# cycle, so a table no module has touched is invisible to this check. The report
# says how many tables it compared rather than implying it saw the whole schema,
# because a drift report that silently checks three tables reads as a clean bill
# of health for a hundred.


def _type_affinity(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text.split("(")[0].strip() or "unknown"


def _type_detail(value: Any) -> dict[str, Any]:
    text = str(value or "").strip().lower()
    detail: dict[str, Any] = {"affinity": _type_affinity(text), "declared": text}
    if "(" in text:
        inner = text[text.index("(") + 1 : text.rfind(")")] if text.endswith(")") else ""
        if inner:
            detail["params"] = inner
    return detail


def drift_severity(check: str) -> str:
    """Severity for a drift kind, defaulting to ``warning`` for unknown kinds.

    Unknown defaults to a warning rather than ``info``: a drift this module does
    not recognise is still drift, and quietly filing it as informational is how
    an unknown drift kind becomes a permanent silent one.
    """
    for entry in SCHEMA_DRIFT_CHECKS:
        if str(entry["check"]) == check:
            return str(entry["severity"])
    return DRIFT_SEVERITY_DEFAULT


def _declared_indexes(table: Any) -> set[str]:
    names: set[str] = set()
    for index in table.indexes:
        names.add(str(index.name))
    for constraint in table.constraints:
        if getattr(constraint, "name", None) and type(constraint).__name__ == "UniqueConstraint":
            names.add(str(constraint.name))
    return names


def _drift_finding(check: str, **detail: Any) -> dict[str, Any]:
    entry = next(e for e in SCHEMA_DRIFT_CHECKS if str(e["check"]) == check)
    return {
        "check": check,
        "severity": str(entry["severity"]),
        "finding": str(entry["finding"]),
        "remedy": str(entry["remedy"]),
        **detail,
    }


def compare_table(name: str, declared: Any, live: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: declared table vs one reflected table. Returns drift findings.

    Compares type *affinity* rather than the rendered type string, because
    ``VARCHAR`` and ``VARCHAR(255)`` agree on everything a mismatch would break
    and disagree textually; the full strings ride along for a human. Parameters
    are compared whenever *either* side declares them, so a model that says
    ``VARCHAR(255)`` against a live unbounded ``VARCHAR`` is reported — that
    difference is exactly the case where the database stops enforcing a bound
    the model assumed.
    """
    findings: list[dict[str, Any]] = []
    live_columns = {str(col["name"]): col for col in live.get("columns", [])}
    for column in declared.columns:
        column_name = str(column.name)
        actual = live_columns.pop(column_name, None)
        if actual is None:
            findings.append(
                _drift_finding("missing_column", table=name, column=column_name)
            )
            continue
        declared_type = _type_detail(column.type)
        live_type = _type_detail(actual.get("type"))
        same_affinity = declared_type["affinity"] == live_type["affinity"]
        same_params = declared_type.get("params") == live_type.get("params")
        if not same_affinity or not same_params:
            findings.append(
                _drift_finding(
                    "type_mismatch",
                    table=name,
                    column=column_name,
                    declared=declared_type,
                    live=live_type,
                    cause="affinity" if not same_affinity else "parameters",
                )
            )
        if bool(column.nullable) != bool(actual.get("nullable", True)):
            findings.append(
                _drift_finding(
                    "nullable_mismatch",
                    table=name,
                    column=column_name,
                    declared_nullable=bool(column.nullable),
                    live_nullable=bool(actual.get("nullable", True)),
                )
            )
    live_indexes = {str(idx.get("name")) for idx in live.get("indexes", [])}
    for index_name in sorted(_declared_indexes(declared) - live_indexes):
        findings.append(_drift_finding("missing_index", table=name, index=index_name))
    return findings


async def schema_drift_report(
    _engine=engine,
    *,
    tables: Iterable[str] | None = None,
    include_indexes: bool = True,
) -> dict[str, Any]:
    """Compare ``Base.metadata`` against the live schema. Never raises.

    ``tables`` scopes the work; unscoped, it reflects every table the connected
    user can see, which is right for a full audit and wasteful for a smoke test.
    """
    from sqlalchemy import inspect  # local: only this report needs reflection

    started = time.monotonic()
    scope = sorted({str(t) for t in tables}) if tables is not None else None

    def _inspect(sync_conn: Any) -> dict[str, Any]:
        inspector = inspect(sync_conn)
        live_names = sorted(inspector.get_table_names())
        if scope is not None:
            live_names = [name for name in live_names if name in set(scope)]
        reflected: dict[str, Any] = {}
        for live_name in live_names:
            entry: dict[str, Any] = {"columns": inspector.get_columns(live_name)}
            if include_indexes:
                entry["indexes"] = inspector.get_indexes(live_name)
            reflected[live_name] = entry
        return {"live_names": live_names, "reflected": reflected}

    try:
        async with _engine.connect() as conn:
            reflection = await conn.run_sync(_inspect)
    except Exception as exc:
        return {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        }

    declared_tables = {
        str(table.name): table for table in Base.metadata.sorted_tables
    }
    live_names = list(reflection["live_names"])
    live_set = set(live_names)

    if scope is not None:
        candidates = [name for name in scope if name in declared_tables]
    else:
        candidates = sorted(declared_tables)

    findings: list[dict[str, Any]] = []
    for name in candidates:
        if name not in live_set:
            findings.append(_drift_finding("missing_table", table=name))
            continue
        findings.extend(
            compare_table(name, declared_tables[name], reflection["reflected"][name])
        )
    if scope is None:
        # Scoped runs deliberately ignore both directions: the caller asked
        # about specific tables, so a table outside the scope is not "unexpected".
        for name in sorted(live_set - set(declared_tables)):
            findings.append(_drift_finding("unexpected_table", table=name))

    summary = {check: 0 for check in DRIFT_CHECK_NAMES}
    for finding in findings:
        summary[str(finding["check"])] = summary.get(str(finding["check"]), 0) + 1
    rank = {name: index for index, name in enumerate(DRIFT_SEVERITIES)}
    findings.sort(key=lambda f: (rank.get(str(f["severity"]), 9), str(f.get("table")), str(f.get("column") or f.get("index") or "")))

    return {
        "available": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scoped": scope is not None,
        "scope": scope,
        "declared_tables": len(declared_tables),
        "compared_tables": len(candidates),
        "live_tables": len(live_names),
        "include_indexes": include_indexes,
        "coverage_note": (
            "only tables registered on Base.metadata at call time are compared; "
            "modules that have not been imported are invisible to this check"
        ),
        "summary": summary,
        "drift_detected": bool(findings),
        "findings": findings,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
    }


# --- Policy validation, simulation, catalog ------------------------------------


def validate_db_ops_policy() -> dict[str, Any]:
    """Referential integrity of every table in the database-operations layer.

    The point is to make the config *checkable* rather than trusted. Each check
    below corresponds to a way this layer could be subtly, silently wrong:
    a band referencing a tunable that does not exist (a threshold that resolves
    to nothing and quietly falls through to the default), a normalization rule
    that is not idempotent (two fingerprints for one statement, defeating the
    table entirely), a query class with no split target (an ``elif`` that
    forgets a case), or an executing EXPLAIN mode reachable without the
    acknowledgement that its side effects require.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def error(message: str) -> None:
        errors.append(message)

    def warn(message: str) -> None:
        warnings.append(message)

    tunables = resolve_db_ops_tunables()
    names = list(DB_OPS_TUNABLE_NAMES)
    if len(set(names)) != len(names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        error(f"duplicate DB_OPS_TUNABLES names: {', '.join(duplicates)}")
    for tunable in DB_OPS_TUNABLES:
        kind = str(tunable["kind"])
        if kind not in TUNABLE_KINDS:
            error(
                f"tunable {tunable['name']!r} has unknown kind {kind!r}; "
                f"known: {', '.join(TUNABLE_KINDS)}"
            )
        default = tunable["default"]
        if kind == "scalar" and (not isinstance(default, (int, float)) or default < 0):
            error(f"tunable {tunable['name']!r} must default to a non-negative number")
        if kind == "flag" and not isinstance(default, bool):
            error(f"tunable {tunable['name']!r} must default to a bool")
    try:
        resolve_db_ops_tunables({"definitely_not_a_tunable": 1})
    except ValueError:
        pass
    else:  # pragma: no cover - would mean the unknown-key guard is gone
        error("resolve_db_ops_tunables accepted an unknown tunable name")

    # Slow-query bands: every threshold resolves, ordering is strict, severities
    # are known, and the highest band is above the lowest.
    thresholds: list[float] = []
    for band in SLOW_QUERY_BANDS:
        severity = str(band["severity"])
        if severity not in QUERY_SEVERITIES:
            error(f"slow-query band {severity!r} is not one of {', '.join(QUERY_SEVERITIES)}")
        source = str(band["tunable"])
        if source not in tunables:
            error(
                f"slow-query band {severity!r} references unknown tunable {source!r}; "
                "the band would resolve to no threshold and fall through silently"
            )
        else:
            thresholds.append(float(tunables[source]))
    if len(set(thresholds)) != len(thresholds):
        error("slow-query bands share a threshold; one severity would be unreachable")
    elif thresholds != sorted(thresholds, reverse=True):
        error("SLOW_QUERY_BANDS is not ordered highest threshold first")
    if SLOW_QUERY_DEFAULT not in QUERY_SEVERITIES:
        error(f"SLOW_QUERY_DEFAULT {SLOW_QUERY_DEFAULT!r} is not a known severity")

    # Verb rules: unique verbs, known classes, tri-state mutability.
    verbs = [str(rule["verb"]) for rule in QUERY_VERB_RULES]
    if len(set(verbs)) != len(verbs):
        duplicates = sorted({v for v in verbs if verbs.count(v) > 1})
        error(f"duplicate QUERY_VERB_RULES verbs: {', '.join(duplicates)}")
    for rule in QUERY_VERB_RULES:
        query_class = str(rule["query_class"])
        if query_class not in QUERY_CLASSES:
            error(
                f"verb {rule['verb']!r} maps to unknown query class {query_class!r}; "
                f"known: {', '.join(QUERY_CLASSES)}"
            )
        if rule.get("mutating") not in (True, False, None):
            error(f"verb {rule['verb']!r} has non-tri-state mutating {rule.get('mutating')!r}")
    if str(QUERY_VERB_FALLBACK["query_class"]) not in QUERY_CLASSES:
        error("QUERY_VERB_FALLBACK names a query class outside QUERY_CLASSES")
    mutating = set(MUTATING_VERBS)
    if mutating and not mutating <= set(verbs):
        error(f"MUTATING_VERBS contains verbs absent from the rules: {sorted(mutating - set(verbs))}")

    # Split routing: complete coverage, known targets, and the two invariants
    # that make the split safe rather than merely convenient.
    for query_class in QUERY_CLASSES:
        if query_class not in SPLIT_TARGET_BY_QUERY_CLASS:
            error(f"query class {query_class!r} has no split target")
    for query_class, target in SPLIT_TARGET_BY_QUERY_CLASS.items():
        if target not in SPLIT_TARGETS:
            error(f"split target {target!r} for {query_class!r} is not one of {', '.join(SPLIT_TARGETS)}")
    replica_classes = sorted(
        cls for cls, target in SPLIT_TARGET_BY_QUERY_CLASS.items() if target == "replica"
    )
    if replica_classes not in ([], ["read"]):
        error(f"only 'read' may route to a replica; found {', '.join(replica_classes)}")
    for rule in QUERY_VERB_RULES:
        if rule.get("mutating") is True and SPLIT_TARGET_BY_QUERY_CLASS.get(
            str(rule["query_class"])
        ) != "primary":
            error(f"mutating verb {rule['verb']!r} does not route to the primary")
    if "explain" in replica_classes:
        error("EXPLAIN must not route to a replica; ANALYZE executes")

    # Normalization: unique, compilable, and — the real invariant — idempotent.
    rule_ids = [str(rule["id"]) for rule in SQL_NORMALIZATION_RULES]
    if len(set(rule_ids)) != len(rule_ids):
        duplicates = sorted({r for r in rule_ids if rule_ids.count(r) > 1})
        error(f"duplicate SQL_NORMALIZATION_RULES ids: {', '.join(duplicates)}")
    for rule in SQL_NORMALIZATION_RULES:
        try:
            re.compile(str(rule["pattern"]))
        except re.error as exc:
            error(f"normalization rule {rule['id']!r} does not compile: {exc}")
    order = [str(rule["id"]) for rule in SQL_NORMALIZATION_RULES]
    for required in ("strip_comments", "mask_quoted", "mask_bindparams", "mask_numbers"):
        if required not in order:
            error(f"normalization rule {required!r} is missing; statements would not fingerprint stably")
    if "mask_bindparams" in order and "mask_numbers" in order:
        if order.index("mask_bindparams") > order.index("mask_numbers"):
            error(
                "mask_bindparams must precede mask_numbers, or a placeholder like $1 "
                "normalizes to $? and stops matching the placeholder rule"
            )
    for sample in NORMALIZATION_CORPUS:
        try:
            once = normalize_sql(sample)
        except Exception as exc:  # pragma: no cover - defensive
            error(f"normalize_sql failed on {sample!r}: {exc}")
            continue
        twice = normalize_sql(once)
        if once != twice:
            error(f"normalize_sql is not idempotent for {sample!r}: {once!r} then {twice!r}")
    grouped: dict[str, set[str]] = {}
    for sample in NORMALIZATION_CORPUS:
        grouped.setdefault(fingerprint_statement(sample), set()).add(sample)
    collapsing = [
        key
        for key, members in grouped.items()
        if len(members) > 1
        and any("id = 42" in m or "id = 7" in m for m in members)
    ]
    if not collapsing:
        warn("the corpus contains no pair of statements that should collapse to one fingerprint")

    # EXPLAIN: modes and options agree, and executing modes are declared.
    if set(EXPLAIN_MODES) != set(EXPLAIN_OPTIONS):
        error("EXPLAIN_MODES and EXPLAIN_OPTIONS keys disagree")
    for mode in EXPLAIN_EXECUTING_MODES:
        if mode not in EXPLAIN_OPTIONS:
            error(f"EXPLAIN_EXECUTING_MODES names unknown mode {mode!r}")
    if not EXPLAIN_EXECUTING_MODES:
        warn("no EXPLAIN mode is marked as executing; the ANALYZE guard is inert")

    # Diagnostics: known verdicts, and every check declares a usable comparator.
    check_names = [str(check["check"]) for check in CONNECTION_DIAGNOSTIC_CHECKS]
    if len(set(check_names)) != len(check_names):
        duplicates = sorted({c for c in check_names if check_names.count(c) > 1})
        error(f"duplicate CONNECTION_DIAGNOSTIC_CHECKS names: {', '.join(duplicates)}")
    for setting in CONNECTION_DIAGNOSTIC_SETTINGS:
        if not any(str(check["check"]) == setting for check in CONNECTION_DIAGNOSTIC_CHECKS):
            warn(f"setting {setting!r} is requested but no check interprets it")
    for check in CONNECTION_DIAGNOSTIC_CHECKS:
        if str(check["severity"]) not in DIAGNOSTIC_VERDICTS:
            error(
                f"diagnostic check {check['check']!r} has severity "
                f"{check['severity']!r} outside {', '.join(DIAGNOSTIC_VERDICTS)}"
            )
        declared = [c for c in DIAGNOSTIC_COMPARATORS if c in check]
        if not declared:
            warn(f"diagnostic check {check['check']!r} declares no threshold and is always info")
        elif len(declared) > 1:
            error(
                f"diagnostic check {check['check']!r} declares {len(declared)} comparators "
                f"({', '.join(declared)}); the verdict would depend on evaluation order"
            )
        for comparator in declared:
            if comparator in DIAGNOSTIC_NUMERIC_COMPARATORS:
                threshold = parse_postgres_value(check[comparator])
                if threshold is None:
                    error(
                        f"diagnostic check {check['check']!r} has a non-numeric "
                        f"{comparator} ({check[comparator]!r}); it would always skip"
                    )
        if str(check.get("format", "")) not in DIAGNOSTIC_FORMATS:
            error(
                f"diagnostic check {check['check']!r} has format "
                f"{check.get('format')!r} outside {', '.join(DIAGNOSTIC_FORMATS)}"
            )
        if str(check["format"]) == "text" and any(
            c in DIAGNOSTIC_NUMERIC_COMPARATORS for c in declared
        ):
            error(
                f"diagnostic check {check['check']!r} is declared text but compares "
                "numerically; the value would skip instead of being judged"
            )
        for setting in CONNECTION_DIAGNOSTIC_SETTINGS:
            if str(check["check"]) == setting:
                break
        else:
            warn(
                f"check {check['check']!r} is not in CONNECTION_DIAGNOSTIC_SETTINGS, "
                "so it can never be evaluated from a diagnostics payload"
            )

    # Drift checks: unique, known severities, and the reporter's default exists.
    drift_names = [str(check["check"]) for check in SCHEMA_DRIFT_CHECKS]
    if len(set(drift_names)) != len(drift_names):
        duplicates = sorted({c for c in drift_names if drift_names.count(c) > 1})
        error(f"duplicate SCHEMA_DRIFT_CHECKS names: {', '.join(duplicates)}")
    for check in SCHEMA_DRIFT_CHECKS:
        if str(check["severity"]) not in DRIFT_SEVERITIES:
            error(
                f"drift check {check['check']!r} has severity {check['severity']!r} "
                f"outside {', '.join(DRIFT_SEVERITIES)}"
            )
        for required_key in ("finding", "remedy"):
            if not str(check.get(required_key, "")).strip():
                error(
                    f"drift check {check['check']!r} has no {required_key}; a finding an "
                    "operator cannot act on is the same as no finding"
                )
    if DRIFT_SEVERITY_DEFAULT not in DRIFT_SEVERITIES:
        error(f"DRIFT_SEVERITY_DEFAULT {DRIFT_SEVERITY_DEFAULT!r} is not a known severity")
    for check in drift_names:
        if drift_severity(check) not in DRIFT_SEVERITIES:
            error(f"drift_severity({check!r}) resolves outside {', '.join(DRIFT_SEVERITIES)}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "verb_rules": len(QUERY_VERB_RULES),
            "query_classes": len(QUERY_CLASSES),
            "slow_bands": len(SLOW_QUERY_BANDS),
            "normalization_rules": len(SQL_NORMALIZATION_RULES),
            "corpus_statements": len(NORMALIZATION_CORPUS),
            "corpus_fingerprints": len(grouped),
            "explain_modes": len(EXPLAIN_MODES),
            "diagnostic_settings": len(CONNECTION_DIAGNOSTIC_SETTINGS),
            "diagnostic_checks": len(CONNECTION_DIAGNOSTIC_CHECKS),
            "drift_checks": len(SCHEMA_DRIFT_CHECKS),
            "tunables": len(DB_OPS_TUNABLES),
        },
        "defaults": dict(sorted(tunables.items())),
    }


def simulate_db_ops(
    samples: Iterable[dict[str, Any]],
    overrides: dict[str, Any] | None = None,
    *,
    max_fingerprints: int | None = None,
) -> dict[str, Any]:
    """Pure: what a threshold or capacity change would do to the slow-query report.

    Takes sample dicts and returns the comparison; it reads no module global and
    mutates none, so a caller can hold a baseline and several candidates side by
    side. An unknown tunable raises, matching
    :func:`resolve_db_ops_tunables`, rather than being ignored.

    The baseline is always the declared defaults. Each scenario then applies
    exactly one override *to those defaults* and compares against the baseline.
    Building the baseline from the overrides instead would make every scenario
    report ``changed: False`` — an override that is silently a no-op, which is
    the exact failure this function exists to detect.
    """
    capacity = int(
        max_fingerprints
        if max_fingerprints is not None
        else int(resolve_db_ops_tunables()["max_fingerprints"])
    )
    rows = [
        {
            "fingerprint": str(sample.get("fingerprint", "")),
            "normalized": str(sample.get("normalized", "")),
            "query_class": str(sample.get("query_class", "other")),
            "max_ms": float(sample.get("max_ms", 0.0) or 0.0),
            "count": int(sample.get("count", 0) or 0),
            "error_count": int(sample.get("error_count", 0) or 0),
        }
        for sample in samples
    ]

    def severities(candidate: dict[str, Any]) -> dict[str, str]:
        return {
            row["fingerprint"] or f"row-{index}": classify_duration(
                row["max_ms"], overrides=candidate
            )
            for index, row in enumerate(rows)
        }

    def reportable(assigned: dict[str, str]) -> list[str]:
        """Fingerprints above the default severity — the rows a report would show."""
        return sorted(key for key, value in assigned.items() if value != SLOW_QUERY_DEFAULT)

    def thresholds_for(candidate: dict[str, Any]) -> dict[str, float]:
        return {
            str(band["severity"]): band["min_ms"]
            for band in resolve_slow_bands(candidate)
        }

    baseline = severities({})
    scenarios: list[dict[str, Any]] = []
    if overrides:
        for tunable, value in sorted(overrides.items()):
            # One knob at a time, so the reported effect is attributable to it.
            candidate = {tunable: value}
            candidate_sev = severities(candidate)
            moved = [
                {
                    "fingerprint": key,
                    "from": baseline.get(key, SLOW_QUERY_DEFAULT),
                    "to": candidate_sev.get(key, SLOW_QUERY_DEFAULT),
                }
                for key in sorted(set(baseline) | set(candidate_sev))
                if baseline.get(key) != candidate_sev.get(key)
            ]
            scenarios.append(
                {
                    "tunable": tunable,
                    "override": value,
                    "kind": next(
                        str(t["kind"]) for t in DB_OPS_TUNABLES if str(t["name"]) == tunable
                    ),
                    "thresholds": thresholds_for(candidate),
                    "reportable_before": reportable(baseline),
                    "reportable_after": reportable(candidate_sev),
                    "promoted": sorted(set(reportable(candidate_sev)) - set(reportable(baseline))),
                    "demoted": sorted(set(reportable(baseline)) - set(reportable(candidate_sev))),
                    "changed": bool(moved),
                    "changed_severities": moved,
                }
            )
    else:
        # No override supplied: enumerate the tunables an operator can sweep, so
        # the response is a menu rather than an empty result.
        for tunable in DB_OPS_TUNABLES:
            scenarios.append(
                {
                    "tunable": str(tunable["name"]),
                    "kind": str(tunable["kind"]),
                    "default": tunables_default(tunable),
                    "affects": list(tunable["affects"]),
                    "thresholds": thresholds_for({}),
                    "current_severities": dict(baseline),
                    "reportable": reportable(baseline),
                    "note": "pass overrides={'<name>': value} to evaluate this tunable",
                }
            )

    unique = len({row["fingerprint"] for row in rows if row["fingerprint"]})
    return {
        "samples": len(rows),
        "unique_fingerprints": unique,
        "baseline": {
            "thresholds": thresholds_for({}),
            "severities": baseline,
            "reportable": reportable(baseline),
        },
        "capacity": {
            "max_fingerprints": capacity,
            "would_retain": min(unique, capacity),
            "would_drop": max(unique - capacity, 0),
            "note": (
                "a real recorder drops executions arriving after capacity and counts "
                "them; this reports the same figure before anything is written"
            ),
        },
        "scenarios": scenarios,
    }


def tunables_default(tunable: dict[str, Any]) -> Any:
    """The declared default for one tunable row, in its declared kind."""
    default = tunable["default"]
    return bool(default) if str(tunable["kind"]) == "flag" else default


def build_db_ops_catalog() -> dict[str, Any]:
    """Introspectable description of the instrumentation/diagnostics surface."""
    validation = validate_db_ops_policy()
    return {
        "query_classification": {
            "verb_rules": [dict(rule) for rule in QUERY_VERB_RULES],
            "verbs": list(QUERY_VERBS),
            "classes": list(QUERY_CLASSES),
            "fallback": dict(QUERY_VERB_FALLBACK),
            "mutating_verbs": list(MUTATING_VERBS),
            "note": (
                "mutating is tri-state; an unresolved statement routes to the primary "
                "because a data-modifying CTE can hide behind a leading WITH"
            ),
        },
        "read_write_split": {
            "targets": list(SPLIT_TARGETS),
            "target_by_query_class": dict(SPLIT_TARGET_BY_QUERY_CLASS),
            "replica_url_configured": READ_DATABASE_URL is not None,
            "replica_engine_available": read_replica_configured(),
            "current_target": read_session_target(),
            "note": "with no replica configured every statement routes to the primary",
        },
        "slow_query": {
            "bands": [dict(band) for band in SLOW_QUERY_BANDS],
            "resolved_bands": resolve_slow_bands(),
            "default_severity": SLOW_QUERY_DEFAULT,
            "severities": list(QUERY_SEVERITIES),
            "recorder": {
                "retained": len(QUERY_RECORDER.samples()),
                "report": QUERY_RECORDER.report(top=5),
            },
        },
        "fingerprinting": {
            "rules": [dict(rule) for rule in SQL_NORMALIZATION_RULES],
            "rule_order_matters": True,
            "corpus": [
                {
                    "statement": sample,
                    "normalized": normalize_sql(sample),
                    "fingerprint": fingerprint_statement(sample),
                }
                for sample in NORMALIZATION_CORPUS
            ],
        },
        "explain": {
            "modes": list(EXPLAIN_MODES),
            "options": dict(EXPLAIN_OPTIONS),
            "executing_modes": list(EXPLAIN_EXECUTING_MODES),
            "gate": "EXPLAIN ANALYZE and any mutating statement require execute=True",
        },
        "diagnostics": {
            "settings": list(CONNECTION_DIAGNOSTIC_SETTINGS),
            "checks": [dict(check) for check in CONNECTION_DIAGNOSTIC_CHECKS],
            "verdicts": list(DIAGNOSTIC_VERDICTS),
            "comparators": list(DIAGNOSTIC_COMPARATORS),
            "formats": list(DIAGNOSTIC_FORMATS),
            "duration_units_ms": dict(POSTGRES_DURATION_UNITS),
        },
        "schema_drift": {
            "checks": [dict(check) for check in SCHEMA_DRIFT_CHECKS],
            "severities": list(DRIFT_SEVERITIES),
            "default_severity": DRIFT_SEVERITY_DEFAULT,
            "declared_tables": len(Base.metadata.tables),
            "note": (
                "compares live columns against Base.metadata, which holds only the "
                "tables imported so far; unimported models are invisible to this check"
            ),
        },
        "guarded_call": {
            "idempotent_attempts": int(resolve_db_ops_tunables()["guard_idempotent_attempts"]),
            "non_idempotent_retries": bool(resolve_db_ops_tunables()["guard_non_idempotent_retries"]),
            "circuit": DATABASE_CIRCUIT.snapshot(),
            "note": "the breaker records the outcome of the whole call, not of each attempt",
        },
        "tunables": [dict(tunable) for tunable in DB_OPS_TUNABLES],
        "validation": validation,
        "capabilities": {
            "validate_db_ops_policy": "referential integrity of every table in this layer",
            "simulate_db_ops": "pure baseline-vs-override slow-query comparison",
            "explain_plan_request": "pure explain gate; no database needed",
            "classify_statement": "verb, class, mutability, split target, fingerprint",
            "normalize_sql": "idempotent literal/param/list masking",
            "build_db_ops_catalog": "this document",
        },
    }
