"""Is each part of this backend actually built, and how far?

The flow simulator answers "does this behave correctly". It cannot answer "does
it exist", and at an early stage that is the more important question: a probe
against an engine that was never written raises ``AttributeError``, the
simulator records ``probe_raised``, and the report says *the subflow could not
be exercised at all*. That is technically true and practically useless, because
"not built yet" and "broken" want opposite responses -- one is the roadmap,
the other is an incident.

So completeness is a separate axis, assessed here, and it is graded rather than
boolean:

``absent``
    Nothing to test. No callable, no route, no probe.
``declared_only``
    Something *promises* it and nothing implements it. The most deceptive
    state, and the one this repo is most likely to produce, because it has a
    governance layer that writes declarations by hand: a flow names a route, or
    an ``AUTHZ_RULES`` row claims a surface, and the code behind it is not there.
``stub``
    Present and callable, but it does not do the work. Detected the only way
    that survives a rename: run it for two personas whose *expectations differ*
    and see whether it returns the same answer anyway.
``partial``
    Real implementation, wrong for at least one persona. A defect, not a gap.
``untested``
    Present, callable, and **no probe exercises it**. Its own state rather than
    a flag on ``complete``, because "we found nothing wrong" and "we looked at
    nothing" are different sentences and merging them is how an unmeasured part
    gets shipped on the strength of silence. Found the honest way: the topics
    engine is fully built and completely untested, and only a per-capability
    count could see that.
``complete``
    Present, non-stub, and every persona agreed.

Six states, because the middle of that list is where the work is. A boolean
"complete or not" cannot tell a roadmap from a defect, and the two want
different responses from the same person on the same day.

**The rule that keeps this from becoming an escape hatch.** Absence must be
established by *evidence*, never by a caught exception. A probe that raised is
not evidence that the part is missing -- it is equally consistent with a typo, a
renamed argument, and a regression. So :func:`resolve_engine` returns
``unknown`` when it cannot answer, ``unknown`` never degrades to ``absent``, and
a probe is only deferred when the target positively failed to resolve *and* the
probe raised. Every other failure stays a failure.

**Why routes are checked through ``deps.iter_authz_routes`` and not
``app.routes``.** This FastAPI release keeps included routers as lazy
``_IncludedRouter`` wrappers, so ``app.routes`` reports 39 entries where the app
actually serves 274 effective paths. A naive completeness check therefore
declared **29 of the 30 surfaces the flows name as absent** -- 26 of them false.
That is the same failure mode as the first version of the flow harness (twelve
confident false blockages from three wrong assumptions), arrived at from the
other direction: a tool that is wrong while looking rigorous is worse than no
tool, because the next person cannot tell which findings to act on.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

#: The six states, ordered least to most evidenced. Order matters: it is the
#: comparison used by :func:`is_at_least` and it is why ``untested`` sits above
#: ``partial``. An untested part is not worse than a wrong one -- it is
#: *unknown*, and unknown is closer to "working" than "wrong" only in the sense
#: that nobody has proved otherwise. Ranking it last-but-one is what stops it
#: being counted as evidence of health.
CAPABILITY_STATES: tuple[str, ...] = (
    "absent",
    "declared_only",
    "stub",
    "partial",
    "untested",
    "complete",
)
STATE_RANK: dict[str, int] = {name: index for index, name in enumerate(CAPABILITY_STATES)}

#: States that mean "this part exists and misbehaves". Distinct from a gap: the
#: remedy is a fix, not more work, and it is true at every rung.
DEFECT_STATES: frozenset[str] = frozenset({"stub", "partial"})

#: States that mean "not finished yet". Correct at ``l0_draft``, a defect at
#: ``l4_live`` -- which is the whole reason the ladder reads this.
INCOMPLETE_STATES: frozenset[str] = frozenset({"absent", "declared_only"})

#: States that mean "nobody looked". Not a defect and not a gap, and the only
#: one that gets *more* serious as you promote, because promotion is the act
#: that turns an unmeasured part into an everyday one.
UNKNOWN_STATES: frozenset[str] = frozenset({"untested"})


def is_at_least(state: str, minimum: str) -> bool:
    """``state`` is as built as ``minimum`` or more so."""
    return STATE_RANK.get(str(state), -1) >= STATE_RANK.get(str(minimum), 99)


# ---------------------------------------------------------------------------
# The inventory. Declared, not discovered.
# ---------------------------------------------------------------------------


#: probe id -> capability id. Small on purpose: one row per *part*, not per
#: probe, because a part is what an operator would ask about ("is the complaint
#: engine finished?") and a probe is only evidence about it.
#:
#: Derived from what the probes actually call rather than from a module list, so
#: renaming a probe is a one-line change here instead of a silent stale target.
PROBE_CAPABILITY: dict[str, str] = {
    "access_band_matches_thresholds": "policy_scoring",
    "access_band_resolves": "policy_scoring",
    "tier_and_posture_resolve": "policy_scoring",
    "posture_adjustment_is_effective": "policy_scoring",
    "rule_pack_selects": "rule_engine",
    "booking_states_are_valid": "bookings",
    "booking_events_are_logged": "bookings",
    "retention_series_builds": "retention",
    "retention_health_bands": "retention",
    "forecast_confidence_decays": "retention",
    "recovery_lifecycle_stage": "recovery_playbooks",
    "recovery_never_withholds_a_fix": "recovery_playbooks",
    "consent_gate_excludes_service": "preferences",
    "complaint_is_routed": "complaints",
    "complaint_sla_is_monotonic": "complaints",
    "complaint_verdict_is_explained": "complaints",
    "shadow_is_one_way": "shadow_env",
    "authz_routes_classified": "authz_governance",
    # Stage E. Each of these was graded `untested` when its engine was first
    # registered, which is the audit being right: a part that resolves is not a
    # part that has been measured. The mapping is what promotes it.
    "contact_hour_is_local": "region_windows",
    "personalization_is_purpose_limited": "care_personalization",
    "trusted_device_never_overreaches": "care_personalization",
    "copilot_is_the_default_pane": "relationship_view",
    # New probes for formerly single-probe flows and untested capabilities
    "topic_classification_works": "topics",
    "release_ladder_gates_evaluate": "release_ladder",
    "blockage_log_renders": "blockage_log",
    "points_quote_is_reproducible": "points_exchange",
    "auth_rate_limit_fires": "topics",
    # Stage F.
    "policy_motion_needs_evidence": "policy_motion",
    "broken_promise_is_visible": "policy_motion",
    "status_never_decays": "value_evolution",
}


#: capability id -> how to find it, and what a found one must be able to do.
#:
#: ``target`` is a dotted path resolved against a declared root. It is only ever
#: *resolved*, never called: the audit answers "is this here", and the probes
#: answer "does it work". Keeping those apart means a broken call cannot be
#: mistaken for an absent part.
ENGINES: tuple[dict[str, Any], ...] = (
    {
        "capability_id": "policy_scoring",
        "title": "access bands, tiers and posture adjustments",
        "root": "app.services.policy_scoring",
        "target": "resolve_access_band",
        "declared_by": "meta_feature:policy_score",
        "flows": ("new_customer_first_booking", "auth_failure_and_recovery"),
    },
    {
        "capability_id": "rule_engine",
        "title": "the shared when-DSL rule engine",
        "root": "app.rule_engine",
        "target": "select_rules",
        "declared_by": "meta_feature:rule_engine_core",
        "flows": ("support_agent_triage",),
    },
    {
        "capability_id": "bookings",
        "title": "the booking lifecycle",
        "root": "app.services.bookings",
        "target": "ensure_booking_transition_allowed",
        "declared_by": "meta_feature:bookings",
        "flows": ("new_customer_first_booking", "repeat_customer_changes_booking"),
    },
    {
        "capability_id": "retention",
        "title": "retention health, series and forecast",
        "root": "app.services.retention",
        "target": "build_retention_forecast",
        "declared_by": "meta_feature:retention",
        "flows": ("dormant_customer_win_back",),
    },
    {
        "capability_id": "recovery_playbooks",
        "title": "the recovery playbook engine and its consent gate",
        "root": "app.services.recovery_playbooks",
        "target": "evaluate_recovery_playbooks",
        "declared_by": "meta_feature:recovery_playbooks_admin",
        "flows": ("at_risk_customer_recovery",),
    },
    {
        "capability_id": "preferences",
        "title": "the preference and consent centre",
        "root": "app.services.preferences",
        "target": "update_user_preferences",
        "declared_by": "meta_feature:preference_consent",
        "flows": ("preference_and_consent_change",),
    },
    {
        "capability_id": "complaints",
        "title": "the complaint case spine and escalation engine",
        "root": "app.services.complaints",
        "target": "build_sla_report",
        "declared_by": "meta_feature:complaints",
        "flows": ("complaint_escalation_to_resolution", "regulatory_complaint_deadline"),
    },
    {
        "capability_id": "topics",
        "title": "topic classification and routing",
        "root": "app.services.topics",
        "target": "rank_topics",
        "declared_by": "meta_feature:topic_intelligence_overview",
        "flows": ("support_agent_triage",),
    },
    {
        "capability_id": "shadow_env",
        "title": "the one-way shadow environment",
        "root": "app.shadow_env",
        "target": "evaluate_isolation",
        "declared_by": "meta_feature:kaizen_shadow",
        "flows": ("admin_governance_review",),
    },
    {
        "capability_id": "release_ladder",
        "title": "the graded maturity ladder",
        "root": "app.release_ladder",
        "target": "evaluate_gates",
        "declared_by": "meta_feature:kaizen_levels",
        "flows": ("admin_governance_review",),
    },
    {
        "capability_id": "blockage_log",
        "title": "the BLOCKAGES.md finding log",
        "root": "app.real_life_flows",
        "target": "render_blockages_markdown",
        "declared_by": "file:BLOCKAGES.md",
        "flows": ("admin_governance_review",),
    },
    {
        "capability_id": "points_exchange",
        "title": "the points exchange rate and quote engine",
        "root": "app.services.points_exchange",
        "target": "quote_points_exchange",
        "declared_by": "meta_feature:points_exchange",
        "flows": ("points_and_arrears_payment",),
    },
    {
        "capability_id": "authz_governance",
        "title": "the authorization rule table and its drift report",
        "root": "app.deps",
        "target": "authz_drift_report",
        "declared_by": "meta_feature:authz_catalog",
        "flows": ("admin_governance_review",),
    },
    # Stage E. Registered because a part with no probe grades `untested`, and
    # `untested` blocks promotion from l4_live -- so leaving these out would have
    # made the audit describe a promotion barrier nobody had implemented.
    {
        "capability_id": "region_windows",
        "title": "regional local hours and contact windows",
        "root": "app.services.region_windows",
        "target": "evaluate_contact_window",
        "declared_by": "meta_feature:region_windows",
        "flows": ("at_risk_customer_recovery", "preference_and_consent_change"),
    },
    {
        "capability_id": "care_personalization",
        "title": "purpose-limited personalization and trusted-device care paths",
        "root": "app.services.care_personalization",
        "target": "resolve_personalization",
        "declared_by": "meta_feature:care_personalization",
        "flows": ("at_risk_customer_recovery", "preference_and_consent_change"),
    },
    {
        "capability_id": "policy_motion",
        "title": "evidence-based policy motion and trust under continuous change",
        "root": "app.services.policy_motion",
        "target": "build_motion",
        "declared_by": "meta_feature:policy_motion_stage_f",
        "flows": ("admin_governance_review", "at_risk_customer_recovery"),
    },
    {
        "capability_id": "value_evolution",
        "title": "how a membership's value evolves, without decaying",
        "root": "app.services.loyalty_status",
        "target": "build_value_trajectory",
        "declared_by": "meta_feature:policy_motion_stage_f",
        "flows": ("customer_360_review", "dormant_customer_win_back"),
    },
    {
        "capability_id": "relationship_view",
        "title": "the unified relationship view and its default pane",
        "root": "app.services.relationship_view",
        "target": "build_relationship_view",
        "declared_by": "meta_feature:relationship_view",
        "flows": ("customer_360_review", "support_agent_triage"),
    },
)

ENGINE_BY_ID: dict[str, dict[str, Any]] = {
    str(row["capability_id"]): dict(row) for row in ENGINES
}
ENGINE_IDS: tuple[str, ...] = tuple(sorted(ENGINE_BY_ID))


# ---------------------------------------------------------------------------
# Evidence readers. Each one returns a verdict and never raises.
# ---------------------------------------------------------------------------


def resolve_engine(row: Mapping[str, Any]) -> dict[str, Any]:
    """Does the engine exist? ``state`` is ``present``, ``absent`` or ``unknown``.

    ``unknown`` is a real outcome, not a fallback: it means the audit could not
    tell, and the caller must not read it as ``absent``. A dotted path that
    cannot be imported, a root that exists but has no such attribute, and a
    target that is not callable are three different situations and only the
    middle one is an absence.

    The import is cached by the interpreter, and the module is not held -- only
    the resolved attribute -- so this does not pin a module in memory across a
    reload in the test suite.
    """
    capability_id = str(row.get("capability_id") or "")
    root_name = str(row.get("root") or "")
    target_name = str(row.get("target") or "")
    if not root_name or not target_name:
        return {
            "capability_id": capability_id,
            "state": "unknown",
            "resolved": None,
            "detail": f"the capability row names no resolvable target (root={root_name!r})",
        }
    try:
        __import__(root_name)
    except Exception as exc:  # noqa: BLE001 - the reason is the finding
        return {
            "capability_id": capability_id,
            "state": "unknown",
            "resolved": None,
            "detail": f"could not import {root_name}: {type(exc).__name__}: {exc}",
        }
    try:
        module = __import__(root_name, fromlist=[target_name])
        attribute = getattr(module, target_name)
    except AttributeError:
        # The positive evidence of absence this whole module depends on: the
        # module is importable and simply does not have the name.
        return {
            "capability_id": capability_id,
            "state": "absent",
            "resolved": None,
            "detail": f"{root_name} has no {target_name!r}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "capability_id": capability_id,
            "state": "unknown",
            "resolved": None,
            "detail": f"resolving {root_name}.{target_name} raised {type(exc).__name__}: {exc}",
        }
    if not callable(attribute):
        return {
            "capability_id": capability_id,
            "state": "stub",
            "resolved": f"{root_name}.{target_name}",
            "detail": f"{root_name}.{target_name} is {type(attribute).__name__}, not callable",
        }
    return {
        "capability_id": capability_id,
        "state": "present",
        "resolved": f"{root_name}.{target_name}",
        "detail": "",
    }


def _normalise_path(path: str) -> str:
    """Fold the two spellings of a collection root into one.

    ``/bookings`` and ``/bookings/`` are the same surface, and a completeness
    check that reported one as missing while the other existed would be
    reporting a spelling. Placeholders are folded too, because a flow names a
    surface by shape (``/complaints/{reference}``) rather than by one example
    value.
    """
    folded = path.strip()
    if len(folded) > 1 and folded.endswith("/"):
        folded = folded.rstrip("/")
    out: list[str] = []
    depth = 0
    for char in folded:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif depth and not char.isalnum() and char != "_":
            # Inside a placeholder, collapse to one token.
            if out and out[-1] == "{":
                continue
        if depth and char != "{":
            if out and out[-1] == "{":
                out.append("param")
                if char == "}":
                    depth = max(0, depth - 1)
                continue
            if char == "}":
                continue
        out.append(char)
    return "".join(out) or "/"


def effective_route_paths(routes: Any) -> dict[str, list[str]]:
    """Every served path, normalised, mapped to the routes that serve it.

    Uses ``deps.iter_authz_routes`` rather than ``app.routes``: the included
    routers in this FastAPI release are lazy wrappers, so ``app.routes``
    under-reports the served surface by a factor of seven and every flow
    surface reads as absent. See the module docstring for what that cost.
    """
    from app import deps  # local: keeps this module importable without app

    paths: dict[str, list[str]] = {}
    for path, _route in deps.iter_authz_routes(routes):
        key = _normalise_path(path)
        bucket = paths.setdefault(key, [])
        if path not in bucket:
            bucket.append(path)
    return paths


def _surface_state(
    surface: str, served: Mapping[str, Sequence[str]], rule_matched: bool
) -> tuple[str, str]:
    """Grade one declared surface. Returns ``(state, detail)``.

    A surface that *contains* served routes counts as served, because a flow
    naming ``/bookings`` means the booking surface, not one HTTP path. That is a
    deliberate widening: a stricter rule would report the collection root as
    missing on a backend where every one of its routes exists.
    """
    key = _normalise_path(surface)
    if key in served:
        return "complete", f"served by {len(served[key])} route(s)"
    prefix = f"{key}/"
    contained = sorted(
        candidate for candidate in served if candidate.startswith(prefix)
    )
    if contained:
        return "complete", f"collection root of {len(contained)} served route(s)"
    if rule_matched:
        return (
            "declared_only",
            "AUTHZ_RULES describes this path but no route serves it",
        )
    return "absent", "no route serves this path and no rule describes it"


# ---------------------------------------------------------------------------
# The stub detector
# ---------------------------------------------------------------------------


def _fingerprints(rows: Sequence[Any]) -> tuple[set[str], set[str]]:
    """The distinct expectations and the distinct answers among some outcomes."""

    def _expected(row: Any) -> str:
        expected = getattr(row, "expected", {}) or {}
        return repr(sorted((str(k), repr(v)) for k, v in dict(expected).items()))

    def _observed(row: Any) -> str:
        observed = getattr(row, "observed", {}) or {}
        return repr(sorted((str(k), repr(v)) for k, v in dict(observed).items()))

    return ({_expected(row) for row in rows}, {_observed(row) for row in rows})


def _blind_probes(outcomes: Iterable[Any]) -> set[str]:
    """Probes every persona answered identically, so a constant engine passes.

    The complement of the stub detector, and the more common finding. Three of
    the seventeen probes on this tree assert only that a value is *present* --
    ``non_empty: True``, ``tier_in_vocabulary: True`` -- so they hold for the
    loyal customer and the dormant one alike, and would pass against an engine
    that returned the same thing to everybody. That is a real limitation of the
    simulation, and until it is measured the completeness report should not let
    anyone read those three rows as evidence.
    """
    by_probe: dict[str, list[Any]] = {}
    for row in outcomes:
        if str(getattr(row, "error", "") or ""):
            continue
        by_probe.setdefault(str(getattr(row, "subflow_id", "") or ""), []).append(row)
    blind: set[str] = set()
    for probe_id, rows in by_probe.items():
        if len(rows) < 2:
            continue
        expectations, _answers = _fingerprints(rows)
        if len(expectations) == 1:
            blind.add(probe_id)
    return blind


def stub_signal(outcomes: Sequence[Any]) -> dict[str, Any]:
    """Is a present-but-constant engine a stub?

    The only detector that survives a rename: compare what the engine returned
    across personas whose *expectations differ*. A real engine branches; a stub
    returns one answer, and the fact that two different customers with different
    states got the same answer is the finding.

    **Comparison is per probe, and getting that wrong was the first bug in this
    module.** Grouping every outcome a capability produced together compares
    ``complaint_is_routed`` against ``complaint_verdict_is_explained`` -- two
    different questions with different observation shapes -- and declares the
    engine a stub on the strength of an artefact of that pairing. It reported
    the complaint engine as a stub on a healthy tree. Personas only mean
    anything *against the same question asked of each of them.*

    Three outcomes, and the third is the useful one:

    * ``stub`` -- at least two distinct expectations produced one answer.
    * ``complete`` -- wherever expectations differed, answers differed too.
    * blind probes, reported in the ``blind`` key while still returning
      ``complete`` -- every persona was asked the *same* question, so a constant
      engine would pass. That is not a defect in the engine; it is a hole in the
      probe, and it is invisible to the flow harness by construction, because a
      probe asserting ``non_empty`` holds for every persona. Reporting it is the
      difference between "42 capabilities assessed" and "42 assessed, 3 of them
      by a question that cannot fail".
    """
    blind: list[str] = []
    by_probe: dict[str, list[Any]] = {}
    for row in outcomes:
        if str(getattr(row, "error", "") or ""):
            # A raised outcome's observed dict is empty by construction, so
            # including it would make every failing probe look like a constant.
            continue
        by_probe.setdefault(str(getattr(row, "subflow_id", "") or ""), []).append(row)

    def _fingerprint(row: Any) -> str:
        observed = getattr(row, "observed", {}) or {}
        return repr(sorted((str(k), repr(v)) for k, v in dict(observed).items()))

    def _expected(row: Any) -> str:
        expected = getattr(row, "expected", {}) or {}
        return repr(sorted((str(k), repr(v)) for k, v in dict(expected).items()))

    for probe_id in sorted(by_probe):
        rows = by_probe[probe_id]
        if len(rows) < 2:
            continue
        expectations = {_expected(row) for row in rows}
        answers = {_fingerprint(row) for row in rows}
        if len(expectations) == 1:
            # Every persona was asked the same question, so a constant answer is
            # indistinguishable from a correct one. Reported as `blind` rather
            # than folded into the verdict: this probe cannot detect a stub, and
            # a probe that cannot detect a stub is worth knowing about.
            blind.append(probe_id)
            continue
        if len(answers) == 1:
            # The comparison that means something, stated directly: personas that
            # should differ got the same answer.
            return {
                "state": "stub",
                "detail": (
                    f"probe {probe_id}: {len(expectations)} distinct expectations "
                    f"across personas received one identical answer, so the engine "
                    "is not branching"
                ),
                "blind": blind,
            }
    return {
        "state": "complete",
        "detail": (
            "every probe whose personas disagree on the expected value also "
            "disagreed on the observed one"
        ),
        "blind": blind,
    }


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


def _outcomes_by_capability(outcomes: Iterable[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for row in outcomes:
        probe_id = str(getattr(row, "subflow_id", "") or "")
        capability_id = PROBE_CAPABILITY.get(probe_id)
        if capability_id:
            grouped.setdefault(capability_id, []).append(row)
    return grouped


def assess_capabilities(
    *,
    routes: Any,
    flows: Sequence[Mapping[str, Any]],
    probes: Mapping[str, Callable[..., Any]],
    outcomes: Sequence[Any],
    features: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Grade every part, from evidence, and say what the evidence was.

    Takes the flow rows, the probe registry and the outcomes as arguments
    rather than importing them, so the dependency runs one way: the simulator
    asks this module about itself. Every row carries ``evidence`` -- the strings
    a reader would need to disagree with the verdict -- because a completeness
    report that says "partial" without saying what it looked at is an oracle
    with no audit trail.
    """
    served = effective_route_paths(routes)
    by_capability = _outcomes_by_capability(outcomes)
    rows: list[dict[str, Any]] = []

    # --- engines and the probes that exercise them
    #
    # Iterating `ENGINES` rather than `ENGINE_BY_ID`, for the reason
    # `validate_authz` gives for reading tables instead of import-time indexes:
    # a derived index is a second copy, it can disagree with the table it came
    # from, and a caller who edits the table has no way to tell which one the
    # assessor is reading. It also makes the inventory testable -- a test that
    # deletes a target and watches the grading change is deleting from the same
    # place the production path reads.
    for engine in ENGINES:
        capability_id = str(engine["capability_id"])
        probe_rows = [
            outcome
            for outcome in by_capability.get(capability_id, [])
        ]
        probe_ids = sorted(
            {
                str(getattr(row, "subflow_id", "") or "")
                for row in probe_rows
            }
        )
        resolution = resolve_engine(engine)
        declared_by = str(engine.get("declared_by") or "")
        declaration = _declaration_present(declared_by, features)

        if resolution["state"] == "absent":
            state = "declared_only" if declaration else "absent"
            evidence = [resolution["detail"]]
            if declaration:
                evidence.append(f"{declared_by} still declares it")
            rows.append(
                _row(
                    capability_id=capability_id,
                    kind="engine",
                    title=str(engine.get("title") or capability_id),
                    state=state,
                    evidence=evidence,
                    probes=probe_ids,
                    flows=tuple(_str_tuple(engine.get("flows"))),
                    detail=resolution["detail"],
                )
            )
            continue

        if resolution["state"] == "unknown":
            # Fail closed. An engine the audit cannot find is not evidence that
            # the part is missing, and reporting it as absent would let a broken
            # audit shrink the report.
            rows.append(
                _row(
                    capability_id=capability_id,
                    kind="engine",
                    title=str(engine.get("title") or capability_id),
                    state="partial",
                    evidence=[f"the audit could not resolve it: {resolution['detail']}"],
                    probes=probe_ids,
                    flows=tuple(_str_tuple(engine.get("flows"))),
                    detail=resolution["detail"],
                    unassessable=True,
                )
            )
            continue

        stub = stub_signal(probe_rows)
        if resolution["state"] == "stub" or stub["state"] == "stub":
            rows.append(
                _row(
                    capability_id=capability_id,
                    kind="engine",
                    title=str(engine.get("title") or capability_id),
                    state="stub",
                    evidence=[
                        resolution["detail"] or "the target resolved but did not branch",
                        stub.get("detail", ""),
                    ],
                    probes=probe_ids,
                    flows=tuple(_str_tuple(engine.get("flows"))),
                    detail="present but not branching",
                )
            )
            continue

        failed = [
            row for row in probe_rows if not bool(getattr(row, "held", False))
        ]
        if failed:
            rows.append(
                _row(
                    capability_id=capability_id,
                    kind="engine",
                    title=str(engine.get("title") or capability_id),
                    state="partial",
                    evidence=[
                        f"{len(failed)}/{len(probe_rows)} probe outcome(s) did not hold"
                    ],
                    probes=probe_ids,
                    flows=tuple(_str_tuple(engine.get("flows"))),
                    detail="; ".join(
                        sorted({str(getattr(row, "summary", "") or "") for row in failed})
                    )[:400],
                )
            )
            continue

        if not probe_rows:
            # Its own state. Grading this `complete` because nothing failed
            # would claim evidence that does not exist, and "we found nothing
            # wrong" is not what a silent engine has established.
            rows.append(
                _row(
                    capability_id=capability_id,
                    kind="engine",
                    title=str(engine.get("title") or capability_id),
                    state="untested",
                    evidence=[
                        f"{resolution['resolved']} resolves and is callable, but no "
                        "probe in PROBES exercises it",
                        "the flows that name it assert only through other parts",
                    ],
                    probes=[],
                    flows=tuple(_str_tuple(engine.get("flows"))),
                    detail="present, callable, and unexercised",
                    untested=True,
                )
            )
            continue

        rows.append(
            _row(
                capability_id=capability_id,
                kind="engine",
                title=str(engine.get("title") or capability_id),
                state="complete",
                evidence=[
                    f"{resolution['resolved']} resolved and "
                    f"{len(probe_rows)}/{len(probe_rows)} probe outcome(s) held",
                    stub.get("detail", ""),
                ],
                probes=probe_ids,
                flows=tuple(_str_tuple(engine.get("flows"))),
                detail="",
            )
        )

    # --- surfaces: every route a flow names
    for flow in flows:
        for surface in _str_tuple(flow.get("surfaces")):
            rule_matched = _rule_matches(surface)
            state, detail = _surface_state(surface, served, rule_matched)
            rows.append(
                _row(
                    capability_id=f"surface:{surface}",
                    kind="surface",
                    title=str(surface),
                    state=state,
                    evidence=[detail],
                    probes=sorted(_str_tuple(flow.get("probe_ids"))),
                    flows=(str(flow.get("flow_id") or ""),),
                    detail=detail,
                )
            )

    # --- probes: a flow that names a probe nobody registered
    registered = sorted(probes)
    for flow in flows:
        for probe_id in _str_tuple(flow.get("probe_ids")):
            if probe_id in probes:
                continue
            rows.append(
                _row(
                    capability_id=f"probe:{probe_id}",
                    kind="probe",
                    title=str(probe_id),
                    state="absent",
                    evidence=[
                        f"flow {flow.get('flow_id')!r} names this probe and "
                        "PROBES has no such entry"
                    ],
                    probes=(str(probe_id),),
                    flows=(str(flow.get("flow_id") or ""),),
                    detail="named by a flow, not registered",
                )
            )

    counts = {state: 0 for state in CAPABILITY_STATES}
    for row in rows:
        counts[str(row["state"])] = counts.get(str(row["state"]), 0) + 1
    complete_share = (
        round(counts["complete"] / len(rows), 4) if rows else 0.0
    )
    untested = [row["capability_id"] for row in rows if row.get("untested")]
    unassessable = [row["capability_id"] for row in rows if row.get("unassessable")]
    exercised_probes = sorted(
        {
            str(getattr(outcome, "subflow_id", "") or "")
            for outcome in outcomes
            if str(getattr(outcome, "subflow_id", "") or "")
        }
    )
    # A probe nobody invokes is the mirror image of a capability nobody probes,
    # and just as misleading: it is in the registry, so a reader counts it as
    # coverage, while no flow ever runs it. Two exist on this tree today
    # (`posture_adjustment_is_effective`, `rule_pack_selects`) -- written, wired,
    # and never executed.
    orphan_probes = sorted(set(registered) - set(exercised_probes))
    # Blind probes, computed here from the same outcomes rather than carried out
    # of `stub_signal`: a probe is blind when every persona it ran against
    # expected the same thing, so a constant engine would satisfy it. Not a
    # defect in the part -- a hole in the question being asked of it.
    blind_probes = sorted(_blind_probes(outcomes))
    ordered = sorted(
        rows,
        key=lambda row: (
            STATE_RANK.get(str(row["state"]), 99),
            str(row["kind"]),
            str(row["capability_id"]),
        ),
    )
    return {
        "capabilities": ordered,
        "counts": counts,
        "total": len(rows),
        "complete_share": complete_share,
        "untested": sorted(untested),
        "unassessable": sorted(unassessable),
        "probe_registry_size": len(registered),
        "probes_exercised": exercised_probes,
        "orphan_probes": orphan_probes,
        "blind_probes": blind_probes,
        "covered_probes": sorted(
            {probe for row in rows for probe in row["probes"] if probe in probes}
        ),
        "complete": not any(
            row["state"] in DEFECT_STATES or row.get("unassessable")
            for row in rows
        ),
        "note": (
            "counts are over declared parts, not over endpoints. A `declared_only` "
            "row is a promise with nothing behind it -- a flow names a route, or "
            "AUTHZ_RULES describes a path, and no route serves it. `untested` lists "
            "parts that exist and that no probe exercises, which is completeness "
            "without evidence; `orphan_probes` lists the converse, probes that are "
            "registered but that no flow invokes, so the registry overstates its own "
            "coverage. Rows flagged `unassessable` are ones the audit could not "
            "resolve; they are graded partial so a broken audit cannot shrink this "
            "report."
        ),
    }


