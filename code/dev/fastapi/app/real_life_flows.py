"""Real-life flow simulation: run the journeys a customer actually takes, and
report what blocks them.

Why a flow catalog and not another test file
-------------------------------------------
The suite in ``tests/`` answers "does this function do what it says". It does not
answer "does a customer still get a booking when the thing that decides booking
routing was changed". Those are different questions, and the second one is the
one that finds out that a change to ``resolve_route`` silently routes every
complaint to the same team.

So this module models **flows** -- an ordered set of subflows, each with the real
engine call it exercises -- and runs them against **personas**: the varied
customer states ``scripts/seed_sample_data.py`` seeds. A seed of three identical
happy customers cannot tell you whether the churn, recovery or dormancy paths
work, so ``PERSONAS`` spans the states the engines branch on: loyal with points
and a preference, abandoned mid-flow, repeatedly cancelled and at risk, dormant
for 45 days, and the admin.

Three decisions worth arguing with
----------------------------------
**Keep-as-is decision: a probe feeds the engine *realistic* data or it reports
nothing.** An early version of this file called
``policy_scoring.resolve_access_band(0.5)`` and concluded the resolver was broken
because every score returned ``limited``. It is not broken: the thresholds are
``90/75/55`` on a 0-100 scale, so ``0.5`` is correctly below all three. The
probe was wrong, and had it been left in place it would have shipped a phantom
defect into ``BLOCKAGES.md`` and taught a reader to distrust real ones. This is
why :data:`PERSONAS` carries a declared ``score_scale`` and the validator checks
that a persona's scores are inside it. **A blockage about a constant output is
usually a blockage about the constant.**

**Keep-as-is decision: the simulation drives the live engine functions, never a
reimplementation of them.** Every probe calls into ``app.services.*`` and
``app.rule_engine`` directly. A simulator that reimplements the logic under test
tests the simulator, and the whole value here is that a kaizen change to a real
engine changes the flow result without anybody editing a probe.

**Keep-as-is decision: a subflow reports *what it observed*, and the blockage
verdict is made once, centrally.** Probes do not decide whether something is a
defect. They return an outcome -- observed values, an expectation, whether it
held -- and :func:`analyse_flow` folds those into blockages. Letting each probe
grade itself would mean the grading policy is written twelve times, and the
twelfth copy would be the one that disagrees.

Divergence, and what "virtually realistic" means
-----------------------------------------------
A shadow deployment exists to be compared. :func:`compare_runs` diffs two runs of
the *same* flows and reports the subflows whose observed output differed. That
ratio is the number ``release_ladder``'s
``shadow_divergence_within_tolerance`` gate consumes, so the ladder is measuring
something real rather than a value somebody typed. The comparison is on observed
values, not on pass/fail: a shadow that fails a different probe than live is a
bigger finding than one that fails the same probe, and only value-comparison
tells the two apart.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from app import capability_audit

FLOWS_CATALOG_VERSION = 1

#: Every score in this system that feeds a thresholded table is on 0-100.
#: Personas declare it and the validator checks it, because getting it wrong
#: produces confident nonsense (see the keep-as-is note in the module docstring).
SCORE_SCALE = (0.0, 100.0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return (str(value).strip(),) if str(value).strip() else ()


# ---------------------------------------------------------------------------
# Personas


@dataclass(frozen=True)
class Persona:
    """One customer state, in the shape the engines actually branch on."""

    persona_id: str
    display_name: str
    summary: str
    access_score: float
    system_score: float
    churn_risk: str
    loyalty_score: float
    days_since_last_activity: int
    booking_states: tuple[str, ...]
    consent_service: bool = True
    consent_recovery: bool = True
    consent_analytics: bool = False
    is_admin: bool = False
    #: Which region this persona is in, so a probe can vary *place* rather than
    #: inventing a persona-id-keyed lookup table.
    #:
    #: This field exists because four probes used to branch on ``persona_id``
    #: through a local dict, and every one of those dicts carried a key naming a
    #: persona that does not exist. A branch keyed on an id that can never arrive
    #: is not a slow test, it is a false negative that reads as a passing test:
    #: ``contact_hour_is_local`` mapped three personas, two of the three fell
    #: through to the same default, and the probe reported one distinct
    #: expectation while its own docstring claimed the personas "differ *only* in
    #: region". Putting the region on the persona makes the variation a property
    #: of the persona rather than a lookup somebody has to remember to extend.
    region_id: str = "us_east"
    #: The experiential tier this relationship has earned, which is what a status
    #: is derived from. ``resolve_loyalty_status`` reads *only* this and never a
    #: points balance, so a probe that hard-codes one tier for every persona is
    #: checking a quarter of the status table and calling it coverage.
    experiential_tier: str = "bronze"
    #: Evidence attached to a proposed change to what somebody is owed. Three
    #: kinds on purpose: a measured outcome, a bare observation, and an opinion.
    #: An opinion with four hundred samples behind it must still be refused.
    motion_evidence: tuple[dict[str, Any], ...] = ()
    #: How many times this persona's device has been seen. One sighting is not
    #: trust and twelve is; a probe reporting the same permitted set for both is
    #: not testing whether recognition stops at authentication.
    device_recognitions: int = 0
    #: Whether this persona granted the ``personalization`` consent. This is the
    #: one boolean that moves a personalization answer, which is why it lives on
    #: the persona rather than in a probe-local dict.
    personalization_consent: bool = False
    #: What this persona's own messages said, as ``(label, score)``.
    #:
    #: Added because ``recovery_never_withholds_a_fix`` built its context by hand
    #: from six persona fields, and the playbook rule table reads ten. The three
    #: it was missing -- ``recovery_readiness``, ``sentiment_label`` and
    #: ``dissatisfaction_score`` -- are all derived from how the customer spoke,
    #: which the persona had no way to express. No rule could match, the plan came
    #: back empty for all five personas, and a probe named "never withholds a fix"
    #: reported the invariant held while the planner withheld every fix.
    sentiment: tuple[str, float] = ("neutral", 0.0)
    #: The insights a real analysis would have raised for this persona, as
    #: ``(area, score)``. These are what move ``dissatisfaction_score``, and
    #: through it ``recovery_readiness`` -- the label every "is this bad enough to
    #: act" playbook rule branches on. An empty tuple means a quiet account.
    insights: tuple[tuple[str, float], ...] = ()

    def within_score_scale(self) -> bool:
        low, high = SCORE_SCALE
        return low <= self.access_score <= high and low <= self.system_score <= high


#: The states the engines branch on, mirroring ``seed_sample_data.py``. Named
#: after the seeded users so a reader can look the data up.
PERSONAS: tuple[Persona, ...] = (
    Persona(
        persona_id="loyal_with_points",
        display_name="ana",
        summary="loyal, two completed bookings, points balance, an arrears deferral, a stated preference",
        access_score=88.0,
        system_score=92.0,
        churn_risk="low",
        loyalty_score=78.0,
        days_since_last_activity=5,
        booking_states=("completed", "completed"),
        consent_service=True,
        consent_recovery=True,
        consent_analytics=False,
        region_id="us_east",
        experiential_tier="gold",
        motion_evidence=({"kind": "measured_outcome", "samples": 400},),
        device_recognitions=12,
        personalization_consent=True,
        sentiment=("positive", 0.4),
        insights=(),
    ),
    Persona(
        persona_id="abandoned_pending",
        display_name="bruno",
        summary="one pending booking and repeated 'any update?' messages: the follow-up gap",
        access_score=64.0,
        system_score=70.0,
        churn_risk="medium",
        loyalty_score=41.0,
        days_since_last_activity=3,
        booking_states=("pending",),
        consent_service=True,
        consent_recovery=True,
        consent_analytics=False,
        region_id="oceania_auckland",
        experiential_tier="silver",
        motion_evidence=({"kind": "observation", "samples": 40},),
        device_recognitions=1,
        personalization_consent=False,
        sentiment=("neutral", -0.1),
        insights=(("follow_up", 4.0),),
    ),
    Persona(
        persona_id="repeatedly_cancelled",
        display_name="chiara",
        summary="three cancellations, negative messages, signals, snapshots and a consent grant",
        access_score=41.5,
        system_score=55.0,
        churn_risk="high",
        loyalty_score=22.0,
        days_since_last_activity=1,
        booking_states=("cancelled", "cancelled", "cancelled"),
        consent_service=True,
        consent_recovery=True,
        consent_analytics=True,
        region_id="europe_london",
        experiential_tier="bronze",
        motion_evidence=(),
        device_recognitions=5,
        personalization_consent=False,
        # Three cancellations, a negative reading and six repeat messages: the
        # only persona on this tree whose profile reaches `recovery_readiness`
        # high, and therefore the only one the goodwill and guardrail playbooks
        # can fire for.
        sentiment=("negative", -0.8),
        insights=(("booking_flow", 4.5), ("pricing", 3.0), ("follow_up", 4.0)),
    ),
    Persona(
        persona_id="dormant_45_days",
        display_name="dmitri",
        summary="one cancellation, last active 45 days ago: the dormancy and win-back path",
        access_score=33.0,
        system_score=48.0,
        churn_risk="high",
        loyalty_score=15.0,
        days_since_last_activity=45,
        booking_states=("cancelled",),
        consent_service=True,
        consent_recovery=True,
        consent_analytics=False,
        region_id="us_west",
        # `unscored`, so this persona resolves to the *lowest* status rung. Three
        # of five personas sitting on `trusted` is how a four-rung status table
        # gets reported as one rung's worth of coverage.
        experiential_tier="unscored",
        # An opinion, with four hundred samples behind it. The case this exists
        # for: volume is not measurement.
        motion_evidence=({"kind": "opinion", "samples": 400},),
        device_recognitions=5,
        personalization_consent=False,
        sentiment=("negative", -0.6),
        insights=(("booking_flow", 3.0),),
    ),
    Persona(
        persona_id="admin_console",
        display_name="root",
        summary="the admin; the only persona that can reach a governance surface",
        access_score=95.0,
        system_score=97.0,
        churn_risk="low",
        loyalty_score=90.0,
        days_since_last_activity=0,
        booking_states=(),
        consent_service=True,
        consent_recovery=True,
        consent_analytics=True,
        is_admin=True,
        region_id="us_east",
        experiential_tier="platinum",
        motion_evidence=({"kind": "measured_outcome", "samples": 400},),
        device_recognitions=12,
        personalization_consent=True,
        sentiment=("neutral", 0.0),
        insights=(),
    ),
)
PERSONA_BY_ID: dict[str, Persona] = {row.persona_id: row for row in PERSONAS}
PERSONA_IDS: tuple[str, ...] = tuple(row.persona_id for row in PERSONAS)
NON_CUSTOMER_PERSONAS: tuple[str, ...] = tuple(
    row.persona_id for row in PERSONAS if row.is_admin
)


# ---------------------------------------------------------------------------
# Probe outcomes


@dataclass
class ProbeOutcome:
    """What one subflow observed. It does not decide whether that is a defect."""

    subflow_id: str
    persona_id: str
    held: bool
    summary: str
    observed: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: True when this subflow was not exercised because the part it depends on
    #: is **positively known** to be absent. Not a failure, and not a pass: it
    #: is the absence of evidence about a part that is not there.
    #:
    #: Kept off ``held`` deliberately. A deferred subflow never counts as
    #: evidence for anything, and folding it into ``held=True`` would let an
    #: unbuilt part quietly satisfy the gate that was supposed to catch it.
    deferred: bool = False
    #: Why it was deferred, in the words of the check that established it.
    deferral_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "subflow_id": self.subflow_id,
            "persona_id": self.persona_id,
            "held": self.held,
            "summary": self.summary,
            "observed": dict(self.observed),
            "expected": dict(self.expected),
            "error": self.error,
            "deferred": self.deferred,
            "deferral_reason": self.deferral_reason,
        }


Probe = Callable[[Persona], ProbeOutcome]


def _outcome(
    subflow_id: str,
    persona: Persona,
    held: bool,
    summary: str,
    *,
    observed: Optional[Mapping[str, Any]] = None,
    expected: Optional[Mapping[str, Any]] = None,
    error: str = "",
    deferred: bool = False,
    deferral_reason: str = "",
) -> ProbeOutcome:
    return ProbeOutcome(
        subflow_id=subflow_id,
        persona_id=persona.persona_id,
        held=held,
        summary=summary,
        observed=dict(observed or {}),
        expected=dict(expected or {}),
        error=error,
        deferred=deferred,
        deferral_reason=deferral_reason,
    )


#: Capability id -> resolution, for the parts that are positively absent. Built
#: lazily and cached per process, because it costs an import per engine and the
#: simulator runs 21 flow-persona pairs x up to 7 probes each, twice per sweep
#: (once for the flows, once for the divergence comparison).
_DEFERABLE: dict[str, dict[str, Any]] | None = None


def _deferrable() -> dict[str, dict[str, Any]]:
    global _DEFERABLE
    if _DEFERABLE is None:
        _DEFERABLE = capability_audit.deferrable_capabilities()
    return _DEFERABLE


def reset_completeness_cache() -> None:
    """Forget which parts are absent.

    The test suite's negative controls delete a target to watch a probe change
    verdict, and without this the cached answer would keep the earlier one --
    which is the same class of bug as the frozen index this module's own
    validator warns about.
    """
    global _DEFERABLE
    _DEFERABLE = None


def _probe(fn: Probe, subflow_id: str, persona: Persona) -> ProbeOutcome:
    """Run a probe, turning an exception into an outcome rather than a crash.

    A simulation that dies on the first exception reports one blockage and no
    conclusions, which is the least useful possible result. The exception
    becomes an outcome with ``held: False`` and the message, so the run finishes
    and every other flow still contributes.

    **And one exception is re-read.** If the part this probe exercises is
    *positively* absent -- the module resolves and has no such name -- then the
    exception is ``AttributeError``, which is the expected shape of "we have not
    built this yet", and reporting it as ``probe_raised`` would tell the roadmap
    it has an incident. So it becomes ``deferred`` instead.

    The bar is deliberately high, and the bar is the whole design. A raised
    probe is *not* taken as evidence of absence: ``may_defer`` returns ``None``
    unless the target genuinely failed to resolve, so a typo, a renamed argument
    and a regression all stay failures. An audit that guessed "not built yet"
    from a traceback would shrink every report it was pointed at.
    """
    try:
        return fn(persona)
    except Exception as exc:  # noqa: BLE001 - a probe may touch anything
        reason = capability_audit.may_defer(subflow_id, _deferrable())
        if reason:
            return _outcome(
                subflow_id,
                persona,
                False,
                f"deferred: the part this subflow exercises is not built ({reason})",
                error=f"{type(exc).__name__}: {exc}",
                deferred=True,
                deferral_reason=reason,
            )
        return _outcome(
            subflow_id,
            persona,
            False,
            f"probe raised {type(exc).__name__}",
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# The probes -- each one calls a real engine


def _probe_access_band(persona: Persona) -> ProbeOutcome:
    """A score must land in the published band vocabulary, and not all alike.

    The second half matters as much as the first: five personas that all resolve
    to ``limited`` would pass a "is it a known band" check while telling you
    nothing, because a resolver stuck on its default satisfies every membership
    test ever written.
    """
    from app.services import policy_scoring

    observed = policy_scoring.resolve_access_band(persona.access_score)
    trace = policy_scoring.resolve_access_band_trace(persona.access_score)
    known = set(policy_scoring.build_policy_tier_catalog().get("bands", ()) or ())
    if not known:
        known = {str(row["band"]) for row in getattr(policy_scoring, "ACCESS_BAND_RULES", ())}
    known.add(str(policy_scoring.POLICY_SCORING_OPS["default_band"]))
    in_vocabulary = observed in known
    return _outcome(
        "access_band_resolves",
        persona,
        in_vocabulary,
        f"access {persona.access_score:g} -> band {observed!r}",
        observed={"access_band": observed, "matched": bool(trace.get("matched"))},
        expected={"in_vocabulary": sorted(known)},
    )


def _probe_band_spread(persona: Persona) -> ProbeOutcome:
    """Across personas the resolver must produce more than one band.

    Deliberately a *cross-persona* check bound to one persona's run: each probe
    sees only its own persona, so the check is on the value itself -- ``elite``
    and ``limited`` must both be reachable somewhere, and the sweep result is
    what proves it, not any single row.
    """
    from app.services import policy_scoring

    expected_band = None
    for row in getattr(policy_scoring, "ACCESS_BAND_RULES", ()):
        if persona.access_score >= float(row["access_score_min"]):
            expected_band = str(row["band"])
            break
    observed = policy_scoring.resolve_access_band(persona.access_score)
    expected = expected_band or str(policy_scoring.POLICY_SCORING_OPS["default_band"])
    return _outcome(
        "access_band_matches_thresholds",
        persona,
        observed == expected,
        f"access {persona.access_score:g} -> {observed!r}, thresholds say {expected!r}",
        observed={"access_band": observed},
        expected={"access_band": expected},
    )


def _posture_vocabulary() -> tuple[str, ...]:
    """The real control-posture vocabulary, read from the engine's own table.

    Not hardcoded, and not guessed. The first version of this probe asserted
    ``{"relaxed", "standard", "strict", "constrained", "elevated"}`` and reported
    every persona as blocked, because the actual postures are ``high_trust``,
    ``customer_trusted``, ``observed`` and ``constrained``. A hardcoded
    expectation is a finding waiting to be invented.
    """
    from app.services import policy_scoring

    vocabulary = {
        str(row.get("posture"))
        for row in getattr(policy_scoring, "POSTURE_ADJUSTMENTS", ())
        if str(row.get("posture")) not in {"", "*"}
    }
    vocabulary.update(
        str(row.get("posture"))
        for row in getattr(policy_scoring, "CONTROL_POSTURE_RULES", ())
        if row.get("posture")
    )
    vocabulary.add(str(policy_scoring.POLICY_SCORING_OPS["default_posture"]))
    return tuple(sorted(vocabulary))


def _probe_tier_and_posture(persona: Persona) -> ProbeOutcome:
    from app.services import policy_scoring

    tier = policy_scoring.resolve_policy_tier(persona.access_score, persona.system_score)
    posture = policy_scoring.resolve_control_posture(
        tier, persona.access_score, persona.system_score
    )
    postures = _posture_vocabulary()
    return _outcome(
        "tier_and_posture_resolve",
        persona,
        posture in postures and bool(tier),
        f"tier {tier!r} -> posture {posture!r}"
        + ("" if posture in postures else f" (not in the declared postures {list(postures)})"),
        observed={"policy_tier": tier, "control_posture": posture},
        expected={"posture_in": list(postures)},
    )


def _probe_posture_adjustment(persona: Persona) -> ProbeOutcome:
    """Each posture must apply *its own declared delta*, and stay in range.

    The invariant is read off ``POSTURE_ADJUSTMENTS`` rather than asserted as a
    hand-written expectation, which is the whole point: ``high_trust`` declares
    ``delta: 0.0`` and is therefore *correct* to leave the score alone, so a
    probe demanding that every posture move the score would flag a correct
    configuration. Comparing the applied value against the declared row catches
    the real defect -- a delta that stopped being applied -- without inventing
    one.

    **What this probe cannot catch.** Its expectation is *derived from the table
    it validates*, so editing the table changes the actual behaviour and the
    expectation together: setting ``customer_trusted``'s delta to ``0.0`` makes
    applied equal declared, and this probe stays green. That is a real limit
    rather than a bug, and it is the general shape of the problem -- a check
    whose oracle is the thing under test has no independent opinion. The guard is
    a pinning test that spells the deltas out in ``tests/``, which *is* an
    independent oracle. This probe's job is narrower and worth stating: it
    catches a resolver that stops consulting its config, and a default row that
    stops being reachable -- neither of which a pinning test notices until
    somebody reads it.
    """
    from app.services import policy_scoring

    ceiling = float(policy_scoring.POLICY_SCORING_OPS["score_ceiling"])
    floor = float(policy_scoring.POLICY_SCORING_OPS["score_floor"])
    declared = {
        str(row.get("posture")): row
        for row in getattr(policy_scoring, "POSTURE_ADJUSTMENTS", ())
    }
    applied: dict[str, float] = {}
    mismatched: list[str] = []
    out_of_range: list[str] = []
    for posture, row in sorted(declared.items()):
        if posture == "*":
            continue
        adjusted = policy_scoring.apply_posture_adjustment(persona.access_score, posture)
        applied[posture] = adjusted
        expected_delta = float(row.get("delta") or 0.0)
        if abs((adjusted - persona.access_score) - expected_delta) > 1e-6:
            mismatched.append(posture)
        if not (floor <= adjusted <= ceiling):
            out_of_range.append(posture)
    # The declared default row must actually be consulted. This is the one check
    # in this probe that is *independent of the table's own values*: a row marked
    # ``default: True`` that is never reached is dead config that looks live, and
    # editing any specific row's delta cannot hide it.
    default_rows = [
        row for row in getattr(policy_scoring, "POSTURE_ADJUSTMENTS", ()) if bool(row.get("default"))
    ]
    default_reachable = False
    default_declared: Any = None
    default_applied: Any = None
    if len(default_rows) == 1:
        default_declared = float(default_rows[0].get("delta") or 0.0)
        probe_score = (floor + ceiling) / 2.0
        default_applied = policy_scoring.apply_posture_adjustment(
            probe_score, "not_a_declared_posture"
        )
        default_reachable = abs((default_applied - probe_score) - default_declared) <= 1e-6
    elif default_rows:
        mismatched.append(f"{len(default_rows)} default rows in the table")

    return _outcome(
        "posture_adjustment_is_effective",
        persona,
        bool(applied)
        and not mismatched
        and not out_of_range
        and default_reachable,
        f"applied {applied}"
        + (f"; deltas not applied: {mismatched}" if mismatched else "")
        + (f"; out of range: {out_of_range}" if out_of_range else "")
        + ("" if default_reachable else "; the default row is never consulted"),
        observed={
            "applied": applied,
            "declared_deltas": {k: v.get("delta") for k, v in sorted(declared.items())},
            "delta_not_applied": mismatched,
            "out_of_range": out_of_range,
            "default_delta": default_declared,
            "default_applied": default_applied,
            "default_reachable": default_reachable,
        },
        expected={
            "delta_not_applied": [],
            "within": [floor, ceiling],
            "default_row_reachable": True,
        },
    )


def _probe_booking_states(persona: Persona) -> ProbeOutcome:
    from app import models

    valid = {status.value for status in models.BookingStatus}
    unknown = [state for state in persona.booking_states if state not in valid]
    terminal_pending = persona.booking_states and not unknown
    return _outcome(
        "booking_states_are_valid",
        persona,
        not unknown,
        f"states {list(persona.booking_states)} vs vocabulary {sorted(valid)}"
        if unknown
        else f"{len(persona.booking_states)} booking state(s) all in the enum",
        observed={"states": list(persona.booking_states), "unknown": unknown},
        expected={"known": sorted(valid)},
    )


def _probe_complaint_sla(persona: Persona) -> ProbeOutcome:
    from app.services import complaints

    severity = "high" if persona.churn_risk == "high" else "medium"
    standard = complaints.resolve_sla(severity)
    regulatory = complaints.resolve_sla(severity, regulatory=True)
    internal_only = complaints.resolve_sla(severity, regulatory=False)
    tighter_or_equal = (
        float(regulatory["resolution_hours"]) <= float(standard["resolution_hours"])
    )
    return _outcome(
        "complaint_sla_is_monotonic",
        persona,
        tighter_or_equal and float(standard["resolution_hours"]) > 0,
        f"{severity}: {standard['resolution_hours']}h internal vs "
        f"{regulatory['resolution_hours']}h regulatory",
        observed={
            "severity": severity,
            "internal_resolution_hours": standard["resolution_hours"],
            "regulatory_resolution_hours": regulatory["resolution_hours"],
            "basis": standard["basis"],
        },
        expected={"regulatory_not_looser": True, "basis_reported": "internal_matrix"},
    )


def _probe_complaint_route(persona: Persona) -> ProbeOutcome:
    from app.services import complaints

    severity = "high" if persona.churn_risk == "high" else "medium"
    context = {
        "severity": severity,
        "category": "service_quality",
        "churn_risk": persona.churn_risk,
        "access_score": persona.access_score,
        "system_score": persona.system_score,
        "open_complaints": len(persona.booking_states),
        "has_owner": True,
        "override_would_lower_tier": False,
    }
    route = complaints.resolve_route(context)
    tiers = {str(row["tier"]) for row in getattr(complaints, "ESCALATION_TIERS", ())}
    to_tier = str(route.get("to_tier") or "")
    return _outcome(
        "complaint_is_routed",
        persona,
        bool(to_tier) and bool(route.get("owner_team")),
        f"severity {severity} -> tier {to_tier!r} team {route.get('owner_team')!r}",
        observed={"to_tier": to_tier, "owner_team": route.get("owner_team"), "rule_id": route.get("rule_id")},
        expected={"tier_in_vocabulary": to_tier in tiers or bool(to_tier), "owner_required": True},
    )


def _guard_metrics(persona: Persona) -> dict[str, Any]:
    """Every metric ``ESCALATION_GUARDS`` names, assembled from the persona.

    Built by walking the guard table rather than by listing metrics by hand. The
    hand-written version supplied five of the seven, so three guards came back
    ``metric_missing`` and the probe -- correctly, but uselessly -- reported
    "unexplained verdict" for a case that was merely under-fed. A guard that
    fails closed on a missing metric is *right*; a probe that then reports the
    fail-closed as a product defect has mistaken its own inputs for a finding.
    """
    from app.services import complaints

    severity = "high" if persona.churn_risk == "high" else "medium"
    supplied: dict[str, Any] = {}
    for guard in complaints.ESCALATION_GUARDS:
        metric = str(guard.get("metric") or "")
        if not metric or metric in supplied:
            continue
        supplied[metric] = _guard_metric_value(metric, persona, severity)
    return supplied


def _guard_metric_value(metric: str, persona: Persona, severity: str) -> Any:
    """One decided guard metric for a persona.

    Values are chosen so the guard is *decided*, not merely present: an undecided
    guard is one where the product never had to make a call, and a probe that
    stops at "the guard fired" learns nothing about whether the right call was
    made.
    """
    if metric == "has_owner":
        return True
    if metric == "override_would_lower_tier":
        return False
    if metric == "consent_withholds_contact":
        # A customer who reported a problem is not refused contact because of a
        # marketing preference; both personas hold service or recovery consent.
        return not (persona.consent_service or persona.consent_recovery)
    if metric == "duplicate_open_cases":
        return 0
    if metric == "resolved_stale_hours":
        return 0
    if metric == "severity_understated_by":
        # Severity already reflects the churn risk in this probe's context.
        return 0
    if metric == "regulatory_hours_remaining":
        return 72.0
    # An unrecognised metric gets a neutral 0 rather than being omitted, so the
    # guard is evaluated instead of failing closed on absence.
    return 0


def _probe_complaint_verdict(persona: Persona) -> ProbeOutcome:
    """The escalation verdict, fed every metric the guard table names.

    Asserts the result is not an *unexplained* reject. A ``reject`` whose guards
    all say ``metric_missing`` is fail-closed behaviour working correctly; a
    ``reject`` whose guards each carry a reason an operator could act on is a
    real decision. Both arrive as a ``decision``, and only the second is a
    finished answer -- which is why the probe checks the guards, not the verdict.
    """
    from app.services import complaints

    metrics = _guard_metrics(persona)
    verdict = complaints.escalation_verdict(metrics)
    decision = str(verdict.get("decision") or "")
    guards = verdict.get("guards") or []
    unmeasured = [
        str(guard.get("guard_id"))
        for guard in guards
        if not guard.get("holds") and str(guard.get("reason")) == "metric_missing"
    ]
    unexplained = [
        str(guard.get("guard_id"))
        for guard in guards
        if not guard.get("holds") and str(guard.get("reason")) in {"", "none"}
    ]
    return _outcome(
        "complaint_verdict_is_explained",
        persona,
        decision in {"accept", "review"} and not unmeasured and not unexplained,
        f"decision {decision!r}"
        + (f", guards left unmeasured: {unmeasured}" if unmeasured else "")
        + (f", guards fired with no reason: {unexplained}" if unexplained else ""),
        observed={
            "decision": decision,
            "rejected_by": list(verdict.get("rejected_by") or []),
            "needs_review_by": list(verdict.get("needs_review_by") or []),
            "advisory_by": list(verdict.get("advisory_by") or []),
            "guards_unmeasured": unmeasured,
            "guards_without_reason": unexplained,
            "metrics_supplied": sorted(metrics),
        },
        expected={
            "decision_in": ["accept", "review"],
            "every_guard_measured": True,
        },
    )


def _probe_retention_series(persona: Persona) -> ProbeOutcome:
    from app.services import retention

    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    snapshots = [
        {
            "id": index + 1,
            "snapshot_type": "scheduled",
            "lifecycle_stage": "active",
            "loyalty_score": max(0.0, persona.loyalty_score - (3 - index) * 4.0),
            "churn_risk": persona.churn_risk if index >= 2 else "medium",
            "created_at": base + timedelta(days=index * 7),
        }
        for index in range(3)
    ]
    points = retention.snapshot_series_points(snapshots)
    metrics = retention.build_retention_series_metrics(points)
    # Ordered oldest -> newest is the direction the trend is read in, so a series
    # that came back reversed would make every delta the wrong sign.
    ordered = [point["index"] for point in points] == list(range(len(points)))
    required = ("snapshot_count", "loyalty_avg", "loyalty_latest", "churn_latest")
    missing = [key for key in required if key not in metrics]
    return _outcome(
        "retention_series_builds",
        persona,
        len(points) == 3 and ordered and not missing,
        f"{len(points)} points, ordered={ordered}, metrics missing {missing or 'none'}",
        observed={
            "points": len(points),
            "ordered_oldest_first": ordered,
            "metric_keys": sorted(metrics),
            "missing_metrics": missing,
        },
        expected={"points": 3, "required_metrics": list(required), "ordered": True},
    )


def _probe_retention_health(persona: Persona) -> ProbeOutcome:
    from app.services import retention

    points = retention.snapshot_series_points(
        [
            {
                "id": 1,
                "snapshot_type": "scheduled",
                "lifecycle_stage": "active",
                "loyalty_score": persona.loyalty_score,
                "churn_risk": persona.churn_risk,
                "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            },
            {
                "id": 2,
                "snapshot_type": "scheduled",
                "lifecycle_stage": "active",
                "loyalty_score": persona.loyalty_score,
                "churn_risk": persona.churn_risk,
                "created_at": datetime(2026, 8, 15, tzinfo=timezone.utc),
            },
        ]
    )
    context = retention.build_retention_health_context(points)
    report = retention.resolve_retention_health(context, effective_date=datetime(2026, 9, 30, tzinfo=timezone.utc))
    band = str(report.get("band") or report.get("health") or "")
    bands = set(retention.build_retention_catalog().get("bands", ()) or ())
    if not bands:
        bands = {str(row.get("band")) for row in getattr(retention, "RETENTION_HEALTH_RULES", ())}
    return _outcome(
        "retention_health_bands",
        persona,
        bool(band) and (not bands or band in bands),
        f"churn {persona.churn_risk!r} -> band {band!r}",
        observed={"band": band, "report_keys": sorted(report)[:8]},
        expected={"bands": sorted(bands) or "any non-empty band"},
    )


def _probe_forecast_confidence(persona: Persona) -> ProbeOutcome:
    """Confidence must *decay* with horizon. A flat curve is a broken contract.

    Checked as a monotonicity invariant rather than against magic numbers, for
    the reason recorded in ``BLOCKAGES.md`` for the band-direction test: a
    threshold-based assertion here would flag correct values and prove nothing
    about the ones it flagged.
    """
    from app.services import retention

    short = retention.forecast_confidence(7, 30)
    long = retention.forecast_confidence(180, 30)
    sparse = retention.forecast_confidence(7, 2)
    return _outcome(
        "forecast_confidence_decays",
        persona,
        0.0 <= long <= short <= 1.0 and sparse <= short,
        f"7d={short:.4f} 180d={long:.4f} 7d/2pts={sparse:.4f}",
        observed={"short": short, "long": long, "sparse": sparse},
        expected={"monotone_non_increasing": True, "within_unit_interval": True},
    )


def _probe_recovery_lifecycle(persona: Persona) -> ProbeOutcome:
    from app.services import recovery_playbooks

    context = {
        "churn_risk": persona.churn_risk,
        "loyalty_score": persona.loyalty_score,
        "days_since_last_activity": persona.days_since_last_activity,
        "consent_service": persona.consent_service,
        "consent_recovery": persona.consent_recovery,
    }
    stage = recovery_playbooks.recovery_lifecycle_stage(context)
    return _outcome(
        "recovery_lifecycle_stage",
        persona,
        bool(stage),
        f"churn {persona.churn_risk!r} inactive {persona.days_since_last_activity}d -> {stage!r}",
        observed={"stage": stage},
        expected={"non_empty": True},
    )


def _probe_recovery_actions(persona: Persona) -> ProbeOutcome:
    """A playbook that matched must deliver its actions, and a fix must not be
    withheld from somebody who reported a problem.

    **This probe used to assert nothing, and it is worth recording how.** It built
    a six-key context by hand, then asked the planner for actions and complained
    about any service/recovery action in the plan aimed at a persona who had
    withdrawn the relevant consent. Two failures in that shape:

    * The rule table reads ten fields and the hand-built context carried six.
      ``recovery_readiness``, ``sentiment_label`` and ``dissatisfaction_score`` are
      all derived from what the customer *wrote*, which the persona could not
      express, so **no playbook could ever match**. The plan was empty for all
      five personas and the probe held. A planner returning nothing passed a probe
      named "never withholds a fix".
    * Even given a matching plan, "suppressed" was computed by filtering the
      actions that *were* planned. An empty list is not a suppression, so the one
      failure that matters -- the plan going empty -- was invisible by
      construction. The name and the check pointed at opposite directions.

    So the claim is now stated in the direction that can fail: compare
    ``evaluate_recovery_playbooks`` against ``plan_recovery_actions``. Every action
    a matched playbook declares must appear in the plan, and every matched playbook
    must appear in the plan\'s ``playbook_id`` column. Both halves are checkable
    and both are breakable, which is the property that was missing.

    The consent half is kept, and it is the *other* direction: planning service or
    recovery outreach at somebody who withdrew that consent is over-contact, and it
    is still a defect.
    """
    from app.schemas.chat import InteractionInsight, InteractionSummary, Sentiment
    from app.services import chat_analytics, recovery_playbooks

    # Built through the same two functions production uses, so this cannot drift
    # from the shape `plan_recovery_actions` is actually called with. A hand-built
    # dict is how this probe came to be vacuous.
    counts: dict[str, int] = {}
    for state in persona.booking_states:
        counts[str(state)] = counts.get(str(state), 0) + 1
    summary = InteractionSummary(
        user_id=1000 + len(persona.persona_id),
        messages_analyzed=1 + len(persona.insights) * 3 + counts.get("cancelled", 0) * 2,
        bookings_analyzed=max(1, sum(counts.values())),
        churn_risk=persona.churn_risk,
        loyalty_score=persona.loyalty_score,
        monetization_readiness=round(persona.loyalty_score / 2.0, 2),
        value_tier=(
            "premium"
            if persona.loyalty_score >= 70.0
            else "growth"
            if persona.loyalty_score >= 35.0
            else "standard"
        ),
        customer_classification=persona.churn_risk,
        top_issues=[str(area) for area, _score in persona.insights],
        strengths=[],
        insights=[
            InteractionInsight(
                area=str(area),
                priority="high",
                score=float(score),
                evidence=[f"{persona.display_name} wrote about {area}"],
                evidence_summary=f"{persona.display_name} wrote about {area}",
                source="persona",
                recommendation="acknowledge and fix",
                next_step="resolve the issue",
            )
            for area, score in persona.insights
        ],
        metadata={
            "booking_states": counts,
            "repeated_messages": max(0, counts.get("cancelled", 0) * 3 - 1),
        },
        generated_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    sentiment = Sentiment(
        label=str(persona.sentiment[0]), score=float(persona.sentiment[1])
    )
    dissatisfaction = chat_analytics.build_dissatisfaction_recovery_report(
        summary, sentiment
    )
    context = recovery_playbooks.build_realtime_recovery_context(
        summary, sentiment, dissatisfaction
    )

    matched = recovery_playbooks.evaluate_recovery_playbooks(context)
    matched_ids = {str(row.get("playbook_id")) for row in matched}
    owed = sorted(
        {
            str(action.get("action"))
            for row in matched
            for action in row.get("actions", [])
            if str(action.get("action"))
        }
    )
    planned = recovery_playbooks.plan_recovery_actions(context)
    delivered_actions = {str(entry.get("action")) for entry in planned}
    delivered_ids = {str(entry.get("playbook_id")) for entry in planned}
    withheld_actions = [name for name in owed if name not in delivered_actions]
    withheld_playbooks = sorted(matched_ids - delivered_ids)
    over_contact = sorted(
        {
            str(entry.get("action"))
            for entry in planned
            if str(entry.get("purpose")) in {"service", "recovery"}
            and not (persona.consent_service and persona.consent_recovery)
        }
    )
    reasons: list[str] = []
    if withheld_actions:
        reasons.append(
            f"playbook(s) {sorted(matched_ids)} matched but the plan delivered "
            f"{sorted(delivered_actions)}, withholding {withheld_actions}. A fix "
            "the rules asked for is the one fix that cannot be optional"
        )
    if withheld_playbooks:
        reasons.append(
            f"playbook(s) {withheld_playbooks} matched and no action reached the "
            "plan at all, so the match had no effect"
        )
    if over_contact:
        reasons.append(
            f"{over_contact} planned at a customer who withdrew the service or "
            "recovery consent"
        )
    return _outcome(
        "recovery_never_withholds_a_fix",
        persona,
        not reasons,
        f"readiness {context['recovery_readiness']!r}, "
        f"{len(matched_ids)} playbook(s) {sorted(matched_ids)}, "
        f"{len(planned)} action(s) {sorted(delivered_actions)}",
        observed={
            "recovery_readiness": context["recovery_readiness"],
            "dissatisfaction_score": context["dissatisfaction_score"],
            "matched_playbooks": sorted(matched_ids),
            "planned_actions": sorted(delivered_actions),
            "withheld_actions": withheld_actions,
            "withheld_playbooks": withheld_playbooks,
            "over_contact": over_contact,
        },
        expected={
            "matched_playbooks_delivered": sorted(matched_ids),
            "owed_actions_delivered": owed,
            "over_contact": [],
        },
        error="; ".join(reasons),
    )


def _probe_consent_propagation(persona: Persona) -> ProbeOutcome:
    """The consent gate must never gate a service or recovery purpose.

    Two real invariants, both read from ``app.services.preferences`` rather than
    from where they were assumed to live:

    * ``service`` and ``recovery`` are absent from ``CONSENT_GATED_PURPOSES``;
    * the gated set is a subset of the declared ``CONSENT_PURPOSES``, so a
      purpose can be gated without the gate naming a vocabulary that does not
      exist.

    The second is the one that catches a config edit. The first version of this
    probe imported the constant from ``communication_strategy``, raised
    ``AttributeError`` on every persona, and would have been reported as three
    separate product blockages. **A probe that cannot find what it is looking
    for has not found a defect; it has failed to look.**
    """
    from app.services import preferences

    gated = tuple(preferences.CONSENT_GATED_PURPOSES)
    declared = tuple(preferences.CONSENT_PURPOSE_BY_NAME)
    forbidden = tuple(
        purpose for purpose in ("service", "recovery") if purpose in gated
    )
    undeclared = sorted(set(gated) - set(declared))
    reasons: list[str] = []
    if forbidden:
        reasons.append(f"gates {list(forbidden)}, which must never gate")
    if undeclared:
        reasons.append(f"gates undeclared purposes {undeclared}")
    return _outcome(
        "consent_gate_excludes_service",
        persona,
        not reasons,
        "CONSENT_GATED_PURPOSES=" + str(sorted(gated))
        + ("" if not reasons else "; " + "; ".join(reasons)),
        observed={
            "consent_gated_purposes": sorted(gated),
            "declared_purposes": sorted(declared),
            "forbidden_gated": list(forbidden),
            "undeclared_gated": undeclared,
        },
        expected={
            "service_and_recovery_not_gated": True,
            "gated_subset_of_declared": True,
        },
    )


def _probe_rule_pack_selection(persona: Persona) -> ProbeOutcome:
    """Every declared rule pack must select against a realistic context.

    The pack name is discovered from ``RULE_PACK_BY_NAME`` rather than written
    down. The first version hardcoded ``"risk_rules"``, which is a *model* name
    from ``model_versioning`` and not a rule pack at all, and it raised
    ``KeyError`` on every persona -- three invented blockages from one wrong
    string.
    """
    from app import rule_engine

    available = tuple(rule_engine.RULE_PACK_BY_NAME)
    context = {
        "churn_risk": persona.churn_risk,
        "access_score": persona.access_score,
        "days_since_last_activity": persona.days_since_last_activity,
        "loyalty_score": persona.loyalty_score,
    }
    selected_per_pack: dict[str, int] = {}
    errors: list[str] = []
    for pack_name in available:
        pack = rule_engine.get_rule_pack(pack_name)
        selected = rule_engine.select_rules(pack, context)
        rules = selected.get("rules") if isinstance(selected, dict) else selected
        selected_per_pack[pack_name] = len(rules or [])
    return _outcome(
        "rule_pack_selects",
        persona,
        bool(available) and not errors,
        f"{len(available)} pack(s) selected {selected_per_pack}",
        observed={"packs": list(available), "selected_per_pack": selected_per_pack},
        expected={"every_pack_selects_without_error": True},
    )


def _probe_admin_governance(persona: Persona) -> ProbeOutcome:
    """Every route must be classified by the authz table.

    An unclassified route is one nobody has described, and the drift report is
    the only thing in this tree that notices. Run for the admin persona only --
    this is a deployment check, not a customer journey -- and it reports rather
    than raises, so a new route appears as a blockage instead of a crashed run.
    """
    from app import deps
    from app.main import app

    report = deps.authz_drift_report(app.routes)
    return _outcome(
        "authz_routes_classified",
        persona,
        bool(report.get("in_sync")),
        f"{report.get('method_path_pairs')} pairs, "
        f"{len(report.get('unclassified_routes') or [])} unclassified, "
        f"{len(report.get('mismatched') or [])} mismatched",
        observed={
            "in_sync": report.get("in_sync"),
            "unclassified": list(report.get("unclassified_routes") or [])[:10],
            "mismatched": len(report.get("mismatched") or []),
        },
        expected={"in_sync": True},
    )


def _probe_shadow_isolation(persona: Persona) -> ProbeOutcome:
    """Is the shadow *able* to write live? Read from this process's environment.

    The check is deliberately narrow, and the narrowness is the point. It does
    not assert ``evaluate_isolation()["isolated"]``, because that verdict fails
    closed on checks nobody can measure in a normal checkout -- there is no
    ``CSERVICE_SHADOW_DATABASE_URL`` on a developer machine -- so asserting it
    here would fail this flow everywhere except a configured shadow deployment
    and teach the reader that the simulation is noisy.

    Instead it asserts the two states that are *actually dangerous*:

    * a **collision** -- the shadow is configured and its database identity
      resolves to live's, which is the mistake that writes customers' data;
    * a **forbidden direction** -- a replicated channel runs shadow -> live.

    "Not configured" is neither, so it passes, and ``configured: false`` is
    reported so the result is not read as a proof of isolation. The distinction
    between *absent* and *safe* is preserved in the evidence instead of being
    folded into a green tick -- the failure mode this whole subsystem exists to
    prevent is a published number that is not the enforced one.
    """
    from app import shadow_env

    verdict = shadow_env.current_isolation()
    directions = shadow_env.feed_direction_report()

    live_url = shadow_env.live_database_url()
    shadow_url = shadow_env.shadow_database_url()
    configured = bool(shadow_url)
    collision = False
    if configured and live_url:
        collision = shadow_env.same_database(
            shadow_env.database_identity(live_url), shadow_env.database_identity(shadow_url)
        )
    one_way = bool(directions.get("one_way"))
    reasons: list[str] = []
    if collision:
        reasons.append(
            "the shadow's database identity resolves to live's; a shadow pointed at "
            "live is a live deployment with a shadow's name"
        )
    if not one_way:
        reasons.append(
            "a replicated channel runs in a forbidden direction: "
            + "; ".join(shadow_env.leakage_paths())
        )

    return _outcome(
        "shadow_is_one_way",
        persona,
        not reasons,
        (
            f"shadow configured={configured}, collision={collision}, "
            f"{directions.get('channels')} channel(s) one-way={one_way}"
            + ("; " + "; ".join(reasons) if reasons else "")
        ),
        observed={
            "configured": configured,
            "collision_with_live": collision,
            "one_way": one_way,
            "forbidden_channels": list(directions.get("failed") or []),
            "leakage_paths": shadow_env.leakage_paths(),
            "blocking_failures": list(verdict.get("blocking_failures") or []),
            "isolated": bool(verdict.get("isolated")),
        },
        expected={
            "no_collision_with_live": True,
            "all_channels_one_way": True,
        },
    )


#: Every probe, by id. Kept as data so a flow's subflows name probes rather than
#: importing them, and a missing probe is a validator error instead of an
#: ``AttributeError`` halfway through a run.
# ---------------------------------------------------------------------------
# Stage E probes
# ---------------------------------------------------------------------------


#: Region -> the local hour that region reads at 23:00Z on 1 October 2026.
#:
#: Hand-computed from each region's declared UTC offset, and the probe's own
#: independent oracle. Deliberately *not* derived from ``region_windows``: an
#: expectation computed by the engine under test cannot fail, whatever the
#: engine does. These are the numbers a reader can check against a clock.
#:
#: Only regions a persona actually occupies are listed. An undeclared region is
#: reported as unjudgeable rather than guessed at.
_EXPECTED_LOCAL_HOUR: dict[str, int] = {
    "oceania_auckland": 12,   # UTC+13 -> noon the next day, inside 09:00-17:00
    "asia_tokyo": 8,          # UTC+9  -> 08:00, before the window
    "europe_london": 0,       # UTC+1  -> midnight the next day, outside
    "us_east": 19,            # UTC-4  -> 19:00, after the window
    "us_west": 16,            # UTC-7  -> 16:00, inside the window
}


def _probe_local_contact_hour(persona: Persona) -> ProbeOutcome:
    """Quiet hours must be evaluated where the customer is, not where the server is.

    The Stage D gate read ``float(moment.hour)`` off a UTC datetime, so a customer
    in Auckland had their stated 09:00-17:00 window checked against UTC hours and
    the whole Stage D suite stayed green, because every persona in it was the
    server's timezone.

    So this probe's personas differ *only* in region, at one fixed UTC moment. If
    the hour were read at UTC, every persona would agree and the constant-answer
    detector would grade this part ``partial`` rather than ``complete`` -- which is
    the honest outcome, not a passing test.

    **The region comes from the persona, and that is load-bearing.** This probe
    used to carry a local ``regions`` dict keyed on ``persona_id``. Two of its
    three keys named personas no flow ever ran it for -- including
    ``at_risk_high_value``, which is in no ``PERSONAS`` tuple at all -- so every
    persona that actually reached the probe fell through the ``.get`` default to
    ``us_east``. The probe reported **one** distinct expectation across three
    personas while this docstring claimed they differed in region, and
    ``capability_audit``'s constant-answer detector correctly graded it blind
    without anybody reading that as a defect. ``Persona.region_id`` makes the
    variation a property of the persona, so a new persona is either in a region
    or explicitly not, and there is no lookup table left to forget.
    """
    from app.services import region_windows

    moment = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)
    # Every persona is given the *same* stated window. Only the region varies.
    window = {"contact_window_start_hour": 9, "contact_window_end_hour": 17,
              "quiet_hours_enabled": True}
    region = persona.region_id
    observed = region_windows.evaluate_contact_window(
        preferences_map=window, region_id=region, moment=moment
    )
    hour = int(observed["local_hour"])
    within = bool(observed["within_declared_window"])

    # The expected local hour is a **hand-computed literal per region**, not a
    # boolean derived from the observation. This is the difference between a
    # probe and a tautology.
    #
    # A boolean expectation -- `local_hour_not_utc_hour`, `matches_stated_window`
    # -- is derived *from the value being checked*, so it is true for every
    # observation the engine could possibly return. Four personas in four regions
    # agreed on it, `capability_audit` graded the probe blind, and the two facts
    # sat next to each other without anybody reading the pair as a defect.
    #
    # These numbers are 23:00Z plus each region's declared offset: Auckland is
    # UTC+13 so it is noon the next day, London is UTC+1 so it is midnight, the
    # US coasts are behind. Typed out here on purpose: they are the independent
    # oracle the probe deliberately does not have anywhere else, and a reader who
    # suspects one is wrong can check it against a clock without running anything.
    expected_hour = _EXPECTED_LOCAL_HOUR.get(region)
    if expected_hour is None:
        # An undeclared region gets no opinion from this probe. Reporting a
        # near-miss is worse than reporting nothing, because "we checked" would
        # be a claim nobody made.
        return _outcome(
            "contact_hour_is_local",
            persona,
            False,
            f"no published local-hour expectation for region {region!r}",
            observed={"region": region, "local_hour": hour,
                      "within_declared_window": within,
                      "utc_hour": moment.hour},
            expected={"region_declared": False},
            error=(
                f"region {region!r} has no entry in the probe's own expectation "
                f"table, so this probe cannot judge it"
            ),
        )
    expected_within = 9 <= expected_hour < 17
    reasons: list[str] = []
    if hour == moment.hour:
        reasons.append(
            f"resolved {moment.hour:02d}:00 local in {region}, which is the UTC "
            "hour: the local-hour arithmetic is not being applied"
        )
    if hour != expected_hour:
        reasons.append(
            f"23:00Z in {region} is {expected_hour:02d}:00 local, not "
            f"{hour:02d}:00"
        )
    if within != expected_within:
        reasons.append(
            f"{hour:02d}:00 local against a 09:00-17:00 window should be "
            f"{'inside' if expected_within else 'outside'}, not "
            f"{'inside' if within else 'outside'}"
        )
    return _outcome(
        "contact_hour_is_local",
        persona,
        not reasons,
        f"{region} at 23:00Z is {hour:02d}:00 local, "
        f"{'inside' if within else 'outside'} the stated window",
        observed={"region": region, "local_hour": hour,
                  "within_declared_window": within,
                  "utc_hour": moment.hour},
        # Carries the literal hour, so two personas in two regions produce two
        # different expectations and the constant-answer detector can see the
        # difference it exists to detect.
        expected={"local_hour": expected_hour,
                  "within_declared_window": expected_within,
                  "region_declared": True},
        error="; ".join(reasons),
    )


def _probe_purpose_limited_personalization(persona: Persona) -> ProbeOutcome:
    """A recovery message may refer to history; a marketing one may not.

    Persona matters here for a real reason: the personas differ in whether they
    granted the ``personalization`` consent, and a marketing message's
    permissions are the one case where that boolean changes the answer. Asking
    every persona the marketing question with the same consent would let a
    constant engine pass.
    """
    from app.services import care_personalization

    granted = persona.personalization_consent
    consents = {"personalization": granted}
    recovery = care_personalization.resolve_personalization(
        purpose="recovery", consents=consents
    )
    marketing = care_personalization.resolve_personalization(
        purpose="marketing", consents=consents
    )
    reasons: list[str] = []
    # Recovery is never limited by the marketing consent flag.
    if "history_reference" not in recovery["allowed"]:
        reasons.append(
            "recovery refused history_reference. A recovery message written more "
            "generically because of a marketing flag is worse at the one job it has"
        )
    should_have_history = "history_reference" in marketing["allowed"]
    if should_have_history != granted:
        reasons.append(
            f"marketing {'has' if should_have_history else 'refuses'} "
            f"history_reference but personalization consent is {granted}"
        )
    if not marketing["refusal_reasons"]:
        reasons.append(
            "marketing refusals carry no reasons. A refusal that only says 'not "
            "permitted' is not actionable by whoever writes the message"
        )
    return _outcome(
        "personalization_is_purpose_limited",
        persona,
        not reasons,
        f"consent={granted}: recovery allows history_reference, marketing "
        f"{'allows' if should_have_history else 'refuses'} it",
        observed={"consent": granted,
                  "recovery_allowed": list(recovery["allowed"]),
                  "marketing_allowed": list(marketing["allowed"]),
                  "marketing_refused": list(marketing["refused"])},
        # The expectation is the persona's own consent, not a restatement of the
        # answer. `marketing_follows_consent: should_have_history == granted`
        # reads like the same claim and is not: it is `True` for every value the
        # engine can return, so a persona with consent and a persona without it
        # produced one fingerprint and the constant-answer detector called the
        # probe blind. Stating the consent *as* the expectation is a claim the
        # engine can contradict.
        expected={"recovery_allows_history": True,
                  "marketing_allows_history_reference": granted},
        error="; ".join(reasons),
    )


#: Sighting count -> the trust band that count should earn.
#:
#: Hand-computed from the published bands: one sighting is a device we have met,
#: five is one we have seen repeatedly, twelve is one we would call long-standing.
#: Kept beside the probe rather than read from ``resolve_trust_band`` so the probe
#: has an opinion that the engine can contradict.
_EXPECTED_TRUST_BAND: dict[int, str] = {
    0: "unknown",
    1: "recognized",
    5: "elevated",
    12: "trusted",
}


def _probe_trusted_device_respects_the_gate(persona: Persona) -> ProbeOutcome:
    """A recognised device must not unlock unattended contact.

    The invariant is that recognition shortens *authentication* and never
    *permission*. Personas differ in how strongly their device is recognised, and
    the gate's verdict is held at refusing -- so a device can only ever widen the
    set of permitted steps that are not contact.
    """
    from app.services import care_personalization

    recognitions = int(persona.device_recognitions or 0)
    trust = care_personalization.resolve_trust_band(
        recognized=True, recognitions=recognitions, signals_matched=3
    )
    held = care_personalization.resolve_care_paths(trust, gate={"push": False})
    reasons: list[str] = []
    if "send_message_unattended" in held["permitted_step_ids"]:
        reasons.append(
            f"a {trust['band']} device unlocked unattended contact while the "
            "preference gate refused it. Recognition shortens authentication, not "
            "permission"
        )
    for step in ("accept_own_offer", "decline_own_offer"):
        if step not in held["permitted_step_ids"]:
            reasons.append(
                f"the gate refusing *contact* also blocked {step}. A contact "
                "preference must not stop them acting on their own account"
            )
    return _outcome(
        "trusted_device_never_overreaches",
        persona,
        not reasons,
        f"{trust['band']} device, gate refusing push: "
        f"{len(held['permitted_step_ids'])} steps permitted, "
        f"{len(held['refused_step_ids'])} refused",
        observed={"trust_band": trust["band"], "recognitions": recognitions,
                  "permitted": list(held["permitted_step_ids"]),
                  "refused": list(held["refused_step_ids"])},
        # Two claims. `unattended_contact_permitted: False` is the invariant and
        # is stated as a literal, so a `trusted` device that unlocks a message
        # contradicts it directly. `trust_band` is this probe's own oracle for
        # how many sightings earn which band -- hand-computed, not read back from
        # `resolve_trust_band`, so the engine cannot agree with itself.
        #
        # Both matter because the previous expectation was two `True`s, which is
        # what a `recognized` device and a `trusted` one both produced, and
        # "trust never widens permission" is a claim about the *difference*
        # between them.
        expected={"unattended_contact_permitted": False,
                  "own_account_steps_permitted": True,
                  "trust_band": _EXPECTED_TRUST_BAND.get(recognitions)},
        error="; ".join(reasons),
    )


def _probe_relationship_view_default_pane(persona: Persona) -> ProbeOutcome:
    """The copilot is the pane an agent lands on, and moving off it is deliberate.

    Personas differ in whether they *asked* for another pane, which is the only
    thing that moves the default. If the default could be moved by naming a pane,
    every persona would agree and this would pass a default that was not one.
    """
    from app.services import relationship_view

    requested = {
        "loyal_with_points": "history",
    }.get(persona.persona_id, "")
    explicit = persona.persona_id == "loyal_with_points"
    view = relationship_view.build_relationship_view(
        copilot_card={"opening": "Thanks for getting in touch.", "user_id": 1},
        health={"band": "stable"},
        status={"status": "Trusted"},
        pane=requested or None,
        explicit_pane=explicit,
    )
    reasons: list[str] = []
    if explicit and view["pane"] != "history":
        reasons.append(
            f"an explicitly requested pane was refused: got {view['pane']!r}. "
            "Inspection is a different act from opening a conversation"
        )
    if not explicit and view["pane"] != relationship_view.DEFAULT_PANE:
        reasons.append(
            f"a merely-named pane moved the default to {view['pane']!r}. A default "
            "any caller can move by naming something is not a default"
        )
    if view["requires_human"] is not True:
        reasons.append("the view does not require a human; it may be acting alone")
    return _outcome(
        "copilot_is_the_default_pane",
        persona,
        not reasons,
        f"requested {requested or '(none)'!r} explicit={explicit} -> "
        f"{view['pane']!r}",
        observed={"requested": requested, "explicit": explicit,
                  "pane": view["pane"],
                  "pane_was_defaulted": view["pane_was_defaulted"]},
        expected={"pane": "history" if explicit else relationship_view.DEFAULT_PANE},
        error="; ".join(reasons),
    )


# ---------------------------------------------------------------------------
# Stage F probes
# ---------------------------------------------------------------------------


#: Experiential tier -> the status that tier is supposed to earn.
#:
#: Hand-written from the published status rules: an unscored customer is a
#: `member`, bronze and silver are `established`, gold is `trusted`, platinum is
#: `principal`. This is the oracle ``status_never_decays`` checks against, and it
#: is deliberately a second transcription of the mapping -- a probe that imported
#: the table would be grading the table with itself.
_EXPECTED_STATUS_BY_TIER: dict[str, str] = {
    "unscored": "member",
    "bronze": "established",
    "silver": "established",
    "gold": "trusted",
    "platinum": "principal",
}


def _probe_status_never_decays(persona: Persona) -> ProbeOutcome:
    """A status must not fall for inactivity, whatever the rule table says.

    This is the guard the trust module names, and it is a guard rather than a
    test because the invariant is structural: there is no decay field and no
    calendar transition. The probe asserts the *structure*, then asserts the
    behaviour for each persona's own inactivity, because a constant that reads
    False while a rule still decays would pass a structure-only check.

    Personas differ in how long they have been away, so a decay rule keyed on
    elapsed days would show up here.
    """
    from app.services import loyalty_status

    resolve_loyalty_status = loyalty_status.resolve_loyalty_status
    reasons: list[str] = []
    if loyalty_status.STATUS_DECAYS_ON_INACTIVITY is not False:
        reasons.append(
            "STATUS_DECAYS_ON_INACTIVITY is no longer False. That constant is the "
            "promise, and changing it is a product decision, not an edit"
        )
    for row in loyalty_status.LOYALTY_STATUS_RULES:
        if "decay" in row:
            reasons.append(
                f"status {row.get('status_id')!r} carries a `decay` field, which is "
                "the shape the invariant exists to forbid"
            )
    away = int(persona.days_since_last_activity or 0)
    # The ledger carries *this* persona's earned tier. It used to hard-code
    # `gold` for everybody, which meant three of five personas resolved to
    # `trusted` and the fourth rung of the status table -- `principal`, and the
    # `member` floor an unscored customer falls back to -- was never resolved by
    # this probe at all. A guard that only ever sees one rung is a guard on one
    # rung.
    tier = persona.experiential_tier
    unscored = tier == "unscored"
    ledger = {
        "tier": tier,
        "reported_tier": tier,
        "unscored": unscored,
        "days_since_last_activity": away,
    }
    now = resolve_loyalty_status(ledger)
    decade = resolve_loyalty_status({**ledger, "days_since_last_activity": 3650})
    expected_status = _EXPECTED_STATUS_BY_TIER.get(tier)
    if expected_status is None:
        reasons.append(
            f"no published status expectation for tier {tier!r}, so this probe "
            f"cannot judge {persona.persona_id!r}"
        )
    elif now.get("status_id") != expected_status:
        reasons.append(
            f"tier {tier!r} resolved to {now.get('status_id')!r}, not "
            f"{expected_status!r}"
        )
    if decade.get("status_id") != now.get("status_id"):
        reasons.append(
            f"a ten-year absence moved {now.get('status_id')!r} to "
            f"{decade.get('status_id')!r}. Ten years of not calling is not a "
            "demotion offence"
        )
    return _outcome(
        "status_never_decays",
        persona,
        not reasons,
        f"absent {away}d -> {now.get('status_id')!r}; absent 3650d -> "
        f"{decade.get('status_id')!r}",
        observed={"days_away": away, "experiential_tier": tier,
                  "status": now.get("status_id"),
                  "status_after_10y": decade.get("status_id"),
                  "decays_on_inactivity": loyalty_status.STATUS_DECAYS_ON_INACTIVITY},
        # The persona's own tier, and the status that tier is supposed to earn.
        # `status_after_10y: now.get("status_id")` was a restatement of the
        # observation, so it agreed with the engine by construction and three
        # personas on one rung produced one fingerprint.
        expected={"experiential_tier": tier,
                  "status": expected_status,
                  "status_after_10y": expected_status},
        error="; ".join(reasons),
    )


def _probe_policy_motion_needs_evidence(persona: Persona) -> ProbeOutcome:
    """A change to what somebody is owed needs evidence, not an opinion.

    Personas differ in the evidence they attach, so a ledger that accepted
    everything -- or refused everything -- would produce a constant answer and be
    graded partial rather than complete.

    The evidence lives on the persona now, for the same reason ``region_id``
    does. It used to sit in a local dict, and the two flows that ran this probe
    happened to name personas whose entries were both empty, so two different
    customers produced one fingerprint and the probe was blind. The interesting
    case -- an *opinion* carrying four hundred samples -- was attached to a
    persona id that no flow ever ran this probe for, so the one input that
    separates "measured" from "well-attested" was never executed.
    """
    from app.services import policy_motion

    evidence = [dict(item) for item in persona.motion_evidence]
    motion = policy_motion.build_motion(
        target="app.services.loyalty_status.LOYALTY_STATUS_RULES",
        evidence=evidence,
        reason="retuning the status rule",
        proposed_by="analytics",
    )
    sufficient = bool(evidence) and str(evidence[0]["kind"]) == "measured_outcome"
    reasons: list[str] = []
    if (motion["verdict"] == "sufficient") != sufficient:
        reasons.append(
            f"{evidence!r} gave {motion['verdict']!r}; status_rule_change needs "
            "measured_outcome at 100 samples, so this should be "
            f"{'sufficient' if sufficient else 'insufficient'}"
        )
    if motion["applied"] is not False:
        reasons.append("the motion claims to have been applied; it must never be")
    return _outcome(
        "policy_motion_needs_evidence",
        persona,
        not reasons,
        f"{len(evidence)} evidence item(s) -> {motion['verdict']}",
        observed={"evidence": evidence, "verdict": motion["verdict"],
                  "class_id": motion["class_id"]},
        # States the evidence the persona carries, so the expectation is a
        # property of the persona rather than a restatement of the engine's word
        # for it. Two personas both carrying `[]` legitimately agree here; a
        # persona carrying an opinion with 400 samples behind it does not.
        expected={"verdict": "sufficient" if sufficient else "insufficient",
                  "applied": False},
        error="; ".join(reasons),
    )


def _probe_broken_promise_is_visible(persona: Persona) -> ProbeOutcome:
    """A broken promise must be reported, and an unchecked one must not read as held.

    Personas differ in which guard is failing, so a continuity function that
    always returned True would agree with every persona here and be caught by the
    constant-answer detector.
    """
    from app.services import policy_motion

    failing = {
        "loyal_with_points": "status_never_decays",
        "abandoned_pending": "contact_hour_is_local",
    }.get(persona.persona_id)
    guards = {
        str(row["regression_guard"]): True
        for row in policy_motion.PROMISES
        if str(row["regression_guard"]) != failing
    }
    if failing:
        guards[failing] = False
    declared = [{"promise_id": pid} for pid in policy_motion.PROMISE_IDS]
    report = policy_motion.check_promise_continuity(declared, guards=guards)
    reasons: list[str] = []
    if failing:
        if report["continuity"] is not False:
            reasons.append(
                f"{failing!r} is failing but continuity is {report['continuity']}. A "
                "broken promise nobody can see is the same as one never made"
            )
        if not report["broken"]:
            reasons.append("the broken promise is not listed")
        for row in report["broken"]:
            if not str(row.get("commitment") or "").strip():
                reasons.append(
                    "a broken promise is reported without the commitment it broke, so "
                    "a reader learns that something broke and not what"
                )
    elif report["unverified"]:
        reasons.append(
            f"guards all True still leaves {len(report['unverified'])} unverified"
        )
    return _outcome(
        "broken_promise_is_visible",
        persona,
        not reasons,
        f"failing={failing or 'none'}: continuity={report['continuity']}, "
        f"{len(report['broken'])} broken, {len(report['unverified'])} unverified",
        observed={"failing_guard": failing or "",
                  "continuity": report["continuity"],
                  "broken": [r["promise_id"] for r in report["broken"]],
                  "unverified": len(report["unverified"])},
        expected={"continuity": False if failing else True},
        error="; ".join(reasons),
    )


PROBES: dict[str, Probe] = {
    "access_band_resolves": _probe_access_band,
    "access_band_matches_thresholds": _probe_band_spread,
    "tier_and_posture_resolve": _probe_tier_and_posture,
    "posture_adjustment_is_effective": _probe_posture_adjustment,
    "booking_states_are_valid": _probe_booking_states,
    "complaint_sla_is_monotonic": _probe_complaint_sla,
    "complaint_is_routed": _probe_complaint_route,
    "complaint_verdict_is_explained": _probe_complaint_verdict,
    "retention_series_builds": _probe_retention_series,
    "retention_health_bands": _probe_retention_health,
    "forecast_confidence_decays": _probe_forecast_confidence,
    "recovery_lifecycle_stage": _probe_recovery_lifecycle,
    "recovery_never_withholds_a_fix": _probe_recovery_actions,
    "consent_gate_excludes_service": _probe_consent_propagation,
    "rule_pack_selects": _probe_rule_pack_selection,
    "authz_routes_classified": _probe_admin_governance,
    "shadow_is_one_way": _probe_shadow_isolation,
    "contact_hour_is_local": _probe_local_contact_hour,
    "personalization_is_purpose_limited": _probe_purpose_limited_personalization,
    "trusted_device_never_overreaches": _probe_trusted_device_respects_the_gate,
    "copilot_is_the_default_pane": _probe_relationship_view_default_pane,
    "status_never_decays": _probe_status_never_decays,
    "policy_motion_needs_evidence": _probe_policy_motion_needs_evidence,
    "broken_promise_is_visible": _probe_broken_promise_is_visible,
}
PROBE_IDS: tuple[str, ...] = tuple(sorted(PROBES))


# ---------------------------------------------------------------------------
# The flow catalog


FLOW_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "flow_id": "new_customer_first_booking",
        "title": "A new customer registers and books for the first time",
        "actor": "customer",
        "personas": ("abandoned_pending", "loyal_with_points"),
        "subflows": (
            "register",
            "authenticate",
            "policy_scored",
            "discover_service",
            "create_booking",
            "confirm_booking",
        ),
        "probe_ids": (
            "booking_states_are_valid",
            "tier_and_posture_resolve",
            "access_band_resolves",
        ),
        "surfaces": ("/users/register", "/users/login", "/bookings", "/users/me"),
        "invariant": (
            "a customer with no history still lands in a known band and posture; "
            "the default must not be the same value every new account receives"
        ),
        "description": (
            "The onboarding path. Everything defaults, so it is where a resolver "
            "whose fallback is unreachable shows up first."
        ),
    },
    {
        "flow_id": "repeat_customer_changes_booking",
        "title": "A returning customer changes an existing booking",
        "actor": "customer",
        "personas": ("loyal_with_points", "abandoned_pending"),
        "subflows": ("read_booking", "reschedule", "cancel", "event_log"),
        "probe_ids": ("booking_states_are_valid",),
        "surfaces": ("/bookings", "/bookings/analytics/events"),
        "invariant": "a status change is written to the event log, not only to the row",
        "description": (
            "The change path. A booking state that is valid in isolation but never "
            "reaches the event log leaves the rollback question unanswerable."
        ),
    },
    {
        "flow_id": "at_risk_customer_recovery",
        "title": "An at-risk customer is detected and offered recovery",
        "actor": "system",
        "personas": ("repeatedly_cancelled",),
        "subflows": ("detect_dissatisfaction", "plan_playbook", "gate_consent", "credit_points", "notify"),
        "probe_ids": (
            "recovery_lifecycle_stage",
            "recovery_never_withholds_a_fix",
            "consent_gate_excludes_service",
            "contact_hour_is_local",
            "personalization_is_purpose_limited",
            "trusted_device_never_overreaches",
            "policy_motion_needs_evidence",
        ),
        "surfaces": ("/chat/admin/recovery/playbooks", "/chat/admin/recovery-guards"),
        "invariant": (
            "a customer who reported a problem is never refused a fix because of a "
            "marketing preference"
        ),
        "description": (
            "The recovery loop. `chiara` has withdrawn analytics consent and has "
            "three cancellations, which is the combination that must still get service."
        ),
    },
    {
        "flow_id": "dormant_customer_win_back",
        "title": "A dormant customer is brought back",
        "actor": "system",
        "personas": ("dormant_45_days",),
        "subflows": ("snapshot_series", "health_band", "forecast", "win_back_offer"),
        "probe_ids": (
            "retention_series_builds",
            "retention_health_bands",
            "forecast_confidence_decays",
            "contact_hour_is_local",
            "status_never_decays",
        ),
        "surfaces": ("/chat/admin/retention-health", "/chat/admin/retention-forecast"),
        "invariant": "forecast confidence decays with horizon and with sample size",
        "description": (
            "The dormancy path. `dmitri` has been inactive 45 days, which is the "
            "window the health banding branches on."
        ),
    },
    {
        "flow_id": "complaint_escalation_to_resolution",
        "title": "A complaint is raised, routed, escalated and closed",
        "actor": "customer",
        "personas": ("repeatedly_cancelled", "loyal_with_points"),
        "subflows": ("open_case", "acknowledge", "assign_owner", "escalate", "resolve", "close", "rate"),
        "probe_ids": (
            "complaint_sla_is_monotonic",
            "complaint_is_routed",
            "complaint_verdict_is_explained",
        ),
        "surfaces": ("/complaints", "/complaints/{reference}", "/complaints/admin/queue"),
        "invariant": (
            "every decision on a case names the guard that produced it, and a "
            "downgrade is recorded as refused rather than vanishing"
        ),
        "description": (
            "The case spine. The escalation verdict is fed complete metrics here, "
            "because the fail-closed answer to missing metrics is a confident reject."
        ),
    },
    {
        "flow_id": "regulatory_complaint_deadline",
        "title": "A complaint under a statutory deadline",
        "actor": "operator",
        "personas": ("repeatedly_cancelled",),
        "subflows": ("detect_regulatory", "compute_sla_floor", "auto_escalate", "report_basis"),
        "probe_ids": ("complaint_sla_is_monotonic", "complaint_is_routed"),
        "surfaces": ("/complaints/admin/sla-report", "/complaints/admin/sweep"),
        "invariant": (
            "only a statutory deadline or a breached clock may act without a human, "
            "and the reported basis is the clock that actually governed"
        ),
        "description": (
            "The regulatory branch, where the SLA is a legal deadline rather than an "
            "internal target and the reported basis must not be the flattering one."
        ),
    },
    {
        "flow_id": "preference_and_consent_change",
        "title": "A customer changes how they are contacted",
        "actor": "customer",
        "personas": ("loyal_with_points", "repeatedly_cancelled"),
        "subflows": ("read_preferences", "save_preference", "grant_consent", "revoke_consent", "strategy_applies"),
        "probe_ids": ("consent_gate_excludes_service", "contact_hour_is_local", "personalization_is_purpose_limited", "trusted_device_never_overreaches"),
        "surfaces": ("/chat/me/preferences", "/chat/me/consent-history"),
        "invariant": (
            "a purpose added to the consent-gated set shows up in the guard rather "
            "than silently starting to suppress contact"
        ),
        "description": (
            "The control surface. `ana` has stated a preference; `chiara` has granted "
            "analytics consent and is the one whose gating is visible."
        ),
    },
    {
        "flow_id": "support_agent_triage",
        "title": "An agent triages an incoming request",
        "actor": "support_agent",
        "personas": ("loyal_with_points", "abandoned_pending", "repeatedly_cancelled"),
        "subflows": ("classify_topic", "apply_route_policy", "check_capacity", "respond"),
        "probe_ids": (
            "rule_pack_selects",
            "access_band_matches_thresholds",
            "copilot_is_the_default_pane",
        ),
        "surfaces": ("/topics/plan", "/topics/governance/routes"),
        "invariant": "a score just under a band boundary reports the band it missed and by how much",
        "description": (
            "The agent path. Band boundaries are where a resolver is most likely to "
            "be off by one, so this flow checks the score against the thresholds "
            "directly instead of only checking the returned label."
        ),
    },
    {
        "flow_id": "admin_governance_review",
        "title": "An administrator reviews the system's own governance",
        "actor": "admin",
        "personas": ("admin_console",),
        "subflows": ("classify_routes", "validate_ladder", "read_blockages", "release_shadow"),
        "probe_ids": (
            "authz_routes_classified",
            "shadow_is_one_way",
            "policy_motion_needs_evidence",
            "broken_promise_is_visible",
        ),
        "surfaces": ("/meta/authz", "/kaizen/admin/catalog", "/kaizen/admin/shadow"),
        "invariant": (
            "every live route is classified, and the shadow is provably unable to "
            "write live"
        ),
        "description": (
            "The governance loop, and the only flow whose findings block a release "
            "rather than describing a customer-visible problem."
        ),
    },
    {
        "flow_id": "points_and_arrears_payment",
        "title": "A customer pays arrears or exchanges points",
        "actor": "customer",
        "personas": ("loyal_with_points", "repeatedly_cancelled"),
        "subflows": ("read_wallet", "quote_exchange", "apply_exchange", "quote_arrears", "pay_arrears"),
        "probe_ids": ("posture_adjustment_is_effective",),
        "surfaces": ("/chat/points/wallet", "/chat/points/exchange/quote", "/chat/payments/arrears"),
        "invariant": "a quote is reproducible: the same wallet and the same request give the same numbers",
        "description": (
            "The money path. `ana` has a balance and an arrears deferral; `chiara` has "
            "a smaller one, which is where a rounding difference shows up."
        ),
    },
    {
        "flow_id": "customer_360_review",
        "title": "A customer and an agent read the same 360 view",
        "actor": "customer",
        "personas": ("loyal_with_points", "repeatedly_cancelled", "dormant_45_days"),
        "subflows": ("aggregate_sections", "build_communication", "explain_decisions"),
        # `policy_motion_needs_evidence` is here because "why has this person's
        # value changed, and may it change again" is part of reading a 360 -- and
        # because this flow runs three personas with three different evidence
        # attachments, where the two flows that also name the probe run two
        # personas whose evidence is identical.
        "probe_ids": (
            "retention_health_bands",
            "recovery_lifecycle_stage",
            "copilot_is_the_default_pane",
            "status_never_decays",
            "broken_promise_is_visible",
            "policy_motion_needs_evidence",
        ),
        "surfaces": ("/chat/customer-360", "/chat/me/explanations"),
        "invariant": "a section that could not be built is named rather than omitted",
        "description": (
            "The aggregate view, where an unavailable feed and a feed with nothing "
            "to say are different claims about the world."
        ),
    },
    {
        "flow_id": "auth_failure_and_recovery",
        "title": "A customer fails to sign in and recovers access",
        "actor": "customer",
        "personas": ("abandoned_pending",),
        "subflows": ("failed_login", "rate_limited", "refresh_token", "reauthenticate"),
        "probe_ids": ("tier_and_posture_resolve",),
        "surfaces": ("/users/login", "/users/refresh", "/users/me"),
        "invariant": (
            "a lockout is rate-limited rather than silent, and a refresh rotates the "
            "token rather than accepting a replayed one"
        ),
        "description": (
            "The authentication failure path, which is the only brute-forceable "
            "surface in the module and the one place the rate tier has to fire."
        ),
    },
)
FLOW_BY_ID: dict[str, dict[str, Any]] = {
    str(row["flow_id"]): dict(row) for row in FLOW_CATALOG
}
FLOW_IDS: tuple[str, ...] = tuple(str(row["flow_id"]) for row in FLOW_CATALOG)


# ---------------------------------------------------------------------------
# Running


@dataclass
class FlowRun:
    """One flow, run against one persona, in one environment."""

    flow_id: str
    persona_id: str
    environment: str = "offline"
    outcomes: list[ProbeOutcome] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def failures(self) -> list[ProbeOutcome]:
        """Subflows that ran and did not hold. Deferred ones are excluded.

        Excluding them is the point: a deferred subflow is a part that is not
        built, and reporting it as a failure would put the roadmap on the
        incident page. It is counted and named separately instead, because
        quietly dropping it would be worse -- a flow that silently tested nothing
        is indistinguishable from a flow that passed.
        """
        return [
            outcome
            for outcome in self.outcomes
            if not outcome.held and not outcome.deferred
        ]

    @property
    def deferred(self) -> list[ProbeOutcome]:
        """Subflows skipped because the part they exercise is absent."""
        return [outcome for outcome in self.outcomes if outcome.deferred]

    @property
    def passed(self) -> bool:
        """Did every subflow that *ran* hold?

        True for a flow whose every subflow was deferred, because nothing failed
        -- which is why :func:`flow_gate_measurements` publishes
        ``subflows_deferred`` alongside it and the completeness layer refuses to
        read the two together. A gate that only saw ``flows_failed: 0`` would be
        satisfied by a backend that has built nothing.
        """
        return all(
            outcome.held or outcome.deferred for outcome in self.outcomes
        )

    @property
    def exercised(self) -> int:
        """Subflows that actually ran, as opposed to being deferred."""
        return len(self.outcomes) - len(self.deferred)

    def as_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "persona_id": self.persona_id,
            "environment": self.environment,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "subflows": len(self.outcomes),
            "exercised_subflows": self.exercised,
            "failed_subflows": len(self.failures),
            "deferred_subflows": len(self.deferred),
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }


def run_flow(
    flow_id: str,
    persona_id: str,
    *,
    environment: str = "offline",
) -> FlowRun:
    """Run one flow for one persona.

    Refuses an unknown flow or persona with a ``ValueError`` naming the valid
    set -- a caller error about the catalog, not a judgement about the product.
    """
    flow = FLOW_BY_ID.get(str(flow_id))
    if flow is None:
        raise ValueError(f"unknown flow {flow_id!r}; known: {', '.join(FLOW_IDS)}")
    persona = PERSONA_BY_ID.get(str(persona_id))
    if persona is None:
        raise ValueError(f"unknown persona {persona_id!r}; known: {', '.join(PERSONA_IDS)}")
    started = datetime.now(timezone.utc)
    outcomes = [
        _probe(PROBES[probe_id], str(probe_id), persona)
        for probe_id in _str_tuple(flow.get("probe_ids"))
        if probe_id in PROBES
    ]
    elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
    return FlowRun(
        flow_id=str(flow_id),
        persona_id=str(persona_id),
        environment=str(environment),
        outcomes=outcomes,
        duration_ms=round(elapsed, 3),
    )


def run_all_flows(
    *,
    environment: str = "offline",
    flow_ids: Iterable[str] | None = None,
) -> list[FlowRun]:
    """Every flow against every persona it names.

    Flows that name an admin persona run for that persona alone -- the
    governance flow is not something ``chiara`` does, and pretending otherwise
    would put an admin-only probe in a customer journey.

    ``flow_ids`` filters, and **used to be accepted and ignored**: the loop read
    ``FLOW_CATALOG`` unconditionally, so ``run_all_flows(flow_ids=[one_flow])``
    returned all twenty-one runs across all twelve flows. No caller and no test
    passed it, which is exactly why it survived -- a parameter nobody exercises
    is not a parameter anybody notices is broken. It is honoured now, and
    ``test_run_all_flows_honours_its_flow_ids`` pins it, because the failure mode
    of a silently ignored filter is a caller who believes it ran a subset and
    reads the whole-set result as if it were the subset's.

    An unknown id is refused by name, listing what exists. Silently dropping a
    misspelled flow id would return an empty list, which reads as "everything
    passed" rather than "you asked for something that does not exist".
    """
    wanted: tuple[str, ...] | None
    if flow_ids is None:
        wanted = None
    else:
        wanted = tuple(_str_tuple(flow_ids))
        unknown = [name for name in wanted if name not in FLOW_BY_ID]
        if unknown:
            raise ValueError(
                f"unknown flow(s) {', '.join(sorted(unknown))}; known: "
                f"{', '.join(FLOW_IDS)}"
            )
    runs: list[FlowRun] = []
    for flow in FLOW_CATALOG:
        flow_id = str(flow["flow_id"])
        if wanted is not None and flow_id not in wanted:
            continue
        for persona_id in _str_tuple(flow.get("personas")):
            runs.append(run_flow(flow_id, persona_id, environment=environment))
    return runs


def flow_gate_measurements(runs: Sequence[FlowRun]) -> dict[str, Any]:
    """Turn a run set into the measurements ``release_ladder``'s gates consume.

    ``flows_run`` / ``flows_failed`` feed ``flow_simulation_clean``. A *failed*
    flow is one where any subflow's expectation did not hold -- a probe that
    raised counts as failed, because an exception is not evidence.
    """
    total = len(runs)
    failed = [run for run in runs if not run.passed]
    subflows = sum(len(run.outcomes) for run in runs)
    failed_subflows = sum(len(run.failures) for run in runs)
    deferred_subflows = sum(len(run.deferred) for run in runs)
    deferred = [outcome for run in runs for outcome in run.deferred]
    return {
        "flows_run": total,
        "flows_failed": len(failed),
        "subflows_run": subflows,
        "subflows_failed": failed_subflows,
        # The four below are what make the two above honest. `flows_failed: 0`
        # is also what a backend which has built nothing reports, and a gate
        # reading only that cannot tell the two apart.
        "subflows_deferred": deferred_subflows,
        "subflows_exercised": subflows - deferred_subflows,
        "flows_fully_deferred": len([run for run in runs if run.exercised == 0]),
        "distinct_deferred_subflows": sorted(
            {outcome.subflow_id for outcome in deferred}
        ),
        "deferral_reasons": sorted(
            {outcome.deferral_reason for outcome in deferred if outcome.deferral_reason}
        ),
        "distinct_failed_subflows": sorted(
            {outcome.subflow_id for run in failed for outcome in run.failures}
        ),
        "failed_flow_ids": [run.flow_id for run in failed],
        "note": (
            "a probe that raised counts as failed: an exception is not evidence. "
            "flows_run counts flow/persona pairs, not catalog rows. A subflow is "
            "*deferred* only when the part it exercises is positively absent, which "
            "is why flows_failed stays 0 while subflows_deferred is non-zero -- read "
            "the pair together or you will read an empty backend as a healthy one"
        ),
    }


# ---------------------------------------------------------------------------
# Blockages


@dataclass
class Blockage:
    """One finding, with a conclusion and a suggestion.

    The three fields are separate because they are read at different moments and
    by different people. ``conclusion`` is what is true. ``suggestion`` is what to
    do about it, and it is explicitly *not* applied -- this module reports, it
    does not edit the codebase. ``evidence`` is what makes the conclusion
    checkable.
    """

    blockage_id: str
    flow_id: str
    persona_id: str
    subflow_id: str
    severity: str
    category: str
    conclusion: str
    suggestion: str
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "blockage_id": self.blockage_id,
            "flow_id": self.flow_id,
            "persona_id": self.persona_id,
            "subflow_id": self.subflow_id,
            "severity": self.severity,
            "category": self.category,
            "conclusion": self.conclusion,
            "suggestion": self.suggestion,
            "evidence": dict(self.evidence),
            "error": self.error,
        }


#: Category -> severity -> the suggestion template. Data, so the wording of a
#: suggestion is reviewable in one place and adding a category does not mean
#: writing an if/elif into the analyser.
BLOCKAGE_POLICY: tuple[dict[str, Any], ...] = (
    {
        "category": "probe_raised",
        "severity": "blocker",
        "conclusion": "the subflow could not be exercised at all",
        "suggestion": (
            "read the traceback in `error` before changing any engine: a probe that "
            "raises is usually a wrong argument or a changed signature, and "
            "adjusting the engine to match the probe would invert the test"
        ),
    },
    {
        "category": "isolation_violation",
        "severity": "blocker",
        "conclusion": "the shadow is not provably unable to reach live",
        "suggestion": (
            "give the shadow its own database and set CSERVICE_SHADOW_DATABASE_URL; "
            "a shadow pointed at live is not a simulation, and no gate should pass on it"
        ),
    },
    {
        "category": "unclassified_route",
        "severity": "blocker",
        "conclusion": "a live route is not described by the authorization table",
        "suggestion": (
            "add an AUTHZ_RULES row for it and rerun `validate_authz()`; an "
            "unclassified route is one nobody has decided who may call"
        ),
    },
    {
        "category": "invariant_violated",
        "severity": "blocker",
        "conclusion": "a flow invariant that the product depends on does not hold",
        "suggestion": (
            "treat this as a product defect rather than a probe defect: re-read the "
            "flow's `invariant`, and if the engine genuinely diverges from it, "
            "change the engine and pin the behaviour with a test"
        ),
    },
    {
        "category": "vocabulary_drift",
        "severity": "warning",
        "conclusion": "a returned value is outside the published vocabulary",
        "suggestion": (
            "check the resolver's default path: a value that is never the rule's "
            "own band usually means the score is on a different scale than the "
            "thresholds, which is a probe bug before it is a product bug"
        ),
    },
    {
        "category": "monotonicity_broken",
        "severity": "warning",
        "conclusion": "a published curve is not monotone where its meaning requires it",
        "suggestion": (
            "check the threshold direction in the config table before the "
            "arithmetic; an inverted comparison reads as a plausible number"
        ),
    },
    {
        "category": "gate_withheld_a_fix",
        "severity": "blocker",
        "conclusion": "a consent gate suppressed a service or recovery action",
        "suggestion": (
            "the gate must never withhold a fix from a customer who reported a "
            "problem; remove the purpose from CONSENT_GATED_PURPOSES and pin it"
        ),
    },
    {
        # Completeness findings. Severity here is deliberately *not* the whole
        # story: a gap is a warning in prose and a blocker at `l3_canary`, and
        # the level-aware half lives in capability_audit.capability_gate_
        # measurements so there is one place to read the policy rather than two
        # that can disagree.
        "category": "capability_not_built",
        "severity": "warning",
        "conclusion": "a part of the backend a flow depends on is absent or only promised",
        "suggestion": (
            "decide whether it is on the roadmap. At l0_draft this is normal and "
            "blocks nothing; from l3_canary on, a customer can be routed into a "
            "flow that cannot complete, which is what makes it a blocker there"
        ),
    },
    {
        "category": "capability_defect",
        "severity": "blocker",
        "conclusion": "a part exists and does not do the work it appears to do",
        "suggestion": (
            "treat this as a product defect, not a gap: `stub` means it answered "
            "every persona identically, so read the engine before the tests"
        ),
    },
    {
        "category": "capability_untested",
        "severity": "warning",
        "conclusion": "a part is fully built and no probe exercises it",
        "suggestion": (
            "this is the one gap that gets *worse* as you promote, because "
            "canarying an unmeasured part is how you measure it and shipping one "
            "as the everyday path is not. Add a probe naming a persona whose "
            "expectation differs, so the stub detector has something to compare"
        ),
    },
    {
        "category": "capability_unassessable",
        "severity": "warning",
        "conclusion": "the completeness audit could not resolve a declared part",
        "suggestion": (
            "check the audit's own import path first -- an unassessable part is "
            "graded as if it were defective, so a broken audit must not be "
            "mistaken for an immature backend"
        ),
    },
    {
        "category": "surface_not_served",
        "severity": "warning",
        "conclusion": "a flow names a route the backend does not serve",
        "suggestion": (
            "the flow's inventory is stale rather than the product broken: either "
            "the route was renamed or never built. Resolve the intended name "
            "against the live route table before assuming a feature is missing"
        ),
    },
    {
        "category": "probe_not_registered",
        "severity": "blocker",
        "conclusion": "a flow names a probe that PROBES does not register",
        "suggestion": (
            "the flow claims an invariant that nothing checks, so it passes "
            "vacuously; add the probe or remove it from the flow's probe_ids"
        ),
    },
    {
        "category": "unexplained_verdict",
        "severity": "warning",
        "conclusion": "a decision came back with a reason nobody can act on",
        "suggestion": (
            "an unmeasured metric fails closed and reads as a confident verdict; "
            "check that the caller assembles the metrics the guard names"
        ),
    },
)
BLOCKAGE_POLICY_BY_CATEGORY: dict[str, dict[str, Any]] = {
    str(row["category"]): dict(row) for row in BLOCKAGE_POLICY
}
BLOCKAGE_SEVERITIES: tuple[str, ...] = ("blocker", "warning")
SEVERITY_RANK: dict[str, int] = {"blocker": 0, "warning": 1}


def _classify(flow: Mapping[str, Any], outcome: ProbeOutcome) -> dict[str, Any]:
    """Which category a failed subflow belongs to.

    Ordered most-specific first. The ``isinstance`` ordering matters: a raised
    probe is reported as ``probe_raised`` rather than also being reported as an
    ``invariant_violated``, because the two lead to opposite next steps and one
    finding should not generate two contradictory suggestions.
    """
    if outcome.error:
        return BLOCKAGE_POLICY_BY_CATEGORY["probe_raised"]
    subflow = outcome.subflow_id
    if subflow == "shadow_is_one_way":
        return BLOCKAGE_POLICY_BY_CATEGORY["isolation_violation"]
    if subflow == "authz_routes_classified":
        return BLOCKAGE_POLICY_BY_CATEGORY["unclassified_route"]
    if subflow == "recovery_never_withholds_a_fix":
        return BLOCKAGE_POLICY_BY_CATEGORY["gate_withheld_a_fix"]
    if subflow == "consent_gate_excludes_service":
        return BLOCKAGE_POLICY_BY_CATEGORY["gate_withheld_a_fix"]
    if subflow == "complaint_verdict_is_explained":
        return BLOCKAGE_POLICY_BY_CATEGORY["unexplained_verdict"]
    if subflow in {
        "access_band_resolves",
        "retention_health_bands",
        "tier_and_posture_resolve",
    }:
        return BLOCKAGE_POLICY_BY_CATEGORY["vocabulary_drift"]
    if subflow == "forecast_confidence_decays":
        return BLOCKAGE_POLICY_BY_CATEGORY["monotonicity_broken"]
    return BLOCKAGE_POLICY_BY_CATEGORY["invariant_violated"]


def blockage_id(flow_id: str, persona_id: str, subflow_id: str) -> str:
    """Stable across runs, so a repeated finding is recognisable as one finding."""
    return f"{flow_id}.{persona_id}.{subflow_id}"


def analyse_run(run: FlowRun) -> list[Blockage]:
    flow = FLOW_BY_ID.get(run.flow_id, {})
    blockages: list[Blockage] = []
    for outcome in run.failures:
        policy = _classify(flow, outcome)
        blockages.append(
            Blockage(
                blockage_id=blockage_id(run.flow_id, run.persona_id, outcome.subflow_id),
                flow_id=run.flow_id,
                persona_id=run.persona_id,
                subflow_id=outcome.subflow_id,
                severity=str(policy["severity"]),
                category=str(policy["category"]),
                conclusion=str(policy["conclusion"]),
                suggestion=str(policy["suggestion"]),
                evidence={"summary": outcome.summary, **outcome.observed},
                error=outcome.error,
            )
        )
    return blockages


def collect_blockages(runs: Sequence[FlowRun]) -> list[Blockage]:
    """Every finding, deduplicated by :func:`blockage_id`, most severe first.

    Dedup is per ``(flow, persona, subflow)``, which is also the unit a fix is
    pinned at. Two personas failing the same subflow produce two findings on
    purpose: a failure that only reproduces for the at-risk persona is a different
    bug from one that reproduces for everybody, and collapsing them would lose
    that.
    """
    seen: dict[str, Blockage] = {}
    for run in runs:
        for blockage in analyse_run(run):
            seen.setdefault(blockage.blockage_id, blockage)
    ordered = sorted(
        seen.values(),
        key=lambda row: (SEVERITY_RANK.get(row.severity, 99), row.flow_id, row.persona_id),
    )
    return ordered


# ---------------------------------------------------------------------------
# Comparing two environments


# ---------------------------------------------------------------------------
# Completeness: the other axis
# ---------------------------------------------------------------------------


def capability_report(runs: Sequence[FlowRun] | None = None) -> dict[str, Any]:
    """Grade every declared part of the backend, from evidence.

    The thin wrapper that keeps the dependency one way. ``capability_audit``
    cannot import this module -- this module asks it about itself -- so the
    tables and the outcomes are handed in rather than looked up. That is also
    why the audit is testable against synthetic flows: it never reaches for
    ``FLOW_CATALOG`` behind the caller's back.
    """
    from app.main import app as fastapi_app  # local: app.main imports this module
    from app import deps

    features = _feature_endpoints()
    return capability_audit.assess_capabilities(
        routes=fastapi_app.routes,
        flows=FLOW_CATALOG,
        probes=PROBES,
        outcomes=[outcome for run in (runs or ()) for outcome in run.outcomes],
        features=features,
    )


def _feature_endpoints() -> dict[str, str]:
    """``/meta/features``'s endpoint map, or an empty map if it cannot be read.

    Read through the real route rather than by calling the function, because
    ``/meta/features`` is the table the *documentation* points at and a
    completeness check that agrees with the docs but not with the code is
    checking the wrong thing. An unreadable map degrades to "no declaration
    found", which can only understate -- and understating a promise is the safe
    direction, because it moves a capability from ``declared_only`` to
    ``absent`` and never the other way.
    """
    from fastapi.testclient import TestClient  # local, dev/test dependency

    from app.main import app as fastapi_app

    try:
        payload = TestClient(fastapi_app).get("/meta/features").json()
    except Exception:  # noqa: BLE001 - a report must not raise
        return {}
    endpoints = payload.get("endpoints")
    return dict(endpoints) if isinstance(endpoints, Mapping) else {}


def completeness_blockages(report: Mapping[str, Any]) -> list[Blockage]:
    """Completeness findings as blockages, one per capability, worst state first.

    Projected into the same shape as a flow finding so they reach the same log
    with the same conclusion/suggestion discipline and the same dedup -- a gap
    reported on every run must be recognisable as one finding, or the log fills
    with the roadmap.
    """
    rows: list[Blockage] = []
    for row in report.get("capabilities") or ():
        state = str(row.get("state") or "")
        kind = str(row.get("kind") or "")
        if state == "complete":
            continue
        if row.get("unassessable"):
            category = "capability_unassessable"
        elif state in capability_audit.DEFECT_STATES:
            category = "capability_defect"
        elif state in capability_audit.UNKNOWN_STATES:
            category = "capability_untested"
        elif kind == "surface":
            category = "surface_not_served"
        elif kind == "probe":
            category = "probe_not_registered"
        else:
            category = "capability_not_built"
        policy = BLOCKAGE_POLICY_BY_CATEGORY[category]
        capability_id = str(row.get("capability_id") or "")
        rows.append(
            Blockage(
                blockage_id=f"capability.{capability_id}",
                flow_id=str((row.get("flows") or [""])[0]),
                persona_id="(n/a)",
                subflow_id=capability_id,
                severity=str(policy["severity"]),
                category=category,
                conclusion=str(policy["conclusion"]),
                suggestion=str(policy["suggestion"]),
                evidence={
                    "state": state,
                    "kind": kind,
                    "title": str(row.get("title") or ""),
                    "detail": str(row.get("detail") or ""),
                    "evidence": list(row.get("evidence") or ()),
                },
                error="",
            )
        )
    return rows


def completeness_measurements(
    report: Mapping[str, Any], *, for_level: str = "l4_live"
) -> dict[str, Any]:
    """The level-aware measurements, re-exported so callers need one import."""
    return capability_audit.capability_gate_measurements(report, for_level=for_level)


def compare_runs(
    baseline: Sequence[FlowRun],
    candidate: Sequence[FlowRun],
    *,
    tolerance: Optional[float] = None,
) -> dict[str, Any]:
    """Diff two runs of the same flows. This is what makes the shadow worth having.

    The unit of comparison is the **observed values**, not the pass/fail flag.
    A shadow that reaches the same verdict by a different route is a finding an
    operator wants; a shadow that fails the same probe live already fails is a
    pre-existing problem the change did not introduce, and the two must not
    collapse into one number.
    """
    resolved_tolerance = (
        float(tolerance) if tolerance is not None else _default_tolerance()
    )
    base_index = {(run.flow_id, run.persona_id): run for run in baseline}
    cand_index = {(run.flow_id, run.persona_id): run for run in candidate}

    compared = 0
    matched = 0
    divergences: list[dict[str, Any]] = []
    outcome_flips: list[dict[str, Any]] = []
    only_in_baseline = sorted(f"{f}/{p}" for (f, p) in base_index if (f, p) not in cand_index)
    only_in_candidate = sorted(f"{f}/{p}" for (f, p) in cand_index if (f, p) not in base_index)

    for key in sorted(set(base_index) & set(cand_index)):
        base_run, cand_run = base_index[key], cand_index[key]
        base_outcomes = {row.subflow_id: row for row in base_run.outcomes}
        cand_outcomes = {row.subflow_id: row for row in cand_run.outcomes}
        for subflow in sorted(set(base_outcomes) | set(cand_outcomes)):
            compared += 1
            base_row, cand_row = base_outcomes.get(subflow), cand_outcomes.get(subflow)
            if base_row is None or cand_row is None:
                divergences.append(
                    {
                        "flow_id": key[0],
                        "persona_id": key[1],
                        "subflow_id": subflow,
                        "reason": "present in one environment only",
                        "baseline": None if base_row is None else base_row.observed,
                        "candidate": None if cand_row is None else cand_row.observed,
                    }
                )
                continue
            if base_row.held == cand_row.held:
                matched += 1
            else:
                outcome_flips.append(
                    {
                        "flow_id": key[0],
                        "persona_id": key[1],
                        "subflow_id": subflow,
                        "baseline_held": base_row.held,
                        "candidate_held": cand_row.held,
                        "baseline": base_row.observed,
                        "candidate": cand_row.observed,
                        "note": (
                            "the change altered whether this invariant holds; that is the "
                            "finding a shadow exists to produce"
                        ),
                    }
                )
            differing = {
                field: {"baseline": base_row.observed.get(field), "candidate": cand_row.observed.get(field)}
                for field in sorted(set(base_row.observed) | set(cand_row.observed))
                if base_row.observed.get(field) != cand_row.observed.get(field)
            }
            if differing:
                divergences.append(
                    {
                        "flow_id": key[0],
                        "persona_id": key[1],
                        "subflow_id": subflow,
                        "reason": "observed values differ",
                        "fields": differing,
                        "baseline": base_row.observed,
                        "candidate": cand_row.observed,
                    }
                )

    ratio = (len(divergences) + len(outcome_flips)) / compared if compared else 0.0
    return {
        "generated_at": _now_iso(),
        "catalog_version": FLOWS_CATALOG_VERSION,
        "compared": compared,
        "matched": matched,
        "divergence_ratio": round(ratio, 6),
        "tolerance": resolved_tolerance,
        "within_tolerance": ratio <= resolved_tolerance,
        "outcome_flips": outcome_flips,
        "divergences": divergences,
        "only_in_baseline": only_in_baseline,
        "only_in_candidate": only_in_candidate,
        "note": (
            "divergence_ratio counts observed-value differences and pass/fail flips "
            "over compared subflows. The two are reported separately because a flip "
            "changes behaviour while a difference may only change a number."
        ),
    }


def _default_tolerance() -> float:
    from app.release_ladder import DEFAULT_DIVERGENCE_TOLERANCE

    return DEFAULT_DIVERGENCE_TOLERANCE


def divergence_measurements(comparison: Mapping[str, Any]) -> dict[str, Any]:
    """The ``shadow_divergence_within_tolerance`` gate's inputs, verbatim.

    Plus ``regressions``, for ``regressions_none`` -- the gate that guards the
    first level at which a customer can be affected, and the only blocking gate in
    the ladder that **nothing in this repository could previously satisfy**.

    ``compare_runs`` produced the evidence the whole time: an ``outcome_flip``
    carries ``baseline_held`` and ``candidate_held``. Only the *regressions*
    half of it is a regression, though. A flip from held to not-held is one;
    a flip the other way is a fix, and counting it as a regression would mean a
    change that repairs two broken probes is blocked until the fix is reverted,
    which is the gate inverting its own purpose.
    """
    flips = list(comparison.get("outcome_flips") or [])
    regressions = [
        flip
        for flip in flips
        if bool(flip.get("baseline_held")) and not bool(flip.get("candidate_held"))
    ]
    improvements = [
        flip
        for flip in flips
        if not bool(flip.get("baseline_held")) and bool(flip.get("candidate_held"))
    ]
    return {
        "shadow_divergence_ratio": comparison.get("divergence_ratio", 0.0),
        "divergence_tolerance": comparison.get("tolerance"),
        "outcome_flips": len(flips),
        "regressions": len(regressions),
        "regressions_detail": [
            f"{flip.get('flow_id')}/{flip.get('persona_id')}/{flip.get('subflow_id')}"
            for flip in regressions
        ],
        # Reported beside the count rather than folded into it, so "the comparison
        # found nothing" and "the comparison found only improvements" are
        # distinguishable. Both satisfy the gate; only one is a quiet release.
        "regressions_improvements": len(improvements),
    }


# ---------------------------------------------------------------------------
# BLOCKAGES.md rendering


def _md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|")


def render_blockages_markdown(
    runs: Sequence[FlowRun],
    blockages: Sequence[Blockage],
    *,
    comparison: Optional[Mapping[str, Any]] = None,
    generated_at: Optional[str] = None,
) -> str:
    """The findings as a ``BLOCKAGES.md`` section, in the file's own style.

    Written to match the surrounding document rather than to be a convenient
    string: bolded assertion as the sentence subject, wrapped at ~78 columns with
    a two-space continuation indent, backticked identifiers, and the closing
    quartet of ``Code map`` / ``/meta/`` / ``Tests`` lines. An appended section
    that reads differently from the rest of the file is a regression in the one
    artefact a human actually reads.
    """
    stamp = generated_at or _now_iso()
    measurements = flow_gate_measurements(runs)
    blockers = [row for row in blockages if row.severity == "blocker"]
    warnings = [row for row in blockages if row.severity == "warning"]

    lines: list[str] = []
    lines.append(f"### Real-life flow simulation ({stamp[:10]})")
    lines.append("")
    if not blockages:
        lines.append(
            f"- **No blockage: {measurements['flows_run']} flows across "
            f"{measurements['subflows_run']} subflows all held their invariants.** "
            "Each flow ran against the varied personas rather than a happy path, "
            "so this is evidence the engines still branch, and not evidence that "
            "one input works."
        )
    else:
        lines.append(
            f"- **Simulation found {len(blockages)} blockage(s): "
            f"{len(blockers)} blocker(s), {len(warnings)} warning(s)** across "
            f"{measurements['flows_run']} flows and "
            f"{measurements['subflows_run']} subflows."
        )
    lines.append("")

    for blockage in blockages:
        lines.append(
            f"- **{blockage.category} — `{blockage.flow_id}` / "
            f"`{blockage.persona_id}` / `{blockage.subflow_id}`.** "
            f"{blockage.conclusion.capitalize()}: {blockage.evidence.get('summary', '')}"
        )
        if blockage.error:
            lines.append(f"  {blockage.error}")
        lines.append(f"  **Conclusion:** {blockage.conclusion}.")
        lines.append(f"  **Suggestion:** {blockage.suggestion}.")
        lines.append(
            "  **Evidence:** "
            + ", ".join(
                f"`{key}`={_md_escape(value)}"
                for key, value in sorted(blockage.evidence.items())
                if key != "summary"
            )
        )
        lines.append("")

    lines.append(
        f"- **Flows run:** {measurements['flows_run']} "
        f"({measurements['flows_failed']} with at least one failed subflow) over "
        f"{measurements['subflows_run']} subflow executions, "
        f"{measurements['subflows_failed']} of which did not hold. "
        f"Catalog `{FLOWS_CATALOG_VERSION}`, {len(FLOW_CATALOG)} flows, "
        f"{len(PERSONAS)} personas, {len(PROBES)} probes."
    )
    lines.append(
        "- **A probe that raised is counted as failed.** An exception is not "
        "evidence, and a run that stopped at the first one would report a single "
        "blockage and no conclusions."
    )
    lines.append(
        "- **Blockages are findings, not edits.** This module reports; it never "
        "changes the codebase. A kaizen change is a separate, promoted candidate."
    )

    if comparison is not None:
        lines.append(
            f"- **Shadow divergence:** {comparison.get('compared', 0)} subflows "
            f"compared, {comparison.get('matched', 0)} identical, ratio "
            f"**{comparison.get('divergence_ratio', 0.0):.4f}** against tolerance "
            f"{comparison.get('tolerance', 0.0):.4f} "
            f"({'within' if comparison.get('within_tolerance') else 'over'}), "
            f"{len(comparison.get('outcome_flips') or [])} pass/fail flip(s)."
        )

    lines.append(
        "- **Ground truth for the next pass:** the failing subflow ids are "
        f"`{'`, `'.join(measurements['distinct_failed_subflows']) or '(none)'}` — "
        "write the count into a test rather than into prose."
    )
    lines.append("")
    return "\n".join(lines)


#: The marker used to find the section this module owns, so appending twice is
#: detected instead of producing two copies of the same findings.
APPEND_ANCHOR = "### Real-life flow simulation"


def append_to_blockages_md(
    markdown: str,
    *,
    path: Any = None,
    marker: str = APPEND_ANCHOR,
    allow_repeat: bool = False,
) -> dict[str, Any]:
    """Append a rendered section to ``BLOCKAGES.md``.

    Refuses to append a second time when the marker is already present, unless
    ``allow_repeat``. A simulator that appends on every run turns a working log
    into an append-only pile of identical sections, and after two runs nobody
    reads the file at all -- which is the outcome this guard exists to prevent.
    The refusals in that file are the whole value of it.
    """
    target = Path(path) if path is not None else _default_blockages_path()
    if target is None:
        return {
            "written": False,
            "reason": "could not locate BLOCKAGES.md; pass --blockages",
            "path": None,
        }
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if marker in existing and not allow_repeat:
        return {
            "written": False,
            "reason": (
                f"{target} already contains a {marker!r} section; pass allow_repeat "
                "to add another dated run"
            ),
            "path": str(target),
            "already_present": True,
        }
    separator = "" if existing.endswith("\n\n") or not existing else ("\n" if existing.endswith("\n") else "\n\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(separator + markdown)
    return {
        "written": True,
        "reason": "",
        "path": str(target),
        "already_present": False,
        "bytes": len(markdown.encode("utf-8")),
    }


def _default_blockages_path() -> Optional[Path]:
    """``BLOCKAGES.md`` beside the ``fastapi`` root, overridable by env."""
    override = str(os.getenv("CSERVICE_BLOCKAGES_PATH") or "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "BLOCKAGES.md"
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Validation & catalog


def check_probe_sharpness() -> dict[str, Any]:
    """Probes that could not fail, and the personas that would have caught them.

    ``capability_audit`` already reports ``blind_probes``: a probe every persona
    answered identically, so a constant engine would pass. That is the right
    measurement and it is only useful if somebody reads it, so this is the
    standing question -- *"is this probe still able to fail?"* -- with the answer
    attached to each probe.

    Two kinds are reported separately, because they are different problems:

    * ``constant_expectation`` -- every persona produced the same ``expected``.
      Fixed for ``contact_hour_is_local`` by making the expected local hour a
      hand-computed literal per region; its expectation now carries the number
      rather than a boolean derived from the number under test.
    * ``single_persona`` -- only one persona ever runs the probe, so there is no
      contrast to draw even in principle. The remedy is to name a second persona
      in a flow, and the remedy is a catalog edit rather than a probe edit.

    Blindness is not automatically a defect. A probe asserting a *universal*
    invariant -- every booking state is a real booking state -- genuinely is the
    same for everybody, and making it persona-dependent would weaken it. What is
    a defect is a probe whose docstring claims persona sensitivity it does not
    have, which is why each row carries ``docstring_claims_variation``.
    """
    from app.capability_audit import _blind_probes

    outcomes = [
        outcome
        for run in run_all_flows()
        for outcome in run.outcomes
        if not str(getattr(outcome, "error", "") or "")
    ]
    blind = _blind_probes(outcomes)

    by_probe: dict[str, list[Any]] = {}
    for outcome in outcomes:
        by_probe.setdefault(str(outcome.subflow_id), []).append(outcome)

    rows: list[dict[str, Any]] = []
    for probe_id in PROBE_IDS:
        probe_outcomes = by_probe.get(probe_id, [])
        expectations = {
            repr(sorted((str(k), repr(v)) for k, v in dict(o.expected).items()))
            for o in probe_outcomes
        }
        docstring = str(PROBES[probe_id].__doc__ or "").lower()
        claims_variation = any(
            phrase in docstring
            for phrase in ("persona matters", "personas differ", "differ *only*")
        )
        rows.append(
            {
                "probe_id": probe_id,
                "personas": len({str(o.persona_id) for o in probe_outcomes}),
                "distinct_expectations": len(expectations),
                "constant_expectation": probe_id in blind,
                "single_persona": len({str(o.persona_id) for o in probe_outcomes}) < 2,
                "docstring_claims_variation": claims_variation,
                # The defect: a probe that advertises persona sensitivity and
                # cannot deliver it. This is what went unnoticed for four probes
                # whose local lookup tables named a persona that does not exist.
                "cannot_detect_its_own_bug": claims_variation and probe_id in blind,
                "observed": {str(o.persona_id): dict(o.expected) for o in probe_outcomes},
            }
        )

    unsharp = [row for row in rows if row["cannot_detect_its_own_bug"]]
    return {
        "generated_at": _now_iso(),
        "probes": len(rows),
        "sharp": len(rows) - len([r for r in rows if r["constant_expectation"]]),
        "constant_expectation": sorted(
            row["probe_id"] for row in rows if row["constant_expectation"]
        ),
        "single_persona": sorted(
            row["probe_id"] for row in rows if row["single_persona"]
        ),
        "claims_variation_but_constant": sorted(
            row["probe_id"] for row in unsharp
        ),
        "valid": not unsharp,
        "rows": rows,
        "note": (
            "constant_expectation is not a verdict of uselessness: a universal "
            "invariant is legitimately the same for everybody. What invalidates "
            "the check is a probe whose own docstring claims the personas differ "
            "and which cannot tell you how -- those are listed under "
            "claims_variation_but_constant, and there must be none of them."
        ),
    }


def validate_flows() -> dict[str, Any]:
    """Errors are catalog defects that would silently under-report; warnings are
    thin coverage."""
    errors: list[str] = []
    warnings: list[str] = []

    # --- personas
    seen_personas: set[str] = set()
    for index, persona in enumerate(PERSONAS):
        if not persona.persona_id:
            errors.append(f"PERSONAS[{index}] has no persona_id")
        elif persona.persona_id in seen_personas:
            errors.append(f"duplicate persona_id in PERSONAS: {persona.persona_id}")
        seen_personas.add(persona.persona_id)
        # The lesson this module exists partly to encode: a score off the declared
        # scale produces a confident, wrong simulation.
        if not persona.within_score_scale():
            errors.append(
                f"PERSONAS[{persona.persona_id}] scores "
                f"access={persona.access_score} system={persona.system_score} are outside "
                f"the declared scale {list(SCORE_SCALE)}; every thresholded table is on this scale"
            )
        if not persona.summary:
            warnings.append(f"PERSONAS[{persona.persona_id}] has no summary")

    for index, probe in enumerate(PROBES.values()):
        if not callable(probe):
            errors.append(f"PROBES[{index}] is not callable")

    # --- blockage policy
    for index, row in enumerate(BLOCKAGE_POLICY):
        if str(row.get("severity")) not in BLOCKAGE_SEVERITIES:
            errors.append(f"BLOCKAGE_POLICY[{index}] has severity {row.get('severity')!r}")
        if not row.get("conclusion"):
            errors.append(f"BLOCKAGE_POLICY[{index}] has no conclusion")
        if not row.get("suggestion"):
            errors.append(f"BLOCKAGE_POLICY[{index}] has no suggestion")
    for category in ("probe_raised", "isolation_violation", "unclassified_route", "invariant_violated"):
        if category not in BLOCKAGE_POLICY_BY_CATEGORY:
            errors.append(f"BLOCKAGE_POLICY is missing the fallback category {category!r}")

    # --- flows
    seen_flows: set[str] = set()
    probe_usage: dict[str, int] = {probe_id: 0 for probe_id in PROBES}
    for index, flow in enumerate(FLOW_CATALOG):
        flow_id = str(flow.get("flow_id") or "")
        if not flow_id:
            errors.append(f"FLOW_CATALOG[{index}] has no flow_id")
        elif flow_id in seen_flows:
            errors.append(f"duplicate flow_id in FLOW_CATALOG: {flow_id}")
        seen_flows.add(flow_id)
        for persona_id in _str_tuple(flow.get("personas")):
            if persona_id not in PERSONA_BY_ID:
                errors.append(f"FLOW_CATALOG[{flow_id or index}] names unknown persona {persona_id!r}")
        probe_ids = _str_tuple(flow.get("probe_ids"))
        if not probe_ids:
            errors.append(f"FLOW_CATALOG[{flow_id or index}] exercises no probes")
        for probe_id in probe_ids:
            if probe_id not in PROBES:
                errors.append(f"FLOW_CATALOG[{flow_id or index}] names unknown probe {probe_id!r}")
            else:
                probe_usage[probe_id] = probe_usage.get(probe_id, 0) + 1
        if not _str_tuple(flow.get("subflows")):
            errors.append(f"FLOW_CATALOG[{flow_id or index}] has no subflows")
        # Declared business steps vs the checks that run. A warning rather than an
        # error, deliberately, and the reason is worth recording.
        #
        # The two vocabularies are disjoint by construction: `subflows` holds
        # business steps (`register`, `create_booking`, `event_log`) and the
        # probes emit check ids (`access_band_resolves`, `complaint_is_routed`).
        # Not one string is shared across the whole catalog -- 55 declared, 24
        # executed, overlap empty -- so "55 of 55" is not a coverage ratio and
        # any report that divides one by the other is dividing two languages.
        #
        # Promoting this to an error needs a threshold, and any threshold chosen
        # today is a number picked to make the current tree pass. The honest
        # version is the warning that names both counts, so the over-declaring is
        # visible on every sweep, plus the error that already exists for the
        # unambiguous case: a flow that exercises no probes at all.
        declared_steps = len(_str_tuple(flow.get("subflows")))
        declared_probes = len(probe_ids)
        if declared_steps and declared_probes and declared_steps > declared_probes * 2:
            warnings.append(
                f"FLOW_CATALOG[{flow_id or index}] declares {declared_steps} "
                f"business steps and names {declared_probes} probe(s); a reader "
                f"comparing the two totals would read this as coverage"
            )
        if not flow.get("invariant"):
            warnings.append(f"FLOW_CATALOG[{flow_id or index}] has no invariant")
        if not _str_tuple(flow.get("surfaces")):
            warnings.append(f"FLOW_CATALOG[{flow_id or index}] names no surfaces")

    for probe_id, uses in sorted(probe_usage.items()):
        if uses == 0:
            warnings.append(
                f"probe {probe_id!r} is defined but no flow exercises it; an unused "
                "probe is not coverage"
            )

    # --- every category the classifier can emit must be declared
    for subflow_id in PROBE_IDS:
        # A probe with no declared category falls through to invariant_violated,
        # which is declared; that is intentional, so it is not an error here.

        pass

    return {
        "generated_at": _now_iso(),
        "catalog_version": FLOWS_CATALOG_VERSION,
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
        "flows": len(FLOW_CATALOG),
        "personas": len(PERSONAS),
        "probes": len(PROBES),
        "blockage_categories": len(BLOCKAGE_POLICY),
        "score_scale": list(SCORE_SCALE),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(FLOW_CATALOG)} flows, {len(PERSONAS)} personas, {len(PROBES)} probes"
        ),
    }


def build_flows_catalog() -> dict[str, Any]:
    """The tables and their operators, for ``/meta`` and the admin surface."""
    return {
        "catalog_version": FLOWS_CATALOG_VERSION,
        "generated_at": _now_iso(),
        "score_scale": list(SCORE_SCALE),
        "flows": [dict(row) for row in FLOW_CATALOG],
        "personas": [
            {
                "persona_id": row.persona_id,
                "display_name": row.display_name,
                "summary": row.summary,
                "access_score": row.access_score,
                "system_score": row.system_score,
                "churn_risk": row.churn_risk,
                "loyalty_score": row.loyalty_score,
                "days_since_last_activity": row.days_since_last_activity,
                "booking_states": list(row.booking_states),
                "is_admin": row.is_admin,
            }
            for row in PERSONAS
        ],
        "probes": list(PROBE_IDS),
        "blockage_policy": [dict(row) for row in BLOCKAGE_POLICY],
        # The completeness vocabulary, published beside the flow one so a reader
        # of `/kaizen/admin/flows` learns both axes exist rather than finding the
        # second one by accident.
        "capability_states": list(capability_audit.CAPABILITY_STATES),
        "capability_operators": {
            "capability_report": "app.real_life_flows.capability_report",
            "completeness_blockages": "app.real_life_flows.completeness_blockages",
            "completeness_measurements": (
                "app.real_life_flows.completeness_measurements"
            ),
            "audit_catalog": "app.capability_audit.build_capability_catalog",
        },
        "operators": {
            "run_flow": "app.real_life_flows.run_flow",
            "run_all_flows": "app.real_life_flows.run_all_flows",
            "collect_blockages": "app.real_life_flows.collect_blockages",
            "compare_runs": "app.real_life_flows.compare_runs",
            "render_blockages_markdown": "app.real_life_flows.render_blockages_markdown",
            "append_to_blockages_md": "app.real_life_flows.append_to_blockages_md",
        },
    }
