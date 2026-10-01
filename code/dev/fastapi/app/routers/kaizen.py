"""Kaizen governance: the flow simulation, the shadow environment, and the
release ladder.

Why this is not under ``/meta``
-------------------------------
``/meta*`` is the discovery surface and is public by intent -- a service must be
able to describe itself to a monitor that holds no credentials. Everything on it
is therefore a read of configuration, a pure simulation with no side effects, or
a drift report.

This router does not fit that rule, because three of its endpoints *mutate*:

* ``POST /kaizen/admin/blockages/append`` writes to a tracked file;
* ``POST /kaizen/admin/candidates/{id}/advance`` moves a release up a level;
* ``POST /kaizen/admin/deployments/{id}/rollback`` changes what is live.

A promotion endpoint published on a public surface is a serious defect, and
there is one already: ``POST /meta/decisions/canary/promote`` classifies as
``public`` with no principal, because the ``meta_surface`` rule matches
``/meta*`` first and nothing overrides it. See the ``BLOCKAGES.md`` entry for
this subsystem. The lesson is recorded here as structure rather than as a
comment: this router lives at ``/kaizen/admin*`` and is gated on
``deps.get_current_admin_user`` on **every** route, so it cannot inherit the
public classification by accident. Adding an ungated route under this prefix
makes ``authz_drift_report`` fail, which is the point of the drift report.

Three read/decide/write splits worth stating
--------------------------------------------
* **The simulation writes nothing.** ``/flows/{flow_id}/simulate`` returns the
  runs; appending to ``BLOCKAGES.md`` is a separate, explicit call. A dashboard
  that polls the simulation must not append to a working log on every poll.
* **Advancing is gated, deploying is separate.** ``advance`` moves a candidate
  one level and runs that level's gates. It cannot reach ``l4_live`` without
  having passed the gates at ``l3_canary`` first, so a caller cannot shortcut
  the ladder by registering a well-measured candidate.
* **Rollback appends.** The ledger is never rewritten -- see
  ``release_ladder.DeploymentLedger.rollback``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app import (
    capability_audit,
    deps,
    kaizen_runner,
    models,
    real_life_flows,
    release_ladder,
    shadow_env,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _as_flag(value: str) -> bool:
    """The one spelling of "yes" used across this tree's env variables.

    Centralised so that ``1``, ``true``, ``yes`` and ``on`` mean the same thing
    here as they do in ``build_process_observations``. Two spellings of "true"
    in one codebase is a flag that is set in one place and read as false in
    another, and the symptom is an isolation check that passes for no reason.
    """
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CandidateIn(BaseModel):
    """Register a release candidate. Always lands at ``l0_draft``.

    ``extra="forbid"`` is load-bearing rather than pedantic. There is no
    ``level`` field here, and a caller who sends ``{"level": "l4_live"}`` would
    otherwise get a 201 and a candidate at draft -- a request that asked to go
    live and was told yes, with a body that says otherwise. Silently dropping a
    field a caller believed in is the same class of defect as publishing a number
    that is not the enforced one: the two disagree and only one of them is
    visible. Rejecting it makes the disagreement impossible to miss.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(..., min_length=1, max_length=80)
    commit: str = Field(..., min_length=1, max_length=80)
    revision: str = Field(default="", max_length=80)
    summary: str = Field(default="", max_length=500)
    maintainer: str = Field(default="", max_length=120)
    kaizen_source: str = Field(default="", max_length=200)
    measured: dict[str, Any] = Field(default_factory=dict)
    label: str = Field(default="", max_length=80)


class MeasureIn(BaseModel):
    """Record a gate measurement against a candidate.

    Separate from registration because measurements arrive over time and from
    different places -- a test run, a shadow comparison, a canary report. A
    single "create with everything known" call would push callers to guess at
    the measurements they do not have, and ``None`` for an unknown measurement
    folds to a gate failure anyway.

    ``measured`` is a free map, and that is a real limitation: it is an
    assertion, and a gate fed only by assertions can be satisfied by typing the
    number in. ``from_app`` closes that for the gates whose subject is this
    application rather than a test run. ``authz`` in particular cannot be
    truthfully supplied by hand -- the answer is a property of the route table,
    not of a candidate -- and it is exactly the gate that was already satisfied
    by an unauthenticated ``canary/promote`` while the drift report reported
    ``in_sync: true``. Names in ``measured`` and in ``from_app`` for the same
    key collide, and the app wins: a caller cannot override a measurement of
    the running system with a number they typed.
    """

    measured: dict[str, Any] = Field(default_factory=dict)
    from_app: list[Literal["authz", "flows", "shadow", "care"]] = Field(
        default_factory=list,
        description="measurement sources this server computes, merged over `measured`",
    )


