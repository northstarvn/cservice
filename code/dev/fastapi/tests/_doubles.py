"""Shared async-session doubles for the recovery and complaints test suites.

Both `test_recovery_governance_expansion.py` and
`test_predictive_recovery_expansion.py` needed the same three things: a result
shim, an empty-result shim, and a fake `AsyncSession` that routes `execute` by
table name. They each carried their own copy, and the copies drifted -- the
complaints work had to be applied to *both* `_FakeDb` classes, and a fix applied
to one and not the other would have shown up only as an unrelated test failure
in the other suite.

The routing is substring matching on the compiled statement, which is crude but
it is a test double: the point is to exercise the service's branching, not to
emulate a query planner. `refresh` is modelled because the real `AsyncSession`
has it and because `flush` expires every server-generated column, so a service
that serialises a freshly flushed row must reload it first. A double that
omitted `refresh` would have reported a `MissingGreenlet` the real database
never raises.
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import models
from app.db import Base


class Rows:
    """A non-empty result: `.scalars()`, `.all()`, `.first()`."""

    def __init__(self, rows: Iterable[Any]):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class Empty:
    """An empty result, for a table the double has no rows for."""

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


#: Statement substring -> the instance attribute holding that table's rows.
#: Order matters: the first match wins, so the more specific table names come
#: first. `chat_history` before `users` is not an issue, but `complaint_*` must
#: precede nothing in particular and `users` last, because several queries
#: mention more than one table.
_ROUTES: tuple[tuple[str, str], ...] = (
    ("points_wallets", "wallets"),
    ("chat_history", "chat_rows"),
    ("bookings", "bookings"),
    ("recovery_actions", "recovery_actions"),
    ("complaint_cases", "complaint_cases"),
    ("complaint_decisions", "complaint_decisions"),
    ("complaint_events", "complaint_events"),
    ("retention_snapshots", "retention_snapshots"),
    ("customer_policy_scores", "policy_scores"),
    ("arrears_entries", "arrears"),
    ("users", "users"),
)


class FakeDb:
    """Async session double: table-shaped `execute` routing, plus add/flush/commit/refresh.

    Anything added with `add()` and not committed stays in `pending`, and the
    `complaint_*` views read from there, which is what lets a test assert that a
    handler joined the caller's transaction rather than committing on its own.
    """

    #: Constructor keyword -> instance attribute. Kept as a table so adding a
    #: table is one row rather than a signature edit plus an assignment.
    _SLOTS: tuple[str, ...] = (
        "wallets",
        "chat_rows",
        "bookings",
        "users",
        "recovery_actions",
        "complaint_cases",
        "complaint_events",
        "complaint_decisions",
        "retention_snapshots",
        "policy_scores",
        "arrears",
    )

    def __init__(self, **seeded: Any):
        for slot in self._SLOTS:
            setattr(self, slot, list(seeded.pop(slot, ())))
        if seeded:
            raise TypeError(f"unknown seed tables: {sorted(seeded)}")
        self.pending: list[Any] = []
        self.next_id = 1
        self.commits = 0
        self.refreshes = 0

    # --- result routing ------------------------------------------------------

    def _rows_for(self, table_attr: str) -> list[Any]:
        if table_attr in self._SLOTS:
            return list(getattr(self, table_attr))
        return [obj for obj in self.pending if type(obj).__name__.startswith("Complaint")]

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any):
        text = str(statement)
        for needle, table_attr in _ROUTES:
            if needle in text:
                return Rows(self._rows_for(table_attr))
        return Empty()

    # --- write surface -------------------------------------------------------

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = self.next_id
            self.next_id += 1
        self.pending.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any, *_args: Any, **_kwargs: Any) -> Any:
        """Models the re-read a real session does after a flush.

        `flush` expires every column the database generates -- `TimestampMixin`
        updates `updated_at` through `onupdate=func.now()` even when a value was
        passed to the constructor -- so a service that serialises a flushed row
        without reloading it raises `MissingGreenlet` against a real AsyncSession.
        The double has no server to re-read from, so it only records the call.
        """
        self.refreshes += 1
        return obj

    # --- convenience ---------------------------------------------------------

    def pending_of(self, model: type) -> list[Any]:
        return [obj for obj in self.pending if isinstance(obj, model)]

    def complaint_rows(self) -> list[Any]:
        return self.pending_of(models.ComplaintCase)


class SqliteHarness:
    """A real in-memory database, driven through one event loop.

    `pytest-asyncio` is installed but not enabled for this suite, so this follows
    the convention of calling `run_until_complete` explicitly rather than
    introducing a plugin dependency. The loop is created once and owned by the
    harness because an aiosqlite connection is bound to the loop that opened it
    -- a fresh `asyncio.run` per statement would strand it.

    Use this rather than `FakeDb` when the code under test reads more than one
    query, or when a uniqueness constraint or a flush/expire interaction is part
    of what is being pinned. A session double asserts your own assumptions back
    at you; a real database does not.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.engine: Any = None
        self.session: AsyncSession | None = None

    async def setup(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = AsyncSession(self.engine, expire_on_commit=False)

    async def teardown(self) -> None:
        if self.session is not None:
            await self.session.close()
        if self.engine is not None:
            await self.engine.dispose()
        # aiosqlite runs its connection on a worker thread, so async generators
        # are still parked on the loop when the session closes.
        await self.loop.shutdown_asyncgens()

    def close(self) -> None:
        try:
            self.loop.close()
        except RuntimeError:
            # A loop aiosqlite still considers busy is not worth failing a test
            # over; it is garbage collected with the harness.
            pass

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def user(self, user_id: int = 1, admin: bool = False) -> Any:
        return self.run(self._user(user_id, admin))

    async def _user(self, user_id: int, admin: bool) -> models.User:
        user = models.User(
            id=user_id,
            username=f"u{user_id}",
            email=f"u{user_id}@example.com",
            full_name=f"User {user_id}",
            hashed_password="x",
            is_admin=admin,
        )
        self.session.add(user)
        await self.session.commit()
        return user

    def add(self, obj: Any) -> Any:
        self.session.add(obj)
        return obj

    def commit(self) -> Any:
        return self.run(self.session.commit())

    # Two thin conveniences for the complaints suite, which is the only caller
    # that opens and re-reads cases. They are here rather than in that file so a
    # second suite needing a real database does not grow its own copy of the
    # event-loop plumbing, which is the part that is easy to get wrong.
    def case(self, reference: str) -> Any:
        from app.services import complaints as _complaints

        return self.run(_complaints._load_case(self.session, reference=reference))

    def open(self, user_id: int = 1, **kwargs: Any) -> Any:
        from app.services import complaints as _complaints

        return self.run(_complaints.open_complaint(self.session, user_id, **kwargs))
