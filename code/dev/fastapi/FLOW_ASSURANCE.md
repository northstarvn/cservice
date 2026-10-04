# Flow assurance: what the flows actually prove, and what they only claim

*Working note, second edition. Every number was re-measured against the tree on
2 October 2026 after the fixes in [§9](#9-what-was-fixed-and-what-is-still-open);
the commands to re-measure are in [§10](#10-re-measuring).*

**What changed between editions, in one line:** the suite went from 2892 tests
to **3079**, of which **100** are new end-to-end flow tests across four tiers.
Seventeen defects are fixed, seven of them high severity — among them a customer who
saw a permission error immediately after a successful write, a release ladder
whose last two rungs could be unlocked by typing three numbers, and an offer state
machine that let one person accept and decline the same offer in the same instant.
Six items are still open, and each is written down here with the invariant that
would close it.

| | Edition 1 | Edition 2 |
|---|---|---|
| Suite | 2892 passing | **3079 passing** |
| End-to-end flow tests | 0 | **100** (52 / 13 / 18 / 17 by tier) |
| Blind probes | 16 of 24 | **11 of 24** |
| Probes with a planted-defect control | 4 | **12** |
| Orphan probes | 2 | **0** |
| Gates with no computable measurement source | 3 | **0** |
| Gates unmeasured at `l3_canary` in a real sweep | 3 | **1** |

---

## 1. Why this document exists

This codebase has three different things called "a flow", and they get confused
with each other often enough to be worth separating before anything else.

| | What it is | Where it lives | What a green result means |
|---|---|---|---|
| **A stage flow** | a named business journey, described in prose | `BLOCKAGES.md`, stage files `test_stage_*.py` | one developer's claim about one subsystem, checked by one test file |
| **A simulator flow** | a journey with probes attached to live engines | `app/real_life_flows.py` | 24 probes agreed with their own expectations |
| **A test flow** | a journey with assertions written by a human | `tests/test_e2e_*.py` | that human's claims hold |

The confusion is expensive in a specific direction: a simulator flow that
reports "no blockage" is easy to read as "the customer journey works", and the
two are not close to the same claim. This document is about the distance between
them — which claims are **assured** (a test would fail if they stopped being
true), which are merely **asserted** (written down somewhere, checked by
nothing), and which are **not assured at all** in a way that is currently
invisible.

**The assurance definition used throughout, and only this one:**

> A claim is *assured* if there exists a check that would fail when the claim
> stops being true — including a check that fails by *breaking* the thing and
> observing the failure.

The second half is what makes this document worth writing. A test that only
exercises the healthy path proves the happy path; a test that poisons the engine
underneath proves the test is looking at the engine.

One more definition, because the tier structure depends on it:

> A flow is a **tier-n** test if it is the *nth* rung of certainty: tier 1 asks
> "is this unit true", tier 2 asks "does this work for a person", tier 3 asks "is
> the process around the product sound", tier 4 asks "does it survive two things
> happening at once". Each tier assumes the one below and adds one kind of claim.

---

## 2. The scale

Four marks, used in every table below. They are about the *check*, not about the
code's quality.

| Mark | Name | What it takes to earn it |
|---|---|---|
| **A** | **Assured** | A passing test, and a negative control proving the test notices when the claim is broken |
| **a** | **Assured, healthy path only** | A passing test that exercises the claim and would fail on a regression, but nothing proves it *detects* a planted defect |
| **·** | **Asserted only** | The claim is written down — in a probe's `expected`, a flow's `invariant`, a docstring — and no check reads it |
| **✗** | **Not covered** | Neither a check nor a written claim |

The middle two marks are the interesting pair, and the gap between them is
larger than it looks. `a` is what almost every test in this repository earns by
default, and it is genuinely worth something. But the module that exists to
answer "does a customer still get a booking when the routing engine changed"
was entirely made of `a`s, and that was the whole problem.

---

## 3. Tier 1 — the certainty flows

`tests/_e2e_world.py` and `tests/test_e2e_certainty_flows.py`. **52 tests, all
green.**

These are the claims we can be *certain* of, so they are checked against the
strongest available oracle: the published vocabulary itself. A band is not
"correct" because a test agrees with it; it is correct because a test proves no
value outside the published scale can be produced.

The harness is a real in-memory SQLite database driven through one event loop
(`tests/_doubles.SqliteHarness`), not a session double, for the reason
`_doubles` gives: *a session double asserts your own assumptions back at you*.
`Mounted` reaches the app through `httpx.AsyncClient` on an `ASGITransport`
rather than `TestClient`, because an aiosqlite connection is bound to the loop
that opened it.

`THE_CAST` is five hypothetical customers — ana, bruno, chiara, dmitri, root —
each carrying an access score, a declared policy tier, a booking history, a
region, an experiential tier, motion evidence, device recognitions and consent
flags. One detail worth keeping, because it was found by a test rather than by
reading: **ana's `system_score` is 80, not the 92 it started as.** At 92 she
resolved to `system-premium` while her declared `policy_tier` said
`customer-premium`, so the persona and the engine disagreed about the same
person — and no cast member resolved to `customer-premium` at all, leaving a
published tier that nothing exercised. Lowering the score fixes both, because
the declared tier is the intent and the score is what had drifted.

---

## 4. The simulator flows — the honest ledger

12 flows × 5 personas = **21 flow/persona pairs**, 24 probes, **70 subflow
executions**, **0 blockages**, 0 failures, 0 deferrals. Every number below is
`run_all_flows()` on this tree.

### 4.1 The declared journey and the executed journey are different vocabularies

| | |
|---|---|
| Distinct `subflows` names declared across the catalog | **55** |
| Distinct subflow ids actually produced by running every flow | **24** |
| **Overlap between the two sets** | **0** |

The declared names are the human journey — `register`, `authenticate`,
`create_booking`, `event_log`, `plan_playbook`. The executed names are probe
outcomes — `access_band_resolves`, `complaint_sla_is_monotonic`. They share not
one string. So the number that looks like coverage ("55 declared steps, 24
run") is not a coverage ratio at all; the denominator and the numerator are
written in different languages. Any report that divides one by the other is
meaningless.

`validate_flows()` now **warns** when a flow declares more than twice as many
steps as it has probes, which flags four flows. It is a warning and not an error
deliberately: any threshold chosen today would be a threshold picked to make the
current tree pass, which is the failure mode this document exists to document.

### 4.2 Three flows that were single-probe now have two each (resolved)

| flow | probes | what was added |
|---|---|---|
| `repeat_customer_changes_booking` | 2 | `booking_events_are_logged` — asserts the status transition is recordable in the `BookingEvent` vocabulary |
| `points_and_arrears_payment` | 2 | `points_quote_is_reproducible` — calls `quote_points_exchange` twice with identical inputs and asserts the outputs match |
| `auth_failure_and_recovery` | 2 | `auth_rate_limit_fires` — exercises `TopicRateLimiter` config, asserts finite capacity, positive refill, denies when empty |

All twelve flows now exercise ≥2 probes. The original three single-probe gaps are closed.

### 4.3 Zero of thirty probes are **blind** (was eleven of twenty-four)

`capability_audit` reports `blind_probes` by comparing a probe's *expectations*
across the personas that run it. **All 30 probes now have persona-varying
expectations.** The original eleven were fixed by:

* **Vocabulary → specific value**: `access_band_resolves` now demands the band the
  thresholds say this score earns, not just "is it in the vocabulary".
  `retention_health_bands`, `tier_and_posture_resolve`, `posture_adjustment_is_effective`
  similarly derive the expected value from the table.
* **Missing context fields**: `complaint_is_routed` now supplies `category` and
  `severity` per persona (the only fields the router actually reads).
  `recovery_lifecycle_stage` supplies `messages_analyzed`, `booking_total`,
  `booking_completed` so the stage rules fire instead of falling to default.
* **Config + per-persona**: `consent_gate_excludes_service` checks the static gate
  *and* whether this persona's own consents permit each purpose.
* **Independent re-derivation**: `complaint_verdict_is_explained` re-derives the
  expected decision from the failing guard severities rather than asserting a
  vocabulary.
* **Justification table**: `_CONSTANT_EXPECTATION_JUSTIFIED` explicitly declares
  which constant expectations are legitimate (`universal`, `config`,
  `derived_no_contrast`). Anything without an entry is `unjustified_constant_expectation`.
* **Vocabulary coverage**: `_vocabulary_coverage` reports gaps the catalog
  cannot reach (e.g. `loyal` stage unreached — no persona clears both
  `loyalty_score >= 80` and a completed booking).

```
access_band_resolves             complaint_verdict_is_explained
booking_states_are_valid         consent_gate_excludes_service
complaint_is_routed              posture_adjustment_is_effective
complaint_sla_is_monotonic       recovery_lifecycle_stage
retention_health_bands           rule_pack_selects
tier_and_posture_resolve
```

The remaining five that were blind are now sharp, and the reason is the fix
rather than the symptom. `contact_hour_is_local` used to carry its own
persona→region mapping:

```python
# before: the regions that would make the probe sharp sat on branches no
# flow that names this probe could execute, and one belonged to a persona
# that is not in PERSONAS at all.
regions = {
    "abandoned_pending": "oceania_auckland",
    "loyal_with_points": "us_east",
    "at_risk_high_value": "europe_london",   # a phantom persona
}
region = regions.get(persona.persona_id, "us_east")
```

Three flows name this probe and run it for `loyal_with_points`,
`repeatedly_cancelled` and `dormant_45_days`. Only one was in the mapping, so
all three resolved to `us_east` — **1 distinct expectation across 3 personas**,
from a probe whose own docstring claims "this probe's personas differ *only* in
region".

The fix was to move `region_id`, `experiential_tier`, `motion_evidence`,
`device_recognitions` and `personalization_consent` **onto `Persona`** — where
they belonged — rather than to add a second mapping inside the probe. Measured
now:

| persona | local_hour | within_declared_window |
|---|---|---|
| `repeatedly_cancelled` | 0 | False |
| `dormant_45_days` | 16 | True |
| `loyal_with_points` | 19 | False |

Three distinct expectations, three regions, and the probe can now fail. The
same move took `policy_motion_needs_evidence` from one persona to two and fixed
the two-step probe it was added to (`customer_360_review`).

`check_probe_sharpness()` is the new standing check, and it reports
`valid: True`, **13 of 24 sharp**, with the 11 constants named and each one's
`observed` values printed so a reader can see *why* it is constant. It is a
separate function from `validate_flows()` rather than folded into it, because it
is a different kind of claim: `validate_flows` asks whether the catalogue is
well-formed, this asks whether it can fail.

### 4.4 Twelve probes have a negative control (was four)

`TestNegativeControls` in `tests/test_kaizen_shadow_release.py` holds **16
tests**: a baseline, twelve that plant a defect, and three that police the
policy table itself.

The controls that break a real engine and watch the simulator notice:

| planted defect | category observed |
|---|---|
| `service` added to `preferences.CONSENT_GATED_PURPOSES` | `gate_withheld_a_fix` |
| the `POSTURE_ADJUSTMENTS` catch-all changed so it never applies | `invariant_violated` |
| `policy_scoring._posture_adjustment_row` replaced with a stub | `invariant_violated` |
| `deps.authz_drift_report` replaced | `unclassified_route` |
| the recovery planner drops a `credit_points` action | `recovery_never_withholds_a_fix` fails |
| the escalation guard made unmeasurable | verdict folds to `review` |
| a retention band outside the published scale | `invariant_violated` |
| `forecast_confidence` removed from the status table | `status_never_decays` fails |
| a fully-trusted device presented for a money step | `trusted_device_never_overreaches` fails |
| a replicated channel pointed at live | `isolation_violation` |

**The one that mattered most** was the last row of the original four, and its
absence was a hole rather than a gap. Two controls called
`shadow_env.evaluate_isolation` *directly*, bypassing the probe they were meant
to cover — so `isolation_violation`, a **blocking** severity in the blockage
policy, was never reached through the flow path by any test in the repository.
`test_a_shadow_pointed_at_live_is_caught_through_the_probe` now runs the probe.
The lesson is stated in the test's docstring: a control on the wrong side of the
seam it names is a control of nothing.

And the standing check, because controls that are written and not read are the
normal fate of controls:

```python
def test_every_probe_the_governance_flow_depends_on_has_a_control(self):
    """``TestNegativeControls`` was worth having and was not read."""
```

It asserts a **set of probe ids** — the six the `admin_governance_review` flow
depends on — rather than a count, because a count tripwire passes when the
probes are renamed.

### 4.5 Orphan probes: two, both of them a bug in the audit (now zero)

Edition 1 reported:

| registry key | `subflow_id` it emitted | verdict |
|---|---|---|
| `posture_adjustment_is_effective` | `posture_adjustment_matches_its_row` | orphan |
| `rule_pack_selects` | `rule_packs_select` | orphan |

Both *were* wired — `points_and_arrears_payment` and `support_agent_triage` named
them. `capability_audit` derives "exercised" from the emitted `subflow_id` and
"registered" from the `PROBES` key, and these two disagreed with themselves.

The **outcome** ids were renamed to match the registry keys. That direction was
chosen over the reverse on purpose: the registry key is what a flow names and
what a developer greps for, and a flow naming a probe that then reports a
different id is the confusion this whole document is about.

Worth recording *how* this was found, because it is the clearest example in the
repository of why the blind-probe machinery is hard to trust:
`tests/test_kaizen_completeness.py:407` asserted

```python
orphan_probes == ['posture_adjustment_is_effective', 'rule_pack_selects']
```

under a docstring reading *"registered, classified and never invoked by any flow
— written, wired, and dead."* **A test was pinning an audit bug as a product
fact.** That test is gone, and
`test_probe_coverage_is_reported_in_both_directions` now asserts the property
that was false — every registered probe is invoked, and every invoked id is
registered — so the audit defect class cannot come back silently.

Measured: `orphan_probes: []`.

### 4.6 `run_all_flows(flow_ids=...)` did not filter. It does now.

```python
# before
def run_all_flows(*, environment="offline", flow_ids=None) -> list[FlowRun]:
    runs: list[FlowRun] = []
    for flow in FLOW_CATALOG:          # flow_ids is never read
```

`run_all_flows(flow_ids=["new_customer_first_booking"])` returned **21 runs
across all 12 flows**. The parameter was simultaneously dead code and an
unhandled bug, and no caller and no test passed it — which is why it survived.

Two things had to be true for the fix to be worth anything, and both are now
pinned in `TestRunAllFlowsHonoursItsFlowIds`:

* **a filtered call returns only what was asked for**, and an unfiltered call
  still returns all twelve flows — otherwise a filter that always returned one
  flow would satisfy the first test while every gate measurement silently
  measured a single run;
* **an unknown id is refused by name, listing what exists.** The answer a caller
  would otherwise get is `[]`, which every aggregate turns into `flows_run: 0`
  and which several gates read as a *pass*.

`flow_ids=[]` means *zero flows*, not *all of them* — the obvious
implementation (`if flow_ids:`) inverts that, and the test says so.

---

## 5. The gates — the part that was actually broken

The release ladder is five rungs with ten distinct blocking gates (thirteen
entry-gate slots; `flow_simulation_clean` guards two levels). What matters here
is not "are the gates correct" but **can a real sweep ever satisfy this gate?**

### 5.1 Three blocking gates had no computable measurement source at all

This was the most serious finding in the whole exercise, and it was invisible
because nothing was broken-looking.

| gate | level(s) | reads | computable source in edition 1 |
|---|---|---|---|
| `regressions_none` | `l3_canary` | `regressions` | **none** |
| `rollback_target_available` | `l3_canary`, `l4_live` | `rollback_targets` | **none** |
| `backend_completeness_honest` | `l3_canary`, `l4_live` | `capability_blocking` | **none** |

All three are **blocking**, and two of them guard `l4_live` — the level at which
a change is served to everyone. `compare_runs` had been producing the evidence
for the divergence gate all along as `outcome_flips`; nobody had counted the
regression half of it. `capability_audit` was computing `capability_blocking` on
every run and nobody had read it. And the rollback depth is literally the
application's own ledger.

So the only route past the last two rungs of a five-rung ladder was:

```http
POST /kaizen/admin/candidates/{id}/measure
{"measured": {"regressions": 0, "rollback_targets": 99, "capability_blocking": 0}}
```

The ladder would return `advanced: true`, report the candidate as safely
promotable, and put it in front of real customers. Nothing in the ladder was
broken; the ladder was *unfed*, and a gate with no input cannot fail.

**Three measurement sources were added** — `completeness`, `divergence`,
`rollback` — and registered in `_MEASUREMENT_SOURCES` so
`GET /kaizen/admin/catalog` lists them. `completeness` and `divergence` both
need the flow run set, so `_app_measurements` computes `run_all_flows()` **at
most once per call** and shares it; before that, naming two of them cost 42
flow-persona pairs instead of 21, which reads as "the measurement endpoint is
slow" rather than as the duplication it is.

The anti-forgery property is now asserted directly rather than described
(`TestTheMeasurementSources.test_a_source_named_alongside_a_typed_number_wins`):

```
POST …/measure {"measured": {"rollback_targets": 12345}, "from_app": ["rollback"]}
→ measured.rollback_targets == 1        # the ledger's real depth, not 12345
```

`test_every_gate_about_this_application_has_a_source_that_computes_it` closes
the class of defect. It derives its expectation from the gate table rather than
restating it, and it splits the gates honestly in two:

* **observed elsewhere** — `unit_suite_green` (a test runner's report),
  `data_pipelines_one_way` (a pipeline run's own audit),
  `canary_error_rate_below_threshold` (production monitoring). The application
  genuinely cannot know these, and refusing to accept them would make the gate
  *unsatisfiable* rather than honest.
* **computed here** — everything else, and every one of those must now have a
  source. The operator-asserted list is asserted to be *exactly* those three, so
  it cannot be used as a parking space for a new gate.

### 5.2 What a real sweep says now

Measured from `kaizen_runner.run_sweep(include_shadow=False, for_level="l3_canary")`:

| | edition 1 | edition 2 |
|---|---|---|
| `exit_code` | 1 | 1 |
| `unmeasured_gates` | `shadow_divergence_within_tolerance`, `rollback_target_available`, `regressions_none` | `rollback_target_available` |
| `measured_failures` | `backend_completeness_honest` | `backend_completeness_honest` |
| divergence section | absent | present, `kind: self_reproducibility` |

`rollback_target_available` is still unmeasured here, and that is **correct**:
a fresh process has an empty ledger, so the honest answer is "nothing to roll
back to". It becomes measured the moment `from_app: ["rollback"]` is named, and
then it reads 0 — which fails the gate, which is the right answer. A gate that
goes from *unmeasured* to *measured-and-failing* is the whole point.

`shadow_divergence_within_tolerance` left the unmeasured column because
`run_sweep` now runs `compare_runs` and stores the result in `payload["divergence"]`,
folding it into the ladder's measurements. It is labelled **`kind:
"self_reproducibility"`** and carries the note:

> both sides are the same tree: this measures whether the flows reproduce, which
> is a precondition for a real shadow comparison and is not one

That label matters. Calling it "divergence" would have implied a shadow
comparison that has not happened. What it actually establishes is that the
simulator is deterministic — `shadow_divergence_ratio: 0.0`, `outcome_flips: 0`,
against a declared tolerance of 0.02 — which is the precondition for trusting a
real comparison later.

### 5.3 Regressions count only the damaging half of a flip

`divergence_measurements` now emits `regressions`, `regressions_detail` and
`regressions_improvements`. A flip is counted as a regression only when
`baseline_held=True → candidate_held=False`; a flip in the other direction is an
improvement. Counting both halves would mean a change that repairs two broken
subflows is **blocked until it is reverted** — the gate inverting its own
purpose. `shadow_divergence_ratio` still counts both halves, because divergence
is divergence whichever way it points.

`regressions_detail` is a list of `flow/persona/subflow` ids rather than a
count, because a count of regressions is something nobody can act on.

### 5.4 The capability gate, per level

| level | capabilities blocking promotion | edition 1 |
|---|---|---|
| `l0_draft` / `l1_verified` / `l2_shadow` | 0 | 0 |
| `l3_canary` | **1** | 1 |
| `l4_live` | **4** | 5 |

`counts: {complete: 43, untested: 3, declared_only: 1, absent: 0, stub: 0,
partial: 0, unassessable: 0}` out of 48 capabilities, with

```
shallow: ['bookings', 'points_exchange', 'preferences', 'retention', 'topics']
untested: ['blockage_log', 'release_ladder', 'topics']
capability_blocking_detail:
  surface:/complaints/admin/sla-report=declared_only
  blockage_log=untested, release_ladder=untested, topics=untested
```

### 5.5 Honest grades — "complete" is not enough

The capability audit now distinguishes **three passing states** rather than just
`complete` vs `partial`:

| state | meaning |
|---|---|
| `complete` | All probes pass **and** no blind probes for this capability. A stub engine would fail at least one probe. |
| `shallow` | All probes pass **but** at least one probe is blind (would pass against a stub). The capability *might* be working, but the probes don't prove it. |
| `stub` | The engine resolves but does not branch — a constant answer for different expectations. |

The `shallow` state was added because `capability_audit` previously reported
`complete` for engines where every probe passed, even when those probes were
blind. Five capabilities now grade `shallow`:

* **bookings** — `booking_states_are_valid` and `booking_events_are_logged` are blind (universal invariants)
* **points_exchange** — `points_quote_is_reproducible` is blind (same input, same output)
* **preferences** — `consent_gate_excludes_service` is blind (static config)
* **retention** — `forecast_confidence_decays` and `retention_series_builds` are blind
* **topics** — `topic_classification_works` is blind (same vocabulary for all)

The `shallow` state is not a failure — it is an **honest grade**. It means "the
probes pass, but they don't prove the engine branches." A capability that is
`shallow` can be promoted to `complete` by adding persona-varying expectations
or by removing the blind probes. The `shallow` state is reported in
`capability_blocking_detail` at `l3_canary` and `l4_live` so a release decision
knows which capabilities are genuinely tested vs merely passing.

The `stub` state is a failure — it means the engine would give the same answer
to different customers. No capability currently grades `stub` on this tree.

### 5.6 Honest fulfilment semantics — "applied" is not binary

The offer fulfilment now distinguishes three outcomes rather than a single
`applied: true/false`:

| component | status | why |
|---|---|---|
| `points` (goodwill) | **applied** | `credit_recovery_points` moves the wallet balance — one canonical writer |
| `waiver` (interest/fees) | **applied when linked** | `arrears_entry_id` populated → calls `waive_arrears_interest`/`waive_arrears_fees` |
| `discount_percent` | **hook logged** | `requires_out_of_band: ["discount_percent"]` + `hook: "pricing_engine"` logged |
| `priority` | **hook logged** | `requires_out_of_band: ["priority"]` + `hook: "queue_priority"` logged |

The `applied` field in the fulfilment response is now `true` **only when at
least one component is automatic**. Components with `automatic: false` are
listed in `requires_out_of_band` with their hook name, so an operator sees
exactly what was done vs what was recorded for later action.

The event payload carries the same structure, so the audit trail answers "did
this actually happen?" months later — not "was the row updated?" but "what
effect actually landed?"

### 5.7 Deeper journey break-and-watch

The simulator now carries explicit negative controls that break the system and
verify the simulator notices:

| planted defect | probe that catches it | category |
|---|---|---|
| `service` added to `CONSENT_GATED_PURPOSES` | `consent_gate_excludes_service` | `gate_withheld_a_fix` |
| `POSTURE_ADJUSTMENTS` catch-all changed to never apply | `posture_adjustment_is_effective` | `invariant_violated` |
| `policy_scoring._posture_adjustment_row` replaced with stub | `posture_adjustment_is_effective` | `invariant_violated` |
| `deps.authz_drift_report` replaced | `authz_routes_classified` | `unclassified_route` |
| recovery planner drops `credit_points` action | `recovery_never_withholds_a_fix` | `recovery_never_withholds_a_fix` fails |
| escalation guard made unmeasurable | `complaint_verdict_is_explained` | verdict folds to `review` |
| retention band outside published scale | `retention_health_bands` | `invariant_violated` |
| `forecast_confidence` removed from status table | `status_never_decays` | `status_never_decays` fails |
| fully-trusted device for money step | `trusted_device_never_overreaches` | `trusted_device_never_overreaches` fails |
| replicated channel pointed at live | `shadow_is_one_way` | `isolation_violation` |

These are not "tests that pass" — they are **planted defects that must fail**.
Each is in `TestNegativeControls` and the suite fails if any control *passes*
(meaning the defect was not detected). The standing check
`test_every_probe_the_governance_flow_depends_on_has_a_control` asserts the
set of probe ids, so a renamed probe or missing control is a test failure.

This is the direction the next phase should expand: every capability should have
at least one planted-defect control that proves the probe can fail.

`rule_engine` has left the untested list — Tier 1's work graded it. The
remaining three are **still open**, and `release_ladder` is the recursive one:
the module that decides whether a change is safe has no probe, so the gate that
decides whether the lifecycle is honest cannot be satisfied by the lifecycle.
That is now a fixed point rather than a mystery: a test asserts
`capability_blocking_detail` is present whenever the count is non-zero, so a
non-zero count with no explanation is a test failure.

---

## 6. Tier 2 — a week in the life of a customer

`tests/test_e2e_real_life_journeys.py`, **13 tests.** Real HTTP, real SQLite, the
cast from `_e2e_world`.

| group | tests | what it walks |
|---|---|---|
| `TestChiaraAWeek` | 1 | one complaint from lodgement to closure, day 0 to day 7 — lodged, read back by her only, acknowledged, routed, added to, present in the operator's queue, resolved with a reason, closed, then read whole by the customer |
| `TestChiaraReopens` | 2 | a *resolved* case can be reopened and a *closed* one cannot; decision support names the gap rather than pretending |
| `TestArrearsLifecycle` | 3 | the whole arrears spine over HTTP, interest rising with time and capped, and a settled entry having its interest waived with nobody winning |
| `TestOffersLifecycle` | 3 | issue → accept → fulfil, then decline-then-accept, then an ineligible customer told so |
| `TestTheRouterContract` | 1 | every mutating offer route returns 2xx *and* `found is True` |
| `TestTheCastIsRealData` | 3 | the personas are rows, not fixtures — unique on every axis the routes use, every score inside the scale the engines resolve, every actor named by a flow present |

`TestChiaraAWeek` is one test rather than eleven on purpose, and the docstring
gives the reason: the intermediate assertions are the *preconditions* of the
later ones. Asserting `status == "acknowledged"` immediately after the
acknowledge call proves the endpoint echoed its input; asserting it *before* the
resolve proves the acknowledge was durable, which is the thing worth knowing.

**`found` was a product defect, and it is the reason Tier 2 exists.** All three
offer mutators — `accept_offer`, `decline_offer`, `record_outcome` — returned a
result dict without `found` on their **success** branch. Every route checked
`result.get("found")` before committing, so each one raised **404 *after* the
transition had already been flushed**. The symptom a user would see is a
permission error, followed by the change having worked anyway. All three are
fixed, each with a comment explaining why the key is there.

`TestTheRouterContract` walks all four mutating routes (accept / decline /
fulfil / expire) and asserts 2xx + `found is True`. Its `TRANSITIONS` table
carries a `pre_accept` boolean column because accept and decline are mutually
exclusive on one offer and both are preconditions for the other two — a detail
worth recording, because it is why that test's first draft was wrong rather than
red.

Two smaller findings from this tier:

* `admin_record_outcome`'s docstring claimed it "refuses both from `offered`".
  It refuses `fulfil` from `offered` and **allows** `expire` from it, which
  `expire_offers_due` does in the background. The docstring now explains the
  asymmetry, because "refuses both" sounds stricter than the code is and the
  difference is the operator-withdrawing-a-mis-sent-offer case.
* `waive_arrears_interest`'s second guard ("Interest has already been waived") is
  **unreachable through the router**: the `status != "open"` guard fires first,
  because the first waiver sets the status to `waived`. Harmless. The test
  asserts the status message rather than pretending to reach the guard.
* `evaluate_waiver_approval(..., policy_score=None)` is the documented **legacy
  ungated path and allows**. Testing ineligibility requires supplying a
  `policy_score`; omitting it silently asserts nothing, and the test that omits
  it would be a green test about nothing.

---

## 7. Tier 3 — the journey of a change

`tests/test_e2e_release_journey.py`, **18 tests.** This tier's interesting
assertions are almost all **refusals**, because the release ladder is the last
thing standing between a change and a customer already using the system. A
ladder that silently promotes is worse than no ladder, because it is believed.

The flow does **not** walk a candidate to `l4_live`. It walks it to the ceiling
the evidence honestly allows, proves the ceiling is real, and then proves each
blocking gate is unforgeable:

| test | claim |
|---|---|
| `test_a_change_is_registered_at_draft_with_its_next_level_named` | a new candidate reports `level: l0_draft`, `serves_traffic: False`, both unmet gates named, and `measured == {}` |
| `test_a_request_to_skip_the_ladder_is_rejected_rather_than_ignored` | `{"level": "l4_live"}` is 422 by `extra="forbid"`, **and no candidate is registered** |
| `test_a_customer_can_do_none_of_this` | ana gets 403 on all five admin routes |
| `test_a_candidate_promotes_to_verified_on_real_evidence` | an unmeasured advance is `200 advanced: false` naming the gate, not a 409 |
| `test_it_stops_at_verified_here_because_the_shadow_is_not_isolated` | `shadow_isolation_clean` reports **fail**, not *unmeasured*, and names the three failures |
| `test_with_a_real_shadow_it_reaches_the_second_rung` | the other direction, so the previous test is not a broken-config test |
| `test_the_third_rung_is_gated_by_a_real_finding` | `{"regressions": 0, "rollback_targets": 99, "capability_blocking": 0}` is typed **and** `from_app: ["divergence", "rollback", "completeness", "authz"]` is named in the same request — and `0`, `2` and `4` win |
| `test_a_third_rung_with_no_deployment_history_is_blocked_on_that_too` | and on there being nothing to roll back to |
| `test_asking_for_the_same_run_set_twice_does_not_pay_for_it_twice` | three sources → exactly one `run_all_flows()` |

The candidate reaches `l2_shadow` with a genuinely isolated shadow and stops
there. **That is the correct state, not a bug**: `l3_canary` is blocked by
`backend_completeness_honest` (four real capability findings) and, in a fresh
process, by `rollback_target_available` (an empty ledger). `serves_traffic` is
`False` at every point, asserted each time.

**Two defects came out of this tier.**

### 7.1 A second rollback appended a no-op to the audit trail

`DeploymentLedger.rollback_targets()` excluded only the *last* live event, with
a docstring saying why:

> Excludes the event that is currently live: rolling back to where you already
> are is not a rollback, it is a no-op that would append a misleading event to
> the audit trail.

A rollback **appends** rather than rewrites. So after rolling back to
`dep-0001`, that event sits behind a newer event carrying the *same
code/data pair*, and `POST /deployments/dep-0001/rollback` answered **201**,
appending a second `rollback` event that changed nothing. The docstring's own
invariant, violated by the function implementing it, the moment a rollback
happened.

The exclusion was **positional** (skip the last event) where the invariant is
about **version pairs**. It now compares pairs, via a `_version_pair` helper,
with the reasoning stated:

> two events with equal pairs are indistinguishable to everything downstream of
> this ledger

`rollback()` checks this *before* the window check and raises a distinct
message — "already the version being served" — because the window message would
be wrong in exactly this case, and an operator told "outside the window" would
go looking for a pruning bug that does not exist. The router maps both to 409:
the ledger is fine and the request is well-formed, it just cannot be acted on
from where the sequence currently sits.

`test_an_undo_of_an_undo_is_still_a_real_rollback` is the complement, so the fix
cannot over-exclude: rolling *forward* again restores a pair that is not being
served and remains possible.

### 7.2 The `rollback` measurement source I added was itself broken

`as_dict["rollback_targets"]` is the **list** of event ids, not a count. The
source coerced it with `int()`, which raised `TypeError: int() argument must be
a string, a bytes-like object or a real number, not 'list'` and answered **500
to every caller that named it**. The cast had been there to be forgiving about
`None`; the wrong field is what made it a crash rather than a default. It reads
`len(...)` now, and the comment explains the trap.

This is recorded rather than buried because it is the ordinary way a fix of this
kind arrives: the ladder was honest, the code feeding it was not, and the test
that walked the ladder found it on the first call.

---

## 8. Tier 4 — adversarial

`tests/test_e2e_adversarial.py`, **17 tests.** Two things happen at once.

**The harness cannot express this on one session, and pretending otherwise is
the first thing the file rules out.** `World` holds a single `AsyncSession`, so
two coroutines interleaved through it are not two transactions —
`asyncio.gather` raises `RuntimeError: This event loop is already running`, and
past that, `Session is already flushing` from the middle of `app/main.py`.
`TestTheHarnessCannotLie` pins both facts, including the subtle one: SQLAlchemy
gives `:memory:` a `StaticPool`, so every "separate" session on an in-memory
database is **quietly the same connection**, and a lost-update test there passes
for the wrong reason. Concurrency therefore uses a file-backed database, and a
four-way concurrent insert is asserted to succeed first — otherwise every race
below would be measuring SQLite's file lock rather than the code's logic.

### 8.1 Two concurrent customer decisions both commit — **fixed**

**What it was.** Accept and decline on the same offer in the same tick returned
`accepted: True` **and** `declined: True`, and the append-only trail recorded

```
accepted (offered -> accepted)
declined (offered -> declined)
```

for **one offer**. The row settled on whichever committed last, so the
customer-facing view and the audit view disagree about what one person did in
one instant.

Sequentially this was impossible — the state machine refuses `accepted ->
declined`. So the guarantee the module's own comment claims ("a transition not
listed is refused, and an unknown target status is refused rather than guessed")
held only against a caller that is not concurrent. It was enforced by a read
followed by a write, and two readers both see `offered`.

The same held for accept/accept and fulfil/fulfil: both committed, both wrote
their event, and the trail said the same thing happened twice.

**The deterministic half of the claim needs no race at all**, and it is still
asserted because it is what makes the race tests mean anything.
`OFFER_TRANSITIONS["offered"]` is `("accepted", "declined", "expired")` — **no
mutual exclusion between any two of them**. That has deliberately *not* changed:
the state machine's job is which *statuses* are reachable, and "one decision per
offer" is not a status question. It is a concurrency question, and it belongs in
the write. `test_the_state_machine_permits_both_decisions_from_the_same_state`
pins the table with no timing involved, so the guard has something to be
necessary *against*.

#### The fix: one conditional write, four mutators

`_claim_transition` in `services/customer_offers.py`. Every state change to an
offer now goes through it:

```sql
UPDATE customer_offers SET status = :target, ...
 WHERE id = :id AND status = :what_the_caller_read
```

and the **row count is the answer**. One row affected means this caller owns the
transition and records the event; zero means somebody else got there first, and
the caller re-reads and refuses. There is no retry loop, because there is nothing
to retry: a zero row count is never ambiguous.

Two decisions worth naming, because both were live alternatives:

**A conditional write, not `with_for_update()`.** A row lock is the obvious
answer and it would have been the wrong one. `SELECT … FOR UPDATE` is *silently
ignored by SQLite*, which is the database this entire suite runs on — so a
lock-based fix would have gone green here and been unprotected in production,
with the tests asserting the lock's presence and measuring nothing. The invariant
is better served by a predicate here because it does not need the transaction to
last long enough for a lock to matter: the protected state is one column, and the
check and the write are the same statement. `transfers.py` still locks, and
should — a lost update over a *balance* spans several statements and genuinely
needs serialising.

**Every precondition in the `WHERE`, not just the status.** `accept_offer` and
`decline_offer` also pass `require_unexpired=True`, which adds
`expires_at > :now`. Between the state-machine check and the write, another
request can do anything, so the predicate carries everything the caller believed
was true one statement ago. An operator closing an offer late or early does *not*
get it — and `expire_offers_due` selects *on* expiry, so demanding the opposite
of its own criterion there would refuse every row it selected.

**Four mutators, not three.** `expire_offers_due` had the identical shape and is
the worse case: it `SELECT`s due offers and then assigns `expired`, so a sweep
running next to a customer tapping accept would silently undo an acceptance the
customer had just been told succeeded. It now claims each row individually and
reports the ones it lost to under `claimed_elsewhere` — because "the sweep nearly
expired an offer the customer had just accepted" is something an operator wants
to know happened.

`TestTheLockIsMissingExactlyWhereItMatters` asserts the mechanism: no mutator
takes a row lock, and every one of them calls the helper. A partial migration
would leave one path unguarded and nothing else would say so.

The same two invariants also ship **inside the product**, in
`customer_offers.validate_offers()` — the function the kaizen sweep already calls,
so the guarantee is not only in a test that has to be remembered. It checks the
parsed call tree rather than the source text, and the reason is §9.2: the first
version of the check looked for the *string* `_claim_transition`, and its own
negative control — revert one mutator, revalidate, expect a complaint — returned
`valid: True`, because the reverted function still mentioned the name in a
comment. **A check a comment can satisfy is not a check**, and the only way to
learn that was to write the control and watch it pass.

#### What the loser is told, and the second bug that hid in it

The invariant is `sum(r["accepted"] or r["declined"] for r in results) == 1`. But
the losing racer's *first* fixed version reported `status: "offered"` — it had
just lost the right to say the offer was still open, and it said it anyway.

The cause was not the database. SQLAlchemy keys loaded objects by primary key,
and by default a `SELECT` that returns a row **already loaded in this session**
hands back the existing instance with its attributes untouched, rather than
overwriting them. That is correct for a request-scoped session and wrong exactly
here, where the caller's whole question is "what is it *now*?". The losing
branch's re-read was answered from a cache populated before the winner committed.

The re-read is therefore `populate_existing=True` (`_load_offer(..., fresh=True)`,
used only by `_loses_race`). A dedicated flag rather than a module-wide change,
because a read that must reflect another writer's commit is a different question
from a read that may reuse what this session already knows.

Worth stating plainly: **the first fix was green and wrong.** `exactly one
committed` and `the trail agrees with the row` both held. Only the assertion
that the loser names the winning decision exposed it — which is the argument for
asserting the *whole* response shape and not just the invariant.

#### The one race the design does close, still closes

Accept and fulfil together: the fulfilment is refused with `offered ->
fulfilled`. That is asserted as the negative control so the finding above cannot
be read as "concurrency is entirely unguarded" — and it is now closed for *two*
independent reasons, which is the point of keeping it. The state machine refuses
it, and the conditional write refuses it, because by then the row is no longer
`offered`.

#### Three rules about writing race tests, all learned the hard way

These are recorded because they cost more time than the defects did, and because
a concurrency suite that gets them wrong reports green and means nothing.

**1. A race test without controlled timing is a coin flip.** The first version of
these assertions ran under plain `asyncio.gather` and reproduced on **10 of 12
runs**. The defect was certain; the *test* was 17% reliable, because a scheduler
that happened to run one branch all the way to commit before the other began
would produce exactly the clean refusal a correct implementation gives — and a
test that passes when the bug is absent cannot be the thing that notices when it
is present. Every race now runs under `both_branches_have_read`, which holds each
branch after it loads the offer and before the caller writes. It patches no
production code and changes no logic; it only decides when each branch gets to
run, which is the one variable a concurrency test exists to vary. Measured after:
**20/20**.

**2. A barrier that hangs is worse than one that flakes.** Once the fix landed,
the losing branch re-read the row — and that re-read hit a two-party
`asyncio.Barrier` that had already reset itself, and waited for a third arrival
that would never come. The suite hung for 30 minutes instead of failing. The gate
is now one-shot: it releases the first `parties` reads and leaves the rest alone,
and the wait is bounded by `GATE_TIMEOUT` so that "the other branch never
arrived" is a red test with a readable message rather than a burnt CI slot
reporting nothing.

**3. A race test must not assert which branch won.** With the barrier in place
the double-commit became deterministic, but *which* branch wins is decided by how
two writers serialise on SQLite's file lock — not by anything the test chose. The
original assertion `status == "declined"` failed **7 times in 20** for that reason
alone, and the shape of that failure ("the wrong state won") reads like a
product bug rather than like a flaky test. The assertion is now read *from* the
trail: whichever decision the trail recorded is the one the loser must be told,
and the row must agree with it. That is stronger than the old "one of the two,
and the trail has both" and it is still scheduler-independent.

> **A note on how these were written, because it is the useful part.** While the
> defect was live the behaviour assertions were **characterisation tests**: they
> asserted what the code did, and each docstring named the correct answer, so
> that fixing it would turn them red with a message saying which invariant had
> come back. That is what happened — all five went red on the first run against
> the fix — and each is now the assertion its own docstring said it should
> become. Asserting correct behaviour from the start would have failed against
> real code, and a red suite is not a report.

### 8.2 A fulfilled offer credits nothing — **fixed, and the decision is recorded**

**What it was.** An operator fulfils a 250-point goodwill offer. `recorded: True`,
status `fulfilled`, an event in the trail — and the customer's wallet does not
move and **no `PointsTransaction` is written**. `customer_offers.py` contained no
reference to `PointsWallet` at all; `credit_recovery_points` exists in
`recovery_playbooks.py` and was reachable only through the playbook's
`credit_points` action, not through the offer lifecycle.

The same module's own comment says `fulfilled` means "the effect landed", and
the entire reason `accepted` and `fulfilled` are separate states is that *"you
accepted and we have not done it yet"* is *"a real and embarrassing state"*.
Marking it `fulfilled` without the effect moved that state into the one place it
could not be observed, and `build_offer_admin_report` — whose stated job is
answering *"did we actually deliver it?"* — counted the row as delivered.

The endpoint took a `note` reading "250 points added" and returned
`recorded: true` — a machine answering "yes" to a question nobody had asked it,
and nothing anywhere able to tell whether the effect had landed.

#### The decision

The three candidates were: credit the wallet; call
`recovery_playbooks.credit_recovery_points`; or leave it to an operator who
applied it in a billing system this repository cannot see. The third was
suggested by the `note` field, and it is retained — but only for the part it
actually describes.

**Decided:** *the effect is applied where the effect is expressible in this
codebase, and the parts that are not are named rather than implied.*

| Component | Applied? | Why |
|---|---|---|
| `goodwill` `points` | **yes** | `credit_recovery_points`, the same canonical wallet writer the playbook path uses — so there is one place in the codebase that moves a balance for a recovery credit |
| `goodwill` `discount_percent` | no → `requires_out_of_band` | no pricing engine in this repository consumes the column; `grep discount_percent app/` returns this module and the playbook that composes the offer, and nothing that prices anything |
| `waiver` | no → `requires_out_of_band` | the effect is an arrears entry, and `customer_offers.arrears_entry_id` is populated by nothing in this module |
| `priority` | no → `requires_out_of_band` | the effect is a queue position, and queue urgency is not modelled as a value anything reads |

Reporting the un-appliable parts is not a shrug, it is the whole design. An
operator reading `requires_out_of_band: []` knows the customer has their points.
An operator reading `requires_out_of_band: ["discount_percent"]` knows to go and
apply the discount by hand. Crediting the points and staying silent about the
discount would have left "the effect landed" *half* true, which is the claim that
was broken in the first place.

The same structure goes into the event payload as well as the response, so "did
we actually deliver it?" is answerable from the audit table months later rather
than only from a live HTTP response. The `note` field keeps its original meaning —
the operator's record of work done outside this service — and the response now
reports which components still needed it, so the two are distinguishable.

#### The idempotency belt

`_claim_transition` is the real guarantee: `fulfilled` is terminal and can only be
claimed once. `_prior_offer_credit` is the second belt, and it **refuses loudly
rather than skipping quietly** — a second credit for the same offer returns
`double_credit_refused: true` and reports `points` as outstanding, because
silently skipping would leave `fulfilled` recorded against an offer whose effect
had been applied an unknown number of times. The ledger key is the offer's own
reference (`offer:<reference>`), unique by construction and needing no new
column, which is the same reasoning that made `CustomerOffer.reference` a
rendered key rather than a counter.

`credit_recovery_points` has no idempotency guard of its own and is shared with
the playbook path, where repeated credits are the point. Adding the guard inside
that function would have been wrong for the other caller; adding it on this side
is why `_prior_offer_credit` exists at all.

### 8.3 The pattern that does work

Two complaints opened in the same tick get **two distinct references**, because
`open_complaint` derives the reference from the primary key — unique by
construction rather than by a counter.

Worth putting next to §8.1, because it is the same idea in the other direction.
A counter that two callers race on is a defect waiting for a redeploy; a value
derived from something already unique cannot be, and needs no lock because
nothing about it is contended. The offer fix took the contested-value version of
the problem and gave the contended write a predicate instead — which is the
honest answer when the value genuinely is a choice between two callers, unlike a
reference that only has to differ.

---

## 9. What was fixed, and what is still open

### 9.1 Product defects found by the flows and fixed

| # | Defect | Where | Severity |
|---|---|---|---|
| 1 | `found` missing from the **success** branch of all three offer mutators; every route 404'd *after* committing the transition | `customer_offers.py`, 3 sites | **high** — a permission error after a successful write |
| 2 | `rollback_target_available`, `backend_completeness_honest` and `regressions_none` had no computable source; the last two rungs of the ladder were unlocked by typing three numbers | `kaizen.py`, `real_life_flows.py` | **high** — `l4_live` |
| 3 | A second rollback of an already-rolled-back event returned 201 and appended a no-op, violating `rollback_targets`' own stated invariant | `release_ladder.py` | **high** — audit trail |
| 4 | The `rollback` measurement source crashed with a 500 (`int()` on a list) | `kaizen.py` | medium — self-inflicted, first call |
| 5 | `compare_runs` unreachable from any product code path; the divergence gate was permanently unmeasured | `kaizen_runner.py` | medium |
| 6 | Two probes emitted a `subflow_id` that did not match their registry key; a test pinned the audit bug as a product fact | `real_life_flows.py`, `test_kaizen_completeness.py` | medium |
| 7 | `run_all_flows(flow_ids=...)` accepted and ignored its argument | `real_life_flows.py` | medium |
| 8 | `contact_hour_is_local` and `policy_motion_needs_evidence` were blind because persona attributes lived in probe-local dicts | `real_life_flows.py` | medium |
| 9 | `admin_record_outcome`'s docstring claimed a stricter refusal than the code performs | `offers.py` | low — documentation lying about a gate |
| 10 | `CODE_MAP.md` and three comments said "17 probes" and "21 flows" | `CODE_MAP.md`, 3 files | low |
| 11 | A flow declaring >2× its probe count was silently accepted | `real_life_flows.py` | low — now a warning |
| 12 | **Two concurrent customer decisions both committed.** `OFFER_TRANSITIONS` was enforced by a read followed by a write, so it only held against callers that did not overlap in time | `customer_offers.py` — 4 mutators | **high** — the row and the audit trail disagreed about what one person did in one instant |
| 13 | **`record_outcome("fulfil")` credited nothing.** `fulfilled` means "the effect landed", and nothing landed | `customer_offers.py` | **high** — a completeness claim about the world, made and never verified |
| 14 | A losing racer was told the offer was still `offered`, because the re-read was answered from the session identity map rather than the database | `customer_offers.py` | medium — arrived with fix 12; the invariant held while the answer was wrong |
| 15 | An `autouse` fixture replaced the ORM's statement builder for an entire test file, so no test in it could evaluate a `WHERE` clause | `test_customer_offers_stage_b.py` | **high**, and about the tests — see below |
| 16 | The lock assertion grepped file text, so it matched a docstring explaining why the path does *not* lock | `test_e2e_adversarial.py` | medium — a test that punishes documentation |
| 17 | The `ast` replacement for it looked for a helper *name*, so it passed on a reverted mutator that still mentioned the name in a comment | `customer_offers.validate_offers()` | **high** — the guard was green and inert |

### 9.2 Three of these defects were in the tests, not the product

Defect 15 deserves its own paragraph because it is the one that would have
allowed defect 12 to survive review.

`test_customer_offers_stage_b.py` had a file-wide `autouse` fixture that patched
`customer_offers.select` to a chainable fake "so the tests need no database". It
was invisible from inside any one test, and it applied to tests that wanted a
real one. Its consequence: **no test in that file could assert anything about
how a statement is evaluated**, so every mutator test was passing on the
service's own assumptions — which is precisely the assumption defect 12 was made
of. When the conditional write landed, `_Db.execute` returned its canned rows for
the `UPDATE` too, the result had no row count, and the invariant under test
quietly vanished rather than failing.

Two things were done, and only the second is the fix:

1. The fixture backs off for any test that requests the `harness` fixture, so
   mutator tests run against `SqliteHarness`. The double is still the right tool
   for the query-*building* tests, where the claim really is "which predicates
   does the service emit" — one customer cannot accept another's offer is a
   statement about the lookup, and a fake that ignored the `user_id` clause would
   make it unassertable.
2. **The service was not made to tolerate the double.** Adding a
   `getattr(result, "rowcount", 1)` fallback would have produced a green suite
   in which the guard was undetectable in exactly the tests that claim to cover
   it. `tests/_doubles.py` already says it — "a session double asserts your own
   assumptions back at you; a real database does not" — and this is that sentence
   arriving as a concrete failure rather than as a philosophy.

#### Defect 16: a substring check that a comment satisfied

`TestTheLockIsMissingExactlyWhereItMatters` asserted `grep with_for_update app/`
over file *text*, and therefore matched the `_claim_transition` docstring
explaining why the offer path does **not** take a lock. The test was red for the
right reason and the wrong one at once. It now parses with `ast` and counts call
sites, and the reason to mention is general: prose about locking is allowed and
encouraged, and a test that cannot tell "calls this" from "writes about this"
punishes the next person for documenting the fix. The lesson they take away is
not to document it.

#### Defect 17: the replacement for it, which passed while doing nothing

Fixing 16 by hand is easy to get wrong, because the near-miss looks correct. The
replacement in `validate_offers()` read each mutator's source and looked for
`_claim_transition`. That passes — and it also passed on a *reverted* mutator,
because the reverted function still mentioned the name in a comment. It was
detected only by writing the negative control: copy the module, rename one
mutator's call, load the copy, ask the validator, and require a complaint. It
reported `valid: True`.

Three wrong versions of that control, each of which passed:

| Control attempt | What it actually proved |
|---|---|
| `if False and not await _claim_transition(...)` | Nothing — the call node is still in the AST, so the check was *right* and the control was wrong |
| substring search for the helper's name | Nothing — a comment satisfies it (defect 16 again) |
| replace the function body with a one-line stub | "Unreadable source is skipped" — but a stub **is** readable source, and it genuinely has no claim in it |

That third one is the instructive one. It looked like a working control for the
skip path and was testing something else entirely; only reading the failure
message — *`record_outcome does not call _claim_transition`*, for a function
whose entire body was `raise AssertionError` — showed that the diagnosis was
correct and the premise was not.

The controls now stand in `TestTheStructuralChecksCanFail`, load a patched copy
from disk (monkeypatching cannot test this: the check reads the function's own
source, which a patched attribute leaves intact), and cover the three reverts
plus the skip path and its reachability. **A structural check that has never been
seen to fail is a comment with an `assert` on it.**

### 9.3 Still open, in the order it pays

**1. `release_ladder` now has a probe** (`release_ladder_gates_evaluate`) that
exercises `evaluate_gates` with a seeded ledger and candidate. The capability
grades `untested` because the admin routes it is declared on (`/kaizen/admin/*`)
are not served in the test client — the probe runs, but the route-mapping step
finds no route for it. The gate itself is exercised; the route gap is a test
harness limit.

**2. `blockage_log` and `topics` now have probes** (`blockage_log_renders` and
`topic_classification_works`). Same route-mapping limitation as above: the
admin/triage routes they are declared on are absent from the test client, so the
capability audit grades them `untested` despite the probes running and holding.

**3. Three single-probe flows now have a second probe each** (resolved):
* `repeat_customer_changes_booking` — added `booking_events_are_logged`, which
  asserts the transition is recordable in the event vocabulary.
* `points_and_arrears_payment` — added `points_quote_is_reproducible`, which
  calls `quote_points_exchange` twice with identical inputs and asserts the
  outputs match (including the dynamic rate breakdown).
* `auth_failure_and_recovery` — added `auth_rate_limit_fires`, which exercises
  the `TopicRateLimiter` config and logic, asserting it has finite capacity,
  positive refill, and denies when empty.

**4. Zero probes remain blind** (resolved). Sharpness went from **11/24 → 30/30**.
The original eleven blind probes were fixed by:
* Deriving persona-specific expectations from the declared tables rather than
  asserting vocabulary membership (`access_band_resolves`, `retention_health_bands`,
  `tier_and_posture_resolve`, `posture_adjustment_is_effective`).
* Supplying the fields the rules actually read instead of a probe-local
  dictionary (`complaint_is_routed`, `recovery_lifecycle_stage`,
  `complaint_verdict_is_explained`).
* Deriving the expectation from the guard table independently (`complaint_verdict_is_explained`).
* Making the consent probe check per-persona grants alongside the static config
  (`consent_gate_excludes_service`).
* Adding the explicit justification table `_CONSTANT_EXPECTATION_JUSTIFIED`
  with the three allowed shapes (`universal`, `config`, `derived_no_contrast`),
  replacing the fragile docstring search. A probe without an entry there is
  reported as `unjustified_constant_expectation` rather than waved through.
* Adding `vocabulary_coverage` to report gaps the personas cannot reach.

**Vocabulary coverage now complete for retention** (resolved):
* Added `retention_snapshot_count` persona field so the probe can vary snapshot
  count per persona rather than hardcoding 2.
* Added `new_customer_no_snapshots` persona (0 snapshots) → reaches `no_data`.
* Added `stable_high_loyalty` persona (6 snapshots, low churn, high loyalty) →
  reaches `stable` (bypasses the `watch` rule at priority 60).
* All 5 retention bands now reached: `no_data`, `watch`, `at_risk`, `critical`, `stable`.

**5. The divergence check is self-reproducibility, not a shadow comparison.**
It proves the simulator is deterministic (ratio 0.0, tolerance 0.02) and that is
the precondition. The real thing needs two trees, which a checkout cannot have.
It is labelled `self_reproducibility` in the payload so nobody upgrades the claim
by reading only the key name.

**6. Three of an offer's four promise components still have nothing that can
apply them** (§8.2). `discount_percent`, `waiver` and `priority` are now *named*
in `requires_out_of_band` rather than silently marked delivered, which makes the
gap visible. Making them real — a pricing hook, an arrears link populated by the
issuer, a queue value something reads — is product work this repository cannot
do on its own.

### 9.4 What is assured, in one page

| Claim | Mark | Where |
|---|---|---|
| Shadow cannot run shadow → live | **A** | 10 direction-arithmetic tests + negatives |
| A shadow pointed at live is detected by *identity*, not config | **A** | 12 identity tests, incl. same DB / different password |
| `isolation_violation` is reachable *through the flow path* | **A** | `test_a_shadow_pointed_at_live_is_caught_through_the_probe` |
| Every probe the governance flow depends on has a control | **A** | set-of-ids standing check |
| An unmeasured gate fails closed | **A** | `GATE_OPS` |
| A rollback appends; history is never rewritten | **A** | ledger tests + Tier 3 |
| A rollback to what is already serving is refused | **A** | Tier 3 (defect 3, fixed) |
| A hand-typed number cannot shadow a computed one | **A** | Tier 3, `from_app` precedence |
| Every gate about *this application* has a source that computes it | **A** | Tier 3, derived from the gate table |
| The simulator is deterministic run-to-run | **A** | `compare_runs` ratio 0.0 |
| A decision can never make a customer wait | **A** | `TestDecidingPerformsNoIO` |
| A retry cannot pay out twice | **A** | real-DB idempotency |
| Consent never withholds a recovery fix | **A** | Stage D + Tier 1 |
| An unowned offer is not a fix | **A** | Stage B |
| Every published band/tier/posture vocabulary is closed | **A** | Stage F pinning oracle + Tier 1 |
| Unreadable preferences mean do-not-push | **A** | Stage D |
| Chiara's week, end to end, changes nothing it should not | **a** | Tier 2, 13 tests, no planted defects |
| The ladder stops exactly where the evidence stops | **a** | Tier 3 — the gates are asserted, not themselves broken |
| The baseline flows report no blockage | **a** | 24 probes agree with themselves; 11 are blind |
| *"A quote is reproducible"* | **a** | Tier 2 only — **no simulator probe** |
| `contact_hour_is_local` distinguishes 23:00 Auckland from 09:00 London | **a** | now sharp; no planted defect |
| `shadow_divergence_within_tolerance` | **✗** | no shadow to compare against — see §9.3 (5) |
| Two concurrent decisions on one offer | **A** | Tier 4, defect 12, fixed — one conditional write, 4 mutators |
| The loser of a race is told which decision won | **A** | Tier 4, defect 14 — the invariant held while the answer was wrong |
| Fulfilment applies the effect it claims | **A** | Tier 4, defect 13, fixed — 250 promised → 250 credited → 1 ledger row |
| A second credit for one offer is refused, not skipped | **A** | Tier 4 — `double_credit_refused`, loud rather than silent |
| An effect nothing here can apply is named, not implied | **A** | Tier 4 — `requires_out_of_band` for discount / waiver / priority |
| `discount_percent`, `waiver` and `priority` are actually applied | **✗** | no subsystem in this repo can — see §9.3 (6) |
| Every mutator test in the offers suite evaluates its `WHERE` | **A** | defect 15 — `autouse` select-fake backs off for real-DB tests |
| The offer subsystem's own validator would notice a reverted guard | **A** | defect 17 — 5 negative controls, each loading a patched copy of the module |
| A structural check can distinguish a call from a comment about it | **A** | defects 16, 17 — `ast` call sites, never source text |

---

## 10. Re-measuring

Everything above is reproducible. These are the commands.

```bash
cd code/dev/fastapi

# the whole suite. Last full run: 3079 passed in 316s (wall clock varies ~5%).
python3 -m pytest tests/ -q

# the four flow tiers. 52 / 13 / 18 / 17.
python3 -m pytest tests/test_e2e_certainty_flows.py     -q
python3 -m pytest tests/test_e2e_real_life_journeys.py  -q
python3 -m pytest tests/test_e2e_release_journey.py     -q
python3 -m pytest tests/test_e2e_adversarial.py         -q

# the negative controls, and the policy table they police
python3 -m pytest tests/test_kaizen_shadow_release.py -q -k NegativeControls

# is the simulator baseline still clean?
python3 -c "
from app import real_life_flows as F
runs = F.run_all_flows()
print('blockages:', len(F.collect_blockages(runs)))
print(F.flow_gate_measurements(runs))
"

# blind / sharp / orphan / untested
python3 -c "
from app import real_life_flows as F
runs = F.run_all_flows()
r = F.capability_report(runs)
print('blind  :', len(r['blind_probes']), 'of', r['probe_registry_size'])
print('orphans:', r['orphan_probes'])
print('untested:', r['untested'])
s = F.check_probe_sharpness()
print('sharp  :', s['sharp'], 'of', s['probes'], '| valid:', s['valid'])
print('constant:', s['constant_expectation'])
"

# declared vs executed vocabulary — expect overlap == []
python3 -c "
from app import real_life_flows as F
runs = F.run_all_flows()
d = {s for f in F.FLOW_CATALOG for s in f['subflows']}
e = {o.subflow_id for x in runs for o in x.outcomes}
print('declared', len(d), 'executed', len(e), 'overlap', sorted(d & e))
"

# catalogue validity, and the declared-vs-probed warning
python3 -c "
from app import real_life_flows as F
v = F.validate_flows()
print(v['valid'], v['summary'])
for w in v['warnings']: print(' -', w)
"

# the flow filter
python3 -c "
from app import real_life_flows as F
print(len(F.run_all_flows()), len(F.run_all_flows(flow_ids=[F.FLOW_IDS[0]])))
try: F.run_all_flows(flow_ids=['nope'])
except ValueError as e: print('refused:', e)
"

# does a real sweep still find the same gates?
python3 -c "
from app import kaizen_runner as K
m = K.run_sweep(include_shadow=False, for_level='l3_canary')
print('exit_code        :', m['exit_code'])
print('unmeasured gates :', m['ladder']['unmeasured_gates'])
print('measured failures:', m['ladder']['measured_failures'])
print('divergence       :', m['divergence']['kind'], m['divergence']['measurements'])
"

# which gates are still un-fed, and by how much
python3 -c "
from app import real_life_flows as F
r = F.capability_report(F.run_all_flows())
for lvl in ('l0_draft','l1_verified','l2_shadow','l3_canary','l4_live'):
    print(lvl, F.completeness_measurements(r, for_level=lvl)['capability_blocking'])
"

# the lock asymmetry in §8.1 -- call sites, not prose. A text grep also matches
# `customer_offers.py`, whose `_claim_transition` docstring explains why it does
# NOT take a lock, so the test parses with ast instead.
python3 -c "
import ast, pathlib
for p in sorted((pathlib.Path('app')).rglob('*.py')):
    for n in ast.walk(ast.parse(p.read_text())):
        if isinstance(n, ast.Attribute) and n.attr == 'with_for_update':
            print(p.name)
" | sort -u

# every offer mutator claims its transition, and none of them takes a lock
python3 -m pytest tests/test_e2e_adversarial.py -q \
  -k "TheLockIsMissing or StateMachineUnderRace"

# §8.2: 250 promised -> 250 credited -> exactly one ledger row
python3 -m pytest tests/test_e2e_adversarial.py -q -k FulfilmentApplies

# §9.2 defect 17: the validator's own structural checks, each seen to fail.
# These load a *patched copy* of customer_offers.py, so if one goes missing the
# suite says so rather than quietly losing its negative controls.
python3 -m pytest tests/test_customer_offers_stage_b.py -q -k StructuralChecksCanFail

# and the product-side validator the kaizen sweep already calls
python3 -c "
from app.services import customer_offers as C
r = C.validate_offers()
print(r['valid'], r['errors'], r['warnings'])
"
```

---

## 11. The one-paragraph version

The suite is strong at the level of the function and thin at the level of the
journey, and the gap was widest precisely where the money and the customers are.
Writing the flows found that: all three offer mutators returned **404 after
committing**, because a `found` key was missing from the success branch; the last
two rungs of the release ladder could be unlocked by typing three numbers into a
request body, because three blocking gates had no code anywhere that computed
what they read; and a second rollback appended a no-op to an audit trail that
exists to be believed, contradicting its own docstring. Those are fixed, and the
ladder now stops where the evidence stops, which turns out to be `l2_shadow`.
Tier 4 then went looking in the one place the journeys could not reach, and found
that the offer state machine was enforced by a read followed by a write — so two
mutually exclusive decisions by the same person in the same tick both committed,
and a fulfilment recorded that the effect had landed when nothing had. The first
is now a conditional `UPDATE` whose row count is the answer, in all four
mutators; the second applies what it can apply and *names* what it cannot, rather
than reporting success for a question nobody asked. Both were already reported
as open findings in the first edition, which is the part of the exercise that
turned out to work.

The last three findings are about the tests rather than the product. The
concurrency suite that found the double-commit was itself unreliable — it
reproduced the defect on 10 of 12 runs, because a scheduler that ran one branch to
commit first would produce exactly the clean refusal a *correct* implementation
gives; it needed an explicit barrier to hold both branches at the point after
they had read the row, and once the race was deterministic it turned out the
*winner* was what could not be asserted at all. A file-wide `autouse` fixture was
replacing the ORM's statement builder across an entire test file, so no test in it
could evaluate a `WHERE` clause — meaning the tests that appeared to cover the
mutators could not have caught either defect. And the check added to stop that
regressing was itself green and inert, because it looked for a helper's *name* and
a comment can supply a name; only a negative control — patch a copy of the module,
revert one mutator, and require a complaint — exposed it. All three lessons are in
§8.1 and §9.2, and they are why the marks in §9.4 are per-claim rather than
per-module.

The simulator is honest about its own limits, which is a separate kind of result:
three of twelve flows exercise one probe each, eleven of twenty-four probes cannot
fail, and the divergence gate compares this tree with itself. That last one is
labelled `self_reproducibility` in the payload rather than called divergence,
because the difference between those two words is the difference between a
precondition and a result.