class AdvanceIn(BaseModel):
    include_advisories: bool = Field(default=True)
    extra_gate_ids: list[str] = Field(default_factory=list)


class DeployIn(BaseModel):
    actor: str = Field(default="", max_length=120)
    reason: str = Field(default="", max_length=500)


class RollbackIn(BaseModel):
    actor: str = Field(default="", max_length=120)
    reason: str = Field(default="", max_length=500)


class CareGateConsultIn(BaseModel):
    """Evidence for one gate consultation.

    ``preferences`` is ``Optional`` and ``None`` means *could not read them*, which
    the gate treats as **do not push**. That distinction is the whole point of the
    endpoint: an unreadable profile and an empty one are different facts, and only
    the first fails closed.
    """

    model_config = ConfigDict(extra="forbid")

    path_id: str = Field(default="offer_notification", max_length=60)
    preferences: Optional[dict[str, Any]] = None
    consents: dict[str, bool] = Field(default_factory=dict)
    purpose: str = Field(default="", max_length=40)
    hour: Optional[float] = Field(default=None, ge=0.0, lt=24.0)
    channel: str = Field(default="", max_length=40)
    now: Optional[datetime] = None


class CareWeightsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    preferences: Optional[dict[str, Any]] = None
    consents: dict[str, bool] = Field(default_factory=dict)
    now: Optional[datetime] = None


class OfferOutcomesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offers: list[dict[str, Any]] = Field(default_factory=list)
    states: list[dict[str, Any]] = Field(default_factory=list)
    window_days: int = Field(default=0, ge=0, le=3650)
    now: Optional[datetime] = None


class LoyaltyPreviewIn(BaseModel):
    """Evidence for a Stage C what-if. Pure, no database.

    Timestamps are optional and default to now; every field is optional because
    the point is to ask "what would this look like for *this* evidence", including
    the evidence-free case.
    """

    model_config = ConfigDict(extra="forbid")

    now: Optional[datetime] = None
    bookings: list[dict[str, Any]] = Field(default_factory=list)
    booking_events: list[dict[str, Any]] = Field(default_factory=list)
    complaints: list[dict[str, Any]] = Field(default_factory=list)
    chat_rows: list[dict[str, Any]] = Field(default_factory=list)
    churn_band: str = Field(default="", max_length=30)
    latest_activity_at: Optional[datetime] = None
    journey_plan: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    offer_kind: str = Field(default="", max_length=30)
    recovery_context: dict[str, Any] = Field(default_factory=dict)
    offer_warranted: bool = False


class CopilotPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now: Optional[datetime] = None
    customer_360: dict[str, Any] = Field(default_factory=dict)
    explanations: dict[str, Any] = Field(default_factory=dict)
    journey_plan: dict[str, Any] = Field(default_factory=dict)
    bookings: list[dict[str, Any]] = Field(default_factory=list)
    complaints: list[dict[str, Any]] = Field(default_factory=list)
    chat_rows: list[dict[str, Any]] = Field(default_factory=list)
    churn_band: str = Field(default="", max_length=30)
    latest_activity_at: Optional[datetime] = None
    offers: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    offer_kind: str = Field(default="", max_length=30)
    recovery_context: dict[str, Any] = Field(default_factory=dict)
    offer_warranted: bool = False


class SweepIn(BaseModel):
    """Run every kaizen check once, now.

    ``for_level`` is the field with real content: it decides what the
    completeness grading treats as fatal, so it is a `Literal` rather than a
    free string. ``"l3"`` should be a 422 rather than a silently unrecognised
    level that grades as though nothing were at stake.
    """

    for_level: Literal[
        "l0_draft", "l1_verified", "l2_shadow", "l3_canary", "l4_live"
    ] = Field(
        default="l3_canary",
        description="grade completeness and promotion gates for this maturity level",
    )
    environment: str = Field(default="offline", max_length=40)
    include_shadow: bool = Field(
        default=True,
        description="run the six isolation checks; they are cheap and fail closed",
    )
    append: bool = Field(
        default=False,
        description="refused -- see the endpoint docstring; the CLI is the writer",
    )


