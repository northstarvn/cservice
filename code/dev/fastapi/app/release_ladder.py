"""The release ladder: maturity levels, safety gates, and a deployment ledger
whose rollback reaches five events back.

The problem this solves
-----------------------
An improvement applied to the codebase is not deployed when it is written. It is
deployed when somebody is willing to be blamed if it goes wrong. That step needs
three things this module makes explicit, because each is normally a habit rather
than a record:

1. **How far a change has been observed.** A level. Not a branch name -- a level
   with a definition, so "it worked on staging" has a checkable meaning.
2. **Whether it is safe to put in front of customers.** A verdict. Computed from
   named gates, so the answer to "is it safe?" is a list of things that passed
   rather than an opinion.
3. **How to undo it.** A ledger of deployment events, deep enough to be useful.
   The user asked for five, and five is what :data:`ROLLBACK_WINDOW` is.

Three decisions worth arguing with
----------------------------------
**Keep-as-is decision: a candidate carries code *and* data as one versioned
pair.** They are promoted together, gated together, and rolled back together.
Promoting them separately permits the one state that is always a bug: code that
reads a column the database does not have, or a migration applied while the code
that needs it is still a candidate. A `code_version` on its own is not a
deployable thing. Every level transition here moves the pair or moves nothing.

**Keep-as-is decision: rollback does not rewrite the ledger, it appends to it.**
Going live from v7 to v8 and back to v7 produces *three* events, not two. A
rollback that deleted the event it reversed would leave the audit trail claiming
v8 never happened, which is the one thing an audit trail exists to prevent. So
`rollback` records a new event whose `kind` is `rollback` and whose `restores`
names the event it undid -- the sequence 7 -> 8 -> 7 is fully legible, and the
operator who rolled back is named alongside the deployer they reversed.

**Keep-as-is decision: a gate that could not be evaluated fails.** Same rule as
``consumption_strategy``: an unmeasured safety gate is not a passing safety gate.
`GATE_OPS["unmeasured_gate_verdict"]` is `"fail"``, and the ladder will not
promote a candidate whose gates nobody measured. The alternative -- skip the
gate, note it, promote -- is how a change reaches production having never been
looked at.

What "safe" means, precisely
---------------------------
:func:`evaluate_gates` returns ``safe`` as a single word so the router can branch
on it, but that word is a fold over per-gate results which are always reported.
The fold is `blocking` gates only: an advisory gate that fails produces a
``warnings`` entry and does not make a candidate unsafe. This is the same
severity folding as ``services/complaints.py``, and the reason it matters is
that a ladder where every advisory blocks is a ladder people stop reading.

The five levels
---------------
``l0_draft`` -> ``l1_verified`` -> ``l2_shadow`` -> ``l3_canary`` -> ``l4_live``

Each level's entry requirement is a set of gate ids, so "safe to advance" is
table data. The gates at each level are chosen so that the *cheapest* sufficient
evidence comes first: you do not ask for shadow divergence before the unit suite
is green.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

RELEASE_CATALOG_VERSION = 1


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a config value to a tuple of non-empty strings, never raising."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _candidate_gate_inputs(candidate: Any) -> dict[str, Any]:
    """The facts a gate may read off the candidate rather than a measurement.

    Only the names a gate actually declares, so this cannot quietly widen what a
    gate sees: a gate that checks ``maintainer`` reads ``candidate.maintainer``,
    and a gate that checks ``tests_failed`` still has to be handed that number by
    a run. A gate is never satisfied by a field nobody asked it to read.
    """
    wanted = {
        str(name)
        for gate in PROMOTION_GATES
        for name in _str_tuple(gate.get("checks"))
    }
    return {
        name: getattr(candidate, name)
        for name in sorted(wanted)
        if hasattr(candidate, name) and getattr(candidate, name) is not None
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> Optional[float]:
    """Coerce to a float, or ``None``. Never raises.

    A gate reading a measurement must be able to say "that is not a number" as a
    verdict, which is a different outcome from "that number failed".
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


#: Defaults for the two tolerances, so a caller measuring only the numerator
#: still gets a real comparison instead of an unmeasured verdict. Declared here,
#: above the gates that reference them, because :func:`_apply_rule` falls back to
#: them when the caller measured the ratio but not the threshold.
DEFAULT_DIVERGENCE_TOLERANCE = 0.02
DEFAULT_CANARY_ERROR_THRESHOLD = 0.001

#: How many live deployment events stay reachable by a rollback.
#:
#: The user asked for five, so five it is. Two properties are deliberate and are
#: pinned by tests: it is a *window of events*, not a count of versions (two
#: rollbacks of the same version are two events), and it counts events that are
#: still eligible to be restored -- not the whole history, which grows forever.
ROLLBACK_WINDOW = 5

#: Never let a rollback empty the window. With ``ROLLBACK_WINDOW`` at 5 this
#: guarantees you can always go back one more deployment after you roll back.
MIN_ROLLBACK_TARGETS = 1

GATE_OPS: dict[str, str] = {
    "severity_words": ("advisory", "blocking"),
    "gate_result_words": ("pass", "fail", "unmeasured"),
    "unmeasured_gate_verdict": "fail",
    "fold_rule": "a candidate is safe when no blocking gate failed or went unmeasured",
    "default_blocking_severity": "blocking",
}

