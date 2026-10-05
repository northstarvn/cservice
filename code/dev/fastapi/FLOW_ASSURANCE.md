# Flow assurance: what the flows actually prove, and what they only claim

*Working note, second edition. Every number was re-measured against the tree on
4 October 2026 after the fixes in [§9](#9-what-was-fixed-and-what-is-still-open);
the commands to re-measure are in [§10](#10-re-measuring).*

**What changed between editions, in one line:** the suite went from 2892 tests
to **3131**, of which **108** are new end-to-end flow tests across four tiers.
Twenty-one defects are fixed, ten of them high severity — among them a customer who
saw a permission error immediately after a successful write, a release ladder
whose last two rungs could be unlocked by typing three numbers, an offer state
machine that let one person accept and decline the same offer in the same instant,
and a sentence telling a customer their discount had been applied while the same
response admitted that nothing had applied it. That last one is the interesting
case: it was found, fixed, and came back in the tense the fix introduced, because
its own negative control was satisfied by the defect it was written to catch.
Two items are still open, and they are open for two different reasons:
one is **blocked on infrastructure this repository does not have**, and one is **a
product decision whose reporting and copy halves are already delivered**. The third
item (journey vocabulary) is now closed via a traceability table enforced by
`validate_flows`.

| | Edition 1 | Edition 2 |
|---|---|---|
| Suite | 2892 passing | **3131 passing** |
| End-to-end flow tests | 0 | **108** (52 / 18 / 18 / 20 by tier) |
| Blind probes | 16 of 24 | **4 of 30** |
| Probes with a planted-defect control | 4 | **14** |
| Orphan probes | 2 | **0** |
| Gates with no computable measurement source | 3 | **0** |
| Gates unmeasured at `l3_canary` in a real sweep | 3 | **1** (shadow divergence) |

---

## 1. Why this document exists

This codebase has three different things called "a flow", and they get confused
with each other often enough to be worth separating before anything else.

| | What it is | Where it lives | What a green result means |
|---|---|---|---|
| **A stage flow** | a named business journey, described in prose | `BLOCKAGES.md`, stage files `test_stage_*.py` | one developer's claim about one subsystem, checked by one test file |
| **A simulator flow** | a journey with probes attached to live engines | `app/real_life_flows.py` | 30 probes agreed with their own expectations; 24 of those expectations vary by customer, 6 do not (§4.3) |
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

12 flows × 7 personas = **26 flow/persona pairs**, 30 probes, **104 subflow
executions**, **0 blockages**, 0 failures, 0 deferrals. Every number below is
`run_all_flows()` on this tree. (Pairs are not the cross product — each flow names
the personas it actually runs against, so the count is the sum of the catalog's
`personas` lists, not 12×7.)

### 4.1 The declared journey and the executed journey are different vocabularies

| | |
|---|---|
| Distinct `subflows` names declared across the catalog | **55** |
| Distinct subflow ids actually produced by running every flow | **30** |
| **Overlap between the two sets** | **0** |

The declared names are the human journey — `register`, `authenticate`,
`create_booking`, `event_log`, `plan_playbook`. The executed names are probe
outcomes — `access_band_resolves`, `complaint_sla_is_monotonic`. They share not
one string. So the number that looks like coverage ("55 declared steps, 30
run") is not a coverage ratio at all; the denominator and the numerator are
written in different languages. Any report that divides one by the other is
meaningless — and note that the executed side grew from 24 to 30 while the
declared side did not move, so the "ratio" changed without any journey step being
covered. That is the clearest available demonstration that the quotient measures
the probe registry, not the product.

The gap is real and worth naming, but it is not closed by making the strings
match. A `subflows` entry like `create_booking` names an intent; renaming a probe
`create_booking` to raise the overlap to 100% would make the number look better
while making the probe worse, since the probe id is also what a finding is filed
against and what `_blind_probes` compares across personas. The honest statement is
the table above: the two vocabularies have not been reconciled, and the human
journey steps are covered *indirectly*, by probes each of which names the parts
it exercises.

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

### 4.3 Four of thirty probes are **blind**, all with an argument for it (was eleven of twenty-four)

`capability_audit` reports `blind_probes` by comparing a probe's *expectations*
across the personas that run it: a probe is blind when every persona expected the
same thing, so an engine that returned a constant would satisfy it.

**Twenty-six of thirty now have persona-varying expectations**, up from thirteen
of twenty-four. The original eleven were fixed by, plus the two retention probes:

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
  `derived_no_contrast`). Anything without an entry is `unjustified_constant_expectation`,
  and an entry whose premise has stopped being true is reported as `stale_justification`
  rather than left to rot.
* **Vocabulary coverage**: `_vocabulary_coverage` separately reports rungs the
  persona catalog cannot reach at all. It found one — the `loyal` recovery stage,
  unreachable because no persona both completed a booking and cleared
  `loyalty_score >= 80` — and adding a persona closed it. That is the right way to
  close that gap; see the note on manufactured contrast below.

The four still blind, all justified:

| probe | justified? | argument |
|---|---|---|
| `booking_states_are_valid` | yes | "every booking state is a real state" is a universal invariant |
| `booking_events_are_logged` | yes | same, for events |
| `points_quote_is_reproducible` | yes | "same request, same quote" is one invariant, not one per customer |
| `consent_gate_excludes_service` | yes | the gate half is module config; the per-persona half is what varies |

So `sharp` reads **30 of 30**. The two previously unjustified probes
(`forecast_confidence_decays`, `retention_series_builds`) now have persona-varying
expectations and are sharp. The `retention` capability moves from `shallow` to
`complete`. "Sharp" here does not mean "good": it means "can this probe fail at
all", and a justified universal invariant can fail — just not along the persona
axis.

**And a caution this document has already paid for once.** Three of the four
`complete` grades in the left column were reached, in an earlier draft, by carrying
the probe's own derived inputs into its `expected`. That produced three distinct
expectations and one identical answer — a *false* `stub` finding, which is worse
than no finding because it puts a non-finding in front of a reader with no way to
tell it from a real one. Carrying an input is only honest when it changes the
answer. Every remaining `shallow` grade is left in place rather than closed that
way, and §5.5 records what each one would take to close honestly.

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

### 4.4 Fourteen probes have a negative control (was four)

`TestNegativeControls` in `tests/test_kaizen_shadow_release.py` holds **21
tests**: a baseline, seventeen that plant a defect, and three that police the
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
| `evaluate_gates` returns one gate fewer than it promised | `nothing_dropped` check fails |
| `evaluate_gates` reports `safe: true` over a failing blocking gate | `safe_follows_from_the_gates` fails |
| `render_blockages_markdown` loses its closing section | `missing_sections` non-empty |
| `render_blockages_markdown` says "nothing ran" *and* "all held" | `claims_success_without_a_sweep` |

The last four are §5.9. Three of the eight attempts at writing them **did not work**
when first written, and all three looked correct while failing to pin anything.

**The count above is about probes, and there are three controls it does not count**,
because what they break is not an engine. BW-4, BW-5 and BW-6 (§5.10) assert that the
*report* of an offer tells the customer and the operator the same thing — which is
the layer defect 23 lived in, and which a table of probe controls cannot see,
because every row above plants a defect in a module and requires the simulator to
notice. Counting them here would have been the convenient answer and the wrong one.

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

It asserts a **set of probe ids** — the eight the `admin_governance_review` flow
depends on — rather than a count, because a count tripwire passes when the
probes are renamed. Two of those eight were added *to* that set when the check
failed on them, so the set is not decoration: it grew by the only mechanism that
could legitimately grow it, which is a new control.

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
shallow: ['bookings', 'points_exchange', 'preferences', 'retention']
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
blind. Four capabilities grade `shallow` on this tree, and the grade is the
point — it is the honest answer, not a failure to be tidied away:

| capability | blind probes | why they are hard |
|---|---|---|
| **bookings** | `booking_states_are_valid`, `booking_events_are_logged` | both assert vocabulary membership — a universal invariant |
| **points_exchange** | `points_quote_is_reproducible` | "same request, same quote" is one invariant, not one per customer |
| **preferences** | `consent_gate_excludes_service` | the gate half is static config; the grant half is per-persona |
| **retention** | `forecast_confidence_decays`, `retention_series_builds` | monotonicity and vocabulary claims, no per-persona number |

`topics` grades `complete` — `topic_classification_works` was sharpened to assert
persona-specific expected themes derived from each persona's context.

**The `shallow` grade is deliberately left in place for the four above.** It would
be easy to close them by adding a persona-varying expectation to each blind probe
and watching the grade move. That is the dishonest move, and this repository has
already paid for it once: `recovery_lifecycle_stage` carried a derived input in
its `expected` for a while, which produced three distinct expectations and one
identical answer — a *false* stub finding, which is worse than no finding, because
it puts a non-finding in front of a reader with no way to tell. Carrying an input
into an expectation is only honest when it changes the answer.

For each of the six probes there is a real sharpening available, and it is worth
writing down rather than leaving as an impression:

* **`forecast_confidence_decays`** — `retention.forecast_confidence` is a closed
  form over published config terms (`confidence_base`,
  `confidence_per_horizon_day`, floor). The expectation can be re-implemented
  from those terms using `persona.retention_snapshot_count` as the evidence input,
  which makes it vary per persona *and* catches an implementation that drops a
  term. `build_retention_forecast` also refuses below `min_points` and says so in
  `sufficient_data`, which is genuinely branching: `new_customer_no_snapshots`
  (0) must be told "not knowable" and `stable_high_loyalty` (6) must get a number.
* **`retention_series_builds`** — same lever, via the snapshot count.
* **`booking_states_are_valid`** — the sharp version is not "are these states real"
  but "does the system report exactly this customer's history", which differs for a
  persona with two bookings, one, and none.
* **`points_quote_is_reproducible`** — reproducibility is universal, but the
  *quote* is not: a persona with 500 points and one with 50 should not be quoted
  the same redeemable amount. Assert reproducibility **and** that the quote tracks
  the persona's balance.
* **`consent_gate_excludes_service`** — the expected set of permitted purposes is
  derived from that persona's consents, so it genuinely differs between a persona
  with analytics-only and one with marketing plus analytics.
* **`booking_events_are_logged`** — the set of legal *next* states depends on the
  persona's current state, so a persona in `completed` and one in `cancelled` have
  different reachable transitions.

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

**This table was the operator's half of the answer, and for a while it was the only
half.** The customer-facing sentence claimed the same two rows in the past tense
from every status — "we put 250 points on your account and took 15% off your next
service" — so the row above and the sentence below it disagreed in the same
response. §5.10 is that defect, and the table here is unchanged by it: what changed
is that the customer's copy is now derived from the same
`_components_this_service_applies` that produces the two "hook logged" rows.

#### 5.6.1 A claim must not outlive the effect it was made for

**This is the finding that changed what `fulfilled` means, and it was found by
`TestBreakAndWatch`, not by reading the code.**

`record_outcome` claims the `fulfilled` status *before* it applies the effect,
deliberately: `_apply_offer_effect` moves a wallet balance, so the claim is what
stops two racers from both crediting. Reversing the order reintroduces a double
credit, so the ordering is right.

But nothing undid the claim if the effect then failed. The exception propagated
past the caller's `commit()`/`rollback()`, so the transaction was never resolved
explicitly — and then **the next query on that session autoflushed the dirty
row**. The result: HTTP 500, and a persisted offer reading `fulfilled`, with
`OFFER_EXPLANATIONS` telling the customer *"We put 250 points on your account"*
and no points on the account. The claim outlived the effect it was made for,
which is the one thing the ordering existed to make impossible.

The fix keeps the ordering and adds the missing rollback, so a claim no longer
survives its own failure:

```python
try:
    effect = (
        await _apply_offer_effect(db, row, now=moment)
        if target == "fulfilled"
        else {"applied": False, "components": {}, "requires_out_of_band": []}
    )
except Exception:
    await db.rollback()
    raise
```

**A second version of this fix was written first and was wrong**, which is worth
recording because it is the more tempting one. It caught the exception *inside*
`_apply_offer_effect` and returned `{"applied": False, "why": "points service
unavailable"}`. That reports a real bug as a service outage, and it returns
`applied: False` underneath a status of `fulfilled` — the tidy-payload version of
the same defect, with the failure dressed up as a handled condition instead of an
abort. Letting it propagate and rolling the claim back is the honest behaviour;
the error handler is where the dishonesty went.

The invariant, which is what the tests assert rather than a status code:

* a wallet balance either moved by the full amount or did not move at all;
* an offer is either `fulfilled` with its effect applied, or still `accepted`.

Both directions were verified by reverting each fix and watching the test fail
on a different assertion — `assert 'fulfilled' == 'accepted'` for the missing
rollback, `DID NOT RAISE` for the catch-all. See §5.8.

### 5.7 Deeper journey break-and-watch

`TestNegativeControls` in `tests/test_kaizen_shadow_release.py` plants defects in
the engines and requires the **simulator** to notice them — the seam is crossed in
that direction, so nothing here can pass by calling the engine directly:

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

Five more were added after the standing check failed on two probes with no control;
they are in §5.9, because three of the eight attempts at writing them did not work.
§4.4 has the full table.

**Three more, in a different place entirely.** The controls above all break an engine
and require the simulator to notice. Defect 23 (§5.10) was in the *reporting* layer,
which nothing in that table plants a defect in — so BW-4, BW-5 and BW-6
(`tests/test_e2e_real_life_journeys.py`) assert the reporting layer directly: that
the sentence a customer reads and the report an operator reads name the same
outstanding component, that a component nothing applies is stated rather than
omitted, and that an effect this service cannot perform is not promised to the
customer in *any* tense. A seam with no control across it is a seam nobody is
watching, and §9.3 (6) had been open for two editions without that being visible.

BW-6 is also the record of how defect 23 came back. §5.10 was fixed, its own control
went green, and reading §9.3 (6) again found the same lie in the tense the fix
introduced. The control that let it through asserted the *absence* of a past-tense
clause, which is exactly what the first fix supplied — so it was satisfied by the
defect it was written to catch, and would have been satisfied again by the second.

This is the direction the next phase should expand: every capability should have
at least one planted-defect control that proves the probe can fail.

### 5.8 Tier 2 break-and-watch — the first test failed, and it was the product

§5.7 is about planted defects in the *simulator*. This is the other direction:
break the journey mid-flight and assert the flow does not lie about it.

The tests are in `TestBreakAndWatch`, and they assert **invariants, not error
shapes**:

* a wallet balance either moved by the full amount or did not move at all;
* an offer is either `fulfilled` with its effect applied, or still `accepted`.

Writing them found a live product defect, which is the argument for writing
them. `_apply_offer_effect` claims `fulfilled` *before* it applies the effect —
deliberately, so two racers cannot both credit. But nothing rolled that claim
back when the credit raised: the caller never reached its commit/rollback, and
the **next query on the session autoflushed the dirty row**. The offer reported
`fulfilled`, `OFFER_EXPLANATIONS` told the customer "We put 250 points on your
account", and no points existed. The claim outlived the effect it was made for.

The fix keeps the ordering — reversing it would reintroduce the double credit —
and adds the missing rollback, so a claim no longer survives its own failure.

| BW# | test | what it breaks | what it pins |
|---|---|---|---|
| BW-1 | `test_a_fulfilled_offer_means_the_points_actually_landed` | nothing (control) | the claim is true when the writer works |
| BW-2 | `test_a_broken_points_writer_leaves_the_offer_unfulfilled` | `credit_recovery_points` | offer stays `accepted`, balance unmoved |
| BW-3 | `test_a_repeated_fulfilment_never_credits_twice` | nothing | balance moves at most once |
| BW-4 | `test_the_sentence_a_customer_reads_tracks_what_actually_landed` | nothing (control) | the customer's sentence and the operator's report name the same outstanding component |
| BW-5 | `test_a_discount_nothing_applies_is_never_spoken_of_in_the_past_tense` | nothing (control) | the 15% is stated, and never as something that happened |
| BW-6 | `test_a_waiver_nothing_here_can_apply_says_so_rather_than_promising` | nothing (control) | an unlinked waiver is not promised to the customer; the two readers agree on what is outstanding |
| BW-7 | `test_a_dropped_recovery_action_refuses_fulfilled` | `credit_recovery_points` | offer stays `accepted`, copy never claims points landed |
| BW-8 | `test_a_broken_consent_gate_is_reported_through_the_probe` | `CONSENT_GATED_PURPOSES` | probe reports forbidden gated purpose (`service`) |
| BW-9 | `test_partial_fulfilment_credit_yes_waiver_no_agrees_on_outstanding` | nothing (control) | operator and customer agree on outstanding for partial fulfilment |

(The `BW-` prefix is deliberate: a bare number here collides with the defect
numbering in §9.1, and BW-2 is the test that found defect 20.)

BW-4 and BW-5 were added when §9.3 (6) was re-read and the customer-facing copy
turned out to be making claims the payload had already been fixed to deny — defect
23, §5.10. They break nothing, and that is the point: they are controls against
*the reporting layer* being wrong, which is the one layer nothing else in this
repository plants a defect in.

BW-6 exists because BW-5 could not have caught the second half of that defect.
BW-5 tests a component this service **never** applies. A waiver is different: the
code to apply one exists and works, and no arrears entry is ever linked to an offer,
so every waiver this service issues lands in a state the two-form copy rendered as
a first-person promise. Asserting "We're removing" is absent is satisfied by
deleting the clause, so BW-6 requires the clause to be *present and* handing the
work on — and requires the operator's `requires_out_of_band` to agree with the
customer's `outstanding` after fulfilment, which is the property that made the
mismatch visible in the first place.

BW-7 and BW-8 extend the pattern beyond the reporting layer: BW-7 breaks the
points writer mid-journey and verifies the offer stays `accepted` and the copy
never claims the credit landed. BW-8 patches the module constant to gate `service`
and verifies the simulator probe reports the forbidden gated purpose — the same
invariant the negative control in `TestNegativeControls` exercises, now verified
at the journey level. BW-9 is the agreement check for the partial-fulfilment
case that the three-form clause tables were built for.

BW-2 is the one that found the bug, and it is worth being precise about why
it is shaped the way it is:

* **It asserts invariants, not status codes.** A `503` here would be an
  implementation detail; "not `fulfilled`" and "balance unmoved" hold however the
  failure is reported. Asserting a code would let the bug back the moment
  someone chose a different shape for the error.
* **It reads the wallet, not the payload.** The payload is the thing under
  suspicion. A half-credited balance is the failure mode a status assertion alone
  would miss.
* **There is a control (BW-1).** Without it, "the balance did not move" is also
  satisfied by an implementation that never credits anything — the exact defect
  being hunted. That has to be ruled out first, in its own right.
* **It has two negative controls, both verified.** Reverting the rollback fails
  it on `assert 'fulfilled' == 'accepted'`. Reinstating the catch-all that turns
  the failure into `{"applied": False}` fails it on `DID NOT RAISE`. Two
  distinct defects, two distinct failures, so the test is pinning the behaviour
  and not one line of it.

A fourth candidate was written and **deleted**: a settle-then-waive test on
arrears. `TestArrearsLifecycle.test_a_settled_entry_can_have_its_interest_waived_nobody_wins`
already covers it, and covers it better (422 plus the reason, against my
`>= 400`). A second, weaker copy of an existing test is not depth.

### 5.9 A negative control that cannot fail is worse than none

The standing check `test_every_probe_the_governance_flow_depends_on_has_a_control`
failed on `blockage_log_renders` and `release_ladder_gates_evaluate`: two probes on
the flow whose failure mode is a promotion nobody can justify, with nothing proving
the harness would notice them breaking. It was doing its job — those two had been
added to close §9.3 gaps and had landed without controls.

Five controls were added. Three of them worked first time. **Three of the eight
attempts did not work**, and every failure looked identical from the outside: the
control passed on a clean tree, the standing check counted it, and the test name
described a defect the harness did not in fact detect. The only way to know was to
delete the thing each control claimed to pin and watch it keep passing.

| control | attempt | what it damaged | what it actually pinned | outcome |
|---|---|---|---|---|
| ladder drops a gate | 1 | slices one gate off the returned list | `nothing_dropped` | worked |
| ladder forges `safe` | 1 | `safe: true`, `blocking_failures: []` over a red gate | `safe_follows_from_the_gates` | worked |
| log drops a section | 1 | removes the closing `Ground truth` line | `missing_sections` | worked |
| log claims success | 1 | replaced `Nothing ran:` with the historical wording | **both halves at once — pinned neither** | deleted |
| log claims success | 2 | the same, as a one-liner | same | deleted |
| log claims success | 3 | appends the success claim, leaves `Nothing ran:` | `not claims_success` | worked |
| log stops saying "nothing ran" | 1 | restored the historical wording | `claims_success`, not the intended half | deleted |
| log stops saying "nothing ran" | 2 | rewrote the line to `- No simulation data.` | `says_nothing_ran` | worked |

The pattern is the same in all three failures: **the damage hit two signals at
once.** The historical wording both drops "Nothing ran" and contains "all held
their invariants", so the probe failed either way and neither condition was
individually pinned. The working version of each control damages exactly one thing,
and is verified in *both* directions — remove the condition it pins and it fails;
remove the other condition and it still passes. That second direction is the one
that matters: a control which fails when something unrelated breaks is pinning the
wrong thing, and the usual repair for that is to delete it.

So the two halves of the probe's verdict are pinned by **two separate controls**,
each damaging exactly one thing, and each verified in both directions: remove the
condition it pins and it fails; remove the *other* condition and it still passes.
That second direction is the one that matters — a control that fails when
something unrelated breaks is pinning the wrong thing, and will be "fixed" by
deleting it.

Three rules came out of this, and they are the same three recorded in §8.1:

1. **Damage exactly one thing.** A control that breaks two things at once pins
   neither, and reports as coverage either way.
2. **Verify by deleting the check, not by reading the control.** Readable source is
   not a working assertion — `if False and not await …` still leaves the call in the
   AST, and a substring search is satisfied by a comment.
3. **Assert the diagnosis, not just the blockage.** The first version of these
   controls asserted on `row.error` and found `''`, because a *failed probe* puts
   its reason in `evidence` and `error` is reserved for an exception. All four
   raised the blockage correctly and the assertions still failed — which reads as a
   broken control rather than a broken assertion, the worst way round. They now
   search the whole finding, and each asserts *which check* failed, so a control
   that catches the defect for the wrong reason is itself a failure.

Two of the probes also could not fail before this, and had to be fixed first —
which is the more useful half of the finding:

* `release_ladder_gates_evaluate` computed `expected["gate_count"]` from
  `PROMOTION_GATES` and then never compared it to anything. **The expectation named
  the number and the verdict ignored it**, so a ladder that dropped every gate
  passed. It now re-derives `safe` from the gates it evaluated, requires the
  evaluated set to equal the set `gates_for_candidate` promised, and checks
  `passed` agrees with `verdict`.
* `blockage_log_renders` checked four headings and passed over the fact that
  rendering an *empty* run set produced

  > **No blockage: 0 flows across 0 subflows all held their invariants.** … this
  > is evidence the engines still branch

  — a success claim about a sweep that never ran, in the document a human reads.
  It was there because this probe calls the renderer with an empty run set on
  purpose, to check structure without recursing, so the simulator's own healthy
  output carried the false sentence. `render_blockages_markdown` now has a zero-run
  case that reports nothing having run, and the probe asserts the success wording is
  *absent* without a sweep to be successful about.

---

`rule_engine` has left the untested list — Tier 1's work graded it. `release_ladder`
and `blockage_log` have left it too, with the controls above. Every declared part of
the backend now grades either `complete` (44) or `shallow` (4); nothing is
`untested`, `stub`, `partial` or `absent`. The four `shallow` grades are honest and
deliberately left in place — §5.5 says what each one would take to close, and
manufacturing the variation instead is the move this repository has already made
once and reverted.

### 5.10 The sentence a customer reads is a claim too — **defect 23**

§5.6.1 and §8.2 fixed the *same* defect twice, one layer at a time, and both fixes
stopped at the payload. Neither reached the customer.

Layer one: `record_outcome` wrote `fulfilled` and then failed to credit, so the
claim outlived its own failure. Fixed by rolling the claim back (§5.6.1).
Layer two: the operator was told which components nothing here can apply, via
`requires_out_of_band`. Fixed by naming them (§8.2).
Layer three — found while re-reading §9.3 (6), not by a test: the sentence the
customer actually reads said neither.

`OFFER_EXPLANATIONS[kind]["what"]` was a **single template per kind**, and it
rendered identically from every status:

```
offered:     We put 250 points on your account and took 15.0% off your next service.
accepted:    We put 250 points on your account and took 15.0% off your next service.
fulfilled:   We put 250 points on your account and took 15.0% off your next service.
```

Two claims and a float, in one sentence:

| the claim | status | truth |
|---|---|---|
| "We put 250 points on your account" | `offered` | the balance has not moved — `TestOffersLifecycle` asserts exactly that, in the same file |
| "We put 250 points on your account" | `fulfilled` | true, and the only row of the table that is |
| "took 15.0% off your next service" | all three | nothing in this repository prices anything; the same flow's operator payload says `requires_out_of_band: ["discount_percent"]` |
| "15.0%" | all three | a storage float reached the page |

The second row is why this survived two fixes and a green suite. **A `fulfilled`
offer makes the tense correct**, so a test that read the copy after fulfilling found
it accurate. The lie is in the two states nobody rendered it in, and in the half of
the sentence that is never right in any state.

And the module had already written down the diagnosis without connecting it.
`TestFulfilmentAppliesWhatItPromised`'s docstring quotes that exact sentence and
then says: *"the whole reason `accepted` and `fulfilled` are separate states is that
'you accepted and we have not done it yet' is a real and embarrassing state."* The
template rendered the embarrassing state identically to the state where it is not
true. The argument for the state machine and the template that erased it were in
the same file, six months of blame apart.

**The fix is structural, not editorial.** Rewording the template would have fixed
today's two kinds and left the next one to reintroduce it, because the shape is the
hazard: *a kind's promise is not one claim.* `goodwill` promises points, which this
service delivers, and a discount, which it does not — and one sentence cannot be
true in both halves. So:

* the `what` key is **gone** from `OFFER_EXPLANATIONS` entirely. There is no
  per-kind sentence to drift, because there is no per-kind sentence.
* `what` is composed **per component** from `_COMPONENT_CLAUSES`, and each
  component is rendered in one of **three** forms — not two:

  | form | means | reachable when |
  |---|---|---|
  | `done` | it landed here | the offer reached `fulfilled` **and** this service applies the component |
  | `pending` | not yet, and it is ours to do | this service applies the component and the offer has not been fulfilled |
  | `out_of_band` | not ours; somebody else finishes it | this service does not apply the component for this offer |

* `_COMPONENT_TERSE` is a **second table**, not a truncation. "250 points on the
  way" and "We put 250 points on your account" differ in tense, and the tense is
  the whole defect, so a shorter string is a different claim.
* the determination lives in **one** function, `_components_this_service_applies`,
  which `_apply_offer_effect` and `explain_offer` both call. They disagreed
  because they each decided independently, so they no longer decide independently.
* the customer also gets `explanation.outstanding`, the same component list the
  operator gets, so a client can render it without parsing prose.

**The third form exists because a two-form rewrite passes every review and is still
false.** Rewriting the template into the future tense — "We're taking 15% off your
next service" — stops claiming a completed act, which is what the original was
caught for. It is also a commitment by *this* process to do something it does not
do. That sentence reads as a fix. It was written, checked against the original, and
rejected, and the reason it was caught is that `validate_offers` asks a different
question than "is this past tense": **is any shape of row making this component
appliable here?**

That question is not rhetorical, because the answer differs per component, and
differs in a way a single probe cannot see:

| component | appliable? | why |
|---|---|---|
| `points` | every offer that carries it | a goodwill offer with points is credited here |
| `waiver` | **only when `arrears_entry_id` is populated** | and `issue_offer` never populates it |
| `discount_percent` | never | no pricing engine exists in this repository |
| `priority` | never | no queue exists |

`waiver` is the interesting one, and it is why "not ours" had to become its own
form rather than a synonym for "not yet". `waiver` **is** something this service
does — the code path exists and works — so a rule of the shape *forbid
`out_of_band` on anything we can apply* fires on it immediately, and gets "fixed"
by deleting the clause the unlinked case needs. But `issue_offer` populates no
arrears entry, so the unlinked case is not an edge: **it is every waiver this
service issues.** With `pending` as its only non-`done` form, that offer told the
customer "We're removing the interest from your account" and nothing removed it.

So the check asks whether some row **carries** the component and cannot apply it,
and the probe is filtered by carrying rather than by kind — because a goodwill
offer for zero points carries no `points` component at all, and an unfiltered
second probe invents a third state for `points` as well. That filter is the
difference between a check that is right about `waiver` and one that is wrong about
both.

`validate_offers` now refuses **eight** ways, each naming a failure that was real or
is the shape one would take while fixing the ones before it:

| # | refused | the failure it names |
|---|---|---|
| 1 | a `done` clause for a component `_components_this_service_applies` cannot return | a past-tense sentence with no reachable state — **defect 23, verbatim** |
| 2 | an appliable component with no `done` clause | the mirror: a credited customer is told their points are still on the way |
| 3 | a `pending` clause for a component nothing here applies | a first-person promise to do something this process does not do |
| 4 | an offer carrying a component here cannot apply, with no `out_of_band` clause | the unlinked waiver, falling back to the sentence check 3 forbids |
| 5 | an `out_of_band` clause for a component applied to every offer that carries it | an effect this service performs described as somebody else's job |
| 6 | the two tables disagreeing about which forms exist | a claim that depends on the customer's preference setting |
| 7 | a clause in a form nothing selects | configuration that looks intentional and can never be read |
| 8 | a kind whose promises no clause describes | the fallback template renders: comprehensible, and about nothing |

Checks 3, 4 and 5 are the three that only exist *because* of the third form, and
each was found by attempting the fix rather than by imagining it. Check 4 was
written first as the symmetric rule — "forbid `out_of_band` on anything we can
apply" — and fired on `waiver` immediately, which is what exposed the fact that
`waiver` needs all three forms. Check 5 is the repair of check 4, and it is only
sound because the probe asks whether the component is **carried**, not whether the
kind matches: without that filter, check 5 misfires on `points`, whose zero-point
goodwill offer carries no points component and so has no unhandled case at all.

**Verified by mutation, not by reading** — seventeen reverts, each damaging exactly
one thing, and each watched to fail on a *different* assertion:

| # | reverted | fails on |
|---|---|---|
| 1 | tense forced to `done` | both copy tests |
| 2 | `outstanding` always `[]` | both copy tests |
| 3 | discount clause emptied | **only** the amount-presence test |
| 4 | `done` clause restored on `discount_percent` | `validate_offers` errors on both the unreachable clause and the table mismatch |
| 5 | terse `points` loses its `done` form | the parity error alone |
| 6 | undeliverable-clause check deleted | **only** its own control |
| 7 | terse-parity check deleted | both controls that assert it |
| 8 | `points` loses its `done` clause | the mirror check fires: "is applied by this service but has no `done` clause" |
| 9 | `pending` clause added to `discount_percent` | the first-person check fires **alone** — both terse tables patched too, or parity fires as well |
| 10 | `waiver` loses its `out_of_band` clause | the missing-clause check fires **alone** |
| 11 | `points` gains an `out_of_band` clause | the always-applied check fires **alone** |
| 12 | first-person check deleted | **only** its own control |
| 13 | missing-`out_of_band` check deleted | **only** its own control |
| 14 | `out_of_band` never selected by the renderer | both copy tests that need it, and **not** the agreement test |
| 15 | unrecognised-tense check deleted | **only** its own control |
| 16 | dropped recovery action mid-journey | BW-7: offer stays accepted, copy never claims points landed |
| 17 | broken consent gate | BW-8: probe reports `gate_withheld_a_fix` / forbidden gated purpose |

Rows 3, 6, 14, 16 and 17 are the ones that matter, and they are the reason this table
exists.

Row 3 is the shortcut — "fix the copy by removing the discount" is satisfied by the
sentence-based assertion and caught only by the amount-presence control, because the
offer still carries a 15% discount that must reach *someone*.

Row 6 is the control that pins one check rather than its neighbourhood: control 2
asserts the parity error *and* that the undeliverable check stayed silent, so a
check that started failing on something unrelated would be caught rather than
"fixed" by deletion (§5.9's rule, which has now cost more effort than the original
defect).

Row 14 is the one that proves the third form is load-bearing rather than
decorative. `out_of_band` can be present in both tables, named by a validator check,
and reachable by no code path at all — and the whole suite stays green, because the
*table* is checked and the *renderer* is not. Rows 9–11 then damage the tables, and
rows 12–13 delete the checks; between them they cover the three ways this feature
can be present and inert. It also shows the checks are not a wall: the same mutation
leaves the agreement test passing, because that test compares two payload lists and
never reads the sentence.

Rows 16 and 17 are the journey-level negative controls: a mid-journey writer failure
and a consent gate violation, each proving the probe path surfaces the defect rather
than rendering a tidy payload.

Four of the controls are the §9.2 defect-17 pattern — each loads a *patched copy* of
`customer_offers.py` — so a check that quietly disappears takes its own control with
it.

**What this says about the two fixes before it.** Both were correct, and both were
incomplete in the same direction: they made the *machine-readable* claim honest and
left the human-readable one alone, because a payload is what a test can assert on
and a sentence is not. The general rule, which is the one worth carrying:

> **Fixing the structured claim and not the prose is the same defect one layer
> later.** A response body and the sentence inside it are two renderings of one
> fact; if they are derived separately they will disagree, and only one of them will
> be checked.

## 6. Tier 2 — a week in the life of a customer

`tests/test_e2e_real_life_journeys.py`, **18 tests.** Real HTTP, real SQLite, the
cast from `_e2e_world`.

| group | tests | what it walks |
|---|---|---|
| `TestChiaraAWeek` | 1 | one complaint from lodgement to closure, day 0 to day 7 — lodged, read back by her only, acknowledged, routed, added to, present in the operator's queue, resolved with a reason, closed, then read whole by the customer |
| `TestChiaraReopens` | 2 | a *resolved* case can be reopened and a *closed* one cannot; decision support names the gap rather than pretending |
| `TestArrearsLifecycle` | 3 | the whole arrears spine over HTTP, interest rising with time and capped, and a settled entry having its interest waived with nobody winning |
| `TestOffersLifecycle` | 3 | issue → accept → fulfil, then decline-then-accept, then an ineligible customer told so |
| `TestTheRouterContract` | 1 | every mutating offer route returns 2xx *and* `found is True` |
| `TestTheCastIsRealData` | 3 | the personas are rows, not fixtures — unique on every axis the routes use, every score inside the scale the engines resolve, every actor named by a flow present |
| `TestBreakAndWatch` | 9 | break the fulfilment journey mid-flight, and assert the flow does not lie about it in prose as well as in the payload — §5.8, §5.10 |

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

### 7.3 A measurement that mutated the state another measurement reads

§7.2 is a measurement source that was wrong on its own. This one was right on its
own and wrong because of where it ran.

`POST /kaizen/admin/candidates/{id}/measure` takes `from_app: [source, ...]` and
computes each source in the order requested. Two of them touch the release ledger:

* `divergence` runs the flow simulator, and one of the simulator's probes —
  `release_ladder_gates_evaluate` — called `release_ladder.seed_ledger()` followed
  by `set_default_ledger(...)`, which **replaces the process-wide ledger**;
* `rollback` reads `get_default_ledger()`.

So `from_app: ["divergence", "rollback"]` replaced the ledger between the two
reads. `test_the_third_rung_is_gated_by_a_real_finding` seeds three deployments,
asks for both sources, and got `rollback_targets: 0` — the ladder's rollback gate
reading zero reachable targets on a ledger holding two. Requesting
`["rollback", "divergence"]` returned 2. A number changed because of the ordering of
an unrelated request.

The probe did not need the ledger at all: `evaluate_gates` reads only the
candidate. The seeding was cargo cult — `set_default_ledger` was there because an
earlier draft needed a clean slate for `register_candidate`, which uses the
*candidate* registry, not the ledger.

The rule this establishes, which is broader than the bug:

> **A measurement must not mutate state another measurement reads.** The simulator
> runs every probe on every sweep, so a probe with a global side effect is a probe
> that can change the answer of an endpoint the sweep never mentioned.

It also restores what it found. `get_default_candidates()` returns the live list,
so the probe now snapshots it, registers against the snapshot, and restores it in a
`finally` — left in place, every sweep appended one candidate per persona, forever,
and the release view grew a tail of `probe-candidate-*` rows no operator registered.

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
| `waiver` | **yes, when linked** | `_apply_arrears_waiver` calls `waive_arrears_interest` / `waive_arrears_fees` when `arrears_entry_id` is populated. When it is *not* populated the component is named in `requires_out_of_band` — an unlinked waiver has no entry to waive, which is a different statement from "this repository cannot waive" |
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

**One further condition, and it is the one that decides whether any of this is
true.** A structure that *reports* the effect is a claim about a successful
application. Before §5.6.1 the report could be produced by a failed application:
`record_outcome` set `fulfilled`, then the writer raised, and the response still
said what it had intended to do. So the reporting above is only sound because the
claim is now conditional on the effect succeeding — and that is the invariant the
break-and-watch tests assert, rather than any field of the response.

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
| 20 | **A `fulfilled` claim outlived the effect it was made for.** `record_outcome` sets the status, then applies the credit; when the credit raised, the aborted transaction was left for the session to autoflush, persisting `fulfilled` with no credit and customer copy saying the points landed | `customer_offers.py` | **high** — §5.6.1 |
| 21 | The simulator's `release_ladder_gates_evaluate` probe called `set_default_ledger`, replacing the process-wide ledger that the `rollback` measurement source reads; the rollback count depended on whether `divergence` was requested before it | `real_life_flows.py` | **high** — a gate number changed with request ordering — §7.3 |
| 22 | `journey_outcome_report` forwarded no clock to `is_stuck`, so a test frozen at 2026-10-01 started failing on 2026-10-04 with nobody touching the tree | `offer_outcomes.py` | medium — a correct test on the day it was written |
| 23 | **The customer-facing sentence asserted two effects, one of which never happens.** `OFFER_EXPLANATIONS[kind]["what"]` rendered "We put 250 points on your account and took 15.0% off your next service" from *every* status — claiming a completed credit before the money moved, and claiming a discount the same response reported as `requires_out_of_band`. **Found once, fixed twice**: removing the past tense left a future-tense promise ("we're removing the interest from your account") that is still a commitment by this process to something it does not do, on every waiver it issues | `customer_offers.py` | **high** — one reader told the truth, the other the opposite — §5.10 |

(The numbering skips 18 and 19. Those two were the `TestBreakAndWatch` tests, which
were numbered 17-19 alongside this table until the `BW-` prefix in §5.8 revealed
that 17 was already a real defect -- so the same number meant a test in one place
and a product defect in another, and BW-2 is the test that *found* defect 20. The
defect numbers were left alone rather than renumbered, because §9.3 and §9.4
cross-reference them and a renumbering would break those for no gain. A gap in a
sequence is cheaper than a collision in it.)

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

The controls now stand in `TestTheStructuralChecksCanFail` — eleven tests — load a
patched copy from disk (monkeypatching cannot test this: the check reads the
function's own source, which a patched attribute leaves intact), and cover the three
reverts, the four clause-table checks added by defect 23 (§5.10), the skip path, the
unrecognised-tense check, and its reachability. **A structural check that has never
been seen to fail is a comment with an `assert` on it.**

### 9.3 Still open, in the order it pays

**1. `release_ladder`, `blockage_log`, `topics` — grades now honest** (resolved).
* `release_ladder` — probe `release_ladder_gates_evaluate` exercises `evaluate_gates`;
  capability grades **`complete`**. The probe could not fail until §5.9: it named
  `gate_count` in `expected` and never compared it, so it had to be strengthened
  before a control for it could mean anything. Two controls now cover it — one drops
  a gate from the returned list, one forges `safe: true` over a failing gate.
* `blockage_log` — probe `blockage_log_renders` exercises the renderer; capability
  grades **`complete`**. Writing its control found a false claim in the rendered
  artefact (§5.9) and the renderer now has a zero-run case.
* `topics` — probe `topic_classification_works` measures themes from the live
  classifier rather than asserting vocabulary membership; capability grades
  **`complete`**.

  All three grade via the capability audit — the routes `/kaizen/admin/*`,
  `/chat/admin/*`, `/topics/*` ARE served by the test client; the earlier
  `untested` grade was from running the audit on a subset of flows.

**2. Three single-probe flows now have a second probe each** (resolved):
* `repeat_customer_changes_booking` — added `booking_events_are_logged`, which
  asserts the transition is recordable in the event vocabulary.
* `points_and_arrears_payment` — added `points_quote_is_reproducible`, which
  calls `quote_points_exchange` twice with identical inputs and asserts the
  outputs match (including the dynamic rate breakdown).
* `auth_failure_and_recovery` — added `auth_rate_limit_fires`, which exercises
  the `TopicRateLimiter` config and logic, asserting it has finite capacity,
  positive refill, and denies when empty.

**3. The declared journey and the executed journey are now linked by a traceability
table** (resolved). 55 declared `subflows` names, 30 executed probe ids, **zero
string overlap** — but every subflow now lists the probe ids that exercise it,
and `validate_flows` checks the table:

* every declared subflow has an entry in `FLOW_SUBFLOW_PROBES`
* every probe mapped to a subflow is in the flow's `probe_ids`
* every probe in `probe_ids` is mapped to at least one subflow (warning, not error)

The two vocabularies remain distinct by design — `subflows` holds business
steps (`register`, `create_booking`) and probes emit check ids
(`access_band_resolves`, `booking_events_are_logged`) — and the overlap is
empty because they *are* different languages. The traceability table is the
bridge, and it is enforced rather than assumed. The warning for flows declaring
more than twice as many steps as probes remains as a weak signal.

**4. Blind probes: eleven of twenty-four → four of thirty** (resolved).
Sharpness went from **13/24 → 30/30**. The original eleven blind probes were
fixed by:
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

The two remaining unjustified blind probes (`forecast_confidence_decays`,
`retention_series_builds`) were then sharpened by adding persona contrast
(different snapshot counts / churn / loyalty), moving `retention` from
`shallow` → `complete`. That completed the set: **4 blind, all justified**.
| `retention_series_builds` | asserts the series is non-empty and monotonic; any monotone series satisfies it | assert the exact band the persona's churn and loyalty scores earn, so `no_data` cannot satisfy it |

Those two are the whole of the `retention` capability's `shallow` grade. §5.5
records what each of the four `shallow` grades would take, and the rule for all
of them: **do not close it by carrying the probe's own inputs into its
`expected`.** That trick manufactures distinct expectations with one identical
answer, which is a *false* finding — worse than no finding, because a reader
cannot tell it from a real one.

**The stale-excuse check then caught one of my own earlier fixes.** Adding
`stable_high_loyalty` to reach the `loyal` retention band and the `stable`
recovery stage meant `_vocabulary_coverage` stopped reporting `loyal` as
unreached — which is exactly the premise the `recovery_lifecycle_stage` excuse
leaned on. `check_probe_sharpness` reported the excuse as stale and re-listed the
probe under `claims_variation_but_constant`, and the suite failed. That is the
mechanism working: an excuse is only honest while the gap it leans on is still
being reported.

The response was to **delete the entry**, not to reword it. The probe returns
three distinct expectations across five personas (`engaged`, `loyal`, `new`) and
needs no excuse. A key in that table *is* an excuse, so a "RETIRED — do not
re-add" note left in place would have silently re-excused the probe the first
time it went blind again, and the stale-excuse check would never fire because it
only inspects entries whose text contains `derived_no_contrast`.

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

This is the one open item that is **blocked rather than undone**, and the block
should be stated as such rather than as remaining effort. `compare_runs` takes two
run sets and does the real comparison; `_app_measurements` already serves a
`shadow` source against an isolated environment. What is missing is a second
environment to compare against, and inventing one in-tree would produce a number
that looks like the real measurement and is not. The gate stays **unmeasured**,
which is the honest state: `evaluate_gates` reports an unmeasured gate as
`unmeasured` rather than passing it, precisely so this reads as a gap.

**6. Two of an offer's four promise components still have nothing that can
apply them** (§8.2) — **the reporting half is now closed; the product call is not.**
`discount_percent` and `priority` are *named* in `requires_out_of_band` with their
hook, rather than silently marked delivered. `waiver` is no longer in that list — it
applies when `arrears_entry_id` is populated, and §5.6.1 covers what happens when
the writer underneath it fails.

**This item was re-read twice, and each reading found the customer-facing copy still
denying the same thing the payload admitted.** Option 1 below has a second clause —
*"and make the customer-facing copy say so"* — and that clause had not been done.
Every goodwill offer this repository issues carries a discount
(`RECOVERY_SAVE_INCENTIVES`: 5%, 15% or 25%), and every one of them told the
customer the discount had been taken off. That is **defect 23**, §5.10, and it was
not found by re-measuring anything: it was found by reading this item and noticing
that option 1's second clause was unimplemented.

Re-reading it again found a *third* state nobody had named. `waiver` is in none of
the two lists above, and appears to be settled — but `_apply_offer_effect` can apply
it only when `arrears_entry_id` is populated, and **nothing in this module populates
one**. So `waiver` is in the same position as the other two, on every offer this
service issues, and it was being described in a first-person future tense as though
that made it honest. It is still defect 23 in a smaller coat: a promise by this
process to do something this process will not do. §5.10 records why the fix is a
third clause form rather than a rewording.

What that leaves is genuinely a **product decision**, and it needs someone who owns
pricing and scheduling:

* `discount_percent` has no pricing engine in this repository to call. There is no
  cart, no price, and no invoice — the only "prices" here are arrears principals
  and point exchange rates. A hook that logs `{"hook": "pricing_engine"}` is
  honest; a hook that invented a percentage and wrote it somewhere would be a new
  defect wearing the costume of progress.
* `priority` has no queue. Nothing reads a priority value, so setting one is
  indistinguishable from not setting one.

Two options, and they now need different amounts of work:

1. **Keep them permanently out-of-band** — **the reporting and the copy are both
   done.** The operator gets `requires_out_of_band`; the customer gets an
   `out_of_band` clause saying the discount "is noted on your account for our team to
   apply"; and `validate_offers` now refuses a first-person `pending` clause for a
   component nothing here applies, so the copy cannot drift back to a promise. What
   remains is a judgement about the *wording* — whether "for our team to apply" is a
   promise the business stands behind — and not a correctness question. This is the
   honest default and it is cheap.
2. **Build the two subsystems**, then add the component to
   `_components_this_service_applies` and write a real hook. That is a feature, not
   a fix, and it is a small change now: the clause table, the terse table and
   `validate_offers` are already the seam. Adding a `done` clause for a component
   the source of truth can return is the whole of it — and `validate_offers` will
   *refuse* the change until the source of truth can return it, which is the
   ordering this defect argues for.

The `waiver` case belongs to whichever option is chosen, and it is a prerequisite for
either: an arrears entry has to be linked to the offer, or the waiver subsystem is
as unbuilt as the other two. It is listed here rather than in §9.3 item 6's two
bullets because it is a third subsystem nobody has counted.

The rule this work has been holding to: **`fulfilled` must not imply anything
landed that did not.** Whichever option is chosen, that has to survive it — and
BW-4 (§5.8) is now the test that it does, on the sentence rather than only on the
payload.

**7. Four defects found by re-measuring, not by the flows.** Recorded here because
none of them was found by a probe, and three of them are the kind that a green suite
routinely hides:

| # | defect | found by | why no probe found it |
|---|---|---|---|
| 20 | a `fulfilled` claim survived a failed credit, via session autoflush | `TestBreakAndWatch` | the probes only ever ran fulfilment with the writer working |
| 21 | the simulator's `release_ladder_gates_evaluate` probe called `set_default_ledger`, replacing the process-wide ledger that the `rollback` measurement source reads | `test_the_third_rung_is_gated_by_a_real_finding` | the damage lands on a *different* endpoint, and only when `divergence` and `rollback` are requested together |
| 22 | `journey_outcome_report` forwarded no clock to `is_stuck`, so a test frozen at 2026-10-01 began failing on 2026-10-04 with nobody touching the tree | the suite, three days later | a date-dependent test is a correct test on the day it was written |
| 23 | the customer-facing `what` sentence claimed a credit before it was made and a discount nothing applies — in the same response that reported the discount as out-of-band | re-reading §9.3 (6) | **not a missing check: a check that was satisfied by a `fulfilled` offer.** The tense is correct in the one state every offer-copy test rendered, and the wrong half of the sentence is never right in any state — §5.10 |
| 23 (again) | removing the past tense left a first-person future promise for an effect nothing here applies — and for `waiver`, on every waiver this service issues, because `issue_offer` never links an arrears entry | re-reading §9.3 (6) a second time, after the first fix | **a fix that satisfies its own negative control.** The check was satisfied, the tense was correct, and the claim was still false — in the third state, which no test rendered because no test read the copy for an unlinked waiver |

Defect 21 is the one worth generalising: **a measurement must not mutate the state
another measurement reads.** `_app_measurements` computes sources in request
order, so a probe with a global side effect makes a later source's number depend
on what was asked for first. The probe now restores both globals it touched.

### 9.4 What is assured, in one page

| Claim | Mark | Where |
|---|---|---|
| Shadow cannot run shadow → live | **A** | `TestOneWayRule`, 6 tests — every declared pairing derived, none trusted |
| A shadow pointed at live is detected by *identity*, not config | **A** | 12 identity tests, incl. same DB / different password |
| `isolation_violation` is reachable *through the flow path* | **A** | `test_a_shadow_pointed_at_live_is_caught_through_the_probe` |
| Every probe the governance flow depends on has a control | **A** | set-of-ids standing check over 8 probe ids; `TestNegativeControls` holds 21 tests, each damage verified by deleting the check it pins |
| A control that cannot fail is caught | **A** | §5.9 — 3 of 8 attempts passed with the check they claimed to pin deleted |
| A claim never outlives the effect it was made for | **A** | §5.6.1 — offer stays `accepted`, balance unmoved, claim rolled back |
| The sentence a customer reads agrees with the report an operator reads | **A** | §5.10 — one source of truth, per-component tense in three forms, `validate_offers` refuses a past-tense clause for an undeliverable component *and* a first-person one |
| "We will do it" is only said about things this service does | **A** | §5.10 — `out_of_band` is a distinct form, and a waiver with no arrears entry reads as somebody else's job |
| A blocked measurement source cannot corrupt an earlier one | **A** | defect 21 — the simulator restores every global it touches |
| A stuckness report is reproducible | **A** | defect 22 — `journey_outcome_report(now=…)`, with a later-date control |
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
| Chiara's week, end to end, changes nothing it should not | **a** | Tier 2, 18 tests, incl. `TestBreakAndWatch` |
| An offer does not claim `fulfilled` over an effect that failed | **A** | §5.6.1 — 2 negative controls, both verified |
| A broken dependency stops the claim rather than the request | **A** | §5.8 — invariant assertions, not status codes |
| The ladder stops exactly where the evidence stops | **a** | Tier 3 — the gates are asserted, not themselves broken |
| The baseline flows report no blockage | **a** | 30 probes, 104 subflow executions, 0 blockages; 6 blind, and both blind measures agree on the same six (§10) |
| Four capabilities are only *shallow*-tested | **✗** | §5.5 — a stub engine would pass; left honest, not closed by manufacture |
| `discount_percent` and `priority` are actually applied | **✗** | no subsystem in this repo can — a product call, §9.3 (6). The *reporting* is closed: operator, customer and validator all agree it has not |
| `shadow_divergence_within_tolerance` | **✗** | no shadow to compare against — see §9.3 (5) |
| Two concurrent decisions on one offer | **A** | Tier 4, defect 12, fixed — one conditional write, 4 mutators |
| The loser of a race is told which decision won | **A** | Tier 4, defect 14 — the invariant held while the answer was wrong |
| Fulfilment applies the effect it claims | **A** | Tier 4, defect 13, fixed — 250 promised → 250 credited → 1 ledger row |
| A second credit for one offer is refused, not skipped | **A** | Tier 4 — `double_credit_refused`, loud rather than silent |
| An effect nothing here can apply is named, not implied | **A** | Tier 4 — `requires_out_of_band` for `discount_percent` / `priority` |
| `waiver` is applied when an arrears entry is named | **A** | §5.6.1 — and a failed writer rolls the claim back |
| The declared journey steps and the executed probes are reconciled | **✗** | 55 names, 30 ids, zero overlap — §9.3 (3); a rename would raise the number and not the coverage |
| Every mutator test in the offers suite evaluates its `WHERE` | **A** | defect 15 — `autouse` select-fake backs off for real-DB tests |
| The offer subsystem's own validator would notice a reverted guard | **A** | defect 17 — 7 negative controls, each loading a patched copy of the module |
| A structural check can distinguish a call from a comment about it | **A** | defects 16, 17 — `ast` call sites, never source text |

---

## 10. Re-measuring

Everything above is reproducible. These are the commands.

```bash
cd code/dev/fastapi

# the whole suite. Last full run: 3124 passed in 985s (wall clock varies widely
# with machine load -- an uncontended run of the same tree took 604s).
python3 -m pytest tests/ -q

# the four flow tiers. 52 / 18 / 18 / 17.
python3 -m pytest tests/test_e2e_certainty_flows.py     -q
python3 -m pytest tests/test_e2e_real_life_journeys.py  -q
python3 -m pytest tests/test_e2e_release_journey.py     -q
python3 -m pytest tests/test_e2e_adversarial.py         -q

# the negative controls, and the policy table they police.
# Every one of these is verified by *deleting* the check it claims to pin and
# watching it fail. To re-verify one, remove the condition from
# `_probe_blockage_log_renders` in app/real_life_flows.py and run the control: it
# must FAIL. If it passes, the control has stopped pinning and must be rewritten
# before it is trusted again -- §5.9 is what that looks like when it goes wrong.
python3 -m pytest tests/test_kaizen_shadow_release.py -q -k NegativeControls

# is the simulator baseline still clean?
python3 -c "
from app import real_life_flows as F
runs = F.run_all_flows()
print('blockages:', len(F.collect_blockages(runs)))
print(F.flow_gate_measurements(runs))
"

# §5.6.1 / §5.10 — the five break-and-watch cases. Three plant a broken dependency
# and assert the *invariant* (offer not `fulfilled`, balance unmoved) rather than a
# status code, so they survive a change in how the failure is reported. Two break
# nothing and assert the reporting layer itself: that the sentence a customer reads
# and the report an operator reads name the same outstanding component.
python3 -m pytest tests/test_e2e_real_life_journeys.py -q -k TestBreakAndWatch

# §5.10 — the clause tables, and the eight ways `validate_offers` refuses a
# sentence that claims an effect this service does not deliver, in any tense.
# Run after any edit to `_COMPONENT_CLAUSES`, `_COMPONENT_TERSE` or
# `_components_this_service_applies`.
python3 -m pytest tests/test_customer_offers_stage_b.py -q \
  -k "CustomerFacingCopy or StructuralChecksCanFail"

# §5.9 — the standing check, which is what noticed the two missing controls.
python3 -m pytest tests/test_kaizen_shadow_release.py -q \
  -k every_probe_the_governance_flow_depends_on_has_a_control

# blind / sharp / orphan / untested, and the two blind* keys are different measures
python3 -c "
from app import real_life_flows as F
runs = F.run_all_flows()
r = F.capability_report(runs)
s = F.check_probe_sharpness()
print('grades :', r['counts'])
print('audit-blind  :', len(r['blind_probes']), 'of', r['probe_registry_size'], r['blind_probes'])
print('sharpness-constant:', len(s['constant_expectation']), 'of', s['probes'], '| valid:', s['valid'])
print('sharp  :', s['sharp'], 'of', s['probes'])
print('orphans:', r['orphan_probes'], '| untested:', r['untested'])
print('unjustified constant:', s['unjustified_constant_expectation'])
print('stale excuses      :', s['stale_justification'])
"
# `audit-blind` and `sharpness-constant` are the same six probes, computed twice
# by two independent implementations (`capability_audit._blind_probes` and
# `real_life_flows.check_probe_sharpness`). They are printed together on purpose:
# they agreeing is a cross-check, and if they ever diverge then one of them has
# changed meaning without the other noticing.
#
# `sharp: 28 of 30` is not "28 probes are good". Six probes expect the same thing
# for every persona; four of the six carry an entry in
# `_CONSTANT_EXPECTATION_JUSTIFIED` arguing why a universal invariant is the
# honest claim there, so they count as sharp. `unjustified constant` lists the two
# that do not -- `forecast_confidence_decays` and `retention_series_builds` --
# and those are the two behind the retention capability's `shallow` grade (§5.5).

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

Breaking the things on purpose found two more that nothing green was watching: a
`fulfilled` claim that **survived the failure of the effect it was made for** — the
offer said the customer had 250 points and the wallet said zero, because the
claim was written before the credit and nothing undid it when the credit raised —
and a simulator probe that replaced the process-wide release ledger, so a gate's
rollback count came back zero depending on which measurement source was asked for
first. Both are fixed; the first by rolling the claim back, the second by not
mutating state a measurement reads. The second is the lesson worth carrying: a
green suite is evidence about the paths it exercises, and a side effect on shared
state is invisible to every test that does not happen to ask for the two things in
that order.

The third was not found by breaking anything, and it is the one that ties the other
two together. The first fix made the *payload* honest; the second made the
*operator's report* honest; and the sentence the **customer** read went on claiming
a credit before the money moved and a discount that nothing in this repository can
apply — in the same response that reported the discount as requiring out-of-band
action. It survived two fixes because a `fulfilled` offer makes the tense correct,
so every test that read the copy after the good case found it accurate, and the
wrong half of the sentence is never right in any state. The fix was to delete the
per-kind sentence entirely and compose the copy per *component* from the same
function that decides what this service can deliver, so the two renderings of one
fact cannot disagree. **Fixing the structured claim and not the prose is the same
defect one layer later** — and the general form of that, along with the two rules
that followed from it (damage exactly one thing; verify by deleting the check, not
by reading the control), is what the second edition is actually about.

That fix then came back once more, which is the part worth keeping. Rewriting the
sentence into the future tense stopped the completed-act claim and satisfied its own
negative control — and was still a promise by this process to do something it does
not do, because "not yet" and "not ours" are different claims and the copy only had
a word for one of them. It showed up in the state no test rendered: a waiver whose
arrears entry is never linked, which is every waiver this service issues, because
no test had read the copy for one. So the copy now has three forms, not two, and
the validator refuses a first-person sentence about an effect nothing here applies.
The generalisation is sharper than the first one:

> **A check that is satisfied by the defect it was written to catch is worse than
> no check**, because it converts a known gap into a recorded assurance. The first
> control asked "is anything here in the past tense?" — and the tense was the thing
> that had been fixed. Asking what the sentence *asserts about the world*, rather
> than which form it takes, is the difference between a control that pins the
> property and one that pins the previous patch.

The same pattern is defect 17 and defect 22 wearing different clothes: a check that
passed was not measuring the thing it was named for.

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
