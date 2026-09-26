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
"""
import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        except Exception as e:
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
