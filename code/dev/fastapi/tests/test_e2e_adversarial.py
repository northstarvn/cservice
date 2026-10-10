"""Tier 4: the adversarial tier -- what happens when two things happen at once.

Every other tier in this suite sends one request and reads the answer. This one
sends several in the same tick and asks whether the system is still the system
the other tiers described. The question is not academic: the flows in tiers 2
and 3 are full of *one-tap* actions, and a one-tap action is a thing a customer
will perform twice, on two devices, on a flaky connection, because the first
response never arrived.

**The harness cannot express this on one session, and pretending otherwise is
the first thing this file rules out.** ``World`` holds a single
``AsyncSession``, so two coroutines interleaved through it are not two
transactions -- they are two halves of one, and ``asyncio.gather`` over them
raises ``RuntimeError: This event loop is already running`` or, once past that,
``Session is already flushing``. The first version of this tier tried it and got
a stack trace from the middle of ``app/main.py``. Concurrency therefore needs a
*second connection*, and it needs a file-backed database: SQLAlchemy gives
``:memory:`` a ``StaticPool``, so every "separate" session on an in-memory
database is quietly the same connection, which would make a lost-update test
report no lost update for the wrong reason.

:class:`TestTheHarnessCannotLie` pins those two facts so the rest of the tier
cannot be read as proving something it does not.

What the tier found, on the offer state machine:

* **Two concurrent customer decisions both commit.** Accept and decline on the
  same offer in the same tick returned ``accepted: True`` *and* ``declined: True``,
  and the append-only trail recorded ``accepted (offered -> accepted)`` followed
  by ``declined (offered -> declined)`` for one offer. The row settled on whichever
  committed last, so the customer-facing view and the audit view disagree about
  what one person did in one instant. Sequentially this is impossible -- the
  state machine refuses it -- which is exactly why it is worth testing: the
  guarantee is enforced by a read followed by a write, and two readers both see
  ``offered``.

  The **deterministic** half of that claim is
  ``test_the_state_machine_permits_both_decisions_from_the_same_state``: no
  timing at all. ``OFFER_TRANSITIONS["offered"]`` lists ``accepted``, ``declined``
  and ``expired`` with no mutual exclusion between any two of them, so there is
  no "one decision per offer" notion in the machine to violate. The race is only
  how two callers reach the same state. A defect report resting on a race alone
  rests on the scheduler.

* **The same holds for accept/accept and fulfil/fulfil**, which double-write
  their events without disagreeing about the outcome. Less alarming, still a
  trail that says something happened twice.

* **The one race the design does close still closes.** Accept and fulfil
  together: the fulfilment is refused with ``an offer cannot go offered ->
  fulfilled``, because it read the pre-transition status. That refusal is the
  whole reason ``fulfilled`` is unreachable from ``offered``, and it is asserted
  here so the finding above cannot be read as "the race is entirely unguarded".

* **`_load_offer` is the only money-touching read in the codebase without a
  lock.** ``transfers._wallet_for_update`` uses ``with_for_update()`` and its
  docstring names this exact hazard -- "two concurrent transfers from the same
  wallet that both read a balance will both pass the sufficiency check and then
  overdraw". The offer subsystem reads a row, checks a state machine against it,
  and writes a different state, with no lock, in the same codebase. That
  asymmetry is asserted structurally in
  :class:`TestTheLockIsMissingExactlyWhereItMatters` so it cannot be forgotten
  once it is fixed.

**Two rules about writing the races, both learned here the hard way.** Every race
runs under :func:`both_branches_have_read`, which holds each branch at the
barrier after it loads the offer. Without it these tests reproduced on 10 of 12
runs -- a 17%-reliable test of a 100%-real defect, because a scheduler that ran
one branch to commit before the other started would produce exactly the clean
refusal a *correct* implementation gives. And no race here asserts **which**
branch won: with the barrier in place the double-commit is deterministic, but the
winner is decided by how two writers serialise on SQLite's file lock, so the
first draft's ``status == "declined"`` failed 7 times in 20 for that reason
alone. A race test that asserts the winner is asserting the scheduler, and it
will be flaky in the shape that reads like a product bug.

The behaviour assertions above are written as **characterisation tests**: they
assert what the code does, and every one says in its own docstring that what it
does is the defect. Asserting the correct behaviour instead would fail, and a
red suite is not a report. Fixing the race properly -- a conditional
``UPDATE ... WHERE status = 'offered'``, or a lock -- is a change to a
money-moving subsystem and is left to whoever owns it, with the failing
invariant written out in ``FLOW_ASSURANCE.md`` so it can be written as a test the
moment the fix lands.
"""

from __future__ import annotations

import asyncio
import ast
import contextlib
import inspect
import os
import tempfile
import textwrap
from typing import Any, Callable, Optional

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from _e2e_world import World, world

ANA = 1