class AppendBlockagesIn(BaseModel):
    allow_repeat: bool = Field(
        default=False,
        description="append a second dated section even if one is already present",
    )
    compare_to: str = Field(
        default="",
        max_length=40,
        description=(
            "environment label for the run to record; a shadow run reports its own "
            "divergence against the offline baseline when given"
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _load_candidate(candidate_id: str) -> release_ladder.ReleaseCandidate:
    candidate = release_ladder.find_candidate(candidate_id)
    if candidate is None:
        known = ", ".join(sorted(row.candidate_id for row in release_ladder.get_default_candidates()))
        raise _not_found(f"No such candidate {candidate_id!r}. Registered: {known or '(none)'}")
    return candidate


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/catalog")
async def kaizen_catalog(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Every table behind the kaizen subsystem, in one payload.

    The three modules' catalogs are returned side by side rather than merged,
    because they version independently and a reader comparing a flow catalog
    against a ladder catalog needs to see which revision produced which.
    """
    candidates = release_ladder.get_default_candidates()
    return {
        "flows": real_life_flows.build_flows_catalog(),
        "shadow": shadow_env.build_shadow_env_catalog(),
        "release_ladder": release_ladder.build_release_ladder_catalog(
            candidates=candidates, ledger=release_ladder.get_default_ledger()
        ),
        "validation": {
            "flows": real_life_flows.validate_flows(),
            "shadow": shadow_env.validate_shadow_env(),
            "release_ladder": release_ladder.validate_release_ladder(),
        },
        "measurement_sources": dict(_MEASUREMENT_SOURCES),
    }


# ---------------------------------------------------------------------------
# Completeness, and the sweep
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/completeness")
async def list_completeness(
    level: Optional[str] = None,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """What is built, how far, and what that means at ``level``.

    The second axis to ``/flows``. Flow findings say *does it behave*; this says
    *is it there*, which is the question that matters while the backend is still
    immature -- and it grades rather than answering yes or no, because a gap, a
    defect and a part nobody has tested want different responses from the same
    person on the same day.

    ``level`` decides which findings are fatal, and the default is to filter
    nothing rather than assume a level: you get the whole picture, which is what
    a dashboard wants, plus the grading for the level you name.
    """
    from app.main import app as fastapi_app  # local: app.main imports this router

    runs = real_life_flows.run_all_flows()
    report = real_life_flows.capability_report(runs)
    payload: dict[str, Any] = {
        "total": report["total"],
        "counts": report["counts"],
        "states": list(capability_audit.CAPABILITY_STATES),
        "complete_share": report["complete_share"],
        "capabilities": report["capabilities"],
        "orphan_probes": report["orphan_probes"],
        "blind_probes": report["blind_probes"],
        "untested": report["untested"],
        "unassessable": report["unassessable"],
        "flows_measurements": real_life_flows.flow_gate_measurements(runs),
        "blockages": [
            row.as_dict() for row in real_life_flows.completeness_blockages(report)
        ],
        "note": report["note"],
        "route_table_note": (
            "surfaces are resolved through deps.iter_authz_routes, not app.routes: "
            "included routers are lazy in this FastAPI release, so app.routes "
            "under-reports the served surface sevenfold and would declare 29 of the "
            "30 surfaces the flows name as absent"
        ),
    }
    if level is not None:
        payload["for_level"] = level
        payload["measurements"] = real_life_flows.completeness_measurements(
            report, for_level=level
        )
    _ = fastapi_app  # force the local import, so the cycle is explicit
    return payload


@router.get("/kaizen/admin/sweep")
async def sweep_status(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """What the automatic trigger has been doing, and whether it is even on.

    The useful question here is not "did it pass" but "is it running" -- a worker
    that silently never started looks exactly like a healthy one, and publishing
    the configuration beside the result is what tells the two apart.
    """
    return kaizen_runner.autostatus()


@router.post("/kaizen/admin/sweep")
async def run_sweep_now(
    payload: SweepIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Run the whole sweep now, in this request.

    Separate from the timer on purpose: an operator asking "what does it say?"
    should not wait for the next tick, and should not have to enable a timer to
    get an answer.

    **Read-only. ``append`` is refused outright** rather than merely
    discouraged, because this endpoint exists so a human can look and the dated
    -section guard would reject a repeat anyway. Writing the log is
    ``scripts/run_kaizen.py --append``, where the act shows up in a shell
    history.
    """
    if payload.append:
        raise _bad_request(
            "append is not available here: it writes BLOCKAGES.md, and an endpoint "
            "a dashboard can poll should not be able to. Use "
            "`python3 scripts/run_kaizen.py --append`"
        )
    result = await kaizen_runner.run_sweep_async(
        environment=payload.environment,
        include_shadow=payload.include_shadow,
        for_level=payload.for_level,
        append=False,
    )
    return {
        "summary": kaizen_runner.summarize(result),
        "exit_code": result["exit_code"],
        "sweep": result,
    }


# ---------------------------------------------------------------------------
# Stage D: the closed loop
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/care-gate")
async def care_gate_report(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Every declared proactive path, and whether its source calls the gate.

    Published rather than only computed, because the list of proactive paths is
    the thing a reviewer needs to argue with -- and because "are we respecting
    consent everywhere" is only answerable if the set of places that *could*
    contact somebody is written down somewhere.
    """
    from app.services import care_gate

    return {
        **care_gate.gate_coverage(),
        "validation": care_gate.validate_care_gate(),
        "catalog": care_gate.build_care_gate_catalog(),
    }


@router.post("/kaizen/admin/care-gate/consult")
async def care_gate_consult(
    payload: CareGateConsultIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Ask the gate what one proactive path may do, for one customer.

    A what-if for the gate itself, so a policy can be checked before it is
    committed to a table. **Pure** -- it reads the supplied preferences and
    consents and asks nothing else, which is what makes it safe to point at an
    arbitrary evidence set.
    """
    from app.services import care_gate

    return care_gate.consult(
        payload.path_id,
        payload.preferences,
        payload.consents,
        purpose=payload.purpose,
        hour=payload.hour,
        channel=payload.channel,
        now=payload.now,
    )


@router.post("/kaizen/admin/care-weights")
async def care_weights_report(
    payload: CareWeightsIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """What the learned complaint weights say about caring for the next customer.

    Reads the supplied weight map rather than the database, so the policy can be
    exercised against a known evidence set. The three refusals -- severity, tier,
    auto-escalation -- are on the payload so a reader sees them without opening
    the module.
    """
    from app.services import care_weights

    return care_weights.resolve_care_weights(
        payload.weights,
        context=payload.context,
        preferences_map=payload.preferences,
        consents=payload.consents,
        now=payload.now,
    )


@router.post("/kaizen/admin/offer-outcomes")
async def offer_outcomes_report(
    payload: OfferOutcomesIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Rank the offer sources by what happened, and by fulfilment.

    Two scores, published together and never combined: high acceptance with poor
    fulfilment is a *fulfilment* problem, and averaging the two would point an
    operator at the offer volume instead of at us.
    """
    from app.services import offer_outcomes

    rankings = offer_outcomes.rank_offer_outcomes(
        payload.offers, now=payload.now, window_days=payload.window_days
    )
    return {
        "rankings": rankings,
        "generosity_rankings": offer_outcomes.rank_generosity_rules(
            payload.offers, now=payload.now, window_days=payload.window_days
        ),
        "recommended_changes": offer_outcomes.recommend_scale_adjustments(rankings),
        "summary": offer_outcomes.summarise_outcomes(
            payload.offers, now=payload.now, window_days=payload.window_days
        ),
        "journey": offer_outcomes.journey_outcome_report(payload.states),
        "note": (
            "nothing here is applied. This module ranks and recommends; publishing a "
            "changed RECOVERY_SAVE_INCENTIVES row is a human act on a table that "
            "owns its own evidence"
        ),
    }


# ---------------------------------------------------------------------------
# Stage C: loyalty status, relationship health, the orchestrator, the copilot
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/loyalty-status")
async def loyalty_status_catalog(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The Stage C tables: statuses, signals, tiers, investment bands, stages.

    Published rather than only reachable through a computation, because these are
    *policy* -- the weights, the cut points and the state machine -- and a policy
    nobody can read is a policy nobody can argue with before it is wrong.
    """
    from app.services import (
        agent_copilot,
        journey_orchestrator,
        loyalty_status,
        relationship_health,
    )

    return {
        "loyalty_status": loyalty_status.build_loyalty_status_catalog(),
        "relationship_health": relationship_health.build_relationship_health_catalog(),
        "journey_orchestrator": journey_orchestrator.build_orchestrator_catalog(),
        "agent_copilot": agent_copilot.build_copilot_catalog(),
        "validation": {
            "loyalty_status": loyalty_status.validate_loyalty_status(),
            "relationship_health": relationship_health.validate_relationship_health(),
            "journey_orchestrator": journey_orchestrator.validate_orchestrator(),
            "agent_copilot": agent_copilot.validate_copilot(),
        },
        "note": (
            "four modules, four responsibilities, deliberately not merged. loyalty "
            "status is earned and monotone; relationship health is live and may "
            "fall; the orchestrator carries state between passes; the copilot "
            "composes the other three and re-derives none of them"
        ),
    }


@router.post("/kaizen/admin/loyalty-status/preview")
async def loyalty_status_preview(
    payload: LoyaltyPreviewIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Run the Stage C grading over supplied evidence, with no database.

    Read-only and pure on purpose: it is a *what-if* surface for the policy, so
    tuning a cut point or a weight can be checked before it is committed to a
    table. That is the alternative to tuning in production and discovering the
    answer from a customer.
    """
    from app.services import journey_orchestrator, loyalty_status, relationship_health

    ledger = loyalty_status.build_experiential_ledger(
        bookings=payload.bookings,
        booking_events=payload.booking_events,
        complaints=payload.complaints,
        chat_rows=payload.chat_rows,
        now=payload.now,
    )
    status = loyalty_status.resolve_loyalty_status(ledger)
    health = relationship_health.build_relationship_health(
        ledger=ledger,
        churn_band=payload.churn_band,
        complaints=payload.complaints,
        chat_rows=payload.chat_rows,
        latest_activity_at=payload.latest_activity_at,
        now=payload.now,
    )
    step = journey_orchestrator.next_step(
        state=payload.state,
        health=health,
        journey_plan=payload.journey_plan,
        offer_kind=payload.offer_kind,
        recovery_context=payload.recovery_context,
        investment_band=health["investment_band"],
        offer_warranted=payload.offer_warranted,
        now=payload.now,
    )
    return {
        "ledger": ledger,
        "status": status,
        "health": health,
        "investment": loyalty_status.resolve_investment_band(
            ledger=ledger,
            churn_band=payload.churn_band,
            relationship_health=health,
        ),
        "next_step": step,
        "note": (
            "status and health disagreeing is the expected and useful case: a "
            "customer who is Trusted and critical at once is the one that deserves "
            "the fastest response, and a single blended number hides it"
        ),
    }


@router.post("/kaizen/admin/agent-copilot/preview")
async def copilot_preview(
    payload: CopilotPreviewIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Assemble an agent's card from supplied evidence. Pure.

    The card is a composition: it never re-derives the 360, the explanations or
    the journey plan, because a third answer to a question the codebase already
    answers twice is the one an agent would believe.
    """
    from app.services import agent_copilot, journey_orchestrator, loyalty_status

    ledger = loyalty_status.build_experiential_ledger(
        bookings=payload.bookings,
        complaints=payload.complaints,
        chat_rows=payload.chat_rows,
        now=payload.now,
    )
    status = loyalty_status.resolve_loyalty_status(ledger)
    health = None
    if payload.churn_band or payload.complaints or payload.chat_rows:
        from app.services import relationship_health

        health = relationship_health.build_relationship_health(
            ledger=ledger,
            churn_band=payload.churn_band,
            complaints=payload.complaints,
            chat_rows=payload.chat_rows,
            latest_activity_at=payload.latest_activity_at,
            now=payload.now,
        )
    card = agent_copilot.build_copilot_card(
        customer_360=payload.customer_360,
        explanations=payload.explanations,
        journey_plan=payload.journey_plan,
        health=health,
        status=status,
        offers=payload.offers,
        now=payload.now,
    )
    card["orchestrator_step"] = journey_orchestrator.next_step(
        state=payload.state,
        health=health,
        journey_plan=payload.journey_plan,
        offer_kind=payload.offer_kind,
        recovery_context=payload.recovery_context,
        investment_band=str((health or {}).get("investment_band") or ""),
        offer_warranted=payload.offer_warranted,
        now=payload.now,
    )
    return card


# ---------------------------------------------------------------------------
# Flow simulation
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/flows")
async def list_flows(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The real-life flow catalog, with the personas each flow runs against."""
    return real_life_flows.build_flows_catalog()


@router.post("/kaizen/admin/flows/{flow_id}/simulate")
async def simulate_flow(
    flow_id: str,
    persona_id: str = Query(default="", description="empty runs every persona the flow names"),
    environment: str = Query(default="offline", max_length=40),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Run one flow. Writes nothing; the runs come back in the response."""
    if persona_id:
        try:
            runs = [real_life_flows.run_flow(flow_id, persona_id, environment=environment)]
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc
    else:
        flow = real_life_flows.FLOW_BY_ID.get(flow_id)
        if flow is None:
            raise _not_found(
                f"No such flow {flow_id!r}. Known: {', '.join(real_life_flows.FLOW_IDS)}"
            )
        runs = [
            real_life_flows.run_flow(flow_id, pid, environment=environment)
            for pid in flow.get("personas", ())
        ]
    blockages = real_life_flows.collect_blockages(runs)
    return {
        "flow_id": flow_id,
        "environment": environment,
        "runs": [run.as_dict() for run in runs],
        "measurements": real_life_flows.flow_gate_measurements(runs),
        "blockages": [row.as_dict() for row in blockages],
        "blockage_count": len(blockages),
    }


@router.get("/kaizen/admin/flows/run")
async def run_every_flow(
    environment: str = Query(default="offline", max_length=40),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Run the whole catalog and report the release-gate measurements.

    This is the read the ``flow_simulation_clean`` gate is fed from. It returns
    the measurement block verbatim so an operator can see the numbers the gate
    will see rather than a pass/fail of this endpoint.
    """
    runs = real_life_flows.run_all_flows(environment=environment)
    blockages = real_life_flows.collect_blockages(runs)
    return {
        "environment": environment,
        "runs": [run.as_dict() for run in runs],
        "measurements": real_life_flows.flow_gate_measurements(runs),
        "blockages": [row.as_dict() for row in blockages],
        "blockage_count": len(blockages),
        "blockers": [row.blockage_id for row in blockages if row.severity == "blocker"],
    }


@router.get("/kaizen/admin/blockages")
async def list_blockages(
    environment: str = Query(default="offline", max_length=40),
    severity: Optional[Literal["blocker", "warning"]] = Query(
        default=None,
        description="filter to one severity; an unrecognised value is rejected",
    ),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Run the simulation and return its findings without writing nothing.

    ``severity`` is a ``Literal``, not a free string, because a typo in a filter
    is indistinguishable from "nothing to report". ``?severity=catastroph``
    against a ``str`` parameter returns ``200`` with ``count: 0``, and an admin
    reading that concludes the tree is clean -- a filter that fails open on the
    one input an operator is most likely to get wrong. A 422 naming the two
    legal values is the honest answer.
    """
    runs = real_life_flows.run_all_flows(environment=environment)
    blockages = real_life_flows.collect_blockages(runs)
    if severity:
        blockages = [row for row in blockages if row.severity == severity]
    return {
        "environment": environment,
        "count": len(blockages),
        "blockages": [row.as_dict() for row in blockages],
        "measurements": real_life_flows.flow_gate_measurements(runs),
    }


@router.post("/kaizen/admin/blockages/append")
async def append_blockages(
    payload: AppendBlockagesIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Render the findings and append them to ``BLOCKAGES.md``.

    Separate from ``/blockages`` on purpose. A simulation that appended on every
    run would turn a working log into an append-only pile of identical sections,
    and after two runs nobody reads the file -- which is the outcome the whole
    log exists to prevent. The guard refuses a second append unless
    ``allow_repeat`` is set, and says so.

    Every finding arrives here with a conclusion and a suggestion already
    written, because a log entry that only says what broke teaches nobody what
    to do about it.
    """
    runs = real_life_flows.run_all_flows(environment=payload.compare_to or "offline")
    blockages = real_life_flows.collect_blockages(runs)
    markdown = real_life_flows.render_blockages_markdown(runs, blockages)
    result = real_life_flows.append_to_blockages_md(
        markdown, allow_repeat=payload.allow_repeat
    )
    return {
        **result,
        "blockage_count": len(blockages),
        "environment": payload.compare_to or "offline",
        "measurements": real_life_flows.flow_gate_measurements(runs),
    }


# ---------------------------------------------------------------------------
# Shadow environment
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/shadow")
async def shadow_state(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The shadow's identity, its channels, and its live isolation verdict.

    Deliberately returns the per-check breakdown. A single ``isolated: false``
    tells an operator the shadow is unsafe; the breakdown tells them which of the
    six checks failed and what the remedy is, which is the difference between a
    report and a dead end.
    """
    return shadow_env.build_shadow_env_catalog(
        observations=shadow_env.build_process_observations()
    )


@router.post("/kaizen/admin/shadow/verify")
async def verify_shadow(
    shadow_url: str = Query(default="", description="override the configured shadow url"),
    shadow_env_name: str = Query(default="", description="override the environment marker"),
    live_url: str = Query(default="", description="override the live database url"),
    shadow_write_targets: str = Query(
        default="", description="comma-separated; override CSERVICE_SHADOW_WRITE_TARGETS"
    ),
    live_read_only: str = Query(
        default="", description="1/true/yes/on; record the live replication role as read-only"
    ),
    shadow_egress_allowlist: str = Query(
        default="", description="comma-separated host:port; override the egress allowlist"
    ),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Re-run the isolation guard against an explicit configuration.

    Every one of the six blocking inputs is overridable, and that is deliberate.
    The interesting cases -- the same url on both sides, a shadow declaring a live
    write target, an unmarked shadow -- are the ones you want to *test*, and a
    guard you can only exercise by editing a running process's environment is a
    guard nobody tests. An earlier version of this route overrode only
    ``shadow_url`` and ``shadow_env_name``, which left ``write_scope`` and
    ``live_read_only`` reachable only by mutating the environment of the process
    being asked whether it is safe.
    """
    observations = shadow_env.build_process_observations(
        shadow_env_name=shadow_env_name or None,
        shadow_url=shadow_url or None,
        live_url=live_url or None,
        shadow_write_targets=shadow_write_targets or None,
        live_read_only=_as_flag(live_read_only) if live_read_only else None,
        shadow_egress_allowlist=shadow_egress_allowlist or None,
    )
    verdict = shadow_env.evaluate_isolation(observations)
    return {
        "isolated": verdict["isolated"],
        "blocking_failures": verdict["blocking_failures"],
        "advisory_failures": verdict["advisory_failures"],
        "checks": verdict["checks"],
        "feed_directions": shadow_env.feed_direction_report()["verdicts"],
        "leakage_paths": shadow_env.leakage_paths(),
        "note": verdict["note"],
    }


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/levels")
async def list_levels(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The maturity ladder and its gates, plus the ledger's rollback reach."""
    return release_ladder.build_release_ladder_catalog(
        candidates=release_ladder.get_default_candidates(),
        ledger=release_ladder.get_default_ledger(),
    )


@router.get("/kaizen/admin/candidates")
async def list_candidates(
    safe_only: bool = Query(default=False, description="only those safe to advance"),
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Every candidate, or only the ones an admin could promote right now."""
    candidates = release_ladder.get_default_candidates()
    if safe_only:
        return {
            "safe_levels": release_ladder.safe_levels(candidates),
            "count": len(release_ladder.safe_levels(candidates)),
            "note": (
                "sorted by destination level so the smallest blast radius is first; "
                "'admin picks any safe level' is only a sensible offer if the least "
                "exposed option is easy to find"
            ),
        }
    rows = [release_ladder.candidate_safety(row) for row in candidates]
    return {"candidates": rows, "count": len(rows)}


@router.post("/kaizen/admin/candidates", status_code=status.HTTP_201_CREATED)
async def create_candidate(
    payload: CandidateIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Register a candidate. Always lands at ``l0_draft``.

    The level is not in the request body. A candidate that could be created
    straight at ``l4_live`` would make every gate decorative.
    """
    candidate = release_ladder.ReleaseCandidate(
        candidate_id=payload.candidate_id,
        maintainer=payload.maintainer or str(getattr(current_user, "username", "") or ""),
        summary=payload.summary,
        kaizen_source=payload.kaizen_source,
        measured=dict(payload.measured),
        **release_ladder.code_and_data_pair(
            commit=payload.commit,
            revision=payload.revision,
            label=payload.label,
        ),
    )
    try:
        release_ladder.register_candidate(candidate)
    except ValueError as exc:
        raise _conflict(str(exc)) from exc
    return release_ladder.candidate_safety(candidate)


@router.post("/kaizen/admin/candidates/{candidate_id}/measure")
async def measure_candidate(
    candidate_id: str,
    payload: MeasureIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Record gate measurements. Merged, so a later run can add to an earlier one.

    ``from_app`` names the measurements this process computes rather than
    accepts, so that at least the gates about the running system cannot be
    satisfied by typing a number into a request body. See :class:`MeasureIn`.
    """
    candidate = _load_candidate(candidate_id)
    measured = dict(payload.measured)
    measured.update(_app_measurements(payload.from_app))
    candidate.measured.update(measured)
    return release_ladder.candidate_safety(candidate)


#: The measurement sources a caller may name, and where each is computed.
#: Data rather than an if/elif so adding a source does not mean editing a branch
#: and so ``/kaizen/admin/catalog`` can list them.
_MEASUREMENT_SOURCES: dict[str, str] = {
    "care": (
        "offer_outcomes.care_loop_measurements() -- counted from the offer rows "
        "and journey states. Counts decisions and stuckness; it never applies a "
        "change, because an engine that retunes its own table makes the table "
        "unauditable"
    ),
    "authz": (
        "release_ladder.authorization_measurements(app.routes) -- the live "
        "route table, checked against AUTHZ_RULES"
    ),
    "flows": (
        "real_life_flows.flow_gate_measurements(run_all_flows()) -- this process, now"
    ),
    "shadow": (
        "shadow_env.evaluate_isolation(build_process_observations()) -- this process, now"
    ),
}


def _app_measurements(sources: list[str]) -> dict[str, Any]:
    """Compute the named measurement sources. Unknown names are ignored by the schema.

    ``flow`` and ``shadow`` run the simulators, which is why they are opt-in: an
    admin measuring a test run should not also pay for 21 flow simulations they
    did not ask for. Each source returns its own keys and the caller decides the
    merge order -- app-computed last, so a hand-typed number never shadows one.
    """
    from app import main as main_module  # local: app.main imports this module

    computed: dict[str, Any] = {}
    for source in sources:
        if source == "authz":
            computed.update(
                release_ladder.authorization_measurements(main_module.app.routes)
            )
        elif source == "care":
            # Imported here rather than at module scope: offer_outcomes reads the
            # offer model, and a router importing it eagerly would pull the
            # persistence layer in for a route that only counts evidence.
            from app.services import offer_outcomes

            computed.update(offer_outcomes.care_loop_measurements())
        elif source == "flows":
            runs = real_life_flows.run_all_flows()
            computed.update(real_life_flows.flow_gate_measurements(runs))
        elif source == "shadow":
            verdict = shadow_env.evaluate_isolation(
                shadow_env.build_process_observations()
            )
            computed.update(
                {
                    "shadow_isolated": bool(verdict["isolated"]),
                    "shadow_blocking_failures": list(verdict["blocking_failures"]),
                }
            )
    return computed


@router.post("/kaizen/admin/candidates/{candidate_id}/advance")
async def advance_candidate(
    candidate_id: str,
    payload: AdvanceIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Move a candidate up exactly one level, if that level's gates pass.

    A refusal is a normal outcome, not an error: it returns 200 with
    ``advanced: false`` and the gates that blocked it. An admin asking "why is
    this not moving" is asking a question the system should answer, and a 409
    would throw the answer away.
    """
    candidate = _load_candidate(candidate_id)
    decision = release_ladder.advance_candidate(
        candidate,
        include_advisories=payload.include_advisories,
        extra_gate_ids=payload.extra_gate_ids,
    )
    return {**decision, "candidate": release_ladder.candidate_safety(candidate)}


@router.get("/kaizen/admin/safe-levels")
async def list_safe_levels(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Everything an admin could promote right now, least-exposed first."""
    candidates = release_ladder.get_default_candidates()
    rows = release_ladder.safe_levels(candidates)
    return {
        "safe_levels": rows,
        "count": len(rows),
        "levels": [dict(row) for row in release_ladder.MATURITY_LEVELS],
        "note": (
            "a level is safe when no blocking gate failed or went unmeasured; "
            "advisory failures appear in each row's warnings and do not withhold"
        ),
    }


# ---------------------------------------------------------------------------
# Deployments and rollback
# ---------------------------------------------------------------------------


@router.get("/kaizen/admin/deployments")
async def list_deployments(
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """The ledger: every deployment event, and how far back a rollback reaches."""
    ledger = release_ladder.get_default_ledger()
    return {
        "ledger": ledger.as_dict(),
        "events": [dict(row) for row in ledger.events],
        "rollback_window": release_ladder.ROLLBACK_WINDOW,
        "min_rollback_targets": release_ladder.MIN_ROLLBACK_TARGETS,
    }


@router.post("/kaizen/admin/deployments/{event_id}/rollback", status_code=status.HTTP_201_CREATED)
async def rollback_deployment(
    event_id: str,
    payload: RollbackIn,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Restore an earlier deployment's code *and* data, as one pair.

    Appends a new event rather than deleting the one it undid, so the sequence
    deploy -> rollback is legible afterwards. Restoring the code and leaving the
    data at the newer revision is refused by construction: the pair moves together
    or not at all.
    """
    ledger = release_ladder.get_default_ledger()
    try:
        event = ledger.rollback(
            event_id,
            actor=payload.actor or str(getattr(current_user, "username", "") or ""),
            reason=payload.reason,
        )
    except ValueError as exc:
        message = str(exc)
        if "no such deployment event" in message:
            raise _not_found(message) from exc
        if "rollback window" in message:
            raise _conflict(message) from exc
        raise _bad_request(message) from exc
    return {"event": event, "ledger": ledger.as_dict()}
