"""The hypothetical world the end-to-end flows are set in.

Every existing suite in this directory tests a *function*. This one exists to
test a *week in the life of a customer*, which needs three things the
function-level suites deliberately do not have:

* **A real database.** `SqliteHarness` from :mod:`_doubles` -- the same one the
  complaints and recovery suites use. A session double asserts your own
  assumptions back at you; a quote that is only reproducible because the double
  does not have a clock cannot prove reproducibility, and an arrears ledger whose
  status transitions were never constrained cannot prove the state machine.
* **A cast.** :data:`THE_CAST` is five invented customers with states the
  engines actually branch on, plus the scores and rows that put them there. They
  are named and frozen so a failure reads as "dmitri could not be rescued"
  rather than "user 4 has no wallets".
* **One HTTP client bound to that database.** :class:`Mounted`. This is the
  single most surprising thing in the file and it is copied rather than
  re-derived, for the reason ``tests/test_identity_signin_expansion.py`` gives:
  ``TestClient`` runs each request on its own portal thread, and an aiosqlite
  connection is bound to the loop that opened it, so the request either lands on
  a different loop or races itself on the pooled connection. Both surface as an
  opaque 500 from the global exception handler. Requests go through
  ``httpx.AsyncClient`` on the harness's own loop instead, where the session and
  the request share one loop and one connection.

The clock is frozen at :data:`NOW` and passed explicitly into every engine that
accepts a ``now=``. Nothing here reads the wall clock, so a run on 1 January and
a run on 1 July assert the same thing.

Two things this module deliberately does **not** do:

* It does not seed the flows for you. Each journey file lays out its own rows,
  because a fixture that silently seeds half the interesting state is how a test
  comes to pass for a reason nobody can name.
* It does not patch anything. Where a flow needs a broken engine it breaks one
  visibly, in the test, and says which one.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import pytest

from _doubles import SqliteHarness

#: The frozen "now" for every flow in the end-to-end family.
#:
#: 1 October 2026, mid-morning UTC. Chosen so that "days since" arithmetic is
#: unambiguous, and — this is the part that matters for the Stage E contact-hour
#: work — so that the same instant is *inside* working hours in one of the cast's
#: regions and *outside* them in another. A single UTC hour is the thing that made
#: one of the simulator's probes blind; freezing a moment does not fix that, but
#: it does mean the flows here can vary the region deliberately.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

#: A second frozen moment, for flows that must move time without sleeping.
LATER = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# The cast
# ===========================================================================


@dataclass(frozen=True)
class Person:
    """One invented customer.

    The fields are the ones the engines branch on -- not decoration. ``stage``
    selects a loyalty scenario, ``access_score`` an access band, ``churn_risk``
    a retention rule, and so on. A row that a person carries but no engine reads
    is a place for a flow to look thorough while testing nothing, so
    ``test_every_field_on_a_person_is_read_by_something`` pins that each of these
    is load-bearing somewhere.
    """

    user_id: int
    username: str
    email: str
    full_name: str
    is_admin: bool = False
    #: 0-100, the scale ``policy_scoring`` declares. Anything outside it is a
    #: persona the engines were never shown.
    access_score: float = 50.0
    system_score: float = 50.0
    customer_score: float = 50.0
    loyalty_score: float = 50.0
    interest_score: float = 50.0
    closeness_score: float = 50.0
    churn_risk: str = "medium"
    policy_tier: str = "standard"
    control_posture: str = "observed"
    lifecycle_stage: str = "active"
    #: Booking states this person actually holds, which the flow asserts against
    #: rather than imagines.
    booking_states: tuple[str, ...] = ()
    days_since_last_activity: int = 0
    consent_service: bool = True
    consent_recovery: bool = True
    consent_analytics: bool = False
    consent_marketing: bool = False
    consent_personalization: bool = True
    consent_third_party_sharing: bool = False
    region_id: str = "us_east"
    notes: str = ""


#: The five. Ordered by how much state they carry, so a reader meets the simple
#: ones first.
#:
#: ``ana`` is healthy and has points to spend; ``bruno`` has an abandoned
#: booking and no money; ``chiara`` is the at-risk one with three cancellations
#: and a complaint history; ``dmitri`` has been gone 45 days and is the dormancy
#: case; ``root`` is the operator, because the governance flows need somebody who
#: is allowed to press the buttons.
THE_CAST: tuple[Person, ...] = (
    Person(
        user_id=1,
        username="ana",
        email="ana@example.com",
        full_name="Ana Beltran",
        access_score=88.0,
        # 80, not the 92 this started at. At 92 she resolved to
        # `system-premium` while her declared `policy_tier` said
        # `customer-premium`, so the persona and the engine disagreed about the
        # same person -- and no cast member resolved to `customer-premium` at
        # all, leaving a published tier that nothing exercised. Lowering the
        # system score fixes both, because the declared tier is the intent and
        # the score is what drifted.
        system_score=80.0,
        customer_score=90.0,
        loyalty_score=78.0,
        interest_score=84.0,
        closeness_score=86.0,
        churn_risk="low",
        policy_tier="customer-premium",
        lifecycle_stage="active",
        booking_states=("completed", "completed"),
        days_since_last_activity=5,
        consent_analytics=True,
        consent_marketing=True,
        region_id="us_east",
        notes="Two completed bookings and a points balance. The healthy baseline.",
    ),
    Person(
        user_id=2,
        username="bruno",
        email="bruno@example.com",
        full_name="Bruno Cassis",
        access_score=64.0,
        system_score=70.0,
        customer_score=58.0,
        loyalty_score=41.0,
        interest_score=52.0,
        closeness_score=47.0,
        churn_risk="medium",
        policy_tier="standard",
        lifecycle_stage="active",
        booking_states=("pending",),
        days_since_last_activity=3,
        region_id="oceania_auckland",
        notes="An abandoned pending booking and no points. The stuck-but-present case.",
    ),
    Person(
        user_id=3,
        username="chiara",
        email="chiara@example.com",
        full_name="Chiara Neri",
        access_score=41.5,
        system_score=55.0,
        customer_score=36.0,
        loyalty_score=22.0,
        interest_score=31.0,
        closeness_score=24.0,
        churn_risk="high",
        policy_tier="standard",
        lifecycle_stage="at_risk",
        booking_states=("cancelled", "cancelled", "cancelled"),
        days_since_last_activity=1,
        consent_personalization=False,
        region_id="europe_london",
        notes="Three cancellations, no personalization consent, a billing complaint.",
    ),
    Person(
        user_id=4,
        username="dmitri",
        email="dmitri@example.com",
        full_name="Dmitri Volkov",
        access_score=33.0,
        system_score=48.0,
        customer_score=30.0,
        loyalty_score=15.0,
        interest_score=21.0,
        closeness_score=19.0,
        churn_risk="high",
        # `restricted`, not `standard`. At access 33 the engine resolves him to
        # restricted (the standard floor is 40), so declaring `standard` was the
        # persona claiming a tier the resolver would never hand him.
        policy_tier="restricted",
        lifecycle_stage="dormant",
        booking_states=("cancelled",),
        days_since_last_activity=45,
        consent_personalization=False,
        consent_analytics=False,
        region_id="us_east",
        notes="Gone 45 days. The dormancy case, and the one the forecast decays on.",
    ),
    Person(
        user_id=5,
        username="root",
        email="root@example.com",
        full_name="Operations",
        is_admin=True,
        access_score=95.0,
        system_score=97.0,
        customer_score=96.0,
        loyalty_score=90.0,
        interest_score=95.0,
        closeness_score=95.0,
        churn_risk="low",
        policy_tier="system-premium",
        lifecycle_stage="active",
        days_since_last_activity=0,
        consent_analytics=True,
        consent_marketing=True,
        notes="The operator. Only exists so the governance flows have an actor.",
    ),
)

CAST_BY_ID: dict[int, Person] = {person.user_id: person for person in THE_CAST}


def person(person_id: int) -> Person:
    """The cast member, or a loud failure naming who does exist."""
    if person_id not in CAST_BY_ID:
        raise KeyError(f"no cast member {person_id}; the cast is {sorted(CAST_BY_ID)}")
    return CAST_BY_ID[person_id]


# ===========================================================================
# The world
# ===========================================================================


class World:
    """A real in-memory database with a cast already in it.

    Constructed by :func:`world`, which is what the test files call. The seeding
    is deliberately thin -- one ``users`` row and one
    ``customer_policy_scores`` row per cast member -- because a fixture that
    seeds the interesting state is a fixture whose tests cannot fail for the
    reason they claim.
    """

    def __init__(self) -> None:
        self.harness = SqliteHarness()
        self.harness.run(self.harness.setup())
        #: Populated by :meth:`seed_cast`. Kept so a test can assert on the
        #: ORM object it seeded rather than re-querying and comparing two ways.
        self.people: dict[int, Any] = {}

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self.harness.run(self.harness.teardown())
        finally:
            self.harness.close()

    def run(self, coro: Any) -> Any:
        return self.harness.run(coro)

    @property
    def session(self) -> Any:
        return self.harness.session

    def add(self, obj: Any) -> Any:
        self.session.add(obj)
        return obj

    def commit(self) -> Any:
        return self.harness.commit()

    def flush(self) -> Any:
        return self.run(self.session.flush())

    def refresh(self, obj: Any) -> Any:
        return self.harness.refresh(obj)

    async def fetch_all(self, model: Any, **where: Any) -> list[Any]:
        """Every row of ``model``, optionally filtered on exact column values.

        Written as an explicit helper rather than a clever one because a test
        that says ``world.rows(models.BookingEvent)`` reads as "every event this
        customer ever generated", which is the claim being made. The filters are
        equality only; anything cleverer belongs in the test that needs it and in
        prose explaining why.
        """
        from sqlalchemy import select

        statement = select(model)
        for column, value in where.items():
            statement = statement.where(getattr(model, column) == value)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def fetch_one(self, model: Any, **where: Any) -> Any:
        rows = await self.fetch_all(model, **where)
        assert len(rows) <= 1, f"expected at most one {model.__name__}, got {len(rows)}"
        return rows[0] if rows else None

    async def gather(self, *coros: Any) -> list[Any]:
        """Run coroutines on this world's single loop, concurrently.

        The reason this exists: two complaints opened in the same tick have to
        earn two distinct references, and proving that requires two real inserts
        racing. Sequential awaits prove nothing of the sort.
        """
        return list(await asyncio.gather(*coros))

    # --- seeding -----------------------------------------------------------

    def seed_cast(self) -> None:
        """One user and one policy score per cast member.

        The policy score row exists because ``customer_360`` and several waiver
        paths read ``access_score`` and ``policy_tier`` from it, and a missing
        row there fails closed in a way that reads like a business rule rather
        than like absent data.
        """
        from app import models

        for member in THE_CAST:
            user = models.User(
                id=member.user_id,
                username=member.username,
                email=member.email,
                full_name=member.full_name,
                hashed_password="not-a-real-hash",
                is_admin=member.is_admin,
            )
            self.session.add(user)
            self.session.add(
                models.CustomerPolicyScore(
                    user_id=member.user_id,
                    access_score=member.access_score,
                    system_score=member.system_score,
                    customer_score=member.customer_score,
                    interest_score=member.interest_score,
                    closeness_score=member.closeness_score,
                    policy_tier=member.policy_tier,
                    control_posture=member.control_posture,
                    summary=f"seeded for the {member.username} journey",
                )
            )
            self.people[member.user_id] = user
        self.commit()
        for member in THE_CAST:
            self.refresh(self.people[member.user_id])

    # --- builders ----------------------------------------------------------
    #
    # These return un-flushed rows. Callers add and commit, because the whole
    # point of several of the flows is *when* something becomes durable.

    def booking(
        self,
        user_id: int,
        *,
        service_type: str = "consultation",
        status: str = "pending",
        title: str = "",
        details: str = "",
        scheduled: Optional[datetime] = None,
    ) -> Any:
        from app import models

        return models.Booking(
            user_id=user_id,
            service_type=service_type,
            title=title or f"{service_type} for {person(user_id).username}",
            details=details,
            scheduled_date=scheduled or (NOW + timedelta(days=7)),
            status=status,
        )

    def chat(
        self,
        user_id: int,
        message: str,
        response: str,
        *,
        at: Optional[datetime] = None,
    ) -> Any:
        from app import models

        return models.ChatHistory(
            user_id=user_id,
            message=message,
            response=response,
            timestamp=at or NOW,
        )

    def snapshot(
        self,
        user_id: int,
        *,
        loyalty_score: Optional[float] = None,
        churn_risk: Optional[str] = None,
        lifecycle_stage: Optional[str] = None,
        at: Optional[datetime] = None,
        summary: Optional[dict[str, Any]] = None,
    ) -> Any:
        """One retention snapshot.

        ``at`` is written to ``created_at`` rather than to a snapshot-specific
        column, because the snapshot model has no column of its own for "when
        this was taken" and the series builder reads the timestamp. The flows
        that build a *series* therefore construct the objects themselves and
        stamp them, which is why this helper exists at all.
        """
        from app import models

        member = person(user_id)
        row = models.RetentionSnapshot(
            user_id=user_id,
            snapshot_type="scheduled",
            window_days=30,
            loyalty_score=member.loyalty_score if loyalty_score is None else loyalty_score,
            churn_risk=member.churn_risk if churn_risk is None else churn_risk,
            lifecycle_stage=member.lifecycle_stage if lifecycle_stage is None else lifecycle_stage,
            summary_json=json.dumps(summary or {"source": "seeded"}),
        )
        if at is not None:
            row.created_at = at
            row.updated_at = at
        return row

    def wallet(self, user_id: int, balance: float, point_type: str = "loyalty_points") -> Any:
        from app import models

        return models.PointsWallet(user_id=user_id, point_type=point_type, balance=balance)

    def offer(
        self,
        user_id: int,
        *,
        offer_kind: str = "goodwill",
        source_offer_id: str = "",
        status: str = "offered",
        points: float = 250.0,
        headline: str = "",
        issued_at: Optional[datetime] = None,
        expires_at: Optional[datetime] = None,
        generosity_scale: float = 1.0,
    ) -> Any:
        from app import models

        return models.CustomerOffer(
            user_id=user_id,
            offer_kind=offer_kind,
            source_offer_id=source_offer_id,
            status=status,
            headline=headline or "A credit towards your next booking",
            points=points,
            generosity_scale=generosity_scale,
            issued_at=issued_at or NOW,
            expires_at=expires_at or (NOW + timedelta(days=30)),
        )

    def consent_event(
        self,
        user_id: int,
        purpose: str,
        granted: bool,
        *,
        note: str = "",
        at: Optional[datetime] = None,
    ) -> Any:
        from app import models

        row = models.UserConsentEvent(
            user_id=user_id,
            purpose=purpose,
            granted=granted,
            version="1.0",
            recorded_by_id=None,
            note=note,
        )
        if at is not None:
            row.created_at = at
            row.updated_at = at
        return row

    def preference_profile(
        self,
        user_id: int,
        *,
        preferences: Optional[dict[str, Any]] = None,
        consents: Optional[dict[str, bool]] = None,
    ) -> Any:
        from app import models

        member = person(user_id)
        consents = {
            "service": member.consent_service,
            "recovery": member.consent_recovery,
            "analytics": member.consent_analytics,
            "marketing": member.consent_marketing,
            "personalization": member.consent_personalization,
            "third_party_sharing": member.consent_third_party_sharing,
            **(consents or {}),
        }
        return models.UserPreferenceProfile(
            user_id=user_id,
            preferences_json=json.dumps(preferences or {}),
            consents_json=json.dumps(consents),
            consent_version="1.0",
        )

    # --- the cast as a policy-score payload ---------------------------------

    def policy_score(self, user_id: int) -> Any:
        """The cast member's score as the dict the engines are handed.

        The engines take a plain mapping, not the ORM row, and a flow that
        passed the row would be testing a conversion nobody wrote. This is the
        conversion, written once, in the place where a future column has to be
        added deliberately.
        """
        from app.services import policy_scoring

        member = person(user_id)
        return {
            "user_id": member.user_id,
            "access_score": member.access_score,
            "system_score": member.system_score,
            "customer_score": member.customer_score,
            "interest_score": member.interest_score,
            "closeness_score": member.closeness_score,
            "loyalty_score": member.loyalty_score,
            "days_since_last_activity": member.days_since_last_activity,
            "churn_risk": member.churn_risk,
            "policy_tier": member.policy_tier,
            "control_posture": member.control_posture,
            "lifecycle_stage": member.lifecycle_stage,
        }

    def context(self, user_id: int, **extra: Any) -> dict[str, Any]:
        """The ``when``-DSL context for this customer.

        Merged with the cast defaults so a flow can vary exactly the key it is
        about and leave the rest alone. ``policy_scoring`` is imported for its
        access-band threshold table, so a change to the shipped bands moves the
        context rather than leaving a hard-coded copy behind.
        """
        from app.services import policy_scoring

        base = dict(self.policy_score(user_id))
        base["region_id"] = person(user_id).region_id
        base["booking_states"] = list(person(user_id).booking_states)
        base["access_band"] = policy_scoring.resolve_access_band(base["access_score"])
        base.update(extra)
        return base


# ===========================================================================
# Fixtures and the mounted HTTP client
# ===========================================================================


@pytest.fixture()
def world():
    """A fresh world: real database, cast seeded, torn down after the test.

    Yields rather than returns so a caller cannot forget the teardown, which on
    an aiosqlite connection means a worker thread still parked on a loop that
    has been closed -- a leak that surfaces three tests later as somebody else's
    flake.

    There is no module-scoped variant on purpose. The flows mutate process-wide
    state -- the release ladder registry, the shadow environment, the rate
    limiters -- and a shared database across tests would let one test's write be
    the next test's precondition without either of them saying so.
    """
    made = World()
    try:
        made.seed_cast()
        yield made
    finally:
        made.close()


class Mounted:
    """The app, wired to a real session, driven on that session's own loop.

    Copied in spirit from ``tests/test_identity_signin_expansion.py``; the
    reason is recorded in this module's docstring and repeated on
    :meth:`_send` because it is the kind of thing that looks like over-engineering
    until you hit it once.

    The extra thing this one adds is :meth:`as_user`, because a journey has to
    be able to *switch actor* mid-flow -- a customer reads their own offers, an
    operator approves a waiver -- and rebuilding the client per actor would hide
    exactly the state changes the flows are about.
    """

    def __init__(self, world: World, *, user_id: Optional[int] = None) -> None:
        from app.main import app

        self.world = world
        self.app = app
        self.user_id = user_id

    # --- actors ------------------------------------------------------------

    async def _principal(self) -> Any:
        """The actor, with ``policy_score`` eagerly loaded.

        The eager load is not optional and the reason is written down in
        ``app/deps.py`` next to the real dependency: ``current_control_posture``
        is a *synchronous* function that does ``getattr(user, "policy_score", None)``,
        so touching that relationship lazily from inside a request raises
        ``MissingGreenlet``. The production ``get_current_user`` eager-loads it
        for exactly this reason; overriding the dependency skips that, so this
        helper has to do it instead.

        Re-read from the session on every request rather than reusing the object
        seeded at setup. A flow that adds a policy score mid-journey and then
        reads the posture through HTTP must see the new value, and a cached
        detached object would report the state before the journey started.
        """
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app import deps, models

        if self.user_id is None:
            return None
        member = person(self.user_id)
        result = await self.world.session.execute(
            select(models.User)
            .where(models.User.id == member.user_id)
            .options(selectinload(models.User.policy_score))
        )
        user = result.scalar_one()
        roles = frozenset({"admin"}) if member.is_admin else frozenset({"customer"})
        return deps.Principal(
            subject=member.username,
            user=user,
            roles=roles,
            scopes=frozenset(),
            auth_method=deps.AUTH_METHOD_USER,
        )

    def as_user(self, user_id: Optional[int]) -> "Mounted":
        """Switch actor. ``None`` is the anonymous caller."""
        self.user_id = user_id
        return self

    async def _apply(self) -> None:
        from app import deps

        principal = await self._principal()
        self.app.dependency_overrides[deps.get_db] = _session_dep(self.world.session)
        if principal is None:
            self.app.dependency_overrides.pop(deps.get_current_user, None)
            self.app.dependency_overrides.pop(deps.get_principal, None)
            self.app.dependency_overrides.pop(deps.get_current_admin_user, None)
            self.app.dependency_overrides.pop(
                deps.get_current_customer_policy_score, None
            )
            return
        self.app.dependency_overrides[deps.get_current_user] = lambda: principal.user
        self.app.dependency_overrides[deps.get_principal] = lambda: principal
        if "admin" in principal.roles:
            self.app.dependency_overrides[deps.get_current_admin_user] = (
                lambda: principal.user
            )
        # The persona's policy score is what the cast *declares*, so it has to
        # survive to request time.
        #
        # Without this override, `get_current_customer_policy_score` runs
        # `upsert_customer_policy_score`, which recomputes every score from live
        # metrics and **overwrites** the seeded row. Every cast member then
        # resolved to `restricted` (access 29.07, system 0.00) regardless of the
        # tier they were declared with, and every booking route returned 403 --
        # so the personas could not describe anything at HTTP level at all, and
        # the pure-function tests and the written-down flows disagreed about the
        # same five people.
        #
        # Overriding here is the honest reading of what a persona is: a declared
        # starting position. The recompute path is real and worth testing, but it
        # is a different question from "does the route respect the tier this
        # customer is in".
        self.app.dependency_overrides[
            deps.get_current_customer_policy_score
        ] = self._policy_score_loader(principal.user.id)

    def _policy_score_loader(self, user_id: int):
        """Build an *async* override returning the cast member's seeded score.

        Async because FastAPI awaits it on the request's own loop. A sync
        override that called ``world.run(...)`` to reach the harness loop
        re-enters a loop that is already running -- "This event loop is already
        running" -- which is the whole reason the harness binds requests to the
        harness loop in the first place.

        Returns the row from the session rather than a stand-in object: a route
        reading ``matched_fields`` or ``summary`` would otherwise be asserting
        against a shape the real object does not have.
        """
        from sqlalchemy import select

        from app import models

        async def _load() -> Any:
            result = await self.world.session.execute(
                select(models.CustomerPolicyScore).where(
                    models.CustomerPolicyScore.user_id == user_id
                )
            )
            row = result.scalars().first()
            if row is None:
                row = models.CustomerPolicyScore(user_id=user_id)
                self.world.session.add(row)
                await self.world.session.commit()
            return row

        return _load

    def release(self) -> None:
        from app import deps

        for dep in (deps.get_db, deps.get_current_user, deps.get_principal,
                    deps.get_current_admin_user,
                    deps.get_current_customer_policy_score):
            self.app.dependency_overrides.pop(dep, None)

    # --- requests ----------------------------------------------------------

    def _send(self, verb: str, path: str, **kwargs: Any) -> Any:
        return self.world.run(self.asend(verb, path, **kwargs))

    async def asend(self, verb: str, path: str, **kwargs: Any) -> Any:
        """The coroutine behind :meth:`_send`, for callers that are already on the loop.

        The sync verbs go through ``world.run``, which starts the world's loop. A
        test that is *already* inside ``world.run`` -- which is every
        ``world.gather(...)`` -- cannot call them, because ``run_until_complete``
        on a running loop raises ``RuntimeError: This event loop is already
        running``. That is not a harness limitation to work around in each test;
        it is the reason this method exists.
        """
        from httpx import ASGITransport, AsyncClient

        await self._apply()
        try:
            transport = ASGITransport(app=self.app, client=("10.0.0.7", 51234))
            async with AsyncClient(transport=transport, base_url="http://t") as client:
                return await getattr(client, verb)(path, **kwargs)
        finally:
            self.release()

    async def ajson(self, verb: str, path: str, **kwargs: Any) -> Any:
        """:meth:`asend`, asserting a 2xx and parsing, for use inside ``gather``."""
        response = await self.asend(verb, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = response.text
        assert response.status_code < 400, (verb.upper(), path, response.status_code, body)
        return body

        return self.world.run(_go())

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._send("get", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._send("post", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._send("put", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self._send("patch", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._send("delete", path, **kwargs)

    def json(self, verb: str, path: str, **kwargs: Any) -> Any:
        """Send and parse, failing with the body rather than a bare status."""
        response = self._send(verb, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = response.text
        assert response.status_code < 400, (verb.upper(), path, response.status_code, body)
        return body


def _session_dep(session: Any) -> Any:
    async def _db():
        yield session

    return _db


def mounted(world: World, *, user_id: Optional[int] = None) -> Mounted:
    """The world as an HTTP client. Spelled out so no test needs a fixture."""
    return Mounted(world, user_id=user_id)


# ===========================================================================
# Assertions shared by the flows
# ===========================================================================


def assert_money(actual: float, expected: float, *, what: str = "") -> None:
    """Money equality, to the cent, with the arithmetic in the failure message.

    Written rather than imported because the failure message is the point: a bare
    ``assert actual == expected`` on a float in a flow test produces a reader
    who has to go and re-derive the formula to find out whether the code or the
    test is wrong.
    """
    assert abs(float(actual) - float(expected)) < 0.005, (
        f"{what or 'money'}: expected {expected!r}, got {actual!r} "
        f"(difference {float(actual) - float(expected):+.4f})"
    )


def assert_monotonic(
    values: Iterable[float], *, what: str, increasing: bool = False
) -> None:
    """One direction, stated explicitly. The property four of the flows pin.

    ``increasing=False`` (the default) is the common case: risk, churn and
    forecast confidence all fall as the customer improves.

    ``increasing=True`` is for accruals -- interest, a debt, a balance owed --
    where the quantity *rises* with more time. Those flows were previously
    unexpressible, and the first thing anybody did with this helper for them was
    write the series backwards to make the assertion pass, which is the exact
    outcome a shared assertion helper is supposed to make impossible.
    """
    series = [float(value) for value in values]
    if increasing:
        for earlier, later in zip(series, series[1:]):
            assert later >= earlier - 1e-9, f"{what} fell: {series}"
        return
    for earlier, later in zip(series, series[1:]):
        assert later <= earlier + 1e-9, f"{what} rose: {series}"


def explain_keys(payload: Any) -> list[str]:
    """Sorted keys of a mapping, for a failure that should name the shape."""
    return sorted(payload) if isinstance(payload, dict) else []
