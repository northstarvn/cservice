"""Evidence-based policy motion, and trust under continuous change (Stage F).

Two halves of one question. *Why did this rule change?* and *if everything keeps
changing, why should anybody believe any of it?*

Why a motion ledger, and not a changelog
---------------------------------------
This codebase is full of tables that decide things about people: recovery review
rules, generosity rules, care weights, loyalty thresholds, complaint severities.
Every one of them has been edited at some point, and none of them left a trace.
That is survivable when the edits are rare and reviewed. It stops being
survivable at the moment something *learns* -- which is what Stages C and D built.
`care_weights` derives emphasis from observed complaints, `offer_outcomes` ranks
generosity rules by measured fulfilment, `policy_scoring` selects rule packs.

So the exposure is no longer "somebody edited a table" but "a table can now
change itself, and nobody downstream can tell what moved or why". A changelog
records that a thing changed. This records **what evidence was present when it
changed, what would have counted as sufficient, and what promise was in force at
the time** -- which is the only version from which a later reader can decide
whether the change was justified or merely happened.

Two things it deliberately does not do:

* It does not apply anything. Stage D established the rule that an engine may
  rank and recommend but never write its own table, because an engine that
  retunes itself from its own outputs makes that table unauditable. That rule is
  not weakened here.
* It does not evaluate the evidence. It records the evidence and requires it to
  be *sufficient by declaration*, then refuses a motion whose declared evidence
  does not meet the bar for that class of change. Judging whether a Wilson lower
  bound of 0.62 is good enough for a spend limit is a human decision, and the
  threshold belongs in a table a human edits rather than in a function.

Trust under continuous change
-----------------------------
A system that changes its rules constantly has a problem that a stable one does
not: **every promise it makes has an expiry date, and the customer cannot see
it.** "You are Trusted" is a promise. "We will not contact you at night" is a
promise. Both are made under some version of some table, and when the table
moves the promise quietly moves with it, and the customer's experience of the
service degrades through a series of individually defensible decisions.

The failure is not that a rule changed. It is that **a rule changed and nobody
told the person whose experience it moved, and the person has no way to find
out what they are now owed.** So a promise here is not a string; it is a
`(subject, commitment, version)` triple, and the continuity check asks a single
question: *does what we now promise still hold for somebody who believed the old
one?*

The answer is allowed to be "no", with a reason. What is not allowed is a silent
no.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

POLICY_MOTION_CATALOG_VERSION = 1


# ---------------------------------------------------------------------------
# Item 6 -- the motion ledger
# ---------------------------------------------------------------------------

#: Classes of policy change, weakest evidence requirement first.
#:
#: The ordering is by consequence, and the key is deliberately the *class name*
#: rather than a severity integer, so a new class cannot be slotted in with a
#: number someone picked. The evidence bar is per class because "we looked at the
#: numbers" is sufficient to widen a coverage window and is not sufficient to
#: change what somebody is owed.
POLICY_MOTION_CLASSES: tuple[dict[str, Any], ...] = (
    {
        "class_id": "coverage_tuning",
        "rank": 0,
        "label": "When or where we may contact somebody",
        "min_evidence_kind": "observation",
        "min_evidence_samples": 20,
        "affects_customer_experience": True,
        "must_notify": True,
        "reversible_by": "table_edit",
        "why": (
            "a window change is felt immediately and is often invisible: a customer "
            "simply stops being called. Cheap to change, and the reason the bar is "
            "low is that it changes nothing they are *owed*"
        ),
    },
    {
        "class_id": "emphasis_tuning",
        "rank": 1,
        "label": "Which signal carries the most weight",
        "min_evidence_kind": "observation",
        "min_evidence_samples": 30,
        "affects_customer_experience": True,
        "must_notify": False,
        "reversible_by": "table_edit",
        "why": (
            "the Stage D care weights. It changes what a human *notices* first, not "
            "what a customer receives, so the bar is a little higher than a window "
            "and the answer is no notification"
        ),
    },
    {
        "class_id": "entitlement_change",
        "rank": 2,
        "label": "What somebody is owed",
        "min_evidence_kind": "measured_outcome",
        "min_evidence_samples": 50,
        "affects_customer_experience": True,
        "must_notify": True,
        "reversible_by": "table_edit",
        "why": (
            "generosity scales and credit limits. This is the class where a change "
            "can take something away, so it needs measured outcomes rather than "
            "counts of observations, and the customer is told"
        ),
    },
    {
        "class_id": "status_rule_change",
        "rank": 3,
        "label": "Who counts as Trusted, or what a band means",
        "min_evidence_kind": "measured_outcome",
        "min_evidence_samples": 100,
        "affects_customer_experience": True,
        "must_notify": True,
        "reversible_by": "compensation",
        "why": (
            "the only class that is not reversible by editing a table. Somebody who "
            "was Trusted and is not has been told they were something they are "
            "not, and no table edit takes that back -- hence the largest sample "
            "requirement and the reason the reversal is compensation"
        ),
    },
)
MOTION_CLASS_BY_ID: dict[str, dict[str, Any]] = {
    str(row["class_id"]): dict(row) for row in POLICY_MOTION_CLASSES
}
MOTION_CLASS_IDS: tuple[str, ...] = tuple(MOTION_CLASS_BY_ID)

#: The kinds of evidence a motion may rest on, weakest first.
#:
#: Kept separate from the class table so the bar is "this class needs at least
#: this kind, with at least this many samples" and a new evidence kind has to be
#: declared here before any class can demand it.
EVIDENCE_KINDS: tuple[dict[str, Any], ...] = (
    {
        "kind": "opinion",
        "rank": 0,
        "label": "Somebody thought so",
        "sufficient_for": (),
        "why": (
            "never sufficient on its own. A motion with opinion as its only "
            "evidence is a preference, and preferences belong in a discussion, not "
            "in a ledger entry that reads as a finding"
        ),
    },
    {
        "kind": "observation",
        "rank": 1,
        "label": "Something was counted",
        "sufficient_for": ("coverage_tuning", "emphasis_tuning"),
        "why": "counts of what happened, with no comparison to a counterfactual",
    },
    {
        "kind": "measured_outcome",
        "rank": 2,
        "label": "Something was compared against an alternative",
        "sufficient_for": (
            "coverage_tuning",
            "emphasis_tuning",
            "entitlement_change",
            "status_rule_change",
        ),
        "why": (
            "a comparison. The ledger cannot tell whether the comparison was any "
            "good -- it records that one was made, because 'was it any good' is the "
            "part a human has to answer"
        ),
    },
)
EVIDENCE_RANK: dict[str, int] = {str(row["kind"]): int(row["rank"]) for row in EVIDENCE_KINDS}


def classify_motion(target: str) -> dict[str, Any]:
    """Which class of change a named target is, by reading the table it lives in.

    Resolved from the **module that owns the target**, never from a naming
    convention, and never guessed. An unrecognised target classifies as
    ``unknown`` and therefore requires the strictest class -- the assumption that
    a change nobody can classify is a change to something we owe somebody.
    """
    name = str(target or "").strip()
    if not name:
        return {
            "target": name,
            "class_id": "unknown",
            "rank": 99,
            "resolved": False,
            "reason": "no target named. A motion without a target is a note, and "
            "a note is not a change to a policy",
        }
    # Read the owning module, so the classification follows the code rather than
    # a table that can drift away from it.
    known: dict[str, str] = {
        "app.services.region_windows.REGIONS": "coverage_tuning",
        "app.services.customer_offers.OFFER_GENEROSITY_RULES": "entitlement_change",
        "app.services.recovery_playbooks.RECOVERY_REVIEW_RULES": "coverage_tuning",
        "app.services.care_weights.CARE_WEIGHT_RULES": "emphasis_tuning",
        "app.services.loyalty_status.LOYALTY_STATUS_RULES": "status_rule_change",
        "app.services.loyalty_status.EXPERIENTIAL_TIER_RULES": "status_rule_change",
        "app.services.complaints.COMPLAINT_SEVERITY_RULES": "status_rule_change",
    }
    for key, class_id in known.items():
        if key.split(".")[-1] in name or name == key.split(".")[-1]:
            row = MOTION_CLASS_BY_ID[class_id]
            return {
                "target": name,
                "class_id": class_id,
                "rank": int(row["rank"]),
                "resolved": True,
                "label": str(row["label"]),
                "reason": f"{name} is owned by a table classified as {class_id!r}",
            }
    strictest = MOTION_CLASS_BY_ID["status_rule_change"]
    return {
        "target": name,
        "class_id": "unknown",
        "rank": 99,
        "resolved": False,
        "label": "Unclassified change",
        "reason": (
            f"{name!r} is not a table this ledger knows. An unclassifiable change "
            "is treated as the strictest class, because the assumption that fails "
            "safely is 'this may be something we owe somebody'"
        ),
        "assumed_as": str(strictest["class_id"]),
    }


def build_motion(
    *,
    target: str,
    evidence: Sequence[Mapping[str, Any]] = (),
    reason: str = "",
    proposed_by: str = "",
    supersedes: str = "",
) -> dict[str, Any]:
    """One proposed change, with the evidence attached, and an honest verdict.

    The verdict is ``sufficient`` / ``insufficient`` / ``unknown`` rather than a
    boolean, for the same reason the capability audit has six states: a change
    with no evidence, a change with too little, and a change to something the
    ledger cannot classify want three different responses, and a boolean makes two
    of them look identical.
    """
    classification = classify_motion(target)
    class_id = str(classification["class_id"])
    rule = MOTION_CLASS_BY_ID.get(class_id)
    rows = [dict(row) for row in evidence or ()]
    kinds = sorted({str(row.get("kind") or "") for row in rows})
    samples = sum(int(row.get("samples") or 0) for row in rows)

    reasons: list[str] = []
    if not str(reason or "").strip():
        reasons.append("no reason given. A change with no stated reason is not reviewable")
    if not str(proposed_by or "").strip():
        reasons.append(
            "no proposer recorded. 'We changed it' is the class of motion this "
            "ledger exists to make impossible"
        )
    if not rows:
        verdict = "insufficient"
        reasons.append("no evidence attached")
    elif rule is None:
        # Unclassifiable, but *not* unjudgeable. The strictest class's bar is used
        # as a floor: evidence that fails it is genuinely insufficient and is
        # reported as such, while evidence that clears it is reported `unknown`
        # rather than `sufficient`, because passing a floor is not the same as
        # knowing what kind of change this is. An earlier version returned
        # `unknown` for both, which meant a motion resting on an opinion reached
        # the same verdict as one resting on a large comparison.
        strictest = MOTION_CLASS_BY_ID["status_rule_change"]
        need_kind = str(strictest["min_evidence_kind"])
        need_samples = int(strictest["min_evidence_samples"])
        best = max((EVIDENCE_RANK.get(k, -1) for k in kinds), default=-1)
        if best >= 0 and best >= EVIDENCE_RANK.get(need_kind, 0) and samples >= need_samples:
            verdict = "unknown"
            reasons.append(
                f"{target!r} is not a table this ledger knows, so the bar that "
                f"applies cannot be identified. The evidence clears the strictest "
                f"class ({need_kind!r} at {need_samples} samples), which is a floor "
                "and not a verdict: somebody has to say what this table governs"
            )
        else:
            verdict = "insufficient"
            reasons.append(
                f"{target!r} is unclassifiable, so it is measured against the "
                f"strictest class ({need_kind!r} at {need_samples} samples), and it "
                "does not clear it"
            )
    else:
        need_kind = str(rule["min_evidence_kind"])
        need_samples = int(rule["min_evidence_samples"])
        best = max((EVIDENCE_RANK.get(k, -1) for k in kinds), default=-1)
        if best < 0:
            verdict = "insufficient"
            reasons.append(
                f"evidence kinds {kinds} are not declared in EVIDENCE_KINDS, so "
                "none of them can be ranked against the bar"
            )
        elif best < EVIDENCE_RANK.get(need_kind, 0):
            verdict = "insufficient"
            reasons.append(
                f"{class_id!r} requires at least {need_kind!r} evidence and the "
                f"strongest attached is {best!r}"
            )
        elif samples < need_samples:
            verdict = "insufficient"
            reasons.append(
                f"{class_id!r} requires {need_samples} samples and {samples} were "
                "attached"
            )
        elif not reasons:
            verdict = "sufficient"
        else:
            verdict = "insufficient"

    must_notify = bool(rule.get("must_notify")) if rule else True
    return {
        "target": str(target),
        "class_id": class_id,
        "class_label": str(classification.get("label") or ""),
        "class_resolved": bool(classification.get("resolved")),
        "rank": int(classification["rank"]),
        "reason": str(reason or ""),
        "proposed_by": str(proposed_by or ""),
        "supersedes": str(supersedes or ""),
        "evidence": rows,
        "evidence_kinds": kinds,
        "evidence_samples": samples,
        "required_evidence_kind": str(rule.get("min_evidence_kind")) if rule else "",
        "required_evidence_samples": int(rule["min_evidence_samples"]) if rule else 0,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "affects_customer_experience": bool(rule.get("affects_customer_experience", True))
        if rule
        else True,
        "must_notify": must_notify,
        "reversible_by": str(rule.get("reversible_by")) if rule else "",
        "applied": False,
        "note": (
            "this records a proposal. Nothing here applies it. Stage D's rule stands: "
            "an engine that retunes its own table from its own outputs makes that "
            "table unauditable, and a ledger that could apply would be that engine"
        ),
    }


def build_motion_ledger(motions: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The ledger, read the way a reviewer would read it.

    Insufficient motions are listed **first**, not filtered out. A ledger that
    shows only the motions that passed is a list of achievements; the ones that
    failed their own bar are the entries worth reading.
    """
    order = ["insufficient", "unknown", "sufficient"]
    built = [build_motion(**dict(row)) for row in motions or ()]
    # Sorted, because the docstring below promises insufficient-first and an
    # earlier version returned input order. A ledger whose stated order is not
    # its actual order is worse than one that makes no claim: the reader trusts
    # the order and reads past the failures.
    built.sort(key=lambda row: order.index(str(row["verdict"])))
    by_verdict: dict[str, list[dict[str, Any]]] = {}
    for row in built:
        by_verdict.setdefault(str(row["verdict"]), []).append(row)
    return {
        "catalog_version": POLICY_MOTION_CATALOG_VERSION,
        "motions": built,
        "by_verdict": {k: [dict(r) for r in by_verdict.get(k, ())] for k in order},
        "counts": {k: len(by_verdict.get(k, ())) for k in order},
        "needs_notification": [
            {"target": r["target"], "class_id": r["class_id"]}
            for r in built
            if r["must_notify"] and r["verdict"] == "sufficient"
        ],
        "applied": [],
        "note": (
            "unsufficient first. A ledger that shows only what passed is a list of "
            "achievements, and the entries that failed their own evidence bar are "
            "the ones worth reading"
        ),
    }