#: The ladder. ``level_id`` is the stable key; ``rank`` is the position. A
#: candidate advances one rank at a time -- skipping a level is how a change
#: reaches customers having been shadow-tested for nine minutes.
MATURITY_LEVELS: tuple[dict[str, Any], ...] = (
    {
        "level_id": "l0_draft",
        "rank": 0,
        "serves_traffic": False,
        "entry_gates": (),
        "description": (
            "written but unexamined. Nothing has run it. This is where every "
            "kaizen change starts and where it is safe to be wrong."
        ),
    },
    {
        "level_id": "l1_verified",
        "rank": 1,
        "serves_traffic": False,
        "entry_gates": ("unit_suite_green", "flow_simulation_clean"),
        "description": (
            "the suite is green and the real-life flow simulation ran end to end "
            "against it. This is the first level with evidence behind it."
        ),
    },
    {
        "level_id": "l2_shadow",
        "rank": 2,
        "serves_traffic": False,
        "entry_gates": ("shadow_isolation_clean", "flow_simulation_clean", "data_pipelines_one_way"),
        "description": (
            "running in the shadow environment against replicated live data, with "
            "the one-way guard verified. Nobody is served by this."
        ),
    },
    {
        "level_id": "l3_canary",
        "rank": 3,
        "serves_traffic": True,
        "entry_gates": (
            "shadow_divergence_within_tolerance",
            "rollback_target_available",
            "regressions_none",
            "authorization_complete",
            "backend_completeness_honest",
        ),
        "description": (
            "serving a configured slice of real traffic. The first level where a "
            "customer can be affected, and the last one a rollback is cheap in."
        ),
    },
    {
        "level_id": "l4_live",
        "rank": 4,
        "serves_traffic": True,
        "entry_gates": (
            "canary_error_rate_below_threshold",
            "rollback_target_available",
            "backend_completeness_honest",
        ),
        "description": (
            "serving everyone. Reached by promotion, and the only level a "
            "deployment ledger event is written for by ``deploy``."
        ),
    },
)
MATURITY_LEVEL_BY_ID: dict[str, dict[str, Any]] = {
    str(row["level_id"]): dict(row) for row in MATURITY_LEVELS
}
LEVEL_ORDER: tuple[str, ...] = tuple(str(row["level_id"]) for row in MATURITY_LEVELS)
LEVEL_RANK: dict[str, int] = {
    str(row["level_id"]): int(row["rank"]) for row in MATURITY_LEVELS
}
LIVE_LEVEL = "l4_live"
DRAFT_LEVEL = "l0_draft"

#: Gates, by id. ``checks`` names what has to be measured; the ladder does not
#: compute any of them itself, which is the point -- the ladder decides what
#: evidence is required and whether it arrived, and the measuring belongs to the
#: thing that knows.
PROMOTION_GATES: tuple[dict[str, Any], ...] = (
    {
        "gate_id": "unit_suite_green",
        "severity": "blocking",
        "checks": ("tests_failed",),
        "passes_when": "tests_failed == 0",
        "description": "the unit suite runs with no failures",
    },
    {
        "gate_id": "flow_simulation_clean",
        "severity": "blocking",
        "checks": ("flows_run", "flows_failed"),
        "passes_when": "flows_run > 0 and flows_failed == 0",
        "description": (
            "every real-life flow and subflow simulated without a blockage. A "
            "flow nobody ran is not evidence."
        ),
    },
    {
        "gate_id": "shadow_isolation_clean",
        "severity": "blocking",
        "checks": ("shadow_isolated",),
        "passes_when": "shadow_isolated is true",
        "description": (
            "the shadow's isolation verdict has no blocking failure -- proven by "
            "comparing database identity, not by a config value being set"
        ),
    },
    {
        "gate_id": "data_pipelines_one_way",
        "severity": "blocking",
        "checks": ("pipelines_one_way", "shadow_to_live_writes"),
        "passes_when": "pipelines_one_way and shadow_to_live_writes == 0",
        "description": (
            "every data pipeline that runs in the shadow moves live -> shadow. "
            "The user asked for this to hold for pipelines as well as code, "
            "because a pipeline is the part that writes."
        ),
    },
    {
        "gate_id": "shadow_divergence_within_tolerance",
        "severity": "blocking",
        # Only the ratio is a *measurement*. The tolerance is a policy value, and
        # listing it in `checks` made it required -- which silently made
        # DEFAULT_DIVERGENCE_TOLERANCE unreachable, because a gate is unmeasured
        # until every name in `checks` is supplied. Supply it to tighten the
        # bar for one candidate; omit it and the declared default applies.
        "checks": ("shadow_divergence_ratio",),
        "passes_when": "shadow_divergence_ratio <= divergence_tolerance",
        "description": (
            "the share of shadow decisions that disagree with live is under the "
            "configured tolerance. This is the gate that says the changed code "
            "behaves like the code customers are on."
        ),
    },
    {
        "gate_id": "regressions_none",
        "severity": "blocking",
        "checks": ("regressions",),
        "passes_when": "regressions == 0",
        "description": (
            "no known functional regression. Counted as a number rather than a "
            "boolean so 'one regression' and 'none' cannot both be reported by "
            "two different fields."
        ),
    },
    {
        "gate_id": "canary_error_rate_below_threshold",
        "severity": "blocking",
        # Same split as the divergence gate: the rate is measured, the threshold
        # is policy. A gate that named both made DEFAULT_CANARY_ERROR_THRESHOLD
        # dead code and left `l4_live` unreachable for any caller who did not
        # happen to know to pass the threshold in by hand.
        "checks": ("canary_error_rate",),
        "passes_when": "canary_error_rate <= canary_error_threshold",
        "description": "the canary slice's error rate is under its threshold",
    },
    {
        "gate_id": "rollback_target_available",
        "severity": "blocking",
        "checks": ("rollback_targets",),
        "passes_when": "rollback_targets >= MIN_ROLLBACK_TARGETS",
        "description": (
            f"at least {MIN_ROLLBACK_TARGETS} deployment event remains inside the "
            f"{ROLLBACK_WINDOW}-event rollback window. Deploying with nowhere to go "
            "back to is the one irreversible thing this ladder does."
        ),
    },
    {
        "gate_id": "backend_completeness_honest",
        "severity": "blocking",
        # One comparison, and the level-dependence lives in
        # `capability_audit.capability_gate_measurements` rather than here,
        # because a rule that has to be true for two different rungs at once is
        # a rule nobody can read. What counts as blocking:
        #
        #   * stub / partial    -- every level. A part that exists and lies is
        #                          worse than one honestly absent, at every rung,
        #                          because it consumes the attention of whoever
        #                          is triaging the report.
        #   * absent / declared -- from l3_canary. A canary routes a real person
        #                          through the change; discovering there that a
        #                          flow cannot complete is too late, and in front
        #                          of someone.
        #   * untested          -- only at l4_live. Canarying an unmeasured part
        #                          is exactly how you measure it; shipping one
        #                          as the everyday path is not.
        #
        # So at l0_draft and l1_verified an immature backend passes this gate,
        # which is the whole requirement behind it: "not built yet" is the
        # starting state of this project and must not block the first commit of
        # every feature.
        "checks": ("capability_blocking",),
        "passes_when": "capability_blocking == 0",
        "description": (
            "every declared part of the backend is as built as the rung allows: "
            "nothing stubbed or half-working at any level, nothing merely promised "
            "from l3_canary on, and nothing unexercised at l4_live"
        ),
    },
    {
        "gate_id": "authorization_complete",
        "severity": "blocking",
        # Both halves, because either alone misses the class of hole this gate
        # exists for. `authz_in_sync` is the drift report's own verdict; the
        # unclassified half was already covered by the flow simulation's
        # `authz_routes_classified` probe, and a gate that repeated it would
        # just be a second place to forget to look.
        #
        # `unlisted_public_writes` is the half nothing else checked, and it is
        # here because of a real defect: POST /meta/decisions/canary/promote
        # decided which model scores every customer while `/meta*` classified
        # it public and the route bound no dependency. The drift report called
        # that `match` -- the rule and the code agreed, and both were wrong,
        # because the defect *was* the agreement. A mutating route is public
        # only if somebody wrote it down, and promoting to real traffic is the
        # point at which that has to have been done.
        "checks": ("authz_in_sync", "unlisted_public_writes"),
        "passes_when": "authz_in_sync is true and unlisted_public_writes == 0",
        "description": (
            "every live route is classified, every mutating route that is public "
            "is on the record with a reason, and nothing is in one file and not "
            "the other"
        ),
    },
    {
        "gate_id": "audit_contract_intact",
        "severity": "advisory",
        "checks": ("audit_contracts_intact",),
        "passes_when": "audit_contracts_intact is true",
        "description": (
            "the audit-trail contract survived the change. Advisory because a "
            "changed audit shape is a reason to stop and look, not a reason to "
            "withhold a fix from a customer."
        ),
    },
    {
        "gate_id": "maintainer_named",
        "severity": "advisory",
        "checks": ("maintainer",),
        "passes_when": "maintainer is non-empty",
        "description": "somebody is accountable for this at 3am",
    },
)
PROMOTION_GATE_BY_ID: dict[str, dict[str, Any]] = {
    str(row["gate_id"]): dict(row) for row in PROMOTION_GATES
}
GATE_SEVERITIES: tuple[str, ...] = tuple(str(s) for s in GATE_OPS["severity_words"])
GATE_SEVERITY_RANK: dict[str, int] = {
    "blocking": 0,
    "advisory": 1,
}