def _row(**kwargs: Any) -> dict[str, Any]:
    row = {
        "capability_id": str(kwargs.get("capability_id") or ""),
        "kind": str(kwargs.get("kind") or ""),
        "title": str(kwargs.get("title") or ""),
        "state": str(kwargs.get("state") or "absent"),
        "evidence": [str(item) for item in (kwargs.get("evidence") or ()) if str(item)],
        "probes": list(kwargs.get("probes") or ()),
        "flows": list(kwargs.get("flows") or ()),
        "detail": str(kwargs.get("detail") or ""),
    }
    if kwargs.get("untested"):
        row["untested"] = True
    if kwargs.get("unassessable"):
        row["unassessable"] = True
    return row


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return (str(value),)


def _declaration_present(
    declared_by: str, features: Optional[Mapping[str, Any]]
) -> bool:
    """Does something outside the implementation still promise this part?

    Three sources, because the three subsystems that write declarations by hand
    fail in three different ways: ``/meta/features`` for surfaces, a tracked
    file for logs, and ``None`` for anything with no declaration at all.
    """
    if not declared_by:
        return False
    if declared_by.startswith("meta_feature:"):
        key = declared_by.split(":", 1)[1]
        if not isinstance(features, Mapping):
            return False
        return str(features.get(key) or "") != ""
    if declared_by.startswith("file:"):
        return os.path.exists(os.path.join(os.path.dirname(__file__), "..", declared_by.split(":", 1)[1]))
    return False