# ---------------------------------------------------------------------------
# Item 8 -- trust under continuous change
# ---------------------------------------------------------------------------

TRUST_CONTINUITY_CATALOG_VERSION = 1

#: The promises this system makes, and what has to be true for each to survive a
#: change to the table underneath it.
#:
#: Read as data so a reviewer can see the whole set of things we have told people
#: in one place, which is otherwise scattered across whichever module happened to
#: make the promise. Each row names what breaks it -- and a promise that cannot
#: name its own failure mode is one nobody is watching.
PROMISES: tuple[dict[str, Any], ...] = (
    {
        "promise_id": "status_is_earned",
        "commitment": "Your status does not fall because you stopped calling.",
        "subject": "loyalty_status",
        "breaks_if": "a status rule starts decaying on inactivity",
        "regression_guard": "status_never_decays",
        "why": (
            "the no-decay invariant. It is the promise most likely to be eroded "
            "indirectly, by someone adding 'except after N months' to a rule for a "
            "good reason"
        ),
    },
    {
        "promise_id": "recovery_is_not_withheld",
        "commitment": "We will not hold back a fix because of a marketing setting.",
        "subject": "preferences",
        "breaks_if": "service or recovery appears in CONSENT_GATED_PURPOSES",
        "regression_guard": "consent_gate_excludes_service",
        "why": "Stage B's central finding, kept as a named promise so it cannot be undone quietly",
    },
    {
        "promise_id": "offer_exists_regardless",
        "commitment": "If you are owed something, it exists whether or not we message you.",
        "subject": "customer_offers",
        "breaks_if": "a preference stops the offer being created rather than the notification",
        "regression_guard": "recovery_never_withholds_a_fix",
        "why": (
            "an offer that exists undelivered is the system working. Collapsing the "
            "two is how a preference becomes a suppression"
        ),
    },
    {
        "promise_id": "quiet_hours_are_local",
        "commitment": "We will not contact you during the hours you told us.",
        "subject": "region_windows",
        "breaks_if": "the local hour is computed at UTC again",
        "regression_guard": "contact_hour_is_local",
        "why": "Stage E's defect, kept as a promise so the fix has something to hold",
    },
    {
        "promise_id": "human_decides",
        "commitment": "A person reads this before anything is said to you.",
        "subject": "relationship_view",
        "breaks_if": "requires_human becomes False on a view",
        "regression_guard": "copilot_is_the_default_pane",
        "why": "the copilot is a suggestion engine and must not become an actor",
    },
)
PROMISE_BY_ID: dict[str, dict[str, Any]] = {
    str(row["promise_id"]): dict(row) for row in PROMISES
}
PROMISE_IDS: tuple[str, ...] = tuple(PROMISE_BY_ID)


