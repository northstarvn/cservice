import asyncio
import logging
import os

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


# Pool tuning is env-driven so operators can scale without code changes.
DB_POOL_SIZE = _int_env("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _int_env("DB_MAX_OVERFLOW", 10)
DB_POOL_TIMEOUT = _int_env("DB_POOL_TIMEOUT", 30)
# echo stays True by default to preserve the historical behavior.
DB_ECHO = _bool_env("DB_ECHO", True)

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


async def retry_async(
    coro_factory,
    attempts: int = 3,
    delay_seconds: float = 0.5,
    logger: logging.Logger = None,
):
    """Re-invoke ``coro_factory()`` up to ``attempts`` times on failure.

    Useful for startup probes and idempotent recovery paths where a transient
    connection failure should not abort the whole request.
    """
    logger = logger or logging.getLogger(__name__)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning(
                    "Retryable operation failed (attempt %d/%d): %s",
                    attempt,
                    attempts,
                    exc,
                )
                await asyncio.sleep(delay_seconds)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_async called with attempts <= 0")