#: Which gates each level cannot be entered without. Kept in the level rows
#: above; this is the reverse index, for reporting "what would I have to prove".
LEVEL_ENTRY_GATES: dict[str, tuple[str, ...]] = {
    str(row["level_id"]): tuple(_str_tuple(row.get("entry_gates"))) for row in MATURITY_LEVELS
}
#: Gates that apply to every level's own sanity, not just entry.
_ALWAYS_ON: tuple[str, ...] = ("maintainer_named",)

EVENT_KINDS: tuple[str, ...] = ("deploy", "rollback", "canary_widen", "canary_narrow")
EVENT_KIND_BY_NAME: dict[str, dict[str, Any]] = {
    kind: {
        "kind": kind,
        "writes_live": kind in {"deploy", "canary_widen"},
        "description": {
            "deploy": "a candidate at l4_live was promoted to serving everyone",
            "rollback": "an earlier deployment event was restored; appends, never rewrites",
            "canary_widen": "the canary slice grew without a new deployment",
            "canary_narrow": "the canary slice shrunk after a bad signal",
        }[kind],
    }
    for kind in EVENT_KINDS
}

DEFAULT_LEDGER_CAPACITY = 200


# ---------------------------------------------------------------------------
# Versions


def code_version(
    *,
    commit: str = "",
    label: str = "",
    dirty: bool = False,
    notes: str = "",
) -> str:
    """A stable identity for a body of code.

    ``commit`` is required to be a real value: an empty commit produces a
    version string that no rollback can name, and a ledger full of unnameable
    versions is a ledger with no rollback.
    """
    short = str(commit or "").strip() or "unknown"
    if len(short) > 12:
        short = short[:12]
    suffix = "-dirty" if dirty else ""
    tail = f" ({label})" if label else ""
    return f"code:{short}{suffix}{tail}"


def data_version(*, revision: str = "", label: str = "", notes: str = "") -> str:
    """A stable identity for a schema/data state, from its Alembic revision."""
    rev = str(revision or "").strip() or "unknown"
    tail = f" ({label})" if label else ""
    return f"data:{rev}{tail}"


def code_and_data_pair(
    *, commit: str = "", revision: str = "", label: str = "", dirty: bool = False
) -> dict[str, str]:
    """The pair, as the ladder stores it. Code and data move together or not at all."""
    return {
        "code_version": code_version(commit=commit, label=label, dirty=dirty),
        "data_version": data_version(revision=revision, label=label),
    }


def split_version_pair(pair: Mapping[str, Any]) -> dict[str, str]:
    return {
        "code_version": str(pair.get("code_version") or "code:unknown"),
        "data_version": str(pair.get("data_version") or "data:unknown"),
    }


# ---------------------------------------------------------------------------
# Candidates


@dataclass
class ReleaseCandidate:
    """One unit of work on its way to live, carrying code and data as a pair.

    ``level`` is the *current* level, not a target. Advancing is a separate,
    gated operation (:func:`advance_candidate`), because "how far along is this"
    and "may this go further" are different questions and merging them is how a
    change skips a level.
    """

    candidate_id: str
    code_version: str
    data_version: str
    level: str = DRAFT_LEVEL
    summary: str = ""
    maintainer: str = ""
    kaizen_source: str = ""
    measured: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rank(self) -> int:
        return LEVEL_RANK.get(self.level, 0)

    def pair(self) -> dict[str, str]:
        # `split_version_pair` reads with `.get`, so it needs the mapping form.
        # Passing `self` raised `AttributeError: 'ReleaseCandidate' object has no
        # attribute 'get'` on every call. Pre-existing bug: `pair()` is how the
        # rollback path names the versions it must be able to return to, so a
        # rollback that tried to resolve its own version pair could not. Found
        # while wiring complaint improvement proposals onto the ladder.
        return split_version_pair(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "code_version": self.code_version,
            "data_version": self.data_version,
            "level": self.level,
            "rank": self.rank,
            "serves_traffic": bool(
                MATURITY_LEVEL_BY_ID.get(self.level, {}).get("serves_traffic", False)
            ),
            "summary": self.summary,
            "maintainer": self.maintainer,
            "kaizen_source": self.kaizen_source,
            "measured": dict(self.measured),
            "history": [dict(row) for row in self.history],
        }


# ---------------------------------------------------------------------------
# Gate evaluation


