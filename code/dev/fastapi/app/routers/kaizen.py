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
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app import deps, models, real_life_flows, release_ladder, shadow_env

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
    from_app: list[Literal["authz", "flows", "shadow"]] = Field(
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