def _rule_matches(surface: str) -> bool:
    """Does ``AUTHZ_RULES`` describe this path?

    Consulted through ``match_authz_rule`` so first-match-wins and the catch-all
    are the same decision the request path makes. A surface the catch-all
    happens to match is *not* declared -- the catch-all exists so an undeclared
    route still resolves, and treating it as a promise would make every
    undeclared route look `declared_only` and hide the real one.
    """
    from app import deps  # local: keeps this module importable without app

    try:
        match = deps.match_authz_rule("GET", surface)
    except Exception:  # noqa: BLE001 - a validator must not raise
        return False
    if not bool(match.get("matched_fallback")):
        return True
    # A non-fallback rule that is itself the catch-all is still just a fallback.
    return not bool(match.get("matched_fallback")) and str(
        match.get("rule_id") or ""
    ) != str(deps.AUTHZ_OPS["catch_all_rule_id"])


# ---------------------------------------------------------------------------
# Deferral: the seam the simulator uses
# ---------------------------------------------------------------------------


def deferrable_capabilities() -> dict[str, dict[str, Any]]:
    """Capability id -> resolution, for the ones a probe may legitimately skip.

    Built once per call and keyed by capability so ``_probe`` costs one
    dictionary lookup. Only ``absent`` appears here, which is the point: a probe
    against a *present* engine that raises is a real failure and must stay one.
    """
    return {
        str(row["capability_id"]): row
        for row in (resolve_engine(engine) for engine in ENGINES)
        if row["state"] == "absent"
    }