def _gate_result(gate: Mapping[str, Any], measured: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one gate against what was actually measured.

    Three outcomes, not two: ``pass``, ``fail``, ``unmeasured``. ``unmeasured``
    folds to ``fail`` by default because ``GATE_OPS["unmeasured_gate_verdict"]``
    says so, but it is *kept distinct in the report* -- "the gate failed" and
    "nobody measured the thing the gate checks" lead to different conversations,
    and collapsing them is how a team stops trusting a gate list.
    """
    gate_id = str(gate.get("gate_id") or "")
    severity = str(gate.get("severity") or GATE_OPS["default_blocking_severity"])
    required = _str_tuple(gate.get("checks"))
    missing: list[str] = []
    values: dict[str, Any] = {}
    for name in required:
        if name not in measured:
            missing.append(name)
        else:
            values[name] = measured[name]

    if missing:
        return {
            "gate_id": gate_id,
            "severity": severity,
            "result": "unmeasured",
            "passed": False,
            "verdict": "fail",
            "missing": missing,
            "values": values,
            "passes_when": str(gate.get("passes_when") or ""),
            "description": str(gate.get("description") or ""),
        }

    verdict, reason = _apply_rule(gate_id, values, measured)
    return {
        "gate_id": gate_id,
        "severity": severity,
        "result": "pass" if verdict else "fail",
        "passed": verdict,
        "verdict": "pass" if verdict else "fail",
        "missing": [],
        "values": values,
        "reason": reason,
        "passes_when": str(gate.get("passes_when") or ""),
        "description": str(gate.get("description") or ""),
    }


def _apply_rule(
    gate_id: str, values: Mapping[str, Any], measured: Mapping[str, Any]
) -> tuple[bool, str]:
    """The per-gate comparison. Each rule is written out rather than interpreted.

    An expression interpreter for gates would let a gate be *reconfigured* into
    something nobody reviewed, and the whole value of this table is that the
    condition is readable next to the prose saying what it means.
    """
    def _num(name: str) -> Optional[float]:
        return _number(values.get(name))

    if gate_id == "unit_suite_green":
        failed = _num("tests_failed")
        if failed is None:
            return False, "tests_failed is not a number"
        return failed <= 0, f"tests_failed={failed:g}"
    if gate_id == "flow_simulation_clean":
        ran, failed = _num("flows_run"), _num("flows_failed")
        if ran is None or failed is None:
            return False, "flows_run/flows_failed must both be numbers"
        return (ran > 0 and failed <= 0), f"flows_run={ran:g} flows_failed={failed:g}"
    if gate_id == "shadow_isolation_clean":
        ok = bool(values.get("shadow_isolated"))
        return ok, f"shadow_isolated={ok}"
    if gate_id == "data_pipelines_one_way":
        one_way = bool(values.get("pipelines_one_way"))
        writes = _num("shadow_to_live_writes")
        if writes is None:
            return False, "shadow_to_live_writes must be a number"
        return (one_way and writes <= 0), f"pipelines_one_way={one_way} shadow_to_live_writes={writes:g}"
    if gate_id == "shadow_divergence_within_tolerance":
        ratio, tolerance = _num("shadow_divergence_ratio"), _number(measured.get("divergence_tolerance"))
        if tolerance is None:
            tolerance = DEFAULT_DIVERGENCE_TOLERANCE
        if ratio is None or tolerance is None:
            return False, "shadow_divergence_ratio/divergence_tolerance must both be numbers"
        return ratio <= tolerance, f"divergence={ratio:.4f} tolerance={tolerance:.4f}"
    if gate_id == "regressions_none":
        regressions = _num("regressions")
        if regressions is None:
            return False, "regressions must be a number"
        return regressions <= 0, f"regressions={regressions:g}"
    if gate_id == "canary_error_rate_below_threshold":
        rate, threshold = _num("canary_error_rate"), _number(measured.get("canary_error_threshold"))
        if threshold is None:
            threshold = DEFAULT_CANARY_ERROR_THRESHOLD
        if rate is None or threshold is None:
            return False, "canary_error_rate/canary_error_threshold must both be numbers"
        return rate <= threshold, f"rate={rate:.4f} threshold={threshold:.4f}"
    if gate_id == "rollback_target_available":
        targets = _num("rollback_targets")
        if targets is None:
            return False, "rollback_targets must be a number"
        return targets >= MIN_ROLLBACK_TARGETS, f"rollback_targets={targets:g} need>={MIN_ROLLBACK_TARGETS}"
    if gate_id == "backend_completeness_honest":
        blocking = _num("capability_blocking")
        if blocking is None:
            return False, "capability_blocking must be a number"
        detail = measured.get("capability_blocking_detail") or []
        level = str(measured.get("for_level") or "")
        return (
            blocking <= 0,
            f"capability_blocking={blocking:g}"
            + (f" ({', '.join(str(item) for item in detail)})" if detail else "")
            + (f" at {level}" if level else ""),
        )
    if gate_id == "authorization_complete":
        in_sync = bool(values.get("authz_in_sync"))
        unlisted = _num("unlisted_public_writes")
        if unlisted is None:
            return False, "unlisted_public_writes must be a number"
        detail = measured.get("unlisted_public_write_detail") or []
        # Both halves are named in the verdict. `in_sync=False` with zero
        # unlisted writes is not a contradiction to be smoothed over: it is the
        # signature of a mismatch between a rule's declared gate and the
        # dependency a route binds, which is a different defect from a public
        # write nobody wrote down, and it needs a different fix.
        return (
            in_sync and unlisted <= 0,
            f"authz_in_sync={in_sync} unlisted_public_writes={unlisted:g}"
            + (f" ({', '.join(str(item) for item in detail)})" if detail else ""),
        )
    if gate_id == "audit_contract_intact":
        ok = bool(values.get("audit_contracts_intact"))
        return ok, f"audit_contracts_intact={ok}"
    if gate_id == "maintainer_named":
        maintainer = str(values.get("maintainer") or "").strip()
        return bool(maintainer), f"maintainer={maintainer or '(none)'}"
    # An unknown gate_id fails closed, exactly like an unknown severity.
    return False, f"no rule for gate {gate_id!r}; an unknown gate cannot pass"


#: Defaults for the two tolerances live at the top of the module, next to
#: ``_apply_rule``'s other inputs.


def authorization_measurements(routes: Any) -> dict[str, Any]:
    """Measure the ``authorization_complete`` gate from the live route table.

    A ladder that reads every gate's input from a number the caller typed in
    cannot tell a measured fact from an assertion, and the one gate here whose
    subject is the *app itself* is exactly the one where an assertion would have
    hidden a real defect. So this function goes and looks, and takes ``routes``
    rather than importing ``app.main``: ``app.release_ladder`` imports nothing
    from ``app`` at module scope, and ``app.main`` imports this module, so a
    top-level import would be a cycle.

    ``app.deps`` is imported inside the function for the same reason ``deps``
    imports ``app.routers.users`` inside its validator.
    """
    from app import deps  # local: app.deps -> app.db -> ...; no cycle at import time

    report = deps.authz_drift_report(routes)
    public_writes = dict(report.get("public_writes") or {})
    return {
        "authz_in_sync": bool(report.get("in_sync")),
        "unlisted_public_writes": len(public_writes.get("unlisted_public_writes") or []),
        "unclassified_routes": len(report.get("unclassified_routes") or []),
        "mismatched_routes": len(report.get("mismatched") or []),
        "public_write_count": int(public_writes.get("public_write_count") or 0),
        "unlisted_public_write_detail": [
            f"{row['method']} {row['path']}"
            for row in (public_writes.get("unlisted_public_writes") or [])
        ],
    }


def required_gates_for(level: str) -> tuple[str, ...]:
    """The gates a candidate must satisfy to *enter* ``level``.

    Returns ``()`` for the draft level and for an unknown one. An unknown level
    must not be treated as "no gates needed" downstream -- ``advance_candidate``
    refuses unknown levels before it ever asks this.
    """
    row = MATURITY_LEVEL_BY_ID.get(str(level))
    if row is None:
        return ()
    return tuple(_str_tuple(row.get("entry_gates")))


def gates_for_candidate(
    candidate: Any,
    *,
    for_level: str | None = None,
    include_advisories: bool = True,
) -> tuple[str, ...]:
    """The gates a candidate must satisfy to *enter* ``for_level``.

    Defaults to the level the candidate is *moving to*, not the one it is at.

    **That default is the whole point and it was wrong at first.** Reading
    ``required_gates_for(candidate.level)`` checks the gates for the level the
    candidate already occupies -- gates it satisfied on the way in -- so the
    check is a no-op that can only ever fail on evidence already used, and the
    *next* level's gates are never consulted at all. The visible consequence was
    a candidate reaching ``l4_live`` with ``canary_error_rate`` never measured,
    because ``l4_live``'s entry gates were checked one step too late. Passing
    ``for_level`` explicitly keeps the "how is this candidate doing" reading
    available for reporting.
    """
    if for_level is not None:
        base = required_gates_for(for_level)
    else:
        current = getattr(candidate, "level", None) or DRAFT_LEVEL
        forward = next_level(str(current))
        base = required_gates_for(forward) if forward else required_gates_for(current)
    if include_advisories:
        extra = tuple(gate_id for gate_id in _ALWAYS_ON if gate_id not in base)
    else:
        extra = ()
    return tuple(base) + extra


def evaluate_gates(
    candidate: Any,
    *,
    for_level: str | None = None,
    include_advisories: bool = True,
    extra_gate_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Is this candidate safe to advance from where it is?

    Evaluates the gates for the level the candidate would *move to*. See
    :func:`gates_for_candidate` for why the default is the destination rather
    than the current level; pass ``for_level=`` to ask about a specific rung.

    ``safe`` folds the blocking results only, per :data:`GATE_OPS`. The advisory
    results ride along in ``warnings`` so a caller that wants to be strict can
    be strict, and so a reader can see that "safe" did not mean "nothing to say".
    """
    gate_ids = gates_for_candidate(
        candidate, for_level=for_level, include_advisories=include_advisories
    )
    for gate_id in _str_tuple(extra_gate_ids):
        if gate_id not in gate_ids:
            gate_ids = gate_ids + (gate_id,)
    measured = dict(getattr(candidate, "measured", {}) or {})
    # A gate's `checks` name a fact, and a fact can be recorded in two places: as
    # a measurement somebody ran, or as an attribute of the candidate itself.
    # `maintainer_named` is the obvious case -- the candidate carries a
    # `maintainer`, and requiring the same name to *also* appear in `measured`
    # meant a candidate registered by an authenticated admin always produced an
    # advisory warning, because nobody repeats a field the request body already
    # had. Measurements win on a collision: an explicit measurement is a stronger
    # claim about the present than a field set at registration.
    for name, value in _candidate_gate_inputs(candidate).items():
        measured.setdefault(name, value)
    results = [
        _gate_result(PROMOTION_GATE_BY_ID[gate_id], measured)
        for gate_id in gate_ids
        if gate_id in PROMOTION_GATE_BY_ID
    ]
    blocking = [row for row in results if row["severity"] == "blocking"]
    advisories = [row for row in results if row["severity"] == "advisory"]
    blocking_failures = [row["gate_id"] for row in blocking if not row["passed"]]
    advisory_failures = [row["gate_id"] for row in advisories if not row["passed"]]
    current = str(getattr(candidate, "level", DRAFT_LEVEL) or DRAFT_LEVEL)
    target = for_level if for_level is not None else next_level(current)
    return {
        "generated_at": _now_iso(),
        "catalog_version": RELEASE_CATALOG_VERSION,
        "candidate_id": str(getattr(candidate, "candidate_id", "") or ""),
        "level": current,
        "gates_for_level": target or current,
        "next_level": target,
        "safe": not blocking_failures,
        "blocking_failures": blocking_failures,
        "unmeasured_gates": [row["gate_id"] for row in blocking if row["result"] == "unmeasured"],
        "warnings": [
            {
                "gate_id": row["gate_id"],
                "result": row["result"],
                "reason": row.get("reason", ""),
            }
            for row in advisories
            if not row["passed"]
        ],
        "gates": results,
        "blocking_evaluated": len(blocking),
        "advisory_evaluated": len(advisories),
        "note": (
            GATE_OPS["fold_rule"]
            + "; unmeasured folds to "
            + GATE_OPS["unmeasured_gate_verdict"]
            + f"; these are the gates for entering {target or current}"
        ),
    }


def next_level(level: str) -> Optional[str]:
    """The level after ``level``, or ``None`` at the top."""
    rank = LEVEL_RANK.get(str(level))
    if rank is None:
        return None
    for row in MATURITY_LEVELS:
        if int(row["rank"]) == rank + 1:
            return str(row["level_id"])
    return None


def advance_candidate(
    candidate: ReleaseCandidate,
    *,
    include_advisories: bool = True,
    extra_gate_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Move a candidate up exactly one level if its gates pass, else report why.

    Returns a decision, never raises for an unsafe candidate -- refusing to
    advance is the normal outcome here, not an exception. A refused advance is
    *recorded on the candidate's history* rather than discarded, so the sequence
    "tried to promote, was blocked by unmeasured X" is visible afterwards. That
    matters for the next person: without it, a candidate that has been blocked
    forty times looks exactly like one nobody tried.
    """
    if candidate.level not in MATURITY_LEVEL_BY_ID:
        return {
            "advanced": False,
            "candidate_id": candidate.candidate_id,
            "from_level": candidate.level,
            "to_level": None,
            "reason": f"unknown level {candidate.level!r}",
            "gate_report": None,
        }
    target = next_level(candidate.level)
    if target is None:
        report = evaluate_gates(
            candidate, include_advisories=include_advisories, extra_gate_ids=extra_gate_ids
        )
        return {
            "advanced": False,
            "candidate_id": candidate.candidate_id,
            "from_level": candidate.level,
            "to_level": None,
            "reason": f"{candidate.level} is the top of the ladder",
            "gate_report": report,
        }
    report = evaluate_gates(
        candidate, include_advisories=include_advisories, extra_gate_ids=extra_gate_ids
    )
    decision = {
        "advanced": report["safe"],
        "candidate_id": candidate.candidate_id,
        "from_level": candidate.level,
        "to_level": target if report["safe"] else None,
        "reason": (
            "all blocking gates passed"
            if report["safe"]
            else "blocked by " + ", ".join(report["blocking_failures"])
        ),
        "gate_report": report,
    }
    candidate.history.append(
        {
            "at": _now_iso(),
            "action": "advance",
            "from_level": candidate.level,
            "to_level": decision["to_level"],
            "advanced": decision["advanced"],
            "reason": decision["reason"],
        }
    )
    if report["safe"]:
        candidate.level = target
    return decision


# ---------------------------------------------------------------------------
# The deployment ledger


@dataclass
class DeploymentLedger:
    """An append-only record of what was deployed to live, and what undid it.

    Rollback depth is :data:`ROLLBACK_WINDOW` events. The ledger keeps its full
    history for the audit trail and computes the *rollback window* as a view on
    top, so pruning old events (if capacity ever bites) would not silently
    shorten the reach a rollback has -- the window is a property of the policy,
    and the policy is checked against the count of retained events.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    capacity: int = DEFAULT_LEDGER_CAPACITY

    # --- writes

    def record(
        self,
        kind: str,
        *,
        code_version: str,
        data_version: str,
        actor: str = "",
        reason: str = "",
        restores: Optional[str] = None,
        level: str = LIVE_LEVEL,
        candidate_id: str = "",
        extra: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Append an event. The only mutator.

        Refuses an unknown ``kind`` and refuses a ``restores`` that names an
        event not in the ledger -- both raise, because they are caller errors
        about the ledger's own integrity, not a runtime judgement to be reported.
        """
        if kind not in EVENT_KIND_BY_NAME:
            raise ValueError(f"unknown deployment event kind {kind!r}; known: {', '.join(EVENT_KINDS)}")
        if restores is not None and restores not in self.events and restores not in {
            str(row["event_id"]) for row in self.events
        }:
            raise ValueError(f"cannot restore {restores!r}: no such deployment event")
        event = {
            "event_id": f"dep-{len(self.events) + 1:04d}",
            "kind": kind,
            "at": _now_iso(),
            "code_version": code_version,
            "data_version": data_version,
            "level": level,
            "actor": actor,
            "reason": reason,
            "restores": restores,
            "candidate_id": candidate_id,
            "extra": dict(extra or {}),
            "sequence": len(self.events) + 1,
        }
        self.events.append(event)
        return dict(event)

    def deploy(
        self,
        candidate: ReleaseCandidate,
        *,
        actor: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Promote a candidate at :data:`LIVE_LEVEL` into a deployment event."""
        if candidate.level != LIVE_LEVEL:
            raise ValueError(
                f"candidate {candidate.candidate_id} is at {candidate.level}, not {LIVE_LEVEL}; "
                "advance it through the ladder before deploying"
            )
        return self.record(
            "deploy",
            code_version=candidate.code_version,
            data_version=candidate.data_version,
            actor=actor or candidate.maintainer,
            reason=reason or candidate.summary,
            level=candidate.level,
            candidate_id=candidate.candidate_id,
        )

    def rollback(
        self,
        target_event_id: str,
        *,
        actor: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Restore an earlier event's code+data pair, by appending a new event.

        The restored pair is taken whole. Restoring the code and leaving the data
        at the newer revision is the state this module exists to prevent: the
        code that reads the older schema, against a database at the newer one.
        """
        target = self.event(target_event_id)
        if target is None:
            raise ValueError(f"no such deployment event {target_event_id!r}")
        eligible = self.rollback_targets()
        if not any(row["event_id"] == target_event_id for row in eligible):
            raise ValueError(
                f"{target_event_id} is outside the {ROLLBACK_WINDOW}-event rollback window; "
                f"eligible: {', '.join(row['event_id'] for row in eligible) or '(none)'}"
            )
        return self.record(
            "rollback",
            code_version=target["code_version"],
            data_version=target["data_version"],
            actor=actor,
            reason=reason or f"rolled back to {target_event_id}",
            restores=target_event_id,
            level=LIVE_LEVEL,
        )

    # --- reads

    def event(self, event_id: str) -> Optional[dict[str, Any]]:
        for row in self.events:
            if str(row["event_id"]) == str(event_id):
                return dict(row)
        return None

    def live_events(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.events if row["kind"] in {"deploy", "rollback"}]

    def current(self) -> Optional[dict[str, Any]]:
        live = self.live_events()
        return live[-1] if live else None

    def rollback_targets(self) -> list[dict[str, Any]]:
        """The events a rollback may restore, newest first.

        Excludes the event that is currently live: rolling back to where you
        already are is not a rollback, it is a no-op that would append a
        misleading event to the audit trail.
        """
        live = self.live_events()
        if len(live) <= 1:
            return []
        # `live_events` is oldest-first, so the currently-live event is the *last*
        # element, not the first. Slicing from the front would drop the oldest
        # event -- the one furthest outside the window and the least likely to be
        # wanted -- while offering a rollback to where you already are, which is
        # the one thing that must never be offered.
        historical = live[:-1]
        return [dict(row) for row in reversed(historical[-ROLLBACK_WINDOW:])]

    def window_reach(self) -> int:
        """How far back a rollback currently reaches, in events."""
        return len(self.rollback_targets())

    def full_live_reachable(self) -> bool:
        """True when the ledger holds enough live events for the whole window."""
        return len(self.live_events()) - 1 >= ROLLBACK_WINDOW

    def as_dict(self) -> dict[str, Any]:
        targets = self.rollback_targets()
        current = self.current()
        return {
            "events": len(self.events),
            "live_events": len(self.live_events()),
            "rollback_window": ROLLBACK_WINDOW,
            "rollback_targets": [row["event_id"] for row in targets],
            "window_reach": len(targets),
            "min_rollback_targets": MIN_ROLLBACK_TARGETS,
            "window_satisfied": len(targets) >= MIN_ROLLBACK_TARGETS,
            "full_window_reachable": self.full_live_reachable(),
            "current": current,
            "capacity": self.capacity,
            "note": (
                f"rollback reaches {len(targets)} of {ROLLBACK_WINDOW} events. "
                "The window counts live deployment events, and a rollback appends "
                "rather than rewrites, so undoing a rollback moves you forward in "
                "the sequence while moving the served version back."
            ),
        }


def seed_ledger(pairs: Iterable[Mapping[str, Any]] | None = None, **kwargs: Any) -> DeploymentLedger:
    """A ledger pre-filled with deployment events, for demos and tests."""
    ledger = DeploymentLedger(**kwargs)
    for row in pairs or ():
        ledger.record(
            str(row.get("kind") or "deploy"),
            code_version=str(row.get("code_version") or "code:unknown"),
            data_version=str(row.get("data_version") or "data:unknown"),
            actor=str(row.get("actor") or ""),
            reason=str(row.get("reason") or ""),
        )
    return ledger


_default_candidates: Optional[list[ReleaseCandidate]] = None


def get_default_candidates() -> list[ReleaseCandidate]:
    """The process-wide candidate list, created empty on first use.

    A list rather than a dict because ``candidate_id`` uniqueness is the
    registrar's problem, not the store's: :func:`register_candidate` rejects a
    duplicate with the id already in use, so a caller can never end up with two
    rows that answer to the same name and the ladder silently advances the
    first one found.
    """
    global _default_candidates
    if _default_candidates is None:
        _default_candidates = []
    return _default_candidates


def set_default_candidates(candidates: Optional[list[ReleaseCandidate]]) -> None:
    """Inject a candidate list. ``None`` resets to empty.

    Exists for the same reason as :func:`set_default_ledger`: these endpoints
    mutate process state, and without an explicit reset one test's promoted
    candidate is the next test's starting point.
    """
    global _default_candidates
    _default_candidates = candidates


def register_candidate(candidate: ReleaseCandidate) -> ReleaseCandidate:
    """Add a candidate at ``l0_draft``, refusing a duplicate id.

    A candidate may not be *registered* above draft. "How far along is this" is
    decided by :func:`advance_candidate`, which runs gates; letting a POST body
    name ``l4_live`` would make the whole ladder a field that can be set.
    """
    if candidate.level != DRAFT_LEVEL:
        raise ValueError(
            f"candidate {candidate.candidate_id} declares level {candidate.level!r}; a "
            f"candidate is registered at {DRAFT_LEVEL!r} and reaches {LIVE_LEVEL!r} only "
            "by passing each level's gates"
        )
    existing = get_default_candidates()
    if any(row.candidate_id == candidate.candidate_id for row in existing):
        raise ValueError(f"candidate {candidate.candidate_id!r} is already registered")
    existing.append(candidate)
    return candidate


def find_candidate(candidate_id: str) -> Optional[ReleaseCandidate]:
    for row in get_default_candidates():
        if str(row.candidate_id) == str(candidate_id):
            return row
    return None


_default_ledger: Optional[DeploymentLedger] = None


def get_default_ledger() -> DeploymentLedger:
    global _default_ledger
    if _default_ledger is None:
        _default_ledger = DeploymentLedger()
    return _default_ledger


def set_default_ledger(ledger: Optional[DeploymentLedger]) -> None:
    """Inject a ledger. Passing ``None`` resets to empty.

    The router mutates the default ledger, so tests need to restore it; making
    reset explicit is what keeps one test's deployment from being visible to the
    next.
    """
    global _default_ledger
    _default_ledger = ledger


# ---------------------------------------------------------------------------
# The "admin picks any safe level" view


def candidate_safety(candidate: Any) -> dict[str, Any]:
    """A candidate flattened for a table: where it is, whether it is safe, and
    which way it can go next."""
    report = evaluate_gates(candidate)
    forward = next_level(getattr(candidate, "level", DRAFT_LEVEL) or DRAFT_LEVEL)
    return {
        "candidate_id": str(getattr(candidate, "candidate_id", "") or ""),
        "code_version": str(getattr(candidate, "code_version", "") or ""),
        "data_version": str(getattr(candidate, "data_version", "") or ""),
        "level": str(getattr(candidate, "level", DRAFT_LEVEL) or DRAFT_LEVEL),
        "rank": int(getattr(candidate, "rank", 0) or 0),
        "summary": str(getattr(candidate, "summary", "") or ""),
        "maintainer": str(getattr(candidate, "maintainer", "") or ""),
        "serves_traffic": bool(MATURITY_LEVEL_BY_ID.get(getattr(candidate, "level", ""), {}).get("serves_traffic", False)),
        "safe_to_advance": report["safe"],
        "next_level": forward,
        "can_reach_live": _reachable_to_live(getattr(candidate, "level", DRAFT_LEVEL) or DRAFT_LEVEL),
        "blocking_failures": report["blocking_failures"],
        "unmeasured_gates": report["unmeasured_gates"],
        "warnings": report["warnings"],
        "measured": dict(getattr(candidate, "measured", {}) or {}),
    }


def _reachable_to_live(level: str) -> bool:
    """Is ``level`` an ancestor of ``LIVE_LEVEL`` on this ladder?"""
    return level in LEVEL_RANK and LEVEL_RANK[level] <= LEVEL_RANK[LIVE_LEVEL]


def safe_levels(candidates: Iterable[Any]) -> list[dict[str, Any]]:
    """Every candidate that is safe to advance, with its destination.

    This is the "admin could pick up any safe level to deploy live" surface, and
    it is sorted **by ascending destination rank**: a draft advancing to
    ``l1_verified`` is offered before one advancing to ``l3_canary``.

    That ordering is the whole point. A draft with no blocking gate at
    ``l0_draft`` is genuinely safe to advance -- the ladder owes nobody a
    ceremony on the first step -- so it appears here, and an admin looking for
    "any safe level" would find the most-exposed option first if the list were
    sorted the other way. Sorted ascending, the first row is the smallest blast
    radius available, which is the one a reasonable admin reaches for by default
    and the one that costs least when the level turns out to be wrong.

    ``serves_traffic_after`` is carried per row so a caller deploying rather than
    promoting can filter to the levels that take production traffic, without
    re-deriving the rule.
    """
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = candidate_safety(candidate)
        if row["safe_to_advance"] and row["next_level"]:
            row["destination"] = row["next_level"]
            row["serves_traffic_after"] = bool(
                MATURITY_LEVEL_BY_ID.get(row["next_level"], {}).get("serves_traffic", False)
            )
            rows.append(row)
    rows.sort(key=lambda item: (LEVEL_RANK.get(item["destination"], 99), item["candidate_id"]))
    return rows


# ---------------------------------------------------------------------------
# Validation & catalog


def validate_release_ladder() -> dict[str, Any]:
    """Errors are things that would let an unsafe change through; warnings are
    dead or ambiguous config."""
    errors: list[str] = []
    warnings: list[str] = []

    seen_levels: set[str] = set()
    ranks: set[int] = set()
    for index, row in enumerate(MATURITY_LEVELS):
        level_id = str(row.get("level_id") or "")
        if not level_id:
            errors.append(f"MATURITY_LEVELS[{index}] has no level_id")
        elif level_id in seen_levels:
            errors.append(f"duplicate level_id in MATURITY_LEVELS: {level_id}")
        seen_levels.add(level_id)
        rank = row.get("rank")
        if not isinstance(rank, int):
            errors.append(f"MATURITY_LEVELS[{level_id or index}] rank is not an int")
        elif rank in ranks:
            errors.append(f"duplicate rank in MATURITY_LEVELS: {rank}")
        else:
            ranks.add(int(rank))
        for gate_id in _str_tuple(row.get("entry_gates")):
            if gate_id not in PROMOTION_GATE_BY_ID:
                errors.append(f"MATURITY_LEVELS[{level_id}] entry gate {gate_id!r} is not a declared gate")
            elif PROMOTION_GATE_BY_ID[gate_id]["severity"] == "advisory":
                # An advisory gate on the entry list is a contradiction: the level
                # says "required" while the severity says "optional".
                errors.append(
                    f"MATURITY_LEVELS[{level_id}] lists advisory gate {gate_id!r} as an entry "
                    "requirement; entry gates must be blocking"
                )
        if not row.get("description"):
            warnings.append(f"MATURITY_LEVELS[{level_id or index}] has no description")

    ordered = sorted((int(r["rank"]), str(r["level_id"])) for r in MATURITY_LEVELS if isinstance(r.get("rank"), int))
    for (_, lower), (_, higher) in zip(ordered, ordered[1:]):
        if next_level(lower) != higher:
            errors.append(f"MATURITY_LEVELS ranks are not contiguous: {lower} does not lead to {higher}")

    seen_gates: set[str] = set()
    for index, row in enumerate(PROMOTION_GATES):
        gate_id = str(row.get("gate_id") or "")
        if not gate_id:
            errors.append(f"PROMOTION_GATES[{index}] has no gate_id")
        elif gate_id in seen_gates:
            errors.append(f"duplicate gate_id in PROMOTION_GATES: {gate_id}")
        seen_gates.add(gate_id)
        if str(row.get("severity") or "") not in GATE_SEVERITIES:
            errors.append(f"PROMOTION_GATES[{gate_id or index}] has severity {row.get('severity')!r}")
        if not _str_tuple(row.get("checks")):
            errors.append(f"PROMOTION_GATES[{gate_id or index}] checks nothing")
        if not row.get("passes_when"):
            warnings.append(f"PROMOTION_GATES[{gate_id or index}] has no passes_when")
        # A gate with no rule would silently fail forever; check the rule exists.
        if gate_id and _apply_rule(gate_id, {}, {})[0] is False and "no rule" in _apply_rule(gate_id, {}, {})[1]:
            errors.append(f"PROMOTION_GATES[{gate_id}] has no rule in _apply_rule")

    for gate_id in _ALWAYS_ON:
        if gate_id not in PROMOTION_GATE_BY_ID:
            errors.append(f"always-on gate {gate_id!r} is not a declared gate")

    for level_id, gate_ids in LEVEL_ENTRY_GATES.items():
        for gate_id in gate_ids:
            if gate_id not in PROMOTION_GATE_BY_ID:
                errors.append(f"MATURITY_LEVELS[{level_id}] references undeclared gate {gate_id!r}")

    return {
        "generated_at": _now_iso(),
        "catalog_version": RELEASE_CATALOG_VERSION,
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
        "levels": len(MATURITY_LEVELS),
        "gates": len(PROMOTION_GATES),
        "rollback_window": ROLLBACK_WINDOW,
        "min_rollback_targets": MIN_ROLLBACK_TARGETS,
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(MATURITY_LEVELS)} levels and {len(PROMOTION_GATES)} gates"
        ),
    }


def build_release_ladder_catalog(
    *,
    candidates: Iterable[Any] | None = None,
    ledger: Optional[DeploymentLedger] = None,
) -> dict[str, Any]:
    """The tables, their operators, the ladder view, and the ledger state."""
    resolved_ledger = ledger or get_default_ledger()
    rows = [candidate_safety(candidate) for candidate in (candidates or ())]
    return {
        "catalog_version": RELEASE_CATALOG_VERSION,
        "generated_at": _now_iso(),
        "gate_ops": dict(GATE_OPS),
        "levels": [dict(row) for row in MATURITY_LEVELS],
        "gates": [dict(row) for row in PROMOTION_GATES],
        "always_on": list(_ALWAYS_ON),
        "event_kinds": [dict(EVENT_KIND_BY_NAME[kind]) for kind in EVENT_KINDS],
        "candidates": rows,
        "safe_levels": safe_levels(candidates or ()),
        "ledger": resolved_ledger.as_dict(),
        "operators": {
            "evaluate_gates": "app.release_ladder.evaluate_gates",
            "advance_candidate": "app.release_ladder.advance_candidate",
            "safe_levels": "app.release_ladder.safe_levels",
            "validate_release_ladder": "app.release_ladder.validate_release_ladder",
        },
    }


def summary() -> dict[str, Any]:
    """One-line state, for the CLI and for ``/meta``."""
    ledger = get_default_ledger()
    state = ledger.as_dict()
    return {
        "levels": len(MATURITY_LEVELS),
        "gates": len(PROMOTION_GATES),
        "rollback_window": ROLLBACK_WINDOW,
        "window_reach": state["window_reach"],
        "window_satisfied": state["window_satisfied"],
        "live_events": state["live_events"],
    }