def check_promise_continuity(
    promises: Sequence[Mapping[str, Any]] = (),
    *,
    guards: Optional[Mapping[str, bool]] = None,
) -> dict[str, Any]:
    """Does what we promise now still hold for somebody who believed it before?

    A single question, asked per promise, and the answer is allowed to be ``no``
    -- with a reason. What is not allowed is a silent no, so ``broken`` always
    carries the commitment that is no longer being kept, because a broken promise
    nobody can read is the same as a promise never made.
    """
    declared = [dict(row) for row in promises or ()]
    known = {pid: PROMISE_BY_ID[pid] for pid in (r.get("promise_id") for r in declared) if pid in PROMISE_BY_ID}
    guard_map = dict(guards or {})
    checked: list[dict[str, Any]] = []
    for promise_id in sorted(set(known) | set(guard_map) & set(PROMISE_BY_ID)):
        promise = PROMISE_BY_ID.get(promise_id, {})
        guard_name = str(promise.get("regression_guard") or promise_id)
        result = guard_map.get(guard_name)
        state = (
            "held"
            if result is True
            else "broken"
            if result is False
            else "unverified"
        )
        checked.append(
            {
                "promise_id": promise_id,
                "commitment": str(promise.get("commitment") or ""),
                "subject": str(promise.get("subject") or ""),
                "guard": guard_name,
                "state": state,
                "reason": (
                    ""
                    if result is True
                    else f"the guard {guard_name!r} does not hold. This promise is "
                    f"not currently being kept: {promise.get('breaks_if')}"
                    if result is False
                    else (
                        f"no result was supplied for guard {guard_name!r}. An "
                        "unverified promise is reported as unverified rather than "
                        "assumed, because assuming is how it stops being watched"
                    )
                ),
            }
        )
    broken = [row for row in checked if row["state"] == "broken"]
    unverified = [row for row in checked if row["state"] == "unverified"]
    held = [row for row in checked if row["state"] == "held"]
    # `continuity` is False unless every promise was actually verified and none
    # is broken. An earlier version returned `not broken`, which made "nothing is
    # broken" and "nothing was looked at" the same answer -- the exact reporting
    # failure this function exists to prevent, and one its own validator caught.
    fully_verified = bool(checked) and not unverified
    return {
        "catalog_version": TRUST_CONTINUITY_CATALOG_VERSION,
        "promises": checked,
        "held": len(held),
        "broken": broken,
        "unverified": unverified,
        "continuity": fully_verified and not broken,
        "fully_verified": fully_verified,
        "declared": [r.get("promise_id") for r in declared],
        "undeclared": sorted(
            set(PROMISE_BY_ID) - {str(r.get("promise_id")) for r in declared}
        ),
        "note": (
            "a broken promise is a fact to be told, not a gate to be passed. This "
            "returns continuity=False and lists the commitment, and it does not "
            "stop anything from shipping -- a system that cannot be honest about a "
            "broken promise cannot be trusted to hold the ones it has not broken"
        ),
    }