def may_defer(probe_id: str, resolvable: Mapping[str, Mapping[str, Any]]) -> Optional[str]:
    """Why this probe may be deferred, or ``None`` if it may not.

    ``None`` is the common and important answer. The caller must then report the
    probe's own exception as a failure, because "the part is missing" and "the
    part is broken" differ by exactly this check.
    """
    capability_id = PROBE_CAPABILITY.get(str(probe_id))
    if capability_id is None:
        return None
    resolution = resolvable.get(capability_id)
    if resolution is None:
        return None
    return str(resolution.get("detail") or f"{capability_id} does not exist")


# ---------------------------------------------------------------------------
# Level-aware grading, and the ladder measurements
# ---------------------------------------------------------------------------


def worst_state(rows: Iterable[Mapping[str, Any]]) -> str:
    """The least-built row's state."""
    worst = "complete"
    for row in rows:
        if STATE_RANK.get(str(row.get("state")), 99) < STATE_RANK.get(worst, 99):
            worst = str(row.get("state"))
    return worst


def capability_gate_measurements(
    report: Mapping[str, Any], *, for_level: str = "l4_live"
) -> dict[str, Any]:
    """Measure completeness against the rung being entered.

    **The level is the whole point, and the reason this exists separately from
    the rest of the ladder.** An immature backend is the *starting state* of
    this project: at ``l0_draft`` eleven of twelve parts being unbuilt is a
    normal Tuesday, and a gate that treated that as a failure would block the
    first commit of every feature. But the same eleven parts in front of a
    customer at ``l4_live`` is not a roadmap statement, it is the product. One
    absolute threshold is therefore wrong at every rung -- too permissive to
    catch a gap before launch, too strict to let work begin.

    Three different severities, three different rungs:

    * ``stub`` / ``partial`` block **everywhere**. A part that exists and lies
      about what it does is worse at every rung than one that is honestly
      absent, because it consumes the attention of whoever is triaging the
      report. A defect found at ``l0_draft`` is a gift.
    * ``absent`` / ``declared_only`` block from ``l3_canary``. A canary routes a
      real person through the change, and a flow that cannot complete is exactly
      what a canary is for discovering -- which is too late, in front of someone.
    * ``untested`` blocks only at ``l4_live``. Canarying an unmeasured part is
      precisely how you measure it. Shipping one as the everyday path is not.

    The gate's own rule stays a single comparison (``capability_blocking ==
    0``) because the level-dependence lives *here*, where it can be read and
    argued with, rather than buried in an expression that has to be true for two
    different rungs at once.
    """
    rows = list(report.get("capabilities") or ())
    level = str(for_level)

    defects = [row for row in rows if str(row["state"]) in DEFECT_STATES]
    incomplete = [row for row in rows if str(row["state"]) in INCOMPLETE_STATES]
    untested = [row for row in rows if str(row["state"]) in UNKNOWN_STATES]

    gaps_block = level in ("l3_canary", "l4_live")
    untested_blocks = level == "l4_live"

    blocking = list(defects)
    if gaps_block:
        blocking += incomplete
    if untested_blocks:
        blocking += untested

    # Deduplicated by id, worst-first, because the same capability can be both
    # incomplete and untested and an operator should read one line about it.
    seen: set[str] = set()
    blocking_detail: list[str] = []
    for row in sorted(blocking, key=lambda r: (STATE_RANK.get(str(r["state"]), 99), str(r["capability_id"]))):
        capability_id = str(row["capability_id"])
        if capability_id in seen:
            continue
        seen.add(capability_id)
        blocking_detail.append(f"{capability_id}={row['state']}")

    return {
        "capabilities_total": int(report.get("total") or 0),
        "capabilities_complete": int((report.get("counts") or {}).get("complete") or 0),
        "capability_defects": len(defects),
        "capability_incomplete": len(incomplete),
        "capability_untested": len(untested),
        "capability_unassessable": len(list(report.get("unassessable") or ())),
        "capability_blocking": len(blocking_detail),
        "capability_blocking_detail": blocking_detail,
        "capability_defect_detail": [
            f"{row['capability_id']}={row['state']}" for row in defects
        ],
        "completeness_share": float(report.get("complete_share") or 0.0),
        "for_level": level,
        "gaps_block_at_this_level": gaps_block,
        "untested_blocks_at_this_level": untested_blocks,
        "policy_note": (
            f"at {level}: stub/partial block at every level"
            + (", absent/declared_only block from l3_canary" if gaps_block
               else f", absent/declared_only do not block before l3_canary")
            + (", untested blocks at l4_live" if untested_blocks
               else ", untested does not block before l4_live")
        ),
    }