class Raced:
    """A file-backed database with more than one real connection.

    Every method returns a *committed* result or a rolled-back exception, so a
    caller cannot forget the distinction. A race test that leaves one branch
    uncommitted measures the harness rather than the code under test.

    The engine is deliberately **not** the world's. Tiers 1-3 use
    ``SqliteHarness``'s in-memory database, which is a ``StaticPool``: every
    session on it is the same connection, and two "concurrent" sessions there
    serialise themselves before they ever reach the code under test.
    """

    def __init__(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="cservice-race-")
        self.path = os.path.join(self._dir, "race.db")
        self.engine: Any = None

    async def setup(self) -> None:
        from app.db import Base

        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()

    def session(self) -> AsyncSession:
        return AsyncSession(self.engine, expire_on_commit=False)

    def run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run one branch to completion in its own transaction.

        Returns the payload on commit and ``{"__error__": ..., "__type__": ...}``
        on rollback. The rollback is not tidiness: a failed commit leaves the
        session unusable, and the next branch would then fail with a stale error
        rather than its own.
        """
        session = self.session()
        try:
            result = await fn(session, *args, **kwargs)
            await session.commit()
            return dict(result) if isinstance(result, dict) else {"value": result}
        except Exception as exc:  # noqa: BLE001 - the exception *is* the observation
            await session.rollback()
            return {"__type__": type(exc).__name__, "__error__": str(exc)[:120]}
        finally:
            await session.close()

    async def one(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        """A single non-racing call, for arranging state."""
        return await self.call(fn, *args, **kwargs)

    async def rows(self, model: Any, **where: Any) -> list[Any]:
        session = self.session()
        try:
            from sqlalchemy import select

            statement = select(model)
            for column, value in where.items():
                statement = statement.where(getattr(model, column) == value)
            return list((await session.execute(statement)).scalars().all())
        finally:
            await session.close()

    async def events(self, offer_id: Optional[int] = None) -> list[tuple]:
        """The offer trail as ``(kind, from, to)``, in insertion order."""
        rows = await self.rows(_models().CustomerOfferEvent)
        if offer_id is not None:
            rows = [row for row in rows if int(row.offer_id) == int(offer_id)]
        return [(str(row.kind), str(row.from_status or ""), str(row.to_status or "")) for row in rows]

    async def write(self, mutate: Callable[[AsyncSession], Any]) -> Any:
        """Arrange state directly, for things the service deliberately cannot do.

        Two of the fulfilment tests need to put the database into a state the
        service will not produce on request -- a discount the issuer never sets, a
        pre-existing credit for the same offer. Arranging those through the service
        would mean either widening the API or asserting against a fiction, and both
        would leave the test testing the arrangement rather than the behaviour.
        Committed, so the next read sees it.

        ``mutate`` may be sync or async and is awaited either way. That is not
        leniency for its own sake: a mutate that has to await a query to find the
        row it is editing is the natural way to write these, and silently not
        awaiting it leaves the arrangement un-applied while the assertion after it
        still passes for the wrong reason.
        """
        session = self.session()
        try:
            result = mutate(session)
            if inspect.isawaitable(result):
                result = await result
            await session.commit()
            return result
        finally:
            await session.close()

    async def offer(self, reference: str) -> Any:
        """The offer row itself, so a test can change a column on it."""
        return (await self.rows(_models().CustomerOffer, reference=reference))[0]


def _models():
    from app import models

    return models


#: How long a racer waits for the others to finish reading before the test fails.
#: Present so that "the other branch never arrived" is a red test with a readable
#: message rather than a hang that reports nothing. See
#: :func:`both_branches_have_read`.
GATE_TIMEOUT = 10.0


@pytest.fixture()
def raced():
    """The racing database. Torn down on the way out, including after a failure."""
    racer = Raced()
    asyncio.run(racer.setup())
    try:
        yield racer
    finally:
        asyncio.run(racer.close())


async def _seed(raced: Raced) -> None:
    models = _models()
    session = raced.session()
    session.add(
        models.User(
            id=ANA, username="ana", email="ana@example.com", full_name="Ana",
            hashed_password="x",
        )
    )
    await session.commit()
    await session.close()


@contextlib.contextmanager
def both_branches_have_read(module: Any, parties: int = 2):
    """Hold every racer at the point *after* it has read the row.

    Without this, a race test is a coin flip.

    Without this, a race test is a coin flip. ``asyncio.gather`` schedules two
    coroutines and the interleaving depends on where each one happens to await,
    so the second branch often does not start until the first has committed -- at
    which point the second reads ``accepted``, refuses cleanly, and reports
    exactly what a *correct* implementation does. Measured on this tree: the
    accept/decline race below reproduced on 10 of 12 runs, so as an assertion it
    was a 17%-reliable test of a 100%-real defect.

    The defect is that correctness depends on timing, so the honest way to test
    it is to control the timing. This wrapper makes every racer wait at the barrier
    after ``_load_offer`` returns and before the caller writes, which is the
    window the bug lives in. It touches **no production code** and changes no
    logic -- it only decides when each branch gets to run, which is the one
    variable a concurrency test exists to vary.

    **One-shot, and that detail is load-bearing.** ``asyncio.Barrier`` resets
    itself after it trips, so it gates *every* read -- including the one a losing
    branch makes afterwards to find out what it lost to. With two racers and a
    barrier of two, that second read waits for a third party that will never come
    and the test hangs forever. A barrier that hangs is worse than a flaky test:
    it burns a CI slot and reports nothing. So this gates exactly the first
    ``parties`` reads and no more, and the re-read that follows the write is left
    alone.

    ``parties`` is how many branches must arrive before anyone proceeds. A branch
    that takes a different code path and never reads the offer would wait forever,
    so the wait is bounded: :data:`GATE_TIMEOUT` turns "hung" into a failed test
    with a message that says which branch did not arrive. The bound is generous
    because a slow machine should not produce a false negative on a defect that is
    real.
    """
    real = module._load_offer
    pending = parties
    released = asyncio.Event()

    async def gated(*args: Any, **kwargs: Any) -> Any:
        nonlocal pending
        row = await real(*args, **kwargs)
        if pending > 0:
            pending -= 1
            if pending == 0:
                released.set()
            await asyncio.wait_for(released.wait(), GATE_TIMEOUT)
        return row

    module._load_offer = gated
    try:
        yield
    finally:
        module._load_offer = real


async def _issue(
    raced: Raced,
    *,
    user_id: int = ANA,
    kind: str = "goodwill",
    waiver_type: str = "",
) -> str:
    from app.services import customer_offers

    offered = await raced.one(
        customer_offers.issue_offer,
        user_id=user_id,
        kind=kind,
        recovery_context={"recovery_readiness": "high", "churn_risk": "high"},
        waiver_type=waiver_type,
        actor="admin:root",
    )
    assert offered["issued"] is True, offered
    return str(offered["reference"])


def await_commit(raced: Raced, mutate: Optional[Callable[[Any], None]] = None) -> Any:
    """Run one write against the racing database, on its own session."""
    return raced.run(_write(raced, mutate))


async def _write(raced: Raced, mutate: Optional[Callable[[Any], None]]) -> Any:
    session = raced.session()
    try:
        if mutate is not None:
            mutate(session)
        await session.commit()
    finally:
        await session.close()


def await_offer(raced: Raced, reference: str) -> Any:
    """The offer row, loaded so a test can change a column on it."""
    return raced.run(raced.offer(reference))


class TestTheHarnessCannotLie:
    """Two facts about concurrency testing, asserted so nothing here misleads.

    Written before the findings, because every assertion below depends on them.
    A concurrency suite that cannot itself race is worse than none: it reports
    green and the reader concludes the code is safe.
    """

    def test_the_world_database_cannot_express_a_race(self, world):
        """One ``AsyncSession``, so ``gather`` over it is not two transactions.

        Asserted rather than assumed, because the World's own docstring calls
        ``gather`` "run coroutines on this world's single loop, concurrently" and
        that word is doing more work than the implementation supports. The word
        is right about the loop and wrong about the transaction, so this test
        pins the part that is wrong.
        """
        from tests._doubles import SqliteHarness  # noqa: F401  (import guard only)

        assert world.session is world.harness.session, (
            "the World shares one session with its harness, so two concurrent "
            "requests are two halves of one transaction"
        )

    def test_the_racing_database_really_has_separate_connections(self, raced):
        """File-backed, because ``:memory:`` is a ``StaticPool`` and one connection.

        Without this, a lost-update test on ``:memory:`` passes for the wrong
        reason -- the two "concurrent" writes serialised, so the answer looks
        safe -- and the file-backed case here is what makes the same test mean
        something.
        """
        import sqlalchemy
        from sqlalchemy.pool import StaticPool

        assert isinstance(raced.engine.pool, StaticPool) is False, (
            "a StaticPool serialises every session onto one connection, so no "
            "test against it can observe a race"
        )

        async def check() -> bool:
            a, b = raced.session(), raced.session()
            try:
                models = _models()
                a.add(models.User(id=9002, username="probe", email="p@e.com",
                                  full_name="P", hashed_password="x"))
                await a.commit()
                from sqlalchemy import select

                found = (
                    await b.execute(select(models.User).where(models.User.id == 9002))
                ).scalar_one_or_none()
                return found is not None
            finally:
                await a.close()
                await b.close()

        assert asyncio.run(check()) is True, (
            "a session opened after another committed did not see the commit, so "
            "the two sessions are not actually independent"
        )

    def test_four_sessions_commit_concurrently_without_a_locked_database(self, raced):
        """The setup itself must not be the thing that fails.

        SQLite serialises writers with a file lock. If that surfaced as an
        ``OperationalError`` here, every race in this file would be measuring
        SQLite's locking rather than the code's logic -- so the four-way insert is
        asserted to succeed, which is what makes the rest meaningful.
        """
        from app import models

        async def write(n: int) -> dict[str, Any]:
            return await raced.call(
                lambda s: _insert_user(s, models, n),
            )

        results = asyncio.run(_gather(*(write(n) for n in range(4))))
        assert all("__error__" not in row for row in results), results


async def _insert_user(session, models, n: int) -> None:
    session.add(
        models.User(
            id=9100 + n, username=f"racer{n}", email=f"r{n}@e.com",
            full_name="R", hashed_password="x",
        )
    )


async def _gather(*coros: Any) -> list[Any]:
    return list(await asyncio.gather(*coros))


class TestTheOfferStateMachineUnderRace:
    """Two concurrent decisions on one offer. Both cases asserted, both ways.

    These were **characterisation** tests: they asserted the defect, with the
    correct answer named in each docstring, so that fixing the race would turn
    them red with a message saying which invariant had been restored. That is what
    happened. ``_claim_transition`` in ``services/customer_offers.py`` moved every
    state change onto a conditional ``UPDATE ... WHERE status = <what was read>``
    and checks the row count, and the four assertions below are now the thing they
    said the fix should produce.

    Every race here runs under :func:`both_branches_have_read`. The first draft
    did not, and was a 17%-reproducible test of a 100%-real defect: without
    control over the interleaving, a scheduler that happened to run one branch to
    commit before the other started would produce a clean refusal, and a test that
    passes when the bug is absent cannot be the thing that notices when it is
    present.
    """

    def test_the_state_machine_permits_both_decisions_from_the_same_state(self):
        """The static half of the finding, and it is deliberately still true.

        ``OFFER_TRANSITIONS["offered"]`` lists ``accepted``, ``declined`` **and**
        ``expired`` with no mutual exclusion between any two of them, and that has
        not changed -- it should not. The state machine's job is to say which
        *statuses* are reachable, and "one decision per offer" is not a status
        question; it is a concurrency question, and it belongs in the write rather
        than in the table.

        Pinned here because it is what makes the race tests mean anything: the
        table permits both, so nothing *above* the database can prevent both, and
        the guard has to be the conditional write. If someone ever adds mutual
        exclusion to the table instead, this test says so and the race tests below
        need to be re-examined for whether they are still testing the database.
        """
        from app.services import customer_offers

        targets = set(customer_offers.OFFER_TRANSITIONS["offered"])
        assert {"accepted", "declined"} <= targets, sorted(targets)
        assert customer_offers.resolve_offer_transition("offered", "accepted")["allowed"] is True
        assert customer_offers.resolve_offer_transition("offered", "declined")["allowed"] is True
        # And the follow-up transition the design does care about is refused, so
        # the above is a statement about `offered` and not about the machine being
        # permissive in general.
        assert customer_offers.resolve_offer_transition("accepted", "declined")["allowed"] is False
        assert customer_offers.resolve_offer_transition("offered", "fulfilled")["allowed"] is False

    def test_two_concurrent_accepts_produce_one_acceptance_and_one_refusal(self, raced):
        """Two taps, one acceptance, one honest refusal.

        Not pre-accepted: the race *is* the acceptance, so arranging one first
        makes both branches the ordinary "already accepted" refusal and the test
        proves nothing. (The first draft of this file did exactly that and got two
        clean refusals, which is what a correct implementation looks like and is
        the opposite of the finding.)

        The refusal has to be *the same refusal a sequential second tap gets* --
        ``found: True``, naming the status -- rather than a race-flavoured message
        the customer has no way to interpret. Asserted explicitly, because "one
        wins" is only half of what a customer pressing the button twice needs.
        """
        from app.services import customer_offers

        reference = asyncio.run(_prepare(raced))

        async def accept() -> dict[str, Any]:
            return await raced.call(
                customer_offers.accept_offer,
                reference=reference,
                user_id=ANA,
                actor="customer:ana",
            )

        with both_branches_have_read(customer_offers):
            first, second = asyncio.run(_gather(accept(), accept()))

        winners = [r for r in (first, second) if r.get("accepted") is True]
        losers = [r for r in (first, second) if r.get("accepted") is not True]
        assert len(winners) == 1, (
            "two taps must produce exactly one acceptance; "
            f"got {len(winners)}. first={first} second={second}"
        )
        assert len(losers) == 1, f"the loser vanished rather than refusing: {first} {second}"
        # The refusal is a real refusal, not a 404 or a crash, and it names the
        # state that beat it -- which is the answer the *sequential* second tap
        # gets. Same shape, so the one-tap UI has one thing to render.
        assert losers[0].get("found") is True, losers[0]
        assert losers[0].get("status") == "accepted", losers[0]
        assert "cannot be accepted" in str(losers[0].get("reason")), losers[0]

        accepts = [row for row in asyncio.run(raced.events()) if row[0] == "accepted"]
        assert len(accepts) == 1, f"the trail records the acceptance {len(accepts)} times: {accepts}"
        offers = asyncio.run(raced.rows(_models().CustomerOffer))
        assert [row.status for row in offers] == ["accepted"], offers

    def test_a_concurrent_accept_and_decline_produce_exactly_one_decision(self, raced):
        """The one worth a defect report, and now the one worth a regression test.

        Two mutually exclusive decisions by the same person on the same offer, in
        the same tick. What used to happen: both returned success and the trail
        recorded **both** -- ``accepted (offered -> accepted)`` then
        ``declined (offered -> declined)`` for one offer, so the row and the audit
        trail disagreed about what one person did in one instant.

        The invariant, which this now asserts: **at most one of accept and decline
        may commit per offer**, and the loser is told which decision won.
        """
        from app.services import customer_offers

        asyncio.run(_seed(raced))
        reference = asyncio.run(_issue(raced))

        async def accept() -> dict[str, Any]:
            return await raced.call(
                customer_offers.accept_offer,
                reference=reference, user_id=ANA, actor="customer:ana",
            )

        async def decline() -> dict[str, Any]:
            return await raced.call(
                customer_offers.decline_offer,
                reference=reference, user_id=ANA, reason="changed my mind",
                actor="customer:ana",
            )

        with both_branches_have_read(customer_offers):
            accepted, declined = asyncio.run(_gather(accept(), decline()))

        # The invariant, stated the way the characterisation test said it should
        # become: `sum(... or ...) == 1`.
        assert sum(
            1 for r in (accepted, declined) if r.get("accepted") or r.get("declined")
        ) == 1, (
            "at most one of accept/decline may commit per offer; both did. "
            f"accepted={accepted} declined={declined}"
        )

        trail = asyncio.run(raced.events())
        decisions = [row for row in trail if row[0] in {"accepted", "declined"}]
        assert len(decisions) == 1, (
            f"the trail must record one decision for one offer, got {decisions}"
        )
        won = "accepted" if decisions[0][0] == "accepted" else "declined"

        # The row and the trail agree -- that was the whole damage.
        offers = asyncio.run(raced.rows(_models().CustomerOffer))
        assert [row.status for row in offers] == [won], (
            f"the row says something other than the one decision recorded: {offers}"
        )

        # And the loser is told what beat it, by name.
        loser = declined if won == "accepted" else accepted
        assert loser.get("found") is True, loser
        assert loser.get("status") == won, (
            f"the losing branch was not told which decision won: {loser}"
        )

    def test_the_race_the_design_does_close_is_still_closed(self, raced):
        """The negative control for the two above.

        Accept and fulfil together: the fulfilment must be refused. It is, because
        ``fulfilled`` is unreachable from ``offered`` -- and now for two
        independent reasons, which is the point of keeping it. The state machine
        refuses it, *and* the conditional write refuses it, because by the time
        the fulfilment runs the row is no longer ``offered``.

        Run **under the barrier too**, which is what makes it a control rather
        than a hope. Without the barrier this test passes for a boring reason --
        the fulfil branch simply never gets scheduled before the accept commits --
        so it would keep passing even if the guard were removed.
        """
        from app.services import customer_offers

        asyncio.run(_seed(raced))
        reference = asyncio.run(_issue(raced))

        async def accept() -> dict[str, Any]:
            return await raced.call(
                customer_offers.accept_offer,
                reference=reference, user_id=ANA, actor="customer:ana",
            )

        async def fulfil() -> dict[str, Any]:
            return await raced.call(
                customer_offers.record_outcome, reference, "fulfil", actor="admin:root",
            )

        with both_branches_have_read(customer_offers):
            accepted, fulfilled = asyncio.run(_gather(accept(), fulfil()))

        assert accepted.get("accepted") is True, accepted
        assert fulfilled.get("recorded") is False, (
            "an offer nobody had accepted yet was marked fulfilled: " + repr(fulfilled)
        )
        assert "offered -> fulfilled" in str(fulfilled.get("reason")), fulfilled
        # And it applied no effect, which is the half that matters now that
        # fulfilment credits a wallet.
        assert "effect" not in fulfilled, fulfilled

    def test_two_concurrent_fulfilments_produce_one_and_one_credit(self, raced):
        """Double fulfilment, which is double-*claiming* rather than double-spending.

        Worth its own test because of what fulfilment means: it is the assertion
        that the effect **landed**. Two events both saying it landed is a
        completeness claim about the world made twice, and now that fulfilment
        moves real money it would also be a wallet credited twice.

        So this asserts both halves, and they are different guards: the second
        fulfilment is refused by the conditional write (the offer is no longer
        ``accepted``), and the money moved once. The second is *also* checked at
        the ledger by :meth:`TestFulfilmentCreditsNothing`, because "the credit
        happened once" and "the second caller did not get to try" are independent
        claims and either could regress alone.
        """
        from app.services import customer_offers

        reference = asyncio.run(_prepare(raced, accept_once=True, balance=500.0))

        async def fulfil() -> dict[str, Any]:
            return await raced.call(
                customer_offers.record_outcome, reference, "fulfil", actor="admin:root",
            )

        with both_branches_have_read(customer_offers):
            first, second = asyncio.run(_gather(fulfil(), fulfil()))

        assert sum(1 for r in (first, second) if r.get("recorded") is True) == 1, (
            "two ticks of the same fulfilment button must record once; "
            f"first={first} second={second}"
        )
        loser = next(r for r in (first, second) if r.get("recorded") is not True)
        assert loser.get("found") is True, loser
        assert loser.get("status") == "fulfilled", loser

        fulfilments = [row for row in asyncio.run(raced.events()) if row[0] == "fulfilled"]
        assert len(fulfilments) == 1, f"the trail records {len(fulfilments)} fulfilments"
        assert asyncio.run(_balance(raced)) == 750.0, (
            "the offer promised 250 points onto a 500 balance; exactly one credit"
        )


class TestFulfilmentAppliesWhatItPromised:
    """``fulfilled`` means "the effect landed", so fulfilment now lands it.

    **The finding this replaces.** An operator fulfilled a 250-point goodwill
    offer -- ``recorded: True``, status ``fulfilled``, an event in the trail --
    and the customer's wallet did not move and no ``PointsTransaction`` was
    written. The same module's comment says ``fulfilled`` means "the effect
    landed", ``OFFER_EXPLANATIONS`` tells the customer "We put {points} points on
    your account" in the present perfect, and the whole reason ``accepted`` and
    ``fulfilled`` are separate states is that "you accepted and we have not done it
    yet" is "a real and embarrassing state". Marking it ``fulfilled`` without the
    effect moved that state into the one place it could not be observed, and
    ``build_offer_admin_report`` -- whose job is answering *"did we actually
    deliver it?"* -- counted the row as delivered.

    **The decision taken, and why.** This class previously said the answer was a
    product decision and left the question open. It is closed now, in favour of
    *the effect is applied where the effect is expressible, and the parts that are
    not are named rather than implied*:

    * ``goodwill`` points go onto the wallet through
      ``recovery_playbooks.credit_recovery_points`` -- the same canonical writer
      the playbook path uses, so there is one place in the codebase that moves a
      balance for a recovery credit.
    * ``discount_percent``, ``waiver`` and ``priority`` are reported under
      ``requires_out_of_band`` with a reason. None of them has a subsystem here
      that can act on it: no pricing engine reads ``discount_percent``, and
      ``customer_offers.arrears_entry_id`` is populated by nothing. Reporting them
      as delivered would have been the original bug wearing a different hat, and
      silently dropping them would leave an operator with no way to know what is
      still outstanding.

    The ``note`` field keeps its original meaning -- an operator's record of work
    done outside this service -- and the response now reports which components
    still needed it, so the two are distinguishable months later.
    """

    def test_a_fulfilled_offer_credits_the_points_it_promised(self, raced):
        from app.services import customer_offers

        reference = asyncio.run(_prepare(raced, accept_once=True, balance=500.0))
        before = asyncio.run(_balance(raced))
        assert before == 500.0, before

        fulfilled = asyncio.run(
            raced.one(customer_offers.record_outcome, reference, "fulfil",
                      actor="admin:root", note="250 points added")
        )
        assert fulfilled.get("recorded") is True, fulfilled
        assert fulfilled.get("status") == "fulfilled", fulfilled

        after = asyncio.run(_balance(raced))
        assert after == 750.0, (
            f"the offer promised 250 points and the wallet went {before} -> {after}"
        )
        ledger = asyncio.run(raced.rows(_models().PointsTransaction))
        assert len(ledger) == 1, f"expected one ledger row for one credit: {ledger}"
        assert float(ledger[0].points_delta) == 250.0, ledger[0]
        # Keyed by the offer, so a second credit for the same offer is
        # detectable at the ledger and not only by reading the trail.
        assert ledger[0].reference == f"offer:{reference}", ledger[0].reference

    def test_the_response_says_what_moved_and_what_is_still_outstanding(self, raced):
        """The machine answers the question, instead of saying yes to one it never
        was asked.

        ``recorded: true`` used to be the whole answer, and it was a response to
        no question: nothing in the function checked whether an effect had landed
        and nothing in the response could say. A fulfilment is the one event in
        this subsystem that is a claim about the world, so it is the one event
        that has to report on the world.
        """
        from app.services import customer_offers

        reference = asyncio.run(_prepare(raced, accept_once=True, balance=500.0))
        result = asyncio.run(
            raced.one(customer_offers.record_outcome, reference, "fulfil",
                      actor="admin:root", note="250 points added")
        )
        effect = result["effect"]
        assert effect["applied"] is True, effect
        assert effect["credited_points"] == 250.0, effect
        assert effect["components"]["points"]["automatic"] is True, effect
        assert effect["components"]["points"]["wallet_balance"] == 750.0, effect

        # And the trail carries the same structure, so "did we deliver it?" is
        # answerable from the audit table rather than only from a live response.
        events = asyncio.run(raced.events())
        fulfilment = [row for row in events if row[0] == "fulfilled"]
        assert len(fulfilment) == 1, events
        assert fulfilment[0][2] == "fulfilled", fulfilment[0]

    def test_a_component_nothing_can_apply_is_reported_not_implied(self, raced):
        """A discount this service cannot honour is named, not swallowed.

        The offer carries ``discount_percent`` and the customer-facing sentence
        promises the discount, but nothing in this repository consumes the column.
        So fulfilment says so explicitly. This is the case that decides the design:
        the alternative -- crediting points and staying silent about the discount
        -- would still leave "the effect landed" half true, which is the claim
        that was broken in the first place.
        """
        from app.services import customer_offers

        reference = asyncio.run(_prepare(raced, accept_once=True, balance=500.0))

        async def set_discount(session: Any) -> None:
            offer = (
                await raced.rows(_models().CustomerOffer, reference=reference)
            )[0]
            offer.discount_percent = 15.0
            session.add(offer)

        asyncio.run(raced.write(set_discount))

        result = asyncio.run(
            raced.one(customer_offers.record_outcome, reference, "fulfil",
                      actor="admin:root")
        )
        assert result["recorded"] is True, result
        effect = result["effect"]
        assert effect["requires_out_of_band"] == ["discount_percent"], effect
        assert effect["components"]["discount_percent"]["automatic"] is False, effect
        assert "pricing engine" in str(effect["components"]["discount_percent"]["why"]), effect
        # The part that *can* be applied still was.
        assert effect["credited_points"] == 250.0, effect

    def test_a_second_credit_for_the_same_offer_is_refused_not_skipped(self, raced):
        """Defence in depth, and it refuses loudly rather than quietly doing
        nothing.

        ``_claim_transition`` is the real guarantee -- ``fulfilled`` is terminal
        and can only be claimed once. This covers the case where something *else*
        writes a second credit: the fulfilment must report it rather than add to
        it, because silently skipping would leave ``fulfilled`` recorded against
        an offer whose effect had been applied an unknown number of times.
        """
        from app.services import customer_offers

        reference = asyncio.run(_prepare(raced, accept_once=True, balance=500.0))
        models = _models()

        async def pre_existing_credit(session: Any) -> None:
            session.add(
                models.PointsTransaction(
                    user_id=ANA, point_type="loyalty_points",
                    kind="recovery_credit", points_delta=250.0,
                    reference=f"offer:{reference}",
                )
            )

        asyncio.run(raced.write(pre_existing_credit))
        assert asyncio.run(_balance(raced)) == 500.0, (
            "the pre-existing credit has to be a ledger row only -- it did not move "
            "the wallet, so the balance is still the 500 the test set up"
        )

        result = asyncio.run(
            raced.one(customer_offers.record_outcome, reference, "fulfil",
                      actor="admin:root")
        )
        assert result.get("recorded") is True, result
        assert result["effect"]["applied"] is False, result["effect"]
        assert result["effect"].get("double_credit_refused") is True, result["effect"]
        assert result["effect"]["requires_out_of_band"] == ["points"], result["effect"]
        # Still one ledger row: the pre-existing one. Nothing was added.
        assert len(asyncio.run(raced.rows(_models().PointsTransaction))) == 1

    def test_an_offer_with_nothing_to_credit_says_so_rather_than_failing(self, raced):
        """The zero case, which is the ordinary case for a waiver.

        A waiver offer has no points, so there is nothing to credit. That must be
        a successful fulfilment with an honest report -- not an error, and not a
        silent success with nothing in it.
        """
        from app.services import customer_offers

        asyncio.run(_seed(raced))
        reference = asyncio.run(_issue(raced, kind="waiver", waiver_type="fees"))
        accepted = asyncio.run(
            raced.one(customer_offers.accept_offer, reference, user_id=ANA,
                      actor="customer:ana")
        )
        assert accepted.get("accepted") is True, accepted

        result = asyncio.run(
            raced.one(customer_offers.record_outcome, reference, "fulfil",
                      actor="admin:root", note="removed from the invoice in billing")
        )
        assert result.get("recorded") is True, result
        effect = result["effect"]
        assert effect["applied"] is False, effect
        assert effect["components"]["waiver"]["automatic"] is False, effect
        # Named as outstanding, because the charge is removed in a system this
        # repository cannot see and the operator's note is the only record of it.
        assert "waiver" in effect["requires_out_of_band"], effect
        assert asyncio.run(_balance(raced)) == 0.0, "a waiver must not create a wallet"


class TestTheLockIsMissingExactlyWhereItMatters:
    """Structural, so the asymmetry cannot be forgotten -- or quietly repeated.

    ``services/transfers.py`` locks the wallet row it reads, and the docstring on
    that lock names this exact hazard: two concurrent transfers that both read a
    balance will both pass the sufficiency check and then overdraw. The offer
    subsystem read a row, checked a state machine against it, and wrote a
    different state without any guard at all -- which is how accept and decline
    both committed for one offer.

    That gap is closed now, and **not with a lock**:
    :func:`customer_offers._claim_transition` makes the state change a single
    conditional ``UPDATE`` and checks the row count. So these tests are no longer
    "the offer path has no lock" -- they are "nobody in this codebase guards a
    read-modify-write with anything weaker than a predicate or a lock", which is
    the property actually worth holding.

    Asserted as a structural fact rather than as a behaviour, because a
    behavioural test would have to race, and :class:`TestTheOfferStateMachineUnderRace`
    is where the racing happens. This class shows the *absence of a guard* on every
    day instead.
    """

    @staticmethod
    def _lock_callers() -> list[str]:
        """Which modules actually *call* ``with_for_update()``, not which mention it.

        Parsed with :mod:`ast` rather than grepped, because the first version of
        this test was a substring search over file text and it started failing the
        moment ``_claim_transition``'s docstring explained why it does *not* use
        one. That is the failure mode worth avoiding: a test that cannot tell
        "calls this" from "writes about this" punishes the next person for
        documenting the fix, and the lesson they take away is not to document it.

        So this counts call sites. Prose about locking is allowed and encouraged.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "app"
        found: list[str] = []
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - would be a real problem
                raise AssertionError(f"{path} does not parse")
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "with_for_update"
                ):
                    found.append(path.name)
        return sorted(set(found))

    def test_the_only_locked_read_in_the_codebase_is_the_wallet_one(self):
        assert self._lock_callers() == ["transfers.py"], (
            "the set of modules that lock a row before reading it changed. If a new "
            "one appeared, this test should say how it is guarded, and whether the "
            f"offer subsystem is still relying only on a conditional write: "
            f"{self._lock_callers()}"
        )

    def test_the_wallet_lock_documents_the_hazard_the_offer_path_has(self):
        from app.services import transfers

        source = transfers._wallet_for_update.__doc__ or ""
        assert "with_for_update" in source, transfers._wallet_for_update
        assert "concurrent" in source, (
            "the wallet lock stopped explaining why it exists, which is the only "
            "thing that stops the next reader copying an unguarded pattern"
        )

    def test_the_offer_path_guards_its_state_change_with_a_predicate_not_a_lock(self):
        """The specific mechanism, named so it can be found again.

        Asserted structurally rather than behaviourally on purpose. The behaviour
        is :class:`TestTheOfferStateMachineUnderRace`'s job; this pins *how* it is
        achieved, because a future reader who replaces the conditional write with
        a read-then-write -- or with a lock, which on SQLite is silently a no-op and
        would be green here and unprotected in production -- should trip this
        rather than discover it in production.

        On the ``ast`` parse: :meth:`_lock_callers` exists because a substring
        search cannot tell "calls this" from "writes about this", and
        ``_claim_transition``'s docstring is a paragraph about why it does not take
        a lock. Asserting on raw source text here would mean the test fails the
        moment somebody explains the fix, and the lesson that teaches is not to
        explain the fix.
        """
        import inspect

        from app.services import customer_offers

        def locks_in(fn: Any) -> bool:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            return any(
                isinstance(node, ast.Attribute) and node.attr == "with_for_update"
                for node in ast.walk(tree)
            )

        assert not locks_in(customer_offers._claim_transition), (
            "_claim_transition now takes a row lock. Note that this is a no-op on "
            "SQLite, so every race test in this file would still pass and the "
            "production database would be the first place it mattered."
        )
        # The load stays unlocked too: the guard is in the write, which is the point.
        assert not locks_in(customer_offers._load_offer)

        # Every mutator goes through it, not just some of them. A partial
        # migration would leave one path unguarded and nothing would say so.
        for fn in (
            customer_offers.accept_offer,
            customer_offers.decline_offer,
            customer_offers.record_outcome,
            customer_offers.expire_offers_due,
        ):
            body = inspect.getsource(fn)
            assert "_claim_transition" in body, (
                f"{fn.__name__} no longer claims its transition conditionally, so "
                "its write is a read-then-write again. One unguarded mutator is "
                "enough to re-open this whole defect."
            )