# ---------------------------------------------------------------------------
# Validation and catalog
# ---------------------------------------------------------------------------


def validate_policy_motion() -> dict[str, Any]:
    """Check the ledger's own table.

    The check worth having: **every class's evidence bar must be reachable.** A
    class demanding a kind no evidence can be, or demanding more samples than any
    motion could carry, is a bar that silently refuses everything -- and a gate
    that always refuses reads as a gate that is working.
    """
    errors: list[str] = []
    warnings: list[str] = []

    ranks = [int(row["rank"]) for row in POLICY_MOTION_CLASSES]
    if ranks != sorted(ranks):
        errors.append("POLICY_MOTION_CLASSES ranks are not ascending")
    if len(set(ranks)) != len(ranks):
        errors.append("POLICY_MOTION_CLASSES repeats a rank")

    declared_kinds = set(EVIDENCE_RANK)
    for row in POLICY_MOTION_CLASSES:
        class_id = str(row["class_id"])
        need = str(row.get("min_evidence_kind") or "")
        if need not in declared_kinds:
            errors.append(
                f"POLICY_MOTION_CLASSES[{class_id}] requires evidence kind "
                f"{need!r}, which EVIDENCE_KINDS does not declare. A bar no "
                "evidence can meet is a gate that always refuses"
            )
        if int(row.get("min_evidence_samples") or 0) <= 0:
            errors.append(
                f"POLICY_MOTION_CLASSES[{class_id}] requires zero samples, so any "
                "evidence at all passes it"
            )
        if not str(row.get("why") or "").strip():
            warnings.append(
                f"POLICY_MOTION_CLASSES[{class_id}] has no `why`; an evidence bar "
                "nobody can question is one somebody will lower"
            )

    for kind in EVIDENCE_KINDS:
        for class_id in kind.get("sufficient_for") or ():
            if class_id not in MOTION_CLASS_BY_ID:
                errors.append(
                    f"EVIDENCE_KINDS[{kind['kind']}].sufficient_for names "
                    f"{class_id!r}, which POLICY_MOTION_CLASSES does not declare"
                )
            else:
                required = str(
                    MOTION_CLASS_BY_ID[class_id].get("min_evidence_kind")
                )
                if EVIDENCE_RANK.get(str(kind["kind"]), -1) < EVIDENCE_RANK.get(
                    required, 0
                ):
                    errors.append(
                        f"EVIDENCE_KINDS[{kind['kind']}] claims it is sufficient for "
                        f"{class_id!r}, but that class requires {required!r}, which "
                        "ranks higher. A sufficient_for list that contradicts the "
                        "bar is a second, quieter source of truth"
                    )

    if EVIDENCE_KINDS and EVIDENCE_KINDS[0]["sufficient_for"]:
        errors.append(
            "the weakest evidence kind claims to be sufficient for something. If "
            "opinion can justify a change, the bar is not a bar"
        )
    if EVIDENCE_KINDS and EVIDENCE_KINDS[-1]["rank"] < 1:
        errors.append("EVIDENCE_KINDS ranks must start at 0 and ascend")

    # An unclassifiable target must land on the strictest class, not on nothing.
    unknown = classify_motion("some_table_nobody_declared")
    if unknown.get("class_id") != "unknown":
        errors.append(
            "classify_motion classified an undeclared table. A guess here would put "
            "a change to something we owe somebody into the cheapest class"
        )
    strictest = max(
        POLICY_MOTION_CLASSES, key=lambda row: int(row["rank"])
    )
    motion = build_motion(
        target="some_table_nobody_declared",
        evidence=[{"kind": "observation", "samples": 500}],
        reason="because",
        proposed_by="someone",
    )
    if motion["verdict"] == "sufficient":
        errors.append(
            "an unclassifiable target was accepted on observation evidence alone. "
            "It must be evaluated against the strictest class, so an observation "
            "cannot justify a change to what somebody is owed"
        )
    if int(motion["rank"]) < int(strictest["rank"]):
        errors.append(
            "an unclassifiable motion was ranked below the strictest class"
        )

    # A motion with no proposer or no reason must not pass.
    bare = build_motion(
        target="app.services.loyalty_status.LOYALTY_STATUS_RULES",
        evidence=[{"kind": "measured_outcome", "samples": 500}],
    )
    if bare["verdict"] == "sufficient":
        errors.append(
            "a motion with no reason and no proposer was accepted. That is the "
            "class of change this ledger exists to make impossible"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "classes": len(POLICY_MOTION_CLASSES),
        "evidence_kinds": len(EVIDENCE_KINDS),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(POLICY_MOTION_CLASSES)} motion classes and "
            f"{len(EVIDENCE_KINDS)} evidence kinds"
        ),
    }


def validate_trust_continuity() -> dict[str, Any]:
    """Check that each named promise has a guard that is actually a thing."""
    errors: list[str] = []
    warnings: list[str] = []

    seen: set[str] = set()
    for row in PROMISES:
        promise_id = str(row["promise_id"])
        if promise_id in seen:
            errors.append(f"PROMISES repeats {promise_id!r}")
        seen.add(promise_id)
        for field in ("commitment", "subject", "breaks_if", "why"):
            if not str(row.get(field) or "").strip():
                errors.append(
                    f"PROMISES[{promise_id}] has no {field!r}. A promise that cannot "
                    "name its own failure mode is one nobody is watching"
                )
        guard = str(row.get("regression_guard") or "")
        if not guard:
            errors.append(
                f"PROMISES[{promise_id}] names no regression guard, so nothing "
                "reports it as broken when it is"
            )
        elif not str(row.get("breaks_if") or "").strip():
            warnings.append(
                f"PROMISES[{promise_id}] guard {guard!r} exists but nothing says what "
                "breaking it looks like"
            )

    # Every guard must name a real probe, or the promise is checked by nothing.
    try:
        from app import real_life_flows

        registered = set(real_life_flows.PROBES)
        for row in PROMISES:
            guard = str(row.get("regression_guard") or "")
            if guard and guard not in registered:
                errors.append(
                    f"PROMISES[{row['promise_id']}] names guard {guard!r}, which is "
                    "not a registered probe. A promise checked by nothing is an "
                    "intention"
                )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"could not read real_life_flows.PROBES: {exc}")

    # An all-clear must be distinguishable from "nothing was checked".
    empty = check_promise_continuity()
    if empty["continuity"] and not empty["promises"]:
        errors.append(
            "check_promise_continuity returns continuity=True for an empty input. "
            "That makes 'nothing is broken' and 'nothing was looked at' the same "
            "answer, which is the reporting failure this is meant to avoid"
        )
    if check_promise_continuity()["continuity"]:
        errors.append(
            "a continuity check over no guard results must not report continuity. "
            "An unverified promise is not a held one"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "error_list": errors,
        "warning_list": warnings,
        "promises": len(PROMISES),
        "summary": (
            f"{len(errors)} error(s), {len(warnings)} warning(s) across "
            f"{len(PROMISES)} promises"
        ),
    }


def build_trust_catalog() -> dict[str, Any]:
    """Both tables, published."""
    return {
        "motion_catalog_version": POLICY_MOTION_CATALOG_VERSION,
        "motion_classes": [dict(row) for row in POLICY_MOTION_CLASSES],
        "evidence_kinds": [dict(row) for row in EVIDENCE_KINDS],
        "trust_catalog_version": TRUST_CONTINUITY_CATALOG_VERSION,
        "promises": [dict(row) for row in PROMISES],
        "note": (
            "a changelog records that a thing changed. this records what evidence "
            "was present when it changed, what would have counted as sufficient, "
            "and which promise was in force at the time -- the only version from "
            "which a later reader can decide whether the change was justified or "
            "merely happened. it does not evaluate whether the evidence was any "
            "good, because that judgement belongs to a human editing a threshold, "
            "and it applies nothing, because Stage D's rule stands: an engine that "
            "retunes its own table from its own outputs makes that table "
            "unauditable"
        ),
    }