# ---------------------------------------------------------------------------
# Self-validation
# ---------------------------------------------------------------------------


def validate_capabilities(
    *,
    flows: Sequence[Mapping[str, Any]],
    probes: Mapping[str, Callable[..., Any]],
    routes: Any,
) -> dict[str, Any]:
    """Check the inventory against the code, so it cannot rot unremarked.

    A completeness audit whose own table names probes that no longer exist
    reports confident nonsense about which parts are untested -- the same
    failure as a hardcoded expectation, arrived at through the declaration
    instead of the assertion.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for probe_id in sorted(PROBE_CAPABILITY):
        if probe_id not in probes:
            errors.append(
                f"PROBE_CAPABILITY names {probe_id!r}, which PROBES does not register; "
                "a capability whose only evidence is a probe that is not there "
                "cannot be graded"
            )
        if probe_id not in PROBE_CAPABILITY.values() and probe_id in probes:
            warnings.append(f"{probe_id} is registered but belongs to no capability")
    for probe_id in sorted(probes):
        if probe_id not in PROBE_CAPABILITY:
            warnings.append(
                f"probe {probe_id!r} is registered but classified by no capability, so "
                "its findings are attributed to nobody"
            )

    for row in ENGINES:
        name = str(row["capability_id"])
        resolution = resolve_engine(row)
        if resolution["state"] == "unknown":
            warnings.append(f"{name}: {resolution['detail']}")
        for flow_id in _str_tuple(row.get("flows")):
            if flow_id not in {str(item.get("flow_id") or "") for item in flows}:
                errors.append(
                    f"ENGINES[{name}].flows names {flow_id!r}, which FLOW_CATALOG "
                    "does not contain"
                )

    served = effective_route_paths(routes)
    for flow in flows:
        for probe_id in _str_tuple(flow.get("probe_ids")):
            if probe_id not in probes:
                errors.append(
                    f"flow {flow.get('flow_id')!r} names probe {probe_id!r}, "
                    "which PROBES does not register"
                )
        for surface in _str_tuple(flow.get("surfaces")):
            state, _detail = _surface_state(surface, served, _rule_matches(surface))
            if state != "complete":
                warnings.append(
                    f"flow {flow.get('flow_id')!r} names surface {surface!r}, which is "
                    f"{state}: a flow that exercises a surface the backend does not "
                    "serve cannot be simulated end to end"
                )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "engines": len(ENGINE_IDS),
        "classified_probes": len(PROBE_CAPABILITY),
        "registered_probes": len(probes),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(ENGINE_IDS)} capabilities and {len(probes)} probes"
        ),
    }


def build_capability_catalog(
    *,
    flows: Sequence[Mapping[str, Any]],
    probes: Mapping[str, Callable[..., Any]],
) -> dict[str, Any]:
    """The tables, published, so an operator can read the inventory itself."""
    return {
        "catalog_version": 1,
        "states": list(CAPABILITY_STATES),
        "state_meanings": {
            "absent": "nothing to test: no callable, no route, no probe",
            "declared_only": (
                "something promises it and nothing implements it -- the most "
                "deceptive state, and the one this repo is most likely to produce, "
                "because it writes declarations by hand"
            ),
            "stub": (
                "present and callable but not branching; detected by asking two "
                "personas whose expectations differ and seeing one answer"
            ),
            "partial": "real implementation, wrong for at least one persona",
            "untested": (
                "present and callable with no probe exercising it; its own state "
                "because finding nothing wrong and looking at nothing are different"
            ),
            "complete": "present, non-stub, and every persona agreed",
        },
        "defect_states": sorted(DEFECT_STATES),
        "incomplete_states": sorted(INCOMPLETE_STATES),
        "capabilities": [dict(row) for row in ENGINES],
        "probe_classification": dict(sorted(PROBE_CAPABILITY.items())),
        "note": (
            "engine targets are resolved but never called: this layer answers 'is it "
            "here', and the probes answer 'does it work'. A probe that raised is never "
            "taken as evidence of absence -- it is equally consistent with a typo and "
            "with a regression, so it stays a failure unless the target positively "
            "failed to resolve."
        ),
    }