async def _prepare(raced: Raced, *, accept_once: bool = False, balance: float = 0.0) -> str:
    """One customer, optionally with a wallet, plus an issued offer.

    Returns the reference rather than parking it in a module global: a global
    written by a fixture and read by a later test is state that leaks between
    them, and the only reason it is tempting is that threading a string through
    every test signature is noisier. Returning it keeps the ordering explicit.
    """
    from app.services import customer_offers

    models = _models()
    session = raced.session()
    session.add(
        models.User(id=ANA, username="ana", email="ana@example.com", full_name="Ana",
                    hashed_password="x")
    )
    if balance:
        session.add(
            models.PointsWallet(user_id=ANA, point_type="loyalty_points", balance=balance)
        )
    await session.commit()
    await session.close()

    reference = await _issue(raced)
    if accept_once:
        accepted = await raced.one(
            customer_offers.accept_offer, reference=reference, user_id=ANA,
            actor="customer:ana",
        )
        assert accepted.get("accepted") is True, accepted
    return reference


async def _balance(raced: Raced) -> float:
    rows = await raced.rows(_models().PointsWallet, user_id=ANA)
    return float(rows[0].balance) if rows else 0.0


class TestTwoComplaintsOneTick:
    """The one place the pattern works, asserted so the file is not all negative.

    ``open_complaint`` derives its reference from the primary key, so two
    concurrent inserts cannot collide -- the reference is unique by construction
    rather than by a counter. This is the same shape the offer subsystem would
    need, and it is why the finding above is a fix rather than a redesign.
    """

    def test_two_complaints_opened_together_get_two_references(self, raced):
        from app.services import complaints

        asyncio.run(_seed(raced))

        async def open_case(n: int) -> dict[str, Any]:
            return await raced.one(
                complaints.open_complaint,
                ANA,
                category="billing",
                summary=f"case {n}",
                source="chat",
            )

        first, second = asyncio.run(_gather(open_case(1), open_case(2)))

        assert "__error__" not in first, first
        assert "__error__" not in second, second
        references = {str(first.get("reference")), str(second.get("reference"))}
        assert len(references) == 2, (
            f"two concurrent cases shared a reference: {references}"
        )
        cases = asyncio.run(raced.rows(_models().ComplaintCase))
        assert len(cases) == 2, cases