# BLOCKAGES.md

All resolved. Working log of keep-as-is decisions and expansion progress.

## Keep-as-is decisions (do not "fix")

- Router `__globals__` names that the test suite patches must remain module-level
  names in `routers/chat.py`: `_load_user_interaction_window`,
  `_build_interaction_insights`, `_build_summary`, `_build_system_improvement_pack`,
  `analyze_sentiment`, `_save_retention_snapshot`, `_prune_retention_snapshots`,
  `_store_interaction_signals`.
- `services/bookings.py` status helpers keep their distinct None-default
  contracts (do not unify).
- Test-hit routes `/retention/maintenance*` and `/chat/retention-dashboard` are
  part of the test contract — leave their paths/behavior untouched.
- `_current_control_posture` in `routers/users.py` is an alias of
  `deps.current_control_posture` (asserted by tests).
- Overlapping scoring terms are legitimate and pinned by tests: `booking`
  contains "book" (booking_flow), `reminder` matches "remind"+"reminder",
  `follow_up` includes "again".
- **`resolve_communication_strategy` must not read user preferences directly.**
  Its output is pinned by existing consumers, so the stated preference is
  applied at the presentation point (`customer_360._build_communication`,
  `self_service.build_self_service_status`) and the override is reported
  explicitly rather than folded into the engine.
- **Quiet hours are evaluated in the customer's local hour, never the server's.**
  `region_windows.local_hour` is the only place the arithmetic lives; do not
  inline `moment.hour` anywhere else. The Stage D gate did, and the whole Stage D
  suite stayed green because every persona lived in the server's timezone.
- **An unresolved region is `unattributed`, not closed.** Absence of a region has
  no office hours to exclude contact, so the fallback must not carry a staffed
  window. It is conservative about the *hour* (UTC) and exempt from staffing
  closure. Narrowing it was tried and reverted — that fabricates a constraint
  rather than adding caution.
- **A recognised device shortens authentication, never permission.** It must not
  unlock unattended contact, and a contact refusal must not stop a customer
  acting on their own account. `care_personalization.assert_trust_does_not_overreach`
  states this in code.
- **The copilot is the default pane.** Moving off it requires `explicit=True`;
  a default any caller can move by naming a pane is not a default.
- **A policy motion records and never applies.** `build_motion` sets
  `applied: False` and `build_motion_ledger` returns `applied: []`. Do not add a
  write path; an engine that retunes its own table from its own outputs makes that
  table unauditable.
- **An unchecked promise is never a held one.** `check_promise_continuity`
  requires every promise to be verified before reporting `continuity: true`, and
  a broken promise must name the commitment it broke.
- **Every `PROMISES` row names a `regression_guard` that is a registered probe.**
  `validate_trust_continuity` reads `real_life_flows.PROBES` and fails the build
  otherwise. Do not add a promise with a guard nothing runs.
- **No value movement may lower a status without evidence**, and only
  `lowered_for_risk` may lower one at all. A refusal must stay a refusal: a
  refused move is never reported as an event that occurred.
- **Acceptance and fulfilment are never combined into one success rate.** High
  acceptance with poor fulfilment is a fulfilment problem, and averaging the two
  points an operator at the offer volume instead of at us.
- **The consent gate must never suppress `service` or `recovery` outreach.**
  A customer who reported a problem must not have the fix withheld because of a
  marketing setting. `CONSENT_GATED_PURPOSES` is the published list of which
  purposes actually gate; if a purpose is added there, that is a deliberate
  change, not a default.
- **`recovery_playbooks` statuses are a contract, not a display choice.**
  `skipped` means a guard stopped the action and is *not* a failure. A
  customer-visible view must show it; collapsing it into "executed" or
  dropping it makes a correct limit and a silent bug look the same.
- **`app/models.py` still does not import `app/services/*`**, so a judgment two
  services would share is declared twice rather than imported (e.g. the
  `user_consent_events.purpose` vocabulary in `ENUM_FIELD_SPECS` and in
  `CONSENT_PURPOSES`). A duplicate table can drift; a cycle cannot be repaired
  without breaking the import graph.
- **`untested` is deliberately not `complete`.** A part that exists and that no
  probe exercises is reported as unmeasured rather than counted as health, and
  `blind_probes` is published for the same reason: a probe every persona answers
  alike cannot distinguish a working engine from a constant one. Both are limits
  of the *simulation*, and stating them is how a reader knows which rows are
  resting on a question that cannot fail.
- **`communication_frequency: only_reactive` must not suppress an offer.** It is
  a preference about how often to *interrupt*, not about whether to be *told*.
  The offer is issued and visible; the gate withholds the notification and defers
  it with a time. Any future change that makes `only_reactive` skip issuance
  breaks the promise the preference makes, and hides a fix from precisely the
  people who asked for less interruption.
- **A recovery or service offer is never consent-gated**, and the three kinds'
  purposes are asserted to be `service`/`recovery` by
  `test_the_three_kinds_are_all_service_or_recovery`. `validate_offers` warns on
  any kind that is not, so adding a campaign-flavoured kind is a visible act.
- **`customer_offers.generosity_scale` is stored, never recomputed at fulfilment.**
  A scale re-read from the live investment signal can move an amount the customer
  has already accepted.
- **Loyalty status never decays for inactivity, and health is not a status.**
  Together they mean a customer can be `Trusted` and `critical` at the same time,
  which is the combination that deserves the fastest response. A blended number
  hides it and a demotion punishes them for our failure, so neither is allowed:
  `STATUS_DECAYS_ON_INACTIVITY` is an error if set, and `assert_health_is_not_status`
  raises if a report claims otherwise.
- **The investment band is ordinal and must never be summed.** There is no revenue
  column in this schema; an LTV figure built from these inputs would be a guess
  wearing a float. It feeds `OFFER_GENEROSITY_RULES`, and that table's bounds and
  per-kind cap are what stop a band becoming an unbounded liability.
- **`chase_repeats` means extra messages about one booking**, not total message
  volume. Ten messages about ten different things is not chasing, and treating it
  as such makes the ledger punish an engaged customer.
- **The orchestrator's repeat rule compares a precondition hash, never the stage or
  the count.** Cycling `at_risk → … → status → at_risk` is a new attempt, not a
  reset; a state that reset its attempt history on re-entry would permit a fourth
  identical offer to someone who has already declined three.
- **`sla_hours` and `offer_window_hours` are separate columns and must stay so.**
  One means a person is accountable; the other means an automated credit stays
  worth giving. Giving a band an SLA nobody owns makes the one that is owned read as
  decoration — `validate_relationship_health` rejects it.
- **A proactive path must call `care_gate.consult`, and that is checked against
  source.** The list of places that can contact a customer is in
  `care_gate.PROACTIVE_PATHS`, and `gate_coverage()` fails the build if a declared
  path's source never calls the gate. Removing the gate from any of them breaks
  `validate_care_gate()` rather than quietly restoring the defect — which is the
  whole reason that module is not a docstring.
- **`issue` and `push` stay separate, and `issue` follows consent only for a
  consent-gated purpose.** A reactive-only customer must still receive the offer;
  a marketing offer with no consent must not be created at all, because it could
  never be sent.
- **Acceptance and fulfilment are never averaged.** High acceptance with poor
  fulfilment is a fulfilment problem, and the average points the operator at the
  offer volume instead of at us.
- **A learned complaint weight may change emphasis, never verdict.** Severity,
  tier and auto-escalation are refused by reading
  `complaint_learning.LEARNED_WEIGHT_AUTHORITY`; relaxing one upstream fails
  `validate_care_weights()`.
- **`OFFER_STATUSES` keeps `accepted` and `fulfilled` apart.** "You accepted and
  we have not done it yet" is a real state and collapsing it removes the only
  signal that there is a backlog.
- **A deferred subflow is not a pass.** `FlowRun.passed` is true when every
  deferred subflow was skipped, which is why `flow_gate_measurements` publishes
  `subflows_deferred` and `subflows_exercised` beside `flows_failed`. `0`
  failures is also what an *empty* backend reports, and a gate reading only that
  would be satisfied by a backend that has built nothing.
- **A promotion gate failing is not a reason to add a bypass.** The ladder has
  no override flag, and the fix for "this candidate cannot pass
  `authorization_complete`" is to close the route or document the exception in
  `AUTHZ_PUBLIC_WRITE_EXCEPTIONS` — a visible act — not to add a flag that
  silently means the gate is advisory.
- **`high_trust` declares `delta: 0.0` and that is correct**, so a probe
  demanding that every posture move the score would flag a working
  configuration. `TestPinningOracle` pins the deltas as literals precisely so
  that editing the table to satisfy a broken expectation fails there instead of
  quietly making a probe pass.

### Complaint-learning keep-as-is decisions (do not "fix")

These four are not oversights. Each one reads like a bug to someone who has not
read the argument, and "fixing" any of them produces a system that looks
healthier and is measurably worse.

- **A learned weight must never reach a case's tier, and the guarantee is
  structural rather than documented.** `LEARNED_WEIGHT_AUTHORITY` is a dict, so
  it constrains nothing by itself; the guarantee is that no learned number enters
  the `context` dict the escalation triggers are evaluated against. The weight
  reaches the dossier only as `loyalty_contribution`, the eleventh feed, marked
  `required: false`. Do not "improve" this by letting a high-confidence weight
  nudge a severity or an escalation. The reason is in the `why` field: cases that
  get escalated produce the evidence that raises the weight, which escalates more
  cases. Within one quarter that loop decides the policy on its own, and it
  decides it in favour of whatever was escalated first.

- **`LOYALTY_OUTCOMES` is deliberately *not* complaint-lifecycle vocabulary.**
  `complaint_decisions.outcome_observed` answers "was it resolved";
  `complaint_signal_observations.loyalty_outcome` answers "did the customer
  stay". They come apart constantly — a refund issued, the case closed, the
  customer never returns — and a system that learns on the first while believing
  it is learning the second becomes confident about resolutions people walked away
  from. Do not unify the two vocabularies to reduce the number of concepts.

- **`billing_dispute_unresolved` ships inert on purpose.** Its `confidence`
  (0.65) is deliberately below its own `min_confidence` (0.70), so it can never
  fire. An arrears balance plus an open complaint describes most of the customer
  base, and a detector that opened a case for every one would be an accusation
  rather than a service. The row documents the shape a detector needs without
  acting. `validate_complaint_learning` emits a *warning* for it — do not silence
  that warning; it is the only thing marking the row as deliberate.

- **`billing_dispute_unresolved` also has no dedupe, and
  `unowned_stale_case` has no dedupe either — both are deliberate and for
  different reasons.** The first would fold a distinct money problem into an
  unrelated open case. The second is about *this* case having no owner, so
  deduping it into another case would hide the fact that nobody is on it.
  Everything else dedupes, and the dedupe lookup must keep passing
  `exclude_id=<the trigger case>`: without it a detector blocks against the very
  case that woke it up, which is the bug that made five of the six shipped
  detectors unable to ever fire.

### Complaints keep-as-is decisions (do not "fix")

- **Guard polarity: `holds` means the guard is *satisfied*.** A guard holds when
  the condition is fine and stops holding when the condition named in its
  `rationale` is present — the same convention as
  `evaluate_selection_guards` in `services/topics.py`. Six of the seven
  complaint guards were first written holding on the *problem*, which inverted
  the whole verdict: a healthy case reported `reject` because every guard had
  "failed". A backwards guard is worse than no guard, because it makes every
  decision look blocked and trains reviewers to ignore the list.
  `test_each_guard_stops_holding_when_its_condition_is_present` pins one case per
  guard so a single inversion cannot hide.
- **A guard with an `applies_when` scope is *skipped*, not failed.** The
  regulatory-deadline guard's metric is absent for an ordinary complaint, and a
  missing metric fails closed — which would reject every non-regulatory case for
  no stated reason. Same idea as `TOPIC_SELECTION_GUARDS`' `sources` narrowing.
- **Only a statutory deadline or a breached clock may act without a human.**
  `AUTO_ESCALATION_TRIGGER_KINDS` restricts `authority: "auto"` to the
  `regulatory` and `sla_breach` trigger kinds, and `validate_complaints` raises
  an **error** for any other. The set is data so it can be widened deliberately,
  never by adding one row with a typo. The sweep keys its write on
  `decision["auto_triggers"]` and **not** on `decision["acted"]` — keying on
  `acted` lets it apply exactly the judgement calls the policy reserves for a
  person, which is the bug `test_judgment_triggers_are_reported_but_not_applied`
  exists to catch.
- **An owner change is an application even when the tier does not move.** A
  medium-severity case breaching its response SLA computes `to_tier_rank == 1`,
  equal to the tier it is already on, so a tier-only test finds nothing to do and
  the case keeps whoever owned it before — the exact situation
  `regulatory_privacy_open` exists to prevent. `apply_escalation` therefore
  derives `applied` from what would change, not from the engine's own `escalated`
  flag.
- **A downgrade is refused, not forbidden.** `apply_escalation` returns
  `applied: false` with the rule that blocked it. An impossible request is
  something an operator can make, and recording the refusal is more useful than
  a 422 that vanishes.
- **The SLA `basis` field reports which clock actually governed.** Our internal
  targets are usually the stricter ones, so reporting
  `basis: "regulatory_floor"` for every regulatory case would be a lie in the
  common case. Three values: `internal_matrix`,
  `internal_matrix_within_regulatory_limit`, `regulatory_floor_partial`,
  `regulatory_floor`.
- **A required decision feed that could not be built is *named*, not omitted.**
  "No such feed" and "the feed says nothing" are different claims about the
  world. `build_decision_dossier` reports `missing_required_feeds` and
  `unavailable_required_feeds` separately, and the summary line says
  "Incomplete evidence".
- **Dossier confidence is the *weakest required* feed, not the average.**
  Averaging lets a dozen confident internal feeds hide the one external feed
  that failed, which is exactly the case where the decision is shakiest.
- **The external feed is always reported as uncalled on a request path.**
  `DECISION_FEED_BY_ID["external"]["async_only"]` is the rule, mirroring
  `rule_engine.EXTERNAL_OPS`. It appears in the dossier as an explicit gap
  rather than being dropped, so a reader can tell "not consulted" from "nothing
  found".
- **The consent gate is `advisory` on this surface and can never withhold a
  fix.** Same reasoning as the existing decision for the recovery surface: a
  customer who reported a problem is not refused a fix because of a marketing
  preference. It is read from `CONSENT_GATED_PURPOSES` rather than hardcoded, so
  a purpose added to that config shows up in the guard instead of silently
  withholding outreach.
- **A case reference is durable and derived from the primary key**
  (`CMP-{id:06d}`). The `ESC-{user}-{run_seq:03d}` form survives only as a
  documented fallback, and `build_escalation_result` reports
  `reference_is_durable` so a reader is never left wondering whether a reference
  can be looked up.
- **The recovery orchestrator's single-commit unit of work is preserved.**
  `_handle_escalate_ticket` calls `open_complaint(..., commit=False)`; a handler
  that committed on its own would make a case durable while the
  `RecoveryAction` row recording why it was opened was not.
  `test_fulfills_credit_escalation_and_guardrail` pins `db.commits == 1`.
- **Reads never write.** The old `GET /chat/recovery` inserted a
  `RecoveryOutcome` on every hit, so "attempts" inflated with traffic. Every
  complaint report endpoint is read-only; the SLA report is derived from the case
  rows on each read so a breach cannot be made to disappear by not running the
  sweep that would have noticed it.
- **A customer's 404 is not a 403.** `_load_owned_case` returns 404 for a case
  the caller does not own, so a reference is not an enumeration oracle for other
  people's complaints.

### Pre-existing bugs this expansion fixed

- **`rule_engine.matches_field_extended` matched every numeric rule
  unconditionally.** It computed `legacy` as the operators *not* in
  `OPERATOR_FAMILIES`, but `OPERATOR_FAMILIES` contains the numeric operators
  too — so for a rule containing only numeric operators `legacy` was empty,
  nothing was delegated to `matches_field`, the inline loop matched none of the
  remaining families, and the function returned `True`. `select_rules` and
  `explain_when` both route through it, so `{"days_since_login": {"gte": 45}}`
  fired on `0` and the trace reported `reason: "satisfied"` for the failure.
  Fixed by partitioning on family: `string`/`collection`/`null` inline,
  everything else delegated to `matches_field`, which also preserves both
  fail-closed properties (an unknown operator is unknown to `matches_field`, and
  an `external` operator likewise, so `evaluate_when_external` still sees the
  `False` it relies on to hand off).
- **`COMMUNICATION_POLICY_RULES["sensitive_complaint"]` had never fired.** It
  tested `top_issue_1` against `"refund request"`, `"complaints"`,
  `"billing dispute"` and `"privacy concern"`, none of which
  `chat_analytics.build_summary` can emit — it derives `top_issues` from
  `PREDEFINED_POLICY_AREAS` with underscores replaced by spaces, plus the
  synthetic `"repeated concerns"`. Now it tests the reachable vocabulary, and
  `validate_communication_tables` errors if a rule lists a value the context
  cannot hold. The existing test that exercised the precedence ladder passed
  `top_issue_1="refund request"` and so was asserting against a dead branch; it
  now uses a reachable value and a new test pins the vocabulary.
- **`models.build_relationship_catalog` reported an empty `join_columns` for all
  27 relationships.** It unpacked three names from `synchronize_pairs`, which
  yields 2-tuples, *and* then referenced a local `parent` that the function
  never assigned. Both raised, and a bare `except Exception` swallowed both, so
  the catalog has said "no foreign-key columns here" for every relationship
  since it was written. The handler is gone rather than narrowed, because the
  introspection is deterministic and verified against all 27 relationships -- a
  future breakage should be loud instead of silently reporting an empty list.
- **`retention.build_retention_recommendations` had a guaranteed `NameError`** in
  its health-band block. Repaired, not left deleted; see the expansion log.
- **`suppress_recent_complaint` is no longer dead twice over.** It needed
  `complaints_last_30d`, which no producer emitted, *and* the
  `communication_suppression` pack had no caller anywhere in the app. The
  complaint ledger supplies the count (`_with_complaint_history`, which leaves
  the key *absent* on a count failure so a transient DB problem cannot
  masquerade as "no complaints" and switch the safeguard off), and
  `resolve_recovery_outreach_strategy` now evaluates the pack and reports a
  firing suppression rule *alongside* the resolved strategy rather than
  overwriting it. That handler also became async so it can load the admin
  override, which the recovery path previously ignored entirely — meaning a
  playbook's `notify_customer` and `/chat/communication-strategy` could report
  two different strategies for the same person.

## Alembic: what was wrong, and what is still true

### Fixed (2026-09-30)

- **`0366de366666_backfill_and_enforce_booking_timestamps.py` moved to
  `alembic/archived/`.** It shipped with
  `down_revision = "<PUT_PREVIOUS_REVISION_ID_HERE>"`, a literal template
  placeholder. Because alembic builds its revision map from *every* module under
  `versions/`, that one file broke **every** command -- `upgrade`, `downgrade`,
  `history`, `current`, `heads`, `stamp` -- not just the one being run. It was
  moved rather than repaired because three facts ruled out wiring it back: nothing
  referenced it and it referenced nothing; it never sat in the deployed chain
  (the live chain starts at `20260905_01`, which declares `down_revision = None`);
  and its `upgrade()` is PostgreSQL-only (`ALTER COLUMN ... SET NOT NULL`), so it
  could not run against this project's SQLite even with a valid parent. It is
  preserved, with the reasoning, in `alembic/archived/README.md`.
- The chain is now a single linear head and `history` / `heads` / `current` all
  work. The complaint migration's `downgrade` was exercised end to end: it drops
  `complaint_decisions`, `complaint_events` and `complaint_cases`, and
  re-upgrading recreates them.
- Two genuine bugs surfaced on the way and were fixed: a missing `sqlalchemy.func`
  import in `recovery_playbooks._with_complaint_history`, hidden by an
  `except Exception`, which meant `complaints_last_30d` was never produced and
  `suppress_recent_complaint` was still dead; and
  `models.build_relationship_catalog`, which unpacked three names from a 2-tuple
  *and* referenced an unassigned local, both swallowed by a bare
  `except Exception`, so it had reported an empty `join_columns` for all 27
  relationships since it was written.

### Still true, and the thing to know before deploying

- **`alembic upgrade head` does not work against an empty database, and is not
  supposed to.** `app/main.py:116` runs `Base.metadata.create_all` on every
  startup, so the schema is owned by the models, not by the chain. Every
  migration in `versions/` creates a table `create_all` already creates, and the
  chain's base `20260905_01` issues `ALTER TABLE users ADD COLUMN is_admin`
  against a table that does not exist yet on a fresh database.
- **The working workflow for an app-created database is `stamp head`.** Then
  `upgrade head` is a no-op and subsequent migrations apply normally. Documented
  in `alembic/README.md`, which also states the two-things rule for a new table
  (the model *and* the migration) and why only the model is not enough.
- **`20260905_01` also contained three PostgreSQL-only statements**, so the chain
  had never been runnable on SQLite in *either* direction — not just the
  `downgrade` reported earlier. All three now go through `batch_alter_table`:
  adding a `chat_history.user_id` column that carries a `REFERENCES` clause,
  dropping the `users.is_admin` server default, and dropping
  `chat_history.user_id` when it is part of a foreign-key definition. Two details
  are not optional and are commented at the call site: the `users` alter needs
  `recreate="always"` (a bare `alter_column` is not a rebuild trigger, so it
  re-emits the native statement and SQLite raises `NotImplementedError` again),
  and the `chat_history` foreign key had to be given a name
  (`fk_chat_history_user_id_users`) because the copy-and-move path rejects an
  anonymous constraint. `tests/test_migration_chain.py` round-trips the whole
  chain — pre-`20260905` schema up, head, back to base — and asserts the rows
  survive. Verified by reverting each fix in turn and watching the test fail.
- `alembic/README.md` now carries the same table, because "the chain has
  Postgres-only DDL" is the kind of fact that gets rediscovered the hard way.

## Expansion log

### Stage F: why it changed, and whether it still holds (2026-10-01)

Three items. All three exist because of something Stages C and D introduced, and
that connection is the finding.

#### Why this stage was needed at all

This codebase is full of tables that decide things about people — recovery review
rules, generosity rules, care weights, loyalty thresholds, complaint severities.
Every one has been edited at some point and **none left a trace**. That was
survivable while the edits were rare and reviewed by a human who remembered them.

It stops being survivable the moment something *learns*, which is what Stages C
and D built: `care_weights` derives emphasis from observed complaints,
`offer_outcomes` ranks generosity rules by measured fulfilment, `policy_scoring`
selects rule packs. So the exposure is no longer "somebody edited a table" but
**"a table can now change itself and nobody downstream can tell what moved or
why."**

* **Conclusion.** Gap, not defect. No rule was wrong; the ability to reconstruct
  why one is right was missing.
* **Suggestion (not applied).** `policy_motion` records a motion's class, the
  evidence attached, the bar that would have counted, and which promise was in
  force. It does **not** evaluate whether the evidence was any good, and it
  applies nothing — Stage D's rule stands unchanged, because an engine that
  retunes its own table from its own outputs makes that table unauditable. The
  threshold that decides whether a Wilson bound of 0.62 is good enough for a spend
  limit belongs in a table a human edits, not in a function.

#### The bar is per class, because consequence is not uniform

| class | example | evidence bar | notify? |
|---|---|---|---|
| `coverage_tuning` | region contact windows | `observation`, 20 samples | yes |
| `emphasis_tuning` | care weights | `observation`, 30 | no |
| `entitlement_change` | generosity scales | `measured_outcome`, 50 | yes |
| `status_rule_change` | who counts as Trusted | `measured_outcome`, 100 | yes |

"We looked at the numbers" is sufficient to widen a coverage window and is **not**
sufficient to change what somebody is owed. A count is not a comparison.

`status_rule_change` is also the only class not reversible by a table edit:
somebody who was Trusted and is not has been told they were something they are
not, and no edit takes that back. Hence the largest sample bar and
`reversible_by: compensation`.

#### An unclassifiable change is a floor, not a verdict

A target this ledger does not recognise is measured against the **strictest**
class, because the assumption that fails safely is "this may be something we owe
somebody". But clearing that floor is reported `unknown`, not `sufficient` —
passing a floor is not the same as knowing what kind of change this is, and
somebody still has to say what the table governs.

#### Four defects found while building it, all found by validators rather than by reading

| what | how it was caught | why it looked right |
|---|---|---|
| the ledger docstring promised insufficient-first; the code returned input order | a test asserting the *order*, after the docstring and the code disagreed | a ledger whose stated order is not its actual order is worse than one that claims nothing — the reader trusts the order and reads past the failures |
| `check_promise_continuity` returned `not broken`, so "nothing is broken" and "nothing was looked at" were the same answer | its own validator, on an empty input | `not []` is `True`; that is correct Python and the wrong answer |
| `ever_lowered_without_evidence` counted **refused** entries, so a trajectory that correctly blocked an unjustified demotion reported that a demotion had happened | the validator failed a build whose behaviour was right | a field that reports refused events as if they occurred is read as a finding |
| the no-decay promise named a guard `status_earned_not_decayed` that **is not a registered probe** | `validate_trust_continuity` reads `PROBES` and fails the build | a promise checked by nothing is an intention. The validator existed precisely to catch this and did, on its first run |

The last is the one worth keeping. Every promise names a `regression_guard`, and
the validator **reads `real_life_flows.PROBES` rather than trusting the name**. It
failed on the first run, which is the check earning its place.

#### A process defect, recorded because it corrupted a committed file

A scripted edit that splices a tuple by regex offset corrupted
`FLOW_CATALOG` three times, and **one of those corruptions was committed** as part
of Stage E. Two flows carried their `probe_ids` list twice; the duplicate was
invisible because the file parsed, every probe still resolved, and every test
passed. A `",,` cleanup intended to remove a stray comma instead joined two
adjacent string literals — Python's implicit concatenation — producing a probe id
like `contact_hour_is_localretention_series_builds`.

* **Conclusion.** Defect in the *process*, and one that a passing suite cannot
  see. Six duplicate bindings and two synthetic probe ids survived a green run.
* **Suggestion (not applied).** This project's recurring lesson now has a
  procedural form: prefer exact literal replacement over positional splicing when
  editing a list of declarations, and re-`import` the module after every scripted
  edit rather than assuming the edit applied. The audit's
  `test_every_probe_is_exercised_by_some_flow` would have caught the duplicates
  had it compared *sets* and also rejected repeats.

#### Item 7 — membership value evolution

The Stages C/D modules each answered a question at one moment. Nobody could
answer **"why is this different from six months ago"**, which is the only form
the question takes when a customer asks it. `build_value_trajectory` returns a
trajectory of *moves* — `earned`, `confirmed`, `paused`, `reinstated`,
`lowered_for_risk` — and two properties are enforced rather than hoped for:

* **No move lowers a status without evidence.** The no-decay invariant expressed
  over time rather than over one ledger, because "status never decays" is easy to
  keep with a single ledger and easy to lose with a history of them.
* **A gap is reported as a gap.** Long absence produces a `paused` entry rather
  than nothing, because silence in a trajectory reads as a decision nobody made.

`paused` is the movement that matters most for trust, and it is the one with no
arithmetic behind it at all.

#### Audit result

Completeness share **0.8889 → 0.8936**; `complete` 40 → 42, `untested` unchanged
at 4. Three probes added (`status_never_decays`, `policy_motion_needs_evidence`,
`broken_promise_is_visible`), bound to four flows. `status_never_decays` is the
guard the `status_is_earned` promise names, and it exists because the trust
validator refused to accept a promise checked by nothing. 24/24 probes registered,
every one named by a flow, **0 blockers**, 67/67 subflows.

The share moved by less than a point, and that is the honest number: two new parts
were measured and the four already-untested ones are the same four. They are
`blockage_log`, `release_ladder`, `rule_engine` and `topics` — parts that resolve
and are callable, with no probe whose personas disagree.

**2895 passed.**

Honest limits. The ledger records the evidence it was given; it cannot tell
whether a comparison was any good, and it has no counterfactual — a motion whose
evidence shows no change is indistinguishable from one that was never measured.
`check_promise_continuity` trusts the guard results it is handed, so it reports
what a probe said rather than re-running it; that is deliberate (it must be cheap
enough to call on every sweep) and it means a stale guard result would be
reported as a held promise.

### Stage E: contact is a regional question (2026-10-01)

Five items. The first is a defect in code this project shipped last stage, and it
is the most useful thing found in this stage, so it comes first.

#### The contact gate evaluated quiet hours in UTC

`services/care_gate.py` computed the hour for quiet-hours and frequency checks as
`float(moment.hour)` on a UTC datetime. A customer in Auckland — UTC+13 in
October — had their stated 22:00-to-08:00 quiet hours checked against **UTC
hours**:

| moment (UTC) | Auckland local | the gate's answer |
|---|---|---|
| 2026-10-01 10:00Z | 23:00 | **push: true** |
| 2026-10-01 23:00Z | 12:00 | push: false |

The gate was not slightly wrong. It was wrong by up to thirteen hours for roughly
half the world, and **every test of it passed**, because every persona in the
Stage D suite lived in the server's timezone. The two personas that would have
caught it — one in Auckland, one in US/East, at the same UTC moment — did not
exist.

* **Conclusion.** Defect, not a gap. The part was built and wrong for every
  customer outside the server's own offset. Nothing in the design noticed because
  the test suite and the server shared a timezone.
* **Suggestion (not applied).** `region_windows.local_hour` now holds the
  arithmetic in one exported place, and the Stage E probe varies *only* the
  region at a fixed UTC moment so a UTC-reading implementation produces identical
  answers for every persona and the audit grades it `partial` rather than
  `complete`. The generalisable fix is a lint on any `moment.hour` outside
  `region_windows`; this module does not add one, because a lint that flags
  legitimate UTC reads is worse than none.

#### Three bugs found while building the table that fixes it

Each of these is the recurring failure class in this project — a wrong value that
looks right, caught by asserting behaviour rather than by reading code.

| what | how it was found | why it looked fine |
|---|---|---|
| `dst_offset_hours` was a *delta* in some rows and the *total summer offset* in others, so Auckland resolved to **UTC+25** | `validate_regions` range check | every region had a plausible-looking number; only arithmetic disagreed |
| `validate_regions`'s "fallback is the narrowest window" check compared `start - close` and took `min` of the negatives, selecting the **widest** region in the table | a test asserting a fact about the function rather than the intent | a check with an inverted comparison still runs, still returns a report, and reads as a check |
| a Stage D test asserted `push is True` with no `now` and no region, so it was **green by wall-clock luck** | the region change exposed it as clock-dependent | it had never failed because it was never asked at a different time |

The second is the one worth keeping. **A validator with an inverted comparison is
worse than no validator**, because it reports a finding on every run and a reader
concludes the area is checked.

#### Two corrections to my own reasoning, recorded because both felt right

* **"An unknown region should be the narrowest window."** Wrong. "We do not know
  where they are" is not "the region we do not know about is closed" — it is the
  *absence* of a region, and an absence of a region has no office hours.
  Narrowing the fallback to 09:00–17:00 weekdays made every customer with no
  declared region unreachable for most of the day: a fabricated constraint, not a
  cautious one. The fallback is now marked `unattributed`, sits on UTC, and is
  exempt from staffing closure. It is conservative about the *hour*, not about an
  office it does not have.
* **`requires_consent_gate` was all-`False` across every care step**, so the branch
  that refuses device-unlocked contact was never reachable and the invariant
  "recognition shortens authentication, not permission" was asserted in prose and
  untested in code. A step that genuinely depends on the gate
  (`send_message_unattended`) now exists so the branch is live.

#### Purpose-limited personalization

`CONSENT_GATED_PURPOSES` already listed `personalization` and `care_gate` already
refused a gated purpose without consent. That is the **outer** limit. Nothing
recorded which dimensions a message for a given purpose may use, so the inner
limit was enforced by whoever wrote the message — where the temptation is always
towards *more*, because more of it feels more relevant.

* `recovery` and `service` may use `history_reference` unconditionally: the
  message is about something that happened to this person, so referring to it is
  accuracy, not surveillance.
* `marketing` needs the `personalization` consent for it.
* `analytics` may use nothing at all — it names a measurement, not a message.
* `peer_comparison` is refused for every purpose, and
  `validate_personalization_limits` fails the build if any purpose lists it.

**The cross-check that found the gap.** `validate_personalization_limits` reads
`COPILOT_DO_NOT_LEAD_WITH` rather than restating it, and reported four protected
facts — `churn_score`, `access_band`, `policy_tier`, `investment_band` — that
were not personalization dimensions at all. They belong in both lists: a fact kept
out of an agent's opening line while a copywriter is free to reach for it is not
protected. This is why the validator reads the other table instead of holding a
copy of it.

#### The unified view, and why the copilot is the default pane

By the end of Stage D the codebase answered six questions about a customer
correctly — history, health, status, offers, journey, and what to say — and showed
them in **six places**. An agent opening a customer saw `customer_360`, which
contains none of health, status, offers or journey state, and formed a view from
what was there.

* **Conclusion.** Gap, not defect. Each answer was right; the composition was
  missing.
* **Suggestion (not applied).** Adding them to `CUSTOMER_360_SECTIONS` would have
  been the obvious move and wrong twice: it turns a composition into a
  reimplementation, and it puts *judgements* (a health band is a conclusion drawn
  from facts, and it can be wrong) next to *records*. `relationship_view`
  composes instead, and the copilot is the default pane because it is the only one
  an agent can act on directly — every other pane is evidence for it, and evidence
  an agent has to go and find does not get read.
* The default is also the **safe** direction: the copilot refuses to lead with
  internal facts and requires a human. Moving off it needs `explicit=True`, so
  "naming a pane" is not the same as "asking for one" — an agent deliberately
  inspecting a health band is doing something different from one opening a call,
  and both deserve to work.

#### Audit result

Completeness share **0.7872 → 0.8889**; `untested` 7 → 4. The three new engines
graded `untested` when first registered, which is the audit being right: a part
that resolves is not a part that has been measured. They reached `complete` only
once probes existed whose personas disagree. **0 blockers.** Four probes added
(`contact_hour_is_local`, `personalization_is_purpose_limited`,
`trusted_device_never_overreaches`, `copilot_is_the_default_pane`), bound to five
flows; 65/65 subflows exercised, 21/21 probes registered and every one named by a
flow.

One more honest limit: `contact_hour_is_local` derives its expectation from the
**stated window**, not from the region table, so it cannot detect an edit to the
offsets themselves. `validate_regions`'s range and distinct-offset checks are the
independent oracle for the table, and the arithmetic probe is the oracle for the
function.

### Stage D: the closed loop (2026-10-01)

Five items. One of them turned out to be a finding rather than a feature, and that
one is worth reading first.

#### Preference fail-closed: the finding is a count, not a theory

`customer_offers` was the **only** service in the codebase that consulted the
preference centre. Everything else that can contact somebody did not:

| path | what it chose | gate? |
|---|---|---|
| `recovery_playbooks.resolve_recovery_outreach_strategy` | channel, tone, framing | **no** |
| `resolve_recovery_callback_plan` | an owner team and a deadline | **no** |
| `loyalty_journey.next_best_action` | proactive by nature | **no** |
| `communication_strategy` | how and when to speak | **no** |

The only thing that could suppress recovery outreach was the
`communication_suppression` rule pack — which answers a *different* question
("tone this down given recent complaints") rather than "has this person told us
how to contact them". So a customer who had reported a problem, asked for
reactive contact only, and had a complaint last month could still be dialled.

`app/services/care_gate.py` is one gate every path asks, and it **fails closed in
three distinct modes** because they are genuinely different facts:

- **`preferences_missing`** — we could not read them. Do not push. Not knowing how
  somebody wants to be contacted is a reason not to interrupt them, not a reason
  to assume it is fine.
- **`preferences_empty`** — we read them and they expressed nothing. Permitted.
  Silence is not a preference, and treating an unset preference as "do not contact
  me" means nobody is ever contacted until they opt in, which is a different
  product with a different consent model.
- **`unregistered`** — a proactive surface that forgot to register itself, which is
  exactly the case the module exists for.

It keeps Stage B's split, which is what makes a preference respectable rather than
suppressive: a reactive-only customer still receives the offer and can accept it
in one tap; what is withheld is the *notification*, deferred with a time. `issue`
follows consent only for a consent-gated purpose — a marketing offer with no
consent is not created at all, because it could never be sent.

**The proof is structural.** `gate_coverage()` reads the **source** of every
declared path and fails if the gate call is absent — the same technique as the
`authz` drift report, and for the same reason: a property nobody checks is a
comment. `customer_offers.resolve_offer_contact` was **rewritten to delegate**
rather than to keep its own copy of the logic, so there is one implementation to
be wrong rather than two.

#### Offer outcomes → promotion: three bugs, all found by the module's own validator

Stage B published rates. Nothing *learned* from them. The ranking score is a
**Wilson lower bound**, because a naive accept rate rewards making fewer offers:
`1/1 = 100%` and the programme looks perfect.

1. **The formula was not the closed form.** A centre-minus-margin decomposition that
   looked equivalent returned **0.0116 for a 0/10 record** — a bound *above* the
   observed rate of zero — and returned `1.0` for both `1/1` and `1000/1000`, so a
   perfect record on one observation scored the same as a perfect record on a
   thousand. The closed form is also why `p = 0` gives exactly 0: the numerator
   collapses to `z² − z·z`.
2. **`fulfilled` was excluded from `decided`.** An offer that reached fulfilled was
   accepted first, so a tier with **six perfectly-delivered offers reported
   `decided: 0`** and was therefore ranked as *no evidence* — the best-performing
   row in the table, invisible.
3. **The generosity bucket compared each rule against itself.** Every rule reported
   identical numbers, so the ranking was decorative — a report that looks like an
   analysis and measures nothing. Offers record `generosity_scale`, not the rule
   that chose it, so attribution is by scale bucket and is published as coarse
   rather than presented as exact.

**Acceptance and fulfilment are ranked separately and never combined.** High
acceptance with poor fulfilment is a *fulfilment* problem — we are promising things
and not delivering them — and averaging the two would point an operator at the
offer volume instead of at us. On the test data, `save_critical` shows acceptance
`0.96` against fulfilment `0.09` and is correctly held rather than promoted.

**Nothing is applied.** An engine that silently retuned itself from its own outputs
would make `RECOVERY_SAVE_INCENTIVES` unauditable, and an unauditable policy table
is how a system ends up doing something nobody can explain to a customer.

#### Journey outcome loop: completion is a history question

Completion means **"reached `status` at least once"**, read from the history, not
"the state object says it is in status". A cycle sitting in `at_risk` on its fourth
lap having completed three is not 0% complete. `max_cycles` is published because it
distinguishes a working programme from a nagging one.

Stuck stages get **different** actions: `follow_up` being sticky means we accepted
and did not deliver (wake somebody up), while `offer` being sticky means we are
asking and not hearing (a channel or timing change, and *not* the customer ignoring
us). One generic "stuck" would be useless for both.

#### Care weights: emphasis, not verdict

`services/complaint_learning.py` has learned bounded, decayed, persisted weights
since it landed, and until now only a report read them — the same shape as the
`communication_suppression` pack that was authored, exported, validated, catalogued
and had no caller for its whole life.

A learned weight may change `recovery_emphasis`, `follow_up_hours`, a bounded
`generosity_nudge`, and whether an unprompted message leads with an acknowledgement.
It may **not** change severity, tier, or auto-escalation — and those three refusals
are **read from** `complaint_learning.LEARNED_WEIGHT_AUTHORITY` rather than
restated, so relaxing one upstream fails here until someone decides what a relaxed
authority means. `validate_care_weights` asserts that, and a test flips the
upstream flag to prove it fails.

Every dimension is floored in the direction that means worse: `recovery_emphasis`
cannot fall below 1.0, and the `follow_up_hours` floor equals the `critical` band's
4h SLA. `generosity_nudge` is deliberately **two-sided** — a nudge down is the point
of a nudge, and the floor that matters is enforced where the money is, in
`OFFER_GENEROSITY_RULES` with its per-kind cap.

#### Item 4 was already built in Stage B

**Investment band → generosity** shipped with Stage B and needed no Stage D work:
`OFFER_GENEROSITY_RULES` (three rules), `GENEROSITY_SCALE_BOUNDS = (0.5, 2.0)`,
`OFFER_MAX_GENEROSITY_PERCENT = 50.0`, the scale **recorded on the offer row** so it
cannot move under one already accepted, and `investment_context()` returning the one
key `resolve_offer_generosity` reads. What Stage D added is the *evidence* — item 1
ranks those rules by observed fulfilment, which is what turns the mechanism from
"it can scale" into "we know whether scaling works".

#### Verification

- `python3 -m pytest tests/ -q` → **2836 passed** (43 new in
  `tests/test_care_loop_stage_d.py`).
- `validate_care_gate()` → **5 of 5 declared proactive paths call the gate**, 0
  errors. `validate_care_weights()`, `validate_offer_outcomes()` valid.
- `gate_coverage()` → `ungated: []`.
- The gate's negative control: replacing a registry row with a function that does
  not exist makes the validator fail.
- Wilson bound: `0/10 → 0.0000` (never above the raw rate), `1/1 → 0.3784`,
  `1000/1000 → 0.9984`, `500/1000 → 0.4798`.
- `authz_drift_report(app.routes)` → **312 pairs, 0 unclassified, 0 mismatched**;
  `validate_authz()` valid.
- `from_app: ["care"]` records measured values and **app-computed wins** over a
  hand-typed `care_offers_seen`.
- `scripts/build_code_map.py` → `leaves=924 internal_nodes=84`; revision stays
  **`r6`** with a fifth r6 addendum; `scripts/sync_code_map_md.py --check` in sync.

Code map: `/meta/` `care_loop_stage_d` subservice (+4 endpoint keys, `care` added
to the kaizen measurement sources), and `CODE_MAP.md` r6 fifth addendum ·
`/meta/features`, `/meta/ecosystem`, `/meta/authz` · Tests:
`tests/test_care_loop_stage_d.py`, `tests/test_kaizen_shadow_release.py`

### Stage C: what a relationship is worth, and what to do about it (2026-10-01)

Four services, four responsibilities, deliberately not merged. The split *is* the
design, and the first version of the health module had it wrong.

#### 1. Loyalty status + experiential ledger: `services/loyalty_status.py`

The gap is narrower than "loyalty". This system already has points, a loyalty
*score*, churn risk and a next-best-action engine. What it had no answer for is
the question a retention conversation turns on: *how much is this relationship
worth, and are we being generous in proportion?* Today the only answer is a points
balance, which is a poor proxy for both halves — 40 points and four years of
reliable service is worth more than 900 points and a pattern of cancellations.

**The ledger is a computed view over events that already exist.** Nothing is
accumulated or stored. A stored copy would need its own reconciliation and would
leave every existing engine with two sources of truth for the same fact.

**Five signals, and two of them are about us, not them.** `reliability` (was the
work done), `tenure`, `follow_through`, `goodwill_returned` (came back after
something went wrong — the heaviest positive weight, because someone who reported a
problem and returned has demonstrated trust no amount of completed bookings
manufactures), and `chasing_effort`, which is **negative on purpose**: effort spent
chasing is a cost we imposed, and a ledger that only records positives cannot
represent a customer who cost us a great deal and should still be treated well —
which is the case where being stingy costs the most.

**Status is earned and never decays.** No `decay` field, no calendar transition,
and no status rule mentions recency. A customer-visible demotion for going quiet
punishes the exact pause a loyalty programme should forgive, and teaches people the
programme is not worth caring about. `validate_loyalty_status` **errors** if anyone
sets `STATUS_DECAYS_ON_INACTIVITY = True`.

**The investment band is ordinal, not monetary.** There is no revenue column in
this schema, so any LTV figure produced from it would be a guess wearing a float.
`strategic > high > standard > low > unscored` is a claim about ordering, and
`unscored` is a real band because "we don't know what this is worth" and "worth
nothing" must not collapse into one answer. Live risk **inverts** the contribution:
a customer at `critical` churn risk scores zero on it however valuable their
history, which is what routes them to an offer and a follow-up instead of an upsell.

#### Two real defects, both found by looking at the output

**A units bug that reported nobody worth anything.** The first version weighted the
raw signal sums and divided by a "reachable maximum" that was *also* in mixed
units — `tenure` is derived from a day count (2,555) while every other raw is a
0..1 ratio, so `tenure / 365.0` made it dominate by three orders of magnitude, and
the normalisation cancelled the mistake so **everyone** scored near zero. A
seven-year, fully-reliable customer with a resolved complaint scored **0.012**.
Nothing raised. The result read as "this customer is barely worth anything", which
is exactly the conclusion this module exists to reach by evidence.

Fixed by making every signal a 0..1 ratio and combining weights directly, plus a
`countable` flag per signal: an **uncountable** signal (no complaints, so no
return-after-complaint to credit) drops out of the weighted mean rather than
scoring zero. Crediting an absence would let it inflate the score.

**An ISO string silently became tenure 0.** `_aware()` returned `None` for a string,
so a serialised date vanished and a loyal customer aged to zero. Same failure class
as everything else today: a wrong value that reads exactly like a right one.

#### `unscored` is not a tier

`unscored` was the bottom rung at `min_score: 0.0` until a customer with three
cancellations and an open complaint came back as `unscored` — *no recorded
history* for someone whose recorded history was consistently bad. A zero score is
the **bottom of the ladder**, not an absence from it. It is now the ledger's
separate boolean flag; the ladder has four tiers starting at `bronze: 0.0`.

#### 2. LTV / investment steering: the mechanism, already in Stage B

`OFFER_GENEROSITY_RULES` was written in Stage B precisely so this could be filled
in without touching the offers subsystem. The **scale is recorded on the offer
row**, because a scale re-read at fulfilment time can move an amount the customer
has already accepted. An absent signal yields `1.0`, never `0.0`: failing closed on
money owed to a customer is how a well-meant generosity rule becomes a way of
short-changing people. Bounds at both ends and a per-kind cap on top, so a rule
cannot be configured into an unbounded liability.

#### 3. Relationship-health summary: `services/relationship_health.py`

**Health is the opposite of status, and that is the point.** Status is earned and
monotone; health is live and may fall. Keeping them apart is what lets a customer
be `Trusted` and `critical` at once — the combination that deserves the fastest
response, which a single blended number hides and a demotion punishes.
`assert_health_is_not_status()` raises if a report ever claims otherwise, so a
future "let's demote them when they go critical" is a visible edit.

#### **The band was inverted**

This is the defect worth writing the entry for. The signals are *risk* measurements
and the bands are named by *health*, so the arithmetic moved the right direction and
the ladder read it backwards:

| churn risk | first version | correct |
|---|---|---|
| `low` (healthy) | score 0.12 → **`critical`** | `healthy` |
| `critical` (sick) | score 0.36 → **`at_risk`** | `at_risk` |

The healthiest customers were sent to a human and the sickest to an automated
offer. Fixed by declaring `POLARITY = "higher_is_healthier"` and inverting on the
way in, so the published score reads the way the bands do.

**And the band walk was first-match in a worst-first table**, so a flawless 1.000
resolved to `at_risk`. The docstring described a best-first walk the code did not
do. The test now pins the walk, not the description.

**Dispositive findings are rules, not arithmetic.** `HEALTH_OVERRIDES` exists
because a weighted mean cannot express *"an open complaint **and** high churn risk
is a crisis even though nothing else looks wrong"* — and forcing it to means one
signal carries ~70% of the weight and the other five become decoration. So:

- `critical_churn` — never merely `strained`, whatever the arithmetic says.
- `open_complaint_and_risk` — `critical`, with a 4h SLA. Two systems failing the
  same customer at once.
- `severe_dormancy` — at most `strained`. A weighted mean cannot express "not
  heard from in six months is not healthy", because the other five signals still
  score above the healthy floor.

Churn risk does carry 42% of the composite — because the module's own prose calls
it the most predictive number available, and the weight now says so out loud
instead of the prose merely asserting it.

**`sla_hours` and `offer_window_hours` are separate columns.** `sla_hours` means *a
person is accountable*; `offer_window_hours` means *an automated credit stays worth
giving*. The first draft gave `at_risk` a 24h `sla_hours` meaning "act within a day",
and the validator caught it — an SLA nobody owns is decoration and it makes the one
that is owned read as decoration too.

**The rollup is a triage queue, not the health of the book.** It reads cheap rows
rather than building N reports (the expensive path runs eight engines per user and
one calls a sentiment model), so churn risk is published as **unmeasured** rather
than assumed good, and `is_exhaustive: False` is on the payload.

#### 4. Journey orchestrator: `services/journey_orchestrator.py`

`services/loyalty_journey.py` is a good next-best-action engine. It is also a
function of the present — run it twice on the same customer and it returns the same
plan, because it has no memory of what was done. Which produces the loop this
module exists to close:

> we recognise an at-risk customer, offer them something, they accept, and at the
> next check they are recognised as at-risk *again*, so we offer them something
> *again*.

Each pass looks correct in isolation; the repetition is only visible across passes,
which is worse than having no orchestration at all.

**It is a cycle, not a staircase.** `status → at_risk` is the only backward edge
and it is what lets a customer whose health fell be recognised again. A re-entry is
a **new attempt, not a reset**.

**The repeat rule compares a precondition hash** of (offer kind, recovery context,
health band, investment band) — not the stage and not the count. So cycling four
times does not reset the attempt history, and "we already offered this" becomes the
useful question instead of "we already offered *this thing*, in *this situation*".

**Two defects here, the second one instructive.**

1. The count was taken *before* recording, so `MAX_SAME_OFFER_ATTEMPTS = 3`
   permitted a **fourth** identical offer. The off-by-one landed exactly where
   nagging a customer who has already said no three times becomes a blocked number.
2. `next_step` then read the count off the state — which `record_attempt` writes
   for *its own* precondition — so a changed situation stayed suppressed by a
   decision made about the old one. **That is the third time in this work that a
   derived value read instead of the table has been the bug** (the first two being
   `assess_capabilities` reading `ENGINE_BY_ID` and the deferral cache). Each was
   found by asserting behaviour rather than reading code.

#### 5. Agent copilot: `services/agent_copilot.py`

An agent about to speak to a customer wants three things, and the codebase already
produced each of them: what is true (the 360), why in words a customer could hear
(`customer_explain`), and what to do next (`loyalty_journey`). **The copilot
composes them and re-derives none** — a third answer to a question the codebase
already answers twice is the one an agent would believe.

Its actual contribution is an **ordering** and a **prohibition**.
`COPILOT_DO_NOT_LEAD_WITH` is machine-readable and covers the churn score, the
access band, the policy tier and the investment band — the last being the most
indefensible thing to say out loud, because it exists to size a credit and saying
it would be accurate and monstrous. **Every row carries a `say_instead`**: a
prohibition without a substitute is a dead end, and a rule agents work around is
worse than no rule.

**The leak scan covers the whole card, not the opening.** The first version scanned
only `opening` and `headline` and passed a card whose `what_is_true` facts read
*"their churn score is 0.82 and access band is elite"* — which is the realistic
path, because `customer_360.summary_text` is a summary of the customer's own
record and it will happily contain our internal labels. Leaked facts are now
**marked in place rather than stripped**, because hiding them makes the copilot look
as though it does not know the field, and an agent who cannot see it cannot reason
about where else it leaks.

**Critical health overrides a matched journey plan.** A customer at `critical` needs
a person regardless of which onboarding or activation scenario matched, and letting
a plan argue them out of it would be the worst version of this feature: a
correct-looking recommendation to upsell somebody who has an open complaint.

**Nothing writes.** `requires_human: True` on every card, because a
customer-facing statement written by a rule engine is a promise this system cannot
keep.

#### Verification

- `python3 -m pytest tests/ -q` → **2793 passed** (114 new across
  `tests/test_loyalty_status_stage_c.py` and
  `tests/test_relationship_orchestrator_copilot_stage_c.py`).
- All four validators valid: `validate_loyalty_status()`,
  `validate_relationship_health()`, `validate_orchestrator()`,
  `validate_copilot()`.
- `authz_drift_report(app.routes)` → **308 pairs, 0 unclassified, 0 mismatched**;
  `validate_authz()` valid.
- Health ladder end-to-end: `low → healthy 1.000`, `high → strained 0.707`,
  `critical → at_risk 0.581` (override fired), `high + open complaint → critical`
  (arithmetic said `strained`).
- Orchestrator: attempts 1 and 2 execute, attempt 3 suppressed, a changed situation
  is not a repeat, `status → at_risk` counts a cycle, a five-day-old `offer` stage
  reports stuck.
- Copilot: critical churn + an LTV mention → `internal_only_facts_present` names
  both, `opening_rewritten: True`, and the offending fact is flagged with a reason.
- `scripts/build_code_map.py` → `leaves=891 internal_nodes=80`; revision stays
  **`r6`** with a fourth r6 addendum; `scripts/sync_code_map_md.py --check` in sync.

Code map: `/meta/` `loyalty_status_stage_c` subservice (+3 endpoint keys), and
`CODE_MAP.md` r6 fourth addendum · `/meta/features`, `/meta/ecosystem`,
`/meta/authz` · Tests: `tests/test_loyalty_status_stage_c.py`,
`tests/test_relationship_orchestrator_copilot_stage_c.py`,
`tests/test_kaizen_shadow_release.py`

### Stage B: customer-facing recovery offers (2026-10-01)

Five of the five items, in the order they depend on each other. Composing an
offer already existed; issuing one did not.

#### 1–2. Offers and their outcomes: `services/customer_offers.py`

`services/recovery_playbooks.py` has always known what to offer —
`RECOVERY_SAVE_INCENTIVES` holds three tiers and `resolve_recovery_incentive`
composes the right one. It says so itself: the preview carries
`"issued": False` and the registered action is described as *issues nothing*.
That boundary was correct, and it is also the gap: composing an offer and
handing it to a customer are different acts, and the second one had nowhere to
live.

**Three kinds, and no fourth catalogue.** Each names the upstream table it reads,
so an offer records *which policy fired* rather than merely that one was made:

| kind | reads |
|---|---|
| `goodwill` | `RECOVERY_SAVE_INCENTIVES` → `resolve_recovery_incentive` |
| `waiver` | `ARREARS_WAIVER_POLICY` → `evaluate_waiver_approval` |
| `priority` | `RECOVERY_REVIEW_RULES` → `resolve_recovery_review_priority` |

The third is the one that needed saying out loud. `points_exchange` and
`arrears_payments` each declare their own `PRIORITY_RANK` — both
`high/medium/low`, both meaning *rule evaluation order* — and neither means queue
urgency. Adding a third notion named `priority` would have made three things
called priority mean three different things, so this module reads
`urgent/high/normal` from the review rules and adds no fourth copy.
`validate_offers` asserts the two existing copies still agree.

**Four outcomes, and `accepted` is not `fulfilled`.** `offered → accepted →
fulfilled`, plus `declined` and `expired`. "You accepted and we have not done it
yet" is a real and embarrassing state, and collapsing it into `fulfilled` would
erase the only signal that there is a backlog — which is why
`fulfilment_rate_of_accepted` is reported next to `acceptance_rate` in the admin
rollup.

**The state machine is asymmetric on purpose.** `fulfil` is refused from
`offered`: nobody accepted it, so there is nothing to have fulfilled, and a sweep
that marked it so would record a fulfilment that never happened — while the
customer is mid-tap. `expire` is *allowed* from `offered`, twice over: an
offer's own clock running out is what `expire_offers_due` does, and an operator
withdrawing a mis-sent offer needs the same verb. Restricting *fulfilment* rather
than expiry generally is the distinction that lets both exist.

**The reference is the primary key rendered**, not a counter. This schema has
had that exact bug once: the complaint escalation id was
`ESC-{user_id}-{sequence:03d}` from a module-level counter, so it reissued its own
values after every redeploy and two customers ended up holding the same one.

#### 3. Preference gates — on contact, not on existence

The requirement is *preference gates on all proactive outreach and recovery
contact*. Read carefully that is a statement about **contact**, and the
implementation treats it that way:

- **The offer is issued and visible even when the customer asked for reactive
  contact only.** `communication_frequency: only_reactive` means *do not
  interrupt me*; it does not mean *do not tell me things*. Such a customer still
  finds the offer in their inbox and can accept it in one tap. Reading the
  requirement as "withhold the offer" hides a fix from exactly the people who
  asked not to be bothered.
- **The notification is what the preference governs**, and it is deferred with a
  `deferred_until` the scheduler can act on rather than dropped — "we did not tell
  them" is not actionable.
- Quiet hours and the stated frequency cap defer the same way, and the reason
  string carries the numbers (`3.0h` elapsed against a `24h` cap).
- **Every gate question is asked of `services/preferences.py`.**
  `effective_contact_plan` already answers permitted / reactive-only / quiet hours
  / cooldown / channel, and a second implementation of that logic is a second
  thing to keep correct.

#### Consent never withholds a fix

All three kinds are deliberately `service` or `recovery` purposes, and
`validate_offers` **warns** when a kind is not — so a campaign-flavoured fourth
kind produces a warning at review time rather than a silently suppressible fix.

The check is asked of `preferences.is_outreach_permitted(purpose,
service_critical=...)`, which is purpose-aware and is the same primitive the
contact plan uses. That detail is not incidental: the first version read the
plan's top-level `permitted`, which is the **service** gate with
`marketing_permitted` published beside it — so a marketing offer was being
created with the consent explicitly withdrawn. Caught by
`test_a_marketing_purpose_is_still_gated`.

#### 4. Consent-aware explanations

`explain_offer` changes the **framing**, never the substance. A recovery offer
exists because something happened to this customer, which is a
legitimate-interest communication, so the reason is given either way. What
consent changes is the wrapper: with no marketing consent the wording avoids
campaign language, because "we miss you, here's something special" around a
recovery credit is a retention email that happens to contain an apology.
`plain_language_explanations` keeps its meaning everywhere else.

**The internal tier name never reaches the customer.** `RECOVERY_SAVE_INCENTIVES`
calls its top tier *"Critical save offer"*, and showing a customer that string
tells them our churn model graded them critical — unsettling, and a small
disclosure of the scoring. `OFFER_HEADLINES` is customer-safe copy; the internal
name lives in `internal_name` and in the issued event payload, where an operator
can find it and a customer cannot.

#### 5. Self-service offer inbox

`GET /chat/me/offers`. Expired and declined offers are **included and labelled**,
not filtered out: an offer that quietly vanishes reads as *we never offered that*,
which is a different and worse message than *that one expired*. `actionable` is
computed against the offer's own clock rather than its stored status, so a card
never looks live and then fail on tap.

One-tap accept is **idempotent in the sense that matters**: pressing twice, or
pressing an offer that expired while the page was open, returns 200 with the
current status and an explanation. The customer asked "did that work?" and the
answer must never be a stack trace.

#### The router, and three defects on the way

`app/routers/offers.py` — its own router, because an offer has an identity a
customer quotes, a state machine, and a compliance trail. Included with **no
prefix**: the paths already carry `/chat`, and a prefix produced
`/chat/chat/me/offers` on the first attempt.

- **Three `rule_expects_more` mismatches from the drift report.** The operator
  routes were first written with a manual `if not current_user.is_admin` check.
  The drift report flagged all three, correctly: `chat_admin` declares
  `get_current_admin_user` and the route bound `get_current_user`. A dependency
  is the real gate and an attribute check is not, so the manual checks are gone.
- **`preview_waiver_offer` read the wrong key.** The natural guess from the other
  two previews is `approval["approved"]`, which does not exist —
  `evaluate_waiver_approval` spells it `allowed`. Every policy that *granted* a
  waiver came back refused, silently and pointing the wrong way. Now
  `allowed is True` for `customer-premium` and `False` for `standard`, both
  asserted.
- **`policy_score` typed as `dict` could never satisfy the policy.** The engine
  reads `getattr(policy_score, "policy_tier")`; a dict has no such attribute, so
  both reads returned `''` and `0.0` and every waiver was denied for a *missing
  tier* whatever the caller sent. `PolicyScoreIn` makes it unrepresentable.

**No new `AUTHZ_RULES` rows**, deliberately. `chat_reads` / `chat_writes` /
`chat_admin` already classify every path correctly *by prefix*, so a route added
under either prefix inherits its gate rather than having to remember it —
including `chat_admin`'s loa2 step-up, which is correct for a human committing a
credit.

#### Two tables, one migration

- `customer_offers` — mutable, because the customer reads current state. The
  three kinds deliberately do not share a value column: a waiver is worth
  nothing in points and a goodwill credit nothing as a waiver, so one `value`
  field would make `points > 0` mean "this offer has value" on rows where it means
  nothing. `generosity_scale` is recorded **on the row** because a scale
  recomputed at fulfilment time can move an amount the customer already accepted.
- `customer_offer_events` — append-only. `kind` is the verb, not the resulting
  status, because `accepted` and `fulfilled` are two events a status-only
  vocabulary would call the same thing.
- `20261001_02_add_customer_offers.py`, down_revision `0009_identity_storage`,
  single head. Verified column-for-column by `TestMigration`.

Two deliberate non-constraints, in `UNCONSTRAINED_REFERENCE_COLUMNS`:
`issued_by_id` (most offers come from the sweep, not a person) and
`arrears_entry_id` (a waiver can be offered for a charge not yet written).

#### Verification

- `python3 -m pytest tests/ -q` → **2712 passed** (76 new in
  `tests/test_customer_offers_stage_b.py`).
- `validate_offers()` → valid, 3 kinds, 5 statuses, 0 errors, 0 warnings.
- `authz_drift_report(app.routes)` → 305 pairs, **0 unclassified, 0 mismatched**;
  `validate_authz()` valid.
- `scripts/build_code_map.py` → `leaves=840 internal_nodes=75`; revision stays
  **`r6`** (leaf-level additions only, maintenance rule 6) with a third r6
  addendum; `scripts/sync_code_map_md.py --check` in sync.

Code map: `/meta/` `recovery_offers` subservice (+7 endpoint keys), and
`CODE_MAP.md` r6 third addendum · `/meta/features`, `/meta/ecosystem`,
`/meta/scoring-catalog` · Tests: `tests/test_customer_offers_stage_b.py`,
`tests/test_migration_chain.py`

### Completeness grading, one command, and the self-check timer (2026-09-30)

The flow simulator answers *does it behave*. It could not answer *is it there* —
which is the question that matters while the backend is immature. A probe against
an engine that was never written raises `AttributeError`, the harness records
`probe_raised`, and the report says *the subflow could not be exercised at all*.
True, and useless: "not built yet" and "broken" want opposite responses, and
putting the roadmap on the incident page trains people to ignore the page.

#### Six states, because a boolean cannot carry this

`absent` · `declared_only` · `stub` · `partial` · `untested` · `complete`

- **`declared_only`** is the most deceptive state and the one this repo is most
  likely to produce, because it has a governance layer that writes declarations
  by hand. A flow names a route, or `AUTHZ_RULES` describes a path, and nothing
  implements it.
- **`untested`** is its own state rather than a flag on `complete`. "We found
  nothing wrong" and "we looked at nothing" are different sentences, and merging
  them is how an unmeasured part ships on the strength of silence. The topics
  engine is fully built and completely unexercised — only a per-capability count
  could see that.
- **`stub`** is detected the only way that survives a rename: run the engine for
  two personas whose *expectations differ* and see whether it answers alike. A
  real engine branches; a stub returns one answer.

#### The rule that keeps this from becoming an escape hatch

**Absence is evidence, never a caught exception.** A probe that raised is equally
consistent with a typo, a renamed argument and a regression, so
`resolve_engine` distinguishes *absent* (the module is there and the name is not)
from *unknown* (I could not tell), `unknown` never degrades to `absent`, and a
probe is deferred **only** when its target positively failed to resolve. An audit
that guessed "not built yet" from a traceback would shrink every report it was
pointed at — and would report a healthy backend as empty and an incident as a
to-do item. `may_defer` returning `None` is the common and important answer.

#### What is fatal depends on the maturity rung

This is the requirement behind the whole thing. An immature backend is the
*starting state* of this project, so one absolute threshold is wrong at every
level: too permissive to catch a gap before launch, too strict to let work begin.

| state | blocks at |
|---|---|
| `stub`, `partial` | **every** rung — a part that exists and lies is worse than one honestly absent, at every rung, because it consumes the attention of whoever is triaging |
| `absent`, `declared_only` | from `l3_canary` — a canary routes a real person through the change, so discovering there that a flow cannot complete is too late |
| `untested` | only at `l4_live` — canarying an unmeasured part is how you measure it; shipping one as the everyday path is not |

So at `l0_draft` and `l1_verified` an empty backend **passes**, which is exactly
right: "not built yet" must not block the first commit of every feature. The
level-dependence lives in `capability_gate_measurements`, not in the gate's rule,
because a rule that has to be true for two rungs at once is a rule nobody can
read. `backend_completeness_honest` is one comparison at `l3_canary` and
`l4_live`.

#### Three real defects, each found by checking rather than by writing

**1. Naive route introspection reported 29 of 30 surfaces absent.** This
FastAPI release keeps included routers as lazy `_IncludedRouter` wrappers, so
`app.routes` lists 39 entries where **274** effective paths are served — and 26
of those 29 were false. Correctness of the entire completeness report depended on
noticing, so the resolution goes through `deps.iter_authz_routes`. Pinned by
`test_app_routes_alone_under_reports_the_served_surface`. Same failure mode as
the harness's original twelve false blockages, arrived at from the other
direction: wrong while looking rigorous.

**2. The stub detector compared different probes to each other.** It grouped
every outcome a capability produced and asked whether personas disagreed, which
compared `complaint_is_routed` against `complaint_verdict_is_explained` — two
different questions with different observation shapes — and declared a healthy
complaint engine a stub. Personas only mean anything against *the same question
asked of each of them*.

**3. Then it inverted, and reported three healthy engines as stubs.** Identical
*expectations* were being read as disagreement. The honest answer is neither
stub nor complete: every persona was asked the same question, so a constant
engine would satisfy the probe. That is a hole in the *probe*, not the part, and
it is now published as **`blind_probes`** rather than swallowed.

#### Real findings on this tree, none of which a probe could have found

- **`regulatory_complaint_deadline` names `/complaints/admin/sla-report`; the
  served path is `/complaints/admin/sla`.** Graded `declared_only` — the rule
  table describes it and no route serves it. Either the flow's inventory is stale
  or the route is unbuilt, and a rename is not a working surface.
- **4 engines graded `untested`**: `topics`, `rule_engine`, `release_ladder`,
  `blockage_log`. Built, callable, and unexercised.
- **2 orphan probes**: `posture_adjustment_is_effective` and `rule_pack_selects`
  are registered and classified but invoked by **no flow** — written, wired, and
  dead. The mirror image of an unprobed capability, and equally misleading,
  because the registry counts them as coverage.
- **11 blind probes**, so the completeness rows for three engines rest on a
  question that cannot fail.

#### One command, or one timer

Three subsystems each grew its own script, which is the standard way a codebase
ends up with a check nobody runs — and the ladder had no script at all, reachable
only over HTTP by an admin holding a token. `python3 scripts/run_kaizen.py` runs
flows, completeness, shadow isolation, authorization and the promotion gates, and
`scripts/run_kaizen.py --daemon` is the same function on a loop.
`CSERVICE_KAIZEN_AUTORUN=1` starts it as a background task on the same idiom as
the three existing workers, and it runs **immediately** rather than after the
first interval — a worker that first sleeps leaves no evidence it ever ran, and
the failure looks like health.

**Three exit codes**, because two of them must never be confused: `0` nothing
measured is wrong, `1` something measured is wrong, `2` the sweep could not
complete so the answer is unknown. Relatedly, **a gate that was never measured is
not a failed gate** — on a checkout there is no canary and no ledger, so most
gates are unmeasured by definition, and a sweep that said "no" every day would be
a sweep people learned to ignore. Only measured failures change the status; the
unmeasured ones are listed so their absence stays visible.

**The timer never writes to `BLOCKAGES.md`** unless `CSERVICE_KAIZEN_APPEND=1`.
An automatic writer fills the log with dated sections nobody wrote, and the
duplicate-section guard refuses every one of them — so it is either a no-op or a
flood, and neither is useful. Automatic means *measured on a schedule and
readable at an endpoint*; appending stays an act a person performs. `POST
/kaizen/admin/sweep` refuses `append` outright rather than discouraging it, since
an endpoint a dashboard can poll should not be able to write the log.

#### Other fixes on the way

- **Four invented engine targets.** `build_policy_tier`,
  `booking_status_flow`, `apply_preference_change`, `classify_topics` and
  `build_recovery_playbooks` were all guesses; none exists. The audit caught every
  one, which is the argument for resolving by import rather than trusting a
  hand-written list. Four phantom probe ids in `PROBE_CAPABILITY` likewise.
- **`assess_capabilities` read `ENGINE_BY_ID`, not `ENGINES`.** A derived index is
  a second copy that can disagree with the table it came from, and a caller who
  edits the table has no way to tell which one the assessor is reading. Same
  reason `validate_authz` reads tables rather than import-time indexes — and it
  is what makes the inventory testable at all.
- **`sweep_forever` could not be told to skip the shadow checks**, so the timer
  and the CLI could not both be configurable. Found by a test.

#### Verification

- `python3 -m pytest tests/ -q` → **2514 passed** (57 new in
  `tests/test_kaizen_completeness.py`).
- `python3 scripts/run_kaizen.py` → one command, exit `1`: 21 runs / 41 subflows
  exercised, 42 parts at share `0.881`, 0 blockers, 5 warnings, shadow failing
  closed (unconfigured), `in_sync: true`, and the ladder blocked at `l3_canary`
  by `backend_completeness_honest` **with evidence** while naming the three
  gates it could not measure.
- Completeness graded at each rung on the live tree →
  `0, 0, 1, 5` blocking for `l0_draft, l2_shadow, l3_canary, l4_live`.
  Monotonically non-decreasing, and asserted as such: promoting must never loosen
  the bar.
- `authz_drift_report(app.routes)` → 277 pairs, **0 unclassified, 0 mismatched,
  6 public writes on the record, `in_sync: true`**; `validate_authz()` valid.
- `scripts/build_code_map.py` → `leaves=763 internal_nodes=68`; revision stays
  **`r6`** (leaf-level additions only, maintenance rule 6) with a second r6
  addendum; `scripts/sync_code_map_md.py --check` → in sync.

Code map: `/meta/` `kaizen_release_surface` (+`kaizen_completeness`,
`kaizen_sweep`; ecosystem `config_tables` gains `CAPABILITY_STATES`,
`PROBE_CAPABILITY`, `ENGINES`), and `CODE_MAP.md` r6 second addendum ·
`/meta/ecosystem`, `/meta/features`, `/meta/authz` · Tests:
`tests/test_kaizen_completeness.py`, `tests/test_kaizen_shadow_release.py`

### Complaint learning: system-raised complaints, learned weights, and stacked-complaint suggestions (2026-09-30)

A complaint can now be a **system event**. Six detectors watch live cases and can
open a complaint attributed to a customer who never complained — a booking that
failed repeatedly, a payment that did not settle, a first response that never came,
a customer already at critical churn risk. The customer experiences all of these
before they are willing to write to us, and a system that only listens when spoken
to is blind to exactly the cases that matter most.

Three guards make unattended operation defensible, and all three are config
rather than code, so a misbehaving detector is dialled down without touching the
module: the detector's own confidence against its own gate, a per-`(detector,
customer)` cooldown, and dedupe against a genuinely separate live case. A fourth,
a per-call cap, is the guard against one corrupt snapshot becoming one case per
customer.

**Each contributing factor now carries a learned weight, and the objective is
customer retention.** Seventeen signals are read against a case and combined, each
with a learned weight that is bounded, decayed toward its configured prior, and
persisted in a table rather than module state — so it survives a deploy and a
reviewer can answer "why does `sentiment_cliff` weigh 1.8" by reading rows. The
training signal is what happened to the customer *afterwards*, not whether the
complaint was closed, and those two come apart constantly.

The direction inversion is the part worth stating, because getting it backwards
produces a system that looks like it is learning: a **damage** signal attached to
a case the customer *stayed* was overstating its damage, so it gets **lighter**.
A **protection** signal that did not translate into retention was overstated too.
A `neutral` outcome — a complaint three days old, no verdict yet — teaches
nothing at all, because counting "not yet known" as agreement is how a learner
concludes that doing nothing is correct.

**"Loyalty" here means retention and habitual return**, which is measurable from
data the schema already has: churn risk, reactivation, repeat booking, points
accrual, complaint-free lifetime. It explicitly does **not** mean contact
frequency or time-on-service. A complaint system that rewards more contact is
inverting its own purpose — the customer contacting us *is* the failure signal, and
treating it as the goal makes the angriest customer the most valued one. The
catalog states this in `objective.not_optimised` so a later change that adds
"engagement" as a positive signal has to argue with it rather than quietly
adding it.

**When complaints stack up, they become a suggestion.** Five cluster rules group
recent cases by category, owner team, origin and severity, each meeting a
multi-axis threshold. The number that matters is the **reopen rate**: a cluster of
cases that were closed and never reopened is the system working, and a cluster
that keeps coming back is the system not working. So priority is not case count —
a large stable cluster is a capacity problem and caps at medium, and a small one
that keeps reopening outranks it.

A suggestion is shaped like `efficiency_audit.EnhancementProposal` and is
registered on the release ladder at `l0_draft`, the level whose own config
describes it as "written but unexamined... where it is safe to be wrong". It must
earn promotion through the existing gates like anything else. `proposal_id` is a
hash of the cluster key rather than of the evidence, so a cluster that grows from
5 cases to 50 updates the same row and a suggestion a human has dismissed does
not reappear on the next sweep.

**`BLOCKAGES.md` is not written to.** The suggestions are made *renderable*
instead: `GET /complaints/admin/learning/blockages` returns a pasteable markdown
section and a diff of what a paste would add. The diff can only ever propose
additions — a heading already present is left alone and nothing in this file is
ever parsed for removal. That asymmetry is the safety property: this module is
allowed to add a suggestion to a human's document and is structurally incapable
of taking one of their lines out. There is no write path to this file anywhere in
`services/complaint_learning.py`, and a test asserts both the absence of the
call and the absence of a non-`GET` route.

**Two pre-existing defects found on the way.**

`ReleaseCandidate.pair()` raised `AttributeError` on every call — it passed the
dataclass to `split_version_pair`, which reads with `.get`. That is the method the
rollback path uses to name the versions it must return to, so a rollback that
tried to resolve its own version pair could not. Fixed in `app/release_ladder.py`.

`CHAIN` in `tests/test_migration_chain.py` is hand-maintained and nothing compared
it to the directory. My first migration applied cleanly in isolation and reported
itself verified while being absent from the list the chain actually runs, so all
three new tables would have shipped with no migration — the exact failure that let
ten tables ship that way before.
`test_the_chain_lists_every_migration_file_on_disk` now closes it.

### Kaizen: simulated flows, a one-way shadow, and a graded ladder (2026-09-30)

Backend-only pass. Three modules, one admin-gated router, two CLI drivers, and
one confirmed security defect fixed on the way. Frontend is out of scope for
now.

#### A confirmed pre-existing hole: `canary/promote` was unauthenticated

- **Conclusion:** `POST /meta/decisions/canary/promote` and
  `POST /meta/decisions/canary/run` decided which model scores every customer
  and moved the model's divergence counters, with **no credential of any kind**.
  `AUTHZ_RULES["meta_surface"]` classified all of `/meta*` as `public` with
  `enforced_by: ()`, the routes bound no security dependency, and
  `authz_drift_report` reported `in_sync: true` with `delta: match`.
  **Suggestion:** this is the one class of hole the drift report structurally
  cannot find, and the reason is worth writing down: a rule declaring
  `enforced_by: ()` and a route binding no dependency are *the same thing*, so
  the table was not wrong about the code — it was wrong about the world, and the
  defect **was** the agreement. Self-consistency cannot reach it.
- **Fix applied.** `AUTHZ_RULES["decision_canary_admin"]` (`exposure: admin`,
  `enforced_by: ("get_current_admin_user",)`, declared *before* `meta_surface`
  because the lookup is first-match-wins), `Depends(deps.get_current_admin_user)`
  on both handlers, and 4 new tests — including the 401 asserted on the wire and
  the model asserted unmoved afterwards, because a gate that returns 401 after
  the mutation is not a gate.
- **Structural gate added so the class cannot recur.**
  `AUTHZ_PUBLIC_WRITE_EXCEPTIONS` records every mutating route allowed to be
  public, each with the `claim` that makes it acceptable (6 rows: register,
  login, refresh, logout, webhook ingest, and `meta/decisions/simulate` — whose
  "never mutates live config" was an assertion in a docstring and nothing else,
  so it is now in a reviewable table). `authz_public_write_audit(routes)` names
  any mutating route nobody wrote down, plus any *stale* row for a route that no
  longer exists, and folds both into `drift.in_sync`.
- **And on the ladder.** `authorization_complete` is a **blocking** gate at
  `l3_canary` — the first rung that serves a real customer — measured by
  `release_ladder.authorization_measurements(app.routes)`, which goes and looks
  rather than accepting a number from the caller. `MeasureIn.from_app` lets a
  request name measurements the *server* computes; app-computed values are
  merged **last**, so `{"authz_in_sync": true}` in a body cannot override what
  this process measured. `validate_release_ladder()` caught the gate being added
  before its `_apply_rule` comparison, which is the check doing its job.

#### `app/real_life_flows.py` — 12 flows, 41 subflows, 5 personas

- **The finding that shaped the design:** a simulation with wrong assumptions
  manufactures *confident* false findings. The first version of this harness
  reported **12** blockages on a healthy tree from three mistakes — an access
  band read as a 0–1 scale when it is 0–100, `risk_rules` used as a rule-pack
  name when it is a *model* name from `model_versioning`, and a delta that would
  have been "missing" for a posture declaring `delta: 0.0`. The failure mode of
  this kind of harness is not crashing; it is being wrong while looking right.
- **Every expectation is read from config, never hardcoded** —
  `POSTURE_ADJUSTMENTS`, `CONSENT_GATED_PURPOSES`, `RULE_PACK_BY_NAME`,
  `ACCESS_BAND_RULES`, `ESCALATION_GUARDS`. Reading the table is what makes a
  probe survive a rename.
- **A probe that raises counts as *failed*,** and the `probe_raised` category
  tells the reader to read the traceback first: a raising probe is usually a
  wrong argument, and adjusting the engine to match the probe would invert the
  test.
- **5 negative controls**, all verified: consent-gating `service` → 3
  `gate_withheld_a_fix`; the catch-all posture row's `default` flag flipped →
  `invariant_violated`; a stubbed `_posture_adjustment_row` → `invariant_violated`;
  shadow pointed at live → `isolation_violation`; a feed flipped to
  shadow → live → `isolation_violation`; a forced authz drift →
  `unclassified_route`. Each restores what it touched, and
  `test_the_baseline_is_clean` runs first — without it the controls prove
  nothing.
- **Honest limit, stated in the module and pinned from the other side.** A probe
  whose expectation derives from the table it validates cannot detect an edit to
  that table; that is what makes it survive a rename.
  `TestPinningOracle` spells the posture deltas, band cut points and guard
  count out as **literals**, which is the independent oracle the probes
  deliberately do not have. The limit is not removed — it is covered from the
  other direction, and the module says so rather than implying a detection it
  cannot perform.

#### `app/shadow_env.py` — one-way, proven by identity rather than by config

- 6 isolation checks, 7 replicated feeds, 3 declared environments. **Every check
  fails closed when unobserved** — an unobserved check is a failed check.
- Identity is `(scheme, host, port, database)`. `identity_key` **excludes the
  username**, so a read-only replication role does not read as a different
  database. `same_database()` treats an uncomparable identity as *the same*
  database, which also fails closed.
- `CSERVICE_SHADOW_DATABASE_URL` has **no fallback** to `DATABASE_URL`. A shadow
  that inherited live's URL by default would be a live deployment wearing a
  shadow's name, and the only defence would be a convention somebody remembers.
- **4 defects fixed here.** `database_identity` treated an unparseable or
  databaseless URL as `comparable` — fail-open, so a typo produced
  `isolated: true`; it now requires a plausible host and a database name.
  `evaluate_isolation` let a check *implementation* raise, which aborted the
  whole verdict; a raising check is now recorded as failed. `validate_shadow_env`
  read the module `SHADOW_FEEDS` for its direction report while using
  observations for the verdict; it now honours the observed feeds. And
  `?severity=` on the blockages endpoint was a free `str`, so
  `?severity=catastroph` returned `200` with `count: 0` — a filter that fails
  open on the input an operator is most likely to get wrong; now a `Literal`
  and a 422.

#### `app/release_ladder.py` — 5 levels, 11 gates, a 5-event ledger

- Level entry gates: `l0_draft → ()`; `l1_verified → (unit_suite_green,
  flow_simulation_clean)`; `l2_shadow → (shadow_isolation_clean,
  flow_simulation_clean, data_pipelines_one_way)`; `l3_canary →
  (shadow_divergence_within_tolerance, rollback_target_available,
  regressions_none, authorization_complete)`; `l4_live →
  (canary_error_rate_below_threshold, rollback_target_available)`.
- **Code and data version as one promotable pair.** A rollback that restores the
  code and keeps the data is not a rollback. Rollback **appends**
  (`kind: "rollback"`, `restores: <event_id>`), so 7 → 8 → 7 is three events and
  the history stays honest. Window is the last **5** *events*, not 5 versions.
- **3 defects fixed here, the first of them the worst.**
  `gates_for_candidate` read `required_gates_for(candidate.level)` — the rung
  **already occupied** — so the check could only ever fail on evidence already
  used and the *next* rung's gates were never consulted: `l4_live` was reachable
  with `canary_error_rate` never measured. It now defaults to `next_level(...)`,
  and `for_level=` preserves the "how is this candidate doing" reading. Second,
  `rollback_targets` excluded the *oldest* event instead of the currently-live
  one (`live_events()` is oldest-first). Third, the threshold fields were listed
  in gate `checks`, which made `DEFAULT_DIVERGENCE_TOLERANCE` and
  `DEFAULT_CANARY_ERROR_THRESHOLD` unreachable — a gate is unmeasured until every
  name in `checks` is supplied — so they were removed from `checks` and are now
  usable as per-candidate overrides.
- Gates evaluate the **destination** rung. A refused advance is `200` with the
  blocking failures, not a `409`: an admin asking why a candidate is not moving
  is asking a question, and a 409 throws the answer away.
- **Deploying is deliberately not an HTTP verb.** Only promotion is exposed, so
  the gates are read before anyone can act on them.

#### The router, and why it is not under `/meta*`

- 16 routes under `/kaizen/admin*`, gated as **one block** by
  `AUTHZ_RULES["kaizen_admin"]` so a route added under the prefix inherits the
  gate rather than remembering it. Not `/meta*`, which is public by intent and
  this contains three mutating endpoints. Included **without** a prefix — a
  prefix produced `/kaizen/kaizen/...`, caught by the drift report.
- `POST .../blockages/append` is separate from `GET .../blockages` so a polling
  dashboard cannot append on every poll. It refuses a duplicate section unless
  `allow_repeat`, and says so.
- `CandidateIn` is `extra="forbid"`, because a body naming `level: l4_live`
  would otherwise get a `201` and a candidate at draft — a request that asked to
  go live and was told yes, with a body that says otherwise.
- `/kaizen/admin/shadow/verify` now overrides **all six** blocking inputs. It
  previously overrode only `shadow_url` and `shadow_env_name`, which left
  `write_scope` and `live_read_only` reachable only by mutating the environment
  of the process being asked whether it is safe.

#### Verification

- `python3 -m pytest tests/ -q` → **2330 passed** (187 in the new
  `tests/test_kaizen_shadow_release.py`).
- `python3 scripts/run_flow_simulation.py` → 21 runs, 41 subflows, **0
  failures**, 0 blockers, 0 warnings.
- `python3 scripts/run_shadow_env.py --strict` → exit **1**. Unconfigured, and it
  **fails closed** with `database_identity`, `environment_marker` and
  `write_scope` rather than passing for no reason; `feed_direction` is the one
  check that passes, and it passes because 7 channels really are one-way.
- `python3 scripts/run_flow_simulation.py --strict` → exit **0** (exit 2 on a
  blocker).
- `authz_drift_report(app.routes)` → 262 pairs, **0 unclassified, 0 mismatched,
  6 public writes, all on the record, `in_sync: true`**.
- `scripts/build_code_map.py` → `leaves=704 internal_nodes=65`; revision stays
  **`r6`** (leaf-level additions only, maintenance rule 6) with an r6 addendum;
  `scripts/sync_code_map_md.py --check` → in sync.
- `scripts/schema_drift_report.py` → 22 live tables, 22 declared, in sync.

Code map: `/meta/` `kaizen_release_surface` + `authz_catalog.public_writes`, and
`CODE_MAP.md` r6 addendum · `/meta/ecosystem`, `/meta/features`,
`/meta/authz`, `/meta/scoring-catalog` · Tests:
`tests/test_kaizen_shadow_release.py`,
`tests/test_decision_quality_expansion.py`

### Complaints: case spine, escalation engine, decision support (2026-09-30)
- `app/models.py`: `ComplaintCase` / `ComplaintEvent` / `ComplaintDecision`,
  with `ENUM_FIELD_SPECS`, `TABLE_LIFECYCLE`, `UNCONSTRAINED_REFERENCE_COLUMNS`,
  `OPEN_TEXT_COLUMNS` and `TABLE_FIELD_SENSITIVITY` entries for every new column.
- `alembic/versions/20260930_01_add_complaint_cases.py`, down_revision
  `20260929_01_add_preference_consent_tables`. Verified column-for-column and
  index-for-index against `Base.metadata` in `TestMigration`.
- `app/services/complaints.py` — the engine. `validate_complaints()` returns
  errors vs warnings over ten named checks; `build_complaints_catalog()`
  publishes every table plus its operator and severity vocabulary.
- `app/routers/complaints.py` + `schemas/chat.py` — 18 routes, 8 contract
  families, all classified in `deps.AUTHZ_RULES` (four new rules, including one
  for the bare `POST /complaints`, which `/complaints/*` does not match).
- `recovery_playbooks`: `escalate_ticket` opens a real case; `notify_customer`
  loads the admin override and runs the suppression pack;
  `_with_complaint_history` supplies `complaints_last_30d`.
- `communication_strategy`: `validate_communication_tables` added and
  `sensitive_complaint` repointed at the reachable vocabulary.
- `rule_engine`: `matches_field_extended` family partition fixed (see above).
- Added `tests/test_complaints_expansion.py` (114 tests), most of them against a
  **real in-memory SQLite** rather than a session double, because this surface
  is a state machine with a unique constraint and a feedback loop and a double
  would assert my own assumptions back at me.
- `CODE_MAP` r5 → r6: `routers/complaints` (17 leaves for 18 routes, per
  rule 3), `services/complaints` (19 leaves), 8 new `schemas_chat` leaves.
  653 leaves / 60 internal nodes, up from 608 / 58.

### Clear-all pass: alembic, lint baseline, complaints follow-ups (2026-09-30)
- `alembic/archived/` + `alembic/README.md` (see the section above).
- **Lint baseline cleared: 124 -> 0.** Not a blanket `--fix`, because two of the
  categories produced *false* positives that a silent fix would have shipped as
  bugs:
  - `F841` does not count a reference from inside a lambda as a use.
    `routers/users.py` had a `unknown = datetime.min...` sort sentinel used only
    by a sort key lambda; removing it raised `NameError` and failed one test. The
    sentinel is back, with a note at the site.
  - 86 of the `F401`s included imports that were genuinely load-bearing while
    looking unused. `policy_scoring._topic_catalog_size` resolved a
    config-declared catalog name with `globals()[source]`, so the
    `from app.services.topics import TOPIC_CATALOG` was a real dependency no
    linter could see, and an unknown name raised a bare `KeyError`. Replaced with
    an explicit `TOPIC_CATALOG_SOURCES` registry: the import is now a visible
    reference and a typo names the valid options.
- Dead code removed: 5 duplicate function definitions in `models.py` (270 lines;
  the later binding always won, so the earlier copies were unreachable),
  14 unused locals, and a `dataclasses.replace()` whose result was never read.
- `app/cell_matrix.py`: added a `complaint_case` resource (`summary`,
  `resolution_note`, `factors_json`) so the admin rule can name a cell that
  exists. `factors_json` is `read` for admin/agent and `none` for auditor and
  owner -- the decision snapshot is the most re-identifying field in the family.
  `deps.AUTHZ_RULES["complaints_admin"].hardening.cell` now points at it and
  `validate_authz` reports 0 warnings.
- `tests/_doubles.py`: extracted the three async-session doubles that
  `test_recovery_governance_expansion.py` and
  `test_predictive_recovery_expansion.py` each carried a copy of. They had
  drifted -- the complaints work had to be applied to both `_FakeDb` classes --
  and 163 lines went with them.
- `open_complaint` now reports `existing_open_case`: a repeat complaint in a
  category that already has a live case is **linked, not blocked**. Refusing it
  would be `suppress_recent_complaint` applied to the wrong place, since that
  rule exists to stop outreach rather than the right to complain. Pinned by
  `test_a_repeat_complaint_is_reported_not_blocked`, and
  `test_lodging_is_never_gated_by_consent` asserts `open_complaint` reads no
  consent state at all.
### Repaired `build_retention_recommendations` (2026-09-30)
- `app/services/retention.py`: the health-band block referenced a local `health`
  the function never assigned, so it raised `NameError` on every call past the
  empty-series early return. It went unnoticed because the function has no
  callers — nothing routes to it — so the crash could not fire in production
  either. Fixed rather than left deleted, by calling the existing
  `build_retention_health_report(snapshots)["health"]` instead of assembling a
  context locally: that helper already does
  `snapshot_series_points` -> `build_retention_health_context` ->
  `resolve_retention_health` and is what the async dashboard helpers use, so this
  is what keeps the recommendation's band identical to the one shown everywhere
  else. A local assembly would have been a second, free to drift.
- 6 new tests in `tests/test_retention_policy_expansion.py`, against a real
  in-memory SQLite rather than the file's scripted `_FakeDb` (the function makes
  two reads and a single scripted result would hide a mistake in either). One
  asserts the band agrees with `build_retention_health_report`; one is a static
  scope check that the function reads no name it does not assign, because a
  `NameError` in an uncalled function is invisible to every runtime check. Both
  were verified to fail when the bug is reintroduced.
- `tests/_doubles.py` grew a `SqliteHarness`, and `test_complaints_expansion.py`
  now uses it instead of its own copy — the event-loop plumbing is the part that
  is easy to get wrong, so it lives in one place.

- 6 new tests for the swallowed-error class of bug, including an AST check that
  every feed collector handles `SQLAlchemyError` and nothing broader.

### Consolidation (committed `fc1600a`)
- `analyze_sentiment` + constants centralized in `services/chat_analytics.py`
  (import cycle resolved).
- `build_system_improvement_pack` is the single sync canonical builder.
- `RETENTION_KEYWORDS` wired into `score_area`.
- Single `/meta/capabilities` registration; `/meta/features` doc fix;
  `users._current_control_posture` deps alias.
- Added `tests/test_consolidation_regressions.py` (13 tests).

### Dynamic rule engines (committed `4f6332e`)
- `AREA_SCORING_RULES` config table + config-driven `score_area`,
  `score_all_areas`, `build_area_scoring_catalog`, `build_area_keyword_catalog`;
  optional `chat_limit`/`booking_limit` on `load_user_interaction_window`.
- `policy_scoring.py` config tables (`POLICY_TIER_RULES`,
  `CONTROL_POSTURE_RULES`, `ACCESS_BAND_RULES`) + resolvers +
  `build_policy_tier_catalog`; legacy helpers delegate.
- `main.py` `/meta/scoring-catalog` endpoint; documented in `/meta/features`.
- Added `tests/test_dynamic_rules_expansion.py` (19 tests).

### Loyalty journey engine (new: `services/loyalty_journey.py`)
- Hypothesized all new->loyal customer scenarios and encoded them as a
  data-driven `LOYALTY_SCENARIO_CATALOG` (17 scenarios across 10 families:
  onboarding, activation, delivery, habit, recovery, retention, churn, trust,
  monetization, data). Each rule declares a `when` condition DSL evaluated
  against `JourneyContext`; adding a scenario is config-only.
- `match_loyalty_scenarios` / `build_loyalty_journey_plan` (pure),
  `build_loyalty_journey_plan_for_user` (async orchestrator),
  `build_loyalty_journey_admin_report` (per-user rollup with family coverage),
  `build_loyalty_scenario_catalog` (introspection).
- Routes: `GET /chat/loyalty-journey` (self) and
  `GET /chat/admin/loyalty-journey` (admin).
- `main.py`: `/meta/scoring-catalog` now also exposes `loyalty_scenarios`;
  `/meta/features` documents both journey routes; `/meta/ecosystem` lists the
  self route under chat_intelligence.
- Added `tests/test_loyalty_journey_expansion.py` (19 tests).

### Activity-tree monitoring (new: `services/activity_tree.py`)
- Monitoring expansion: customer activities organized as query-time tree
  structures — group by configurable axis (lifecycle_stage, value_tier,
  customer_classification, churn_risk, journey_family), rank members by
  configurable metric (loyalty_score, monetization_readiness, signal_strength,
  churn_risk_score, activity_count) in asc/desc order, smart-filter
  (loyalty range, churn bucket, sentiment, free-text `q`, anomalies_only),
  and highlight anomalies.
- Anomaly highlighting has two layers: absolute rules
  (`ACTIVITY_TREE_ANOMALY_RULES`: churn_spike, loyalty_drop, negative_sentiment,
  cancellation_burst, signal_spike, dormancy) plus group-relative deviation
  rules (`ACTIVITY_TREE_RELATIVE_ANOMALY_RULES`: loyalty_gap, churn_deviation);
  leaf activities are flagged too (negative-chat keywords, cancelled bookings).
  All rules are config tables — adding an axis/rank/rule is config-only.
- `load_history_totals` made public (was `_load_history_totals`) and
  `resolve_top_journey_family` added to `loyalty_journey.py` for reuse.
- Routes: `GET /chat/activity-tree` (self tree, grouped by activity kind) and
  `GET /chat/admin/activity-tree` (cross-user grouped tree).
- `main.py`: `/meta/scoring-catalog` exposes `activity_monitoring` catalog;
  route validation for group/rank/filter params lives in the router
  (`Query(pattern=...)` → 422). `/meta/features` + a new `activity_monitoring`
  subservice in `/meta/ecosystem` document the surface.
- Added `tests/test_activity_tree_expansion.py` (29 tests).

### Communication-strategy resolution (new: `services/communication_strategy.py`)
- Capability to communicate with users based on many criteria **in precedence
  order**: 1) admin select, 2) policy defined, 3) culture, 4) user profile
  (incl. collected history stats), 5) user mood present in the session,
  6) default fallback. Every resolution returns the full decision trail so the
  chosen tone/channel/framing is explainable.
- Config tables (adding a profile/rule/culture/mood/cue is config-only):
  `COMMUNICATION_OVERRIDE_CATALOG` (admin-selectable profiles),
  `COMMUNICATION_POLICY_RULES` (`when`-DSL over the user context incl.
  numeric ops and top-issue lists), `COMMUNICATION_CULTURE_RULES`
  (locale-keyed, `global` fallback excluded from matching),
  `COMMUNICATION_PROFILE_RULES` (stage/tier/churn/family/readiness),
  `COMMUNICATION_MOOD_RULES` + `COMMUNICATION_MOOD_CUES` (keyword-cue session
  mood detection layered on sentiment).
- New table `communication_overrides` (`UserCommunicationOverride` model) so an
  admin selection persists per user (unique user_id).
- Routes: `GET /chat/communication-strategy` (self),
  `GET /chat/admin/communication-strategy` (per-user rollup + layer coverage),
  `GET /chat/admin/communication-overrides`,
  `POST /chat/admin/communication-overrides`,
  `DELETE /chat/admin/communication-overrides/{user_id}`.
- `main.py`: `/meta/scoring-catalog` exposes `communication_strategy` catalog
  (precedence, override catalog, policy/culture/profile/mood rules, mood cues);
  `/meta/features` + new `communication_strategy` subservice in
  `/meta/ecosystem` document the surface.
- Note: `analyze_sentiment` calls Hugging Face and returns `None` on failure;
  session-mood detection treats that as neutral and is keyword-cue driven.
- Added `tests/test_communication_strategy_expansion.py` (28 tests).

### Arrears payments with policy-selected interest (new: `services/arrears_payments.py`)
- Capability: users may **pay in arrears** (defer a payment); interest terms
  are **policy-selected**, not flat. `ARREARS_INTEREST_POLICIES` is a config
  table of `when`-DSL policies — new customer growth deferral (9% / 30-day
  grace), VIP premium deferral (6% / 45-day grace), high-risk secured deferral
  (24% daily-compounded / 7-day grace, hard cap), large project installment
  (service_type + principal-gated), trust-repair deferral (journey-family
  gated), standard deferral, and a permissive universal fallback so every user
  has baseline terms. Adding a policy is config-only.
- Interest accrues only after the grace period, is computed on demand
  (`compute_arrears_interest`: simple or daily-compounding, capped at a
  percentage of principal), and the matched policy's terms are **snapshotted**
  onto the row at open time so later catalog edits never rewrite open
  agreements.
- New table `arrears_entries` (`ArrearsEntry` model); statuses open/settled/
  waived; settle charges accrued interest (principal-only after a waiver);
  waive forgives accrued interest and yields to a principal-only settlement.
- Routes: `GET/POST /chat/payments/arrears` (self list + open),
  `GET /chat/payments/arrears/quote` (no-write quote),
  `GET /chat/admin/payments/arrears` (rollup: principal/interest at risk,
  overdue count, policy coverage),
  `POST /chat/admin/payments/arrears/{entry_id}/settle`,
  `POST /chat/admin/payments/arrears/{entry_id}/waive-interest`.
- `main.py`: `/meta/scoring-catalog` exposes `arrears_payments` catalog;
  `/meta/features` + new `arrears_payments` subservice in `/meta/ecosystem`.
- Note: the journey engine scores an empty history as `value_tier: premium` /
  `journey_family: onboarding`, so quote/open requests on a bare (no-history)
  profile match the VIP policy and the premium exchange rule below. This is
  inherited engine behavior (kept as-is).
- Added `tests/test_arrears_payments_expansion.py` (33 tests).

### Points <-> money/currency exchange (new: `services/points_exchange.py`)
- Capability: convert/exchange **back and forth** between *certain types* of
  points and money/currencies. `POINTS_EXCHANGE_RULES` is a config table: each
  rule binds a (point_type, currency) pair and declares which directions are
  allowed (`redeem` = points -> money, `purchase` = money -> points), the rate
  (points per currency unit), fee %, minimums, daily caps, and `when`-DSL
  eligibility. Examples: loyalty points redeem+purchase in USD/EUR (premium
  users get 90 pts/$ vs 100 pts/$ standard), activity points gated to
  non-new stages, cashback points redeem-only with no fee, referral points
  cannot be purchased at all. Adding a rate/currency/rule is config-only.
- Pure core `quote_points_exchange`/`select_exchange_rule` calculate
  gross/net/fee and enforce minimums and daily caps both ways; execution
  debits/credits a per-type wallet and writes a full ledger row
  (`points_wallets` + `points_transactions` tables, `PointsWallet` /
  `PointsTransaction` models; unique user/point_type on wallets).
- Routes: `GET /chat/points/exchange/rates` (rate card),
  `GET /chat/points/wallet` (balances incl. zero-filled convertible types),
  `GET /chat/points/transactions` (ledger),
  `POST /chat/points/exchange/quote` (no-write quote),
  `POST /chat/points/exchange` (execute, both directions; 422 on
  ineligibility/insufficient balance/daily-cap breach),
  `GET /chat/admin/points/exchange` (cross-user rollup, top user, by-kind and
  by-point-type coverage).
- `main.py`: `/meta/scoring-catalog` exposes `points_exchange` catalog;
  `/meta/features` + new `points_exchange` subservice in `/meta/ecosystem`.
- Note: `_load_user_strategy_inputs` in `communication_strategy.py` was made
  public as `load_user_strategy_context` (same signature) so the payment
  engines share the user-metrics loader; both keep-as-is names untouched.
- Added `tests/test_points_exchange_expansion.py` (30 tests).

### Small-component expansion (infra + audit trail)
- `security.py` (25 -> 121 LOC): access vs refresh token types (`typ` claim),
  `decode_access_token` (used by `deps.py`), configurable password-strength
  policy with `password_policy_payload`; `create_access_token` behavior for
  existing callers is unchanged (adds `typ=access` claim only).
- `db.py` (35 -> 103 LOC): env-tunable pool settings (`DB_POOL_SIZE`,
  `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_ECHO`), non-raising
  `ping_database`, `retry_async` for idempotent recovery paths; `engine`,
  `Base`, `SessionLocal`, `get_db`, `test_connection` untouched.
- `i18n.py` (42 -> 152 LOC): French locale added (requirements mandate
  en/es/fr), `MESSAGE_CATALOG` with per-locale templates, `translate()` with
  English fallback, `build_i18n_catalog()`; `resolve_locale`/`locale_payload`
  contracts unchanged.
- `deps.py` (78 -> 95 LOC): decode now delegates to
  `security.decode_access_token`; new `get_current_user_optional` dependency.
- New audit-trail domain (smallest public surface was `routers/audit.py`):
  - `models.AuditLogEntry` (persisted, severity constrained to
    info/warning/critical).
  - `services/audit_log.py` (185 LOC): `AUDIT_ACTION_CATALOG` config table,
    `record_audit_log_entry`, `list_audit_log_entries`, `count_audit_log_entries`,
    `build_audit_log_summary`, `build_audit_log_catalog`.
  - Routes: `POST /audit/log` (201), `GET /audit/logs` (filterable),
    `GET /audit/logs/summary`, `GET /audit/trail-catalog`.
  - `main.py`: `audit_trail` + `localization` subservices in `/meta/ecosystem`,
    new endpoint keys in `/meta/features`, `audit_log`/`i18n`/`password_policy`
    catalogs in `/meta/scoring-catalog`, and `GET /meta/i18n`.
- New code map artifact: `CODE_MAP.newick` (strict single-line Newick tree of
  the whole backend) + `CODE_MAP.md` (governance, reading rules, revision log).
- Added `tests/test_security_infra_expansion.py` (13 tests) and
  `tests/test_audit_log_expansion.py` (14 tests); suite 351 -> 378 passing.

### Backend evolution stages 1A/1B/1C (multi-tenancy, zero-trust, event pipeline)
- New infra modules (app/ root, all opt-in — single-tenant defaults untouched):
  - Stage 1A: `tenant_router.py` (`TenantRouter`: runtime register/deregister,
    DSN template/declarations, `get_tenant_session`/`get_tenant_db`/
    `get_request_tenant_id`, catalog) + `partition_manager.py`
    (`PARTITION_POLICIES` config, ensure/archive/drop-expired,
    `run_partition_cycle_forever` worker behind `CSERVICE_PARTITION_WORKER`).
  - Stage 1B: `hsm_signer.py` (`HsmSigner` protocol; `hmac` default backend,
    `ed25519`/`mock` via `HSM_BACKEND`; sign/verify, signature envelopes,
    catalog), `biometric_vault.py` (salted-digest vault, similarity-matched
    validation, `_digest_similarity` matching bug fixed),
    `risk_evaluator.py` (`RISK_RULES` config, levels, adaptive weights with
    `reset_weight_state()`), `cell_matrix.py` (`CELL_MATRIX` config, role
    groups, cell access evaluation).
  - Stage 1C: `protobuf_transaction_spec.py` (self-contained protobuf wire
    encoder — varint + length-delimited, deterministic field order, **no
    protoc/generated code**; SHA-256 `prev_hash` frame chain; optional
    file-backed append-only log via `TRANSACTION_LOG_PATH`) +
    `high_throughput_pipeline.py` (bounded queue, workers, submit/batch/
    drain/dead-letter/stats, protobuf-sink flush; autostart behind
    `CSERVICE_PIPELINE_AUTOSTART`).
- `model_bases.py`: isolated polymorphic bases (`TimestampMixin`,
  `TenantScopedMixin`, `PartitionedMixin`) + single-table-inheritance
  `SecurityEvent` family; `models.py` derives from it and re-exports
  `TimestampMixin` (flat `models.py` untouched otherwise — no alembic risk).
- New audit endpoints in `routers/audit.py`: `POST /audit/pipeline/event`
  (202, async — accepted events stay queued until workers/`drain()`/`stop()`
  flush them to the transaction log), `GET /audit/pipeline/stats`,
  `GET /audit/transactions`, `GET /audit/transactions/spec`; 5 new schemas
  in `schemas/audit.py`.
- New meta endpoints `/meta/tenants`, `/meta/partitions`, `/meta/zero-trust`;
  `main.py` adds `tenant_routing`, `partition_management`, `zero_trust`,
  `high_velocity_audit` subservices + features/scoring-catalog keys; lifespan
  wires partition worker + pipeline + `tenant_router.registry.dispose_all`
  (all env-gated, default off).
- **Code map now in-repo**: `scripts/build_code_map.py` is the builder
  (nested tuples → serialize → parse-validate; prints `leaves=N
  internal_nodes=M`); map regenerated to 230 leaves / 44 internal nodes
  (was 174/32), revision bumped to `r2` with new infra groups
  `multi_tenant` / `zero_trust` / `event_pipeline` and models group
  `model_bases`.
- **Pipeline semantics**: event ingestion is a queued accept (202), not a
  synchronous write — durability happens when workers run or on
  `drain()`/`stop()`.
- Tests: `tests/test_tenant_router_expansion.py` (14),
  `tests/test_zero_trust_expansion.py` (17),
  `tests/test_high_velocity_audit_expansion.py` (18) — suite 378 → 427
  passing.

### Phase 1 — Decision quality & consistency (S-02 / S-03 / C-05 / M-03)
- New infra modules (app/ root, pure computation — no workers, no DB, no
  single-tenant behavior change; surfaces are opt-in meta tooling):
  - S-03 `model_versioning.py`: `ModelVersionRegistry` (named models with
    immutable version snapshots; default registry pre-seeded with `risk_rules`
    from the effective risk table and `partition_policies`) + `CanaryRunner`
    shadow-mode scoring — served decisions always come from the active model,
    the candidate scores in shadow, divergence/confidence accumulate; promote
    and rollback are optimistic-lock guarded; auto-promotion behind
    `CSERVICE_CANARY_AUTOPROMOTE` (default off) requiring
    confidence ≥ 0.9 over ≥ 3 runs.
  - S-02 `simulation_engine.py`: pure what-if engines —
    `simulate_risk_whatif` (weight overrides / disabled rules / level-threshold
    retune applied to a copy of the rule table, never live weight state) and
    `simulate_retention_whatif` (replays drop/keep decisions under modified
    retention windows, no DDL); `run_simulation` dispatch records a trace.
  - C-05 `explainability.py`: `DecisionTrace` (inputs, factor trail with
    weights, threshold, model version, outcome) + bounded `DecisionTraceStore`
    (default capacity 1000, oldest evicted), `explain(decision_id)`, filterable
    listing, and a risk-result -> trace converter.
  - M-03 `optimistic_locking.py`: `VersionedRecord` compare-and-swap guard +
    `StaleVersionError` (carries expected/current versions); used by model
    promotion/rollback; mapped to HTTP 409 on the promote endpoint.
- Additive `risk_evaluator` surfaces (folded under the existing `evaluate`
  leaf, live scoring untouched): `score_with_config` (pure config-parameterized
  scoring used by canary + simulation) and `effective_risk_rules` (config +
  learned weights snapshot).
- New meta endpoints: `GET /meta/decisions`, `POST /meta/decisions/simulate`
  (risk|retention), `POST /meta/decisions/canary/run` (shadow score),
  `POST /meta/decisions/canary/promote` (stale `expected_version` -> 409),
  `GET /meta/decisions/explanations[_/{decision_id}]` (404 when unknown).
- `main.py`: `decision_intelligence` subservice in `/meta/ecosystem` (+
  `canary_auto_promote`), six new features keys, and a
  `decision_intelligence` scoring-catalog key with all four catalogs.
- Code map: new `decision_intelligence` infra group (`model_versioning`,
  `simulation_engine`, `explainability`, `optimistic_locking`) + `decisions`
  leaf on `main`; regenerated to 251 leaves / 49 internal nodes (was 230/44),
  revision bumped to `r3`.
- Tests: `tests/test_decision_quality_expansion.py` (28) — suite 427 → 455
  passing.

### Stage 1 R1–R3 — business-rule hyper-flexibility (r4 expansion)
- R1 shared `when`-DSL core (`app/rule_engine.py`): the 4 duplicated engines
  (loyalty_journey, communication_strategy, arrears_payments, points_exchange)
  now delegate to one core and stay behavior-identical (delegation only);
  `risk_evaluator`'s `(op, value)` engine is a different DSL and was left
  as-is. New capabilities: `any`/`all`/`not` combinators, reserved `_date` key,
  date-window operators (`between_dates`, `month_in`, `weekday_in`, `on_date`,
  `within_days`), and safe `=formula` expression params (`evaluate_expression`
  AST-walked calculator; imports/attributes/subscripts rejected at parse).
- R2 points dynamic rates: `POINTS_TIER_MULTIPLIERS`,
  `POINTS_LTV_MULTIPLIERS`, `POINTS_CAMPAIGN_RULES` config tables; multipliers
  compose tier × LTV × campaign. **Key keep-as-is decision:** pinned quote
  contracts (premium 90 pts/$ on empty-history defaults, standard rate math)
  must not change — so multipliers are applied *only* when a quote is
  explicitly driven via `effective_date` and/or `param_overrides`
  (campaign/seasonal evaluation is a deliberate, review-first op action; the
  default user path is never silently repriced). `param_overrides` may carry
  `=formula` `points_per_unit` resolved by the shared rule engine.
- R2 arrears fees + waivers: optional `late_fee_amount` / `late_fee_pct`
  policy params snapshotted onto rows at open (5 additive `ArrearsEntry`
  columns; `getattr` defaults keep fake-row tests safe); fee charged at
  settlement when past-due and not waived; `ARREARS_WAIVER_POLICY` +
  `evaluate_waiver_approval` gate interest/fee waivers on the operator's
  `PolicyScoreSnapshot` (customer-premium tier rank + access score; fees need
  access ≥ 70). **Keep-as-is decision:** legacy calls without a policy score
  stay un-gated (existing integration/tests untouched).
- R3 `app/regional_policy.py`: `REGIONAL_CALENDARS` (global/us/de/jp; fixed +
  annual holidays), `REGIONAL_LABOR_RULES` (shift hours / consecutive days /
  rest), `TAX_RULES` (de 19%, jp 10%, us project 8.5% example); pure engine,
  `bookings.py` lifecycle + pinned routes untouched.
- `main.py`: `rule_engine` + `regional_policy` in `/meta/ecosystem` and
  `/meta/scoring-catalog`; new `GET /meta/regional` (`?region=` resolves
  today's working-day verdict + sample tax); features keys under `/meta`.
- Code map: new `business_rules` infra group (`rule_engine`,
  `regional_policy`) + `regional_endpoint` main leaf; `points_exchange`
  gained `rate_multipliers`/`campaigns`; `arrears_payments` gained
  `late_fees`/`waive_fees`/`waiver_policy`; regenerated to 267 leaves /
  52 internal nodes (was 251/49), revision bumped to `r4`.
- Tests: `tests/test_rule_hyper_flexibility.py` (70) — suite 455 → 525
  passing.

### Predictive sentiment & automated service recovery (r5 expansion)
- New `app/services/recovery_playbooks.py`: realtime dissatisfaction
  indicators (`build_realtime_recovery_context`,
  `compute_realtime_dissatisfaction_indicators`) computed from the *live*
  interaction window (chat rows + bookings + latest-sentence sentiment) rather
  than persisted dashboard snapshots.
- Config-driven `RECOVERY_PLAYBOOKS` table matched by the shared when-DSL
  engine (`rule_engine.evaluate_when` + `=formula` `resolve_params`):
  `recovery_goodwill_points` (premium + negative + high/critical → credit),
  `recovery_ticket_escalation` (critical + churn high → escalate),
  `recovery_policy_guardrail` (premium/growth + high/critical + not-low churn →
  apply goodwill access/customer deltas). Adding a playbook is data, not code.
- Actions: `credit_points` reuses the canonical `get_or_create_wallet` and
  appends a **new** `recovery_credit` ledger kind (additive; existing
  `redeem_points`/`purchase_points` kinds untouched); `escalate_ticket`
  records a support escalation with a generated `ESC-<user>-<seq>` reference;
  `adjust_policy_score` previews the lifted `PolicyScoreSnapshot` (frozen
  snapshot never mutated; tier/posture recomputed via canonical helpers) and
  always records the action.
- Audit trail: new additive `recovery_actions` table (`models.RecoveryAction`)
  with payload/result JSON, reference, and `failure_reason` — one row per
  triggered action; failed actions are recorded, never raised through the
  sweep. `dry_run` mode evaluates matches without writing anything.
- **Keep-as-is decisions:** default single-tenant operation unchanged; the
  background sweep is env-gated (`CSERVICE_AUTO_RECOVERY`, default `0`, wired
  into the lifespan like the partition worker); existing points wallet / ledger
  kinds and recovery-outcome contracts untouched.
- Wiring: `POST /chat/recovery/playbooks` (self-service, optional `dry_run`
  request body) + `GET /chat/admin/recovery/playbooks` (read-only catalog);
  `recovery_automation` subservice in `/meta/ecosystem`, `recovery_playbooks`
  key in `/meta/scoring-catalog`, feature/endpoints keys under `/meta` and
  `/meta/features`; `recovery_actions` added to the efficiency-audit system
  metrics.
- Code map: new `recovery_playbooks` service group (8 leaves) + `recovery_action`
  model leaf + `recovery_playbooks` router/schema-chat leaves; regenerated to
  278 leaves / 53 internal nodes (was 267/52), revision bumped to `r5`.
- Tests: `tests/test_predictive_recovery_expansion.py` (27) — suite
  525 → 552 passing.

### Flexible-core expansion (thin-group pass, r5 leaf-level growth)
- Fourteen LOC-ranked thinnest function groups were grown into configurable,
  introspectable surfaces. **All additive**: existing signatures, returned
  payloads and pinned catalog key sets are untouched, and no existing table
  changed (verified by diffing `sorted(Base.metadata.tables)` against a stashed
  tree — 17 tables identical).
- `security.py` (122 -> ~936 LOC): `key_id_for`/`key_ring` rotation with
  `SECRET_KEY_PREVIOUS`; `TokenRevocationRegistry` (TTL-bounded jti deny-list
  with a subject index, so "revoke everything" is O(1));
  `decode_token_full` -> `TokenValidation` (explains *why*: expired / revoked /
  wrong_token_type / audience_mismatch / issuer_mismatch / missing_subject);
  `STEP_UP_LEVELS` + `with_step_up` (`acr`/`amr` claims); digest-backed
  `ApiKeyRegistry` with hierarchical `:` and `*` scopes (secret never stored);
  `PasswordPolicy` (composable via `with_overrides`), `validate_password_strength`,
  `score_password_strength` (0-4 + entropy/penalties), `PasswordHistory`
  (bcrypt-*verify* reuse ring, because bcrypt is salted so equality cannot
  work). `security_scheme` stays `HTTPBearer()` (auto_error) — the historical
  deps depend on its 403; `optional_security_scheme` is the new
  `auto_error=False` variant.
- `db.py` (104 -> ~542 LOC): `RetryPolicy` (frozen: classification, backoff,
  jitter) + `retry_async`. **Keep-as-is decision:** `policy=None` reproduces the
  exact legacy retry behavior, so every pre-existing caller is byte-identical
  and the new classification/backoff only activates when a policy is passed.
  Plus `CircuitBreaker`/`CircuitOpenError`, statement timeouts, advisory lock,
  `pool_status`, `db_health`, `readiness_probe`, `build_db_catalog`.
- `deps.py` (96 -> ~595 LOC): frozen `Principal` unifying the three credential
  shapes (JWT user, `X-API-Key` machine, delegated `act` claim);
  `get_optional_principal` / `get_principal`; declarative `require_scopes`,
  `require_roles`, `require_step_up`, `require_cell_access`, `require_tenant`,
  `require_rate_limit`; correlation id; `build_deps_catalog`. All seven
  historical dependencies keep their exact behavior.
- `i18n.py` (153 -> ~564 LOC): `normalize_locale`, `parse_accept_language`,
  `negotiate_locale`, `translate_many`, `catalog_coverage`,
  `CatalogOverrides` (per-locale catalog patching without mutating the shipped
  tables), richer plural/fallback metadata. `locales == ["en","es","fr"]` and
  `fallback_locale == "en"` still hold.
- `model_bases.py` (121 -> ~390 LOC): `SoftDeleteMixin`, `RowVersionMixin`,
  `ExpiringMixin`, `ActorAuditMixin`, `SerializationMixin`, `EntityRegistry`.
  **Keep-as-is decision:** the extra single-table-inheritance families live in
  `SECURITY_EVENT_EXTENSIONS`, not `families`, so the pinned 3-key
  `families` catalog and `family_count == 3` stay intact.
- `cell_matrix.py` (116 -> ~723 LOC): row scopes, cell overrides, payload
  masking, resource access plans, temporary grants, and
  `simulate_overrides` made thread-safe (it now takes an overrides *table*
  instead of mutating the module global). `ACCESS_LEVELS == ["none","read","write"]`
  and the pinned `resources` key set are unchanged.
- `optimistic_locking.py` (120 -> ~470 LOC): `VersionedRecord`/
  `VersionedStore`, `diff_fields`, `apply_json_merge_patch` (RFC-7386),
  `update_with_retry`, `LockSet`. `conflict_policy["expected_status"] == 409`
  is pinned by tests and unchanged.
- `tenant_router.py` (230 -> ~737 LOC): tenant-id validation, request
  resolution, DSN redaction, per-tenant pool overrides, health/outcome tracking,
  `plan_provisioning`, and `build_tenant_routing_policy()` exposed at
  `GET /meta/tenants/policy`. **Keep-as-is decision:** `build_tenant_router_catalog()`
  has an exact pinned 7-key set, so the new capability nests *inside*
  `pool` / `registered` rather than adding siblings.
- `hsm_signer.py` (222 -> ~831 LOC): `SigningKeyRing` (rotation),
  `sign_envelope`/`decode_envelope`, `signer_info`, `signer_self_test`,
  `get_signer_health`, `CoSignature` (`sign_multi`/`verify_multi`), and
  `ReplayGuard`. **Keep-as-is decisions:** the key ring and replay guard are
  **opt-in** (`verify_envelope_detailed(..., ring=)`, `replay_guard=`) and
  `supported_backends == ["hmac","ed25519","mock"]` is unchanged.
- `biometric_vault.py` (200 -> ~808 LOC): per-modality config-driven thresholds
  with `adapt_threshold`, slot enrollment, `BiometricChallenge` issue/consume,
  `attempt_state` lockout, `decide`, bounded history, and
  `export_state`/`import_state`. **Keep-as-is decisions:** (a) `_digest_similarity`
  is bit-level Hamming over SHA-256 digests — a documented deterministic
  *pseudo*-similarity, and pre-existing tests depend on a 1-bit input change
  scoring ~0.47, not ~1.0; (b) the challenge gate replays only and is
  deliberately *not* mixed into the similarity digest.
- `risk_evaluator.py` (263 -> ~771 LOC): `normalize_signal`, `context_gaps`,
  `normalize_context`, `required_controls_for`, `RISK_ACTIONS` +
  `action_for_level`, `controls_missing_for`, `counterfactual` +
  `cheapest_clearing_signal`, bounded `weight_history` + `weights_at`,
  `apply_risk_outcomes` (one version bump per batch; `clamped` is computed
  against the *un-clamped* value so a rule already at its bound still reports
  `True`), and `evaluate_risk_decision` + `RiskDecisionStore`.
  **Keep-as-is decision:** `evaluate_risk` score semantics are preserved
  byte-for-byte (`evaluate_risk({"device_proven": False, "odd_hour": True})["score"] == 32`);
  every new behavior is additive.
- `partition_manager.py` (260 -> ~708 LOC): two name patterns because period
  keys legitimately contain `-` — `IDENTIFIER_PATTERN` (strict, for table and
  policy ids) and `PARTITION_NAME_PATTERN`; `policy_for`, attach/detach/restore/
  archive/drop SQL builders, `build_partition_policy`, `validate_policies`,
  `parse_partition_rows`, `read_partition_catalog`, legal holds,
  `plan_lifecycle`, `adopt_partitions`, `attach_partition`/`restore_partition`,
  and `dry_run` on `run_cycle`. **Keep-as-is decision:** `drop_expired_partitions`
  keeps its original `moment - end > retention` comparison (equivalently
  `reference > end + retention`).
- `services/audit_log.py` (186 -> 1341 LOC) and `routers/audit.py`:
  `record_auditable` (justification required for sensitive actions, redaction of
  sensitive detail keys), `iter_audit_pages` (bounded pages + `carry`),
  actor rollups, entity timelines, anomaly detection, retention planning, a
  `SealChain` with `verify_seal_chain`, and CSV/NDJSON export. **Keep-as-is
  decision:** `record_audit_log_entry` / `list_audit_log_entries` /
  `entry_to_payload` / `build_audit_log_catalog` / `build_audit_log_summary` are
  left byte-compatible; all new behavior is additive pure functions.
- `routers/users.py` (185 -> ~880 LOC): `POST /users/refresh` (rotation burns
  the presented refresh token by default), `POST /users/logout` (single token or
  `all_sessions`, idempotent), `GET /users/me/sessions`, `GET|POST
  /users/me/step-up`, `GET|POST /users/me/api-keys` +
  `DELETE /users/me/api-keys/{key_id}`, `GET /users/password-policy`, `POST
  /users/me/password-feedback`, `GET /users/me/security-posture`, and
  `build_users_catalog()`. **Keep-as-is decisions:** `/register`, `/login`, `/me`
  and `/me/password` are byte-identical (including the exact
  "Password updated successfully under controlled access posture." message that
  a test pins); self-issued API keys can never be granted the `*` wildcard.
- Bugs found and fixed while expanding (do not reintroduce): `redact_dsn`
  produced `f"{user}:***@{host}"`; a 300 s expiry skew swallowed negative TTLs
  (split into `expiry_tolerance_seconds` / `not_before_skew_seconds`);
  `verify_multi` could not resolve keys without a ring; `skew` was referenced
  before assignment in `verify_envelope_detailed`; `iter_audit_pages` deduped via
  `getattr(row, "id")` which silently did nothing for mapping rows; the router
  `_trail_payloads` used `entry_to_payload` (crashes on dict rows — now
  `coerce_entry`); `biometric_vault._locked_out` compared a `datetime` to an ISO
  string (new `_as_datetime`).
- **Keep-as-is fact:** python-jose 3.5.0 has no `leeway` kwarg — leeway goes
  through `options={"leeway": N}`; `verify_aud` defaults to `True` so it must be
  disabled when `TOKEN_AUDIENCE` is unset.
- Code map: leaf-level growth on `main`, `db`, `security`, `auth_deps`,
  `localization`, `multi_tenant`, `zero_trust`, `decision_intelligence`,
  `model_bases`, `routers.users`, `routers.audit`, `services.audit_log`,
  `schemas_core`, `schemas_audit`; regenerated via
  `python3 scripts/build_code_map.py` to 428 leaves / 53 internal nodes
  (was 278/53). Revision stays `r5` (no new layer / top-level domain).
- Tests: `tests/test_flexible_core_expansion.py` (77),
  `tests/test_risk_partition_expansion.py` (29),
  `tests/test_users_identity_expansion.py` (42) — suite 552 -> 700 passing.

### Thin-group expansion, second pass (r5 leaf-level growth)
- The twelve LOC-ranked thinnest function groups were grown into configurable,
  introspectable surfaces. **All additive**: existing signatures, returned
  payloads and pinned catalog key sets are untouched, and no existing table,
  column, constraint or index changed — so no Alembic migration is required.
  `app/models.py` was verified byte-identical above its expansion marker
  (diffed against `r1`; the only delta is a 39-line docstring/import header and
  one trailing newline).
- `app/models.py` (363 -> 2158 LOC) gained a **metadata layer** below the DDL
  layer: config tables (`SENSITIVITY_CLASSES`, `FIELD_SENSITIVITY` /
  `TABLE_FIELD_SENSITIVITY`, `REDACTION_PRESETS`, `ENUM_FIELD_SPECS`,
  `ENUM_VOCABULARY_ALIASES`, `OPEN_VOCABULARY_COLUMNS`, `OPEN_TEXT_COLUMNS`,
  `TABLE_LIFECYCLE`, `SIGNED_QUANTITY_COLUMNS`,
  `UNCONSTRAINED_REFERENCE_COLUMNS`) plus pure helpers over `Base.metadata`
  (`sensitivity_of`, `projection_for`, `model_to_dict`, `redact_instance`,
  `model_to_rows`, `loads_json` / `dumps_json`, `normalize_enum_value`,
  `validate_enum_field`, `coerce_booking_status`, `coerce_service_type`,
  `enum_field_report`, `unbacked_enum_like_columns`,
  `check_constraint_coverage`, `referential_integrity_gaps`,
  `build_relationship_catalog`, `build_table_lifecycle_catalog`,
  `build_model_catalog`). **Keep-as-is decisions:** (a) the DDL layer is
  unchanged and stays authoritative, so `create_all`/alembic emit the same
  schema; (b) the module never imports `app.services.*` (the services import the
  models, so a reverse import is a cycle) and therefore implements its own
  redaction; (c) `check_constraint_coverage` and `referential_integrity_gaps`
  **report and do not fix** — closing either gap is new DDL, so the output is
  severity-`advisory`; (d) `retention_days` in `TABLE_LIFECYCLE` is
  documentation for an operator writing a policy, nothing in the module deletes
  a row; (e) an unknown redaction preset resolves to `internal` rather than
  raising, because a redaction helper that throws gets bypassed.
- `app/model_versioning.py` (483 -> 2299 LOC): `VERSION_STATES`, `GATE_OPS`,
  `GATE_SEVERITIES`, `PROMOTION_GATES` (11), `ROLLBACK_POLICIES` (3),
  `DEPRECATION_POLICIES` (3), `PRUNE_POLICIES` (3), `TRAFFIC_SPLITS` (2),
  `LABEL_TEMPLATES`, `DIFF_FORMATS`, `NO_HISTORY_METRICS`, plus `weight_moves`,
  `diff_versions`, `render_diff`, `version_changelog`, `promotion_verdict`,
  `rollback_plan`, `deprecation_status`, `prune_plan`, `traffic_plan`,
  `select_version`, `stage_progress`, `next_stage`, `render_label`,
  `version_report`; the catalog gained `lifecycle`, `promotion_gates`,
  `version_diff`, `rollback_policy`, `deprecation`, `prune`, `traffic_split`,
  `label_templates`. **Keep-as-is decision:** the v1 `_ModelEntry` lifecycle is
  byte-identical, and only *structurally wrong* promotion cases are `blocking` —
  thin evidence, no rollback target, and auto-promote-off are `auto` and
  resolve to verdict `manual`. A missing metric **fails closed**.
  `unknown_rule_ids` compares against the live `effective_risk_rules()` ids, not
  a pinned copy.
- `app/rule_engine.py` (449 -> 1477), `app/regional_policy.py` (424 -> 1445),
  `app/simulation_engine.py` (300 -> 911), `app/optimistic_locking.py`
  (461 -> 1159), `app/explainability.py` (269 -> 946): config tables plus pure
  helpers, all additive. `conflict_policy["expected_status"] == 409` and
  `simulation["kinds"] == ["risk","retention"]` are pinned and unchanged.
  **Keep-as-is decision:** the extended string/collection/null operator families
  in `rule_engine` stay opt-in — an operator that no family declares is never
  silently true, and `matches_field` / `evaluate_when` still fail closed on them.
- `app/protobuf_transaction_spec.py` (379 -> 1392) gained
  `TransactionLog.query` / `integrity_report` / `export` and the matching
  catalog sections; `app/high_throughput_pipeline.py` (287 -> 780) gained
  `REPLAY_POLICY` / `replay_dead_letters` and an additively enriched
  `dead_letter_report` (`by_kind`, `by_error`, `evicted`, `replayable_ratio`,
  `blocked_by_policy`, `max_attempts_seen`, `oldest` / `newest_occurred_at`,
  `ring_capacity`, `drainable`, `generated_at`).
- `services/audit_log.py`, `routers/audit.py` (394 -> 767) and
  `schemas/audit.py` (312 -> 520): `AUDIT_VIEW_PROFILES` (digest / operations /
  investigation), `AUDIT_INTEGRITY_GATES` (7), `verify_entry_seal_chain`, and the
  routes `GET /logs/view`, `GET /integrity/gates`, `GET /pipeline/dead-letters`,
  `POST /pipeline/replay` (with `dry_run`), `GET /transactions/query`,
  `GET /transactions/integrity`, `GET /transactions/export`, plus an optional
  `?section=` on `GET /transactions/spec`. **Keep-as-is decisions:** (a)
  `AUDIT_GATE_OPS` is declared locally rather than imported from
  `model_versioning`, to avoid a decision-intelligence dependency from the
  audit service; (b) `verify_entry_seal_chain()` recomputes each entry's seal
  from the persisted `detail["_integrity"]` block and compares, because the old
  `chain_valid` (via `verify_seal_chain(entries)`) always read valid for real
  entries — an empty trail now reports `chain_valid=True` with
  `sealed_ratio=None` -> verdict `review` instead of a misleading `reject`;
  (c) `GET /transactions/spec` still returns the full catalog by default and
  `build_transaction_spec_catalog` stays imported at `app/routers/audit.py:9`.
- `app/routers/topics.py` (190 -> 303) gained `/taxonomy`, `/governance`,
  `/integrity`, `/match`, `/ranked`, `POST /validate`, `/drift`, `/lifecycle`,
  backed by a governance block in `services/topics.py`.
  **Keep-as-is decisions:** every new guard is **advisory** — `POST
  /topics/validate` is pre-flight only and mutates nothing, known data defects
  are reported by `/topics/integrity` rather than repaired, and `rank_topics` is
  not guaranteed to match the built-in suggester's ordering.
- Meta wiring: new `GET /meta/schema` (optional `?section=`, unknown section ->
  422 so a script typo fails loudly), a `data_model` entry in the
  `/meta/ecosystem` `subservices` map, and a `schema_catalog` key in
  `/meta/features`; `audit_trail` / `high_velocity_audit` route lists and 7
  `features` keys extended for the audit routes.
- Bugs found and fixed while expanding (do not reintroduce): `TransactionLog.
  query(sort=...)` raised `KeyError: 'field'` because the fallback sort spec
  had no `field` key; `/audit/transactions/export` miscounted `frame_count` for
  `json` (line-count returned 1) and for `csv` (the header row was counted as a
  frame); `models.model_to_dict` dropped unset columns on a *transient* instance
  (it now applies `skip_unloaded` only to persistent state, where reading an
  unloaded attribute would emit a query); `_python_default` returned an enum
  member, which is not JSON-serialisable, so the `/meta/schema` payload could
  not be served; `prop.cascade` is a `CascadeOptions` object rather than a
  string in SQLAlchemy 2.x; and `SIMULATION_MODIFIERS["short_retention"]` shipped
  a dead retention override (see next entry).
- **Two more bugs, found by auditing the audit.** The "all resolved" line at the
  top of this file was not evidence of anything, so the tables were checked
  against each other instead. Two real defects surfaced, both in `models.py`
  metadata that I had written earlier in this same pass:
  - `interaction_signals.source` was declared in `ENUM_FIELD_SPECS` as a *closed*
    five-value vocabulary while `source` is simultaneously listed in
    `OPEN_TEXT_COLUMNS` as open on purpose. `validate_enum_field()` therefore
    rejected `slack_channel` for that one column and accepted it for the other
    six `source` columns. Removed: all seven `source` columns are open channel
    labels, and the name is already covered by `OPEN_TEXT_COLUMNS`.
  - `unbacked_enum_like_columns()`'s docstring promised to exclude names listed
    in `OPEN_TEXT_COLUMNS`; the code only checked `ENUM_FIELD_SPECS` /
    `OPEN_VOCABULARY_COLUMNS`. The `source` exclusion was therefore being
    supplied *by accident* by the entry above. Implementing the documented
    exclusion left a residual of `note` and `summary` — unbounded `TEXT` prose
    on three tables each — which are now declared open. **Keep-as-is
    decision:** the residual is now empty, and that is the intended state, not a
    disabled check. A name shared by three or more non-nullable `String` columns
    that is in neither table still surfaces, verified by injecting a synthetic
    `dispute_reason` column into three tables; declaring it open clears it.
- **Resolved placeholder — `SIMULATION_MODIFIERS["short_retention"]`.** The pack
  carried `retention_overrides = {"policy_id": "=retention_days"}`: the literal
  string `"policy_id"` used as a key, where an override is looked up by real
  policy id. The key matched no policy, so every lookup fell through to the
  baseline and the `partition_cost_trim` scenario reported `changed_count: 0`
  while advertising "partitions older than 30 days flip keep -> drop". Fixed by
  **not** renaming the key. **Keep-as-is decision:** a rename to
  `audit_log_monthly` would have fixed only one of the two real policies, still
  contradicting the pack's own label ("Cuts *every* retention window to 30
  days"), and would need hand-editing again the next time a policy is added to
  `PARTITION_POLICIES` — the same silent-drift failure, one level up. Instead:
  - `RETENTION_OVERRIDE_WILDCARD = "*"` makes "every policy" expressible.
    `short_retention` is now `{"*": "=retention_days"}`.
  - Precedence is **specific id > wildcard > the policy's own baseline**, applied
    by one shared helper `_retention_override_for` used by *both*
    `resolve_modifier` and `simulate_retention_whatif`, so the two cannot drift.
    A wildcard that meant "every policy" in the expander but "nothing" in the
    simulator would reintroduce the very no-op being fixed.
  - An `=formula` override is evaluated with `base` bound to *that policy's* own
    `retention_days`, so `=base` stays a per-policy no-op and
    `=retention_days` applies one absolute window everywhere. A formula passed
    straight to `simulate_retention_whatif` (no pack to supply `params`) raises
    naming the remedy, instead of a bare `int()` failure.
  - `unmatched_retention_overrides(overrides, policies)` reports override keys
    that reach no policy. An unmatched key is **dropped silently** at lookup
    time, which is precisely why a placeholder key is indistinguishable from a
    working one at the call site; the reporter makes the config checkable
    against the live policy set instead of trusted. **Keep-as-is decision:** it
    reports and does not raise — an advisory what-if tool that hard-fails on a
    stale key is a tool nobody runs.
  - Fully backward compatible: an explicit `{"audit_log_monthly": N}` map
    behaves exactly as before, and `kinds == ["risk","retention"]` plus every
    other pinned `simulation` catalog key is unchanged.
- **Keep-as-is fact:** `app/models.py` deliberately does not import
  `app.services.*`, so any cross-module judgment (a sensitivity class, an enum
  vocabulary) that two services would otherwise share is **declared twice** — the
  audit-log gate ops live in `services/audit_log.py` as `AUDIT_GATE_OPS` rather
  than being imported from `model_versioning`. A duplicate table can drift; a
  cycle cannot be repaired without breaking the import graph, so the table is
  the cheaper side of that trade.
- Code map: leaf-level growth on `models` (new `metadata` internal node),
  `model_versioning`, `simulation_engine`, `explainability`, `optimistic_locking`,
  `rule_engine`, `regional_policy`, `high_throughput_pipeline`,
  `protobuf_transaction_spec`, `routers.topics`, `routers.audit`,
  `services.audit_log`, `schemas.schemas_audit`; regenerated via
  `python3 scripts/build_code_map.py` to 498 leaves / 54 internal nodes
  (was 428/53), with **zero leaves lost** (verified by diffing parsed leaf
  paths, not by eye). Revision stays `r5` (no new layer / top-level domain).
- Tests: unchanged by policy — no new test files. The suite was 700 passing
  before and after every step of this pass; the new surfaces were verified with
  scripted harnesses in `/tmp/opencode/` instead.

### Thin-group expansion, fourth pass (2026-09-28)

Five groups expanded, thinnest first by `python3 scripts/loc_by_function_group.py`:
`services/retention` (733 → 2033), `services/recovery_playbooks` (598 → 2522),
`routers/topics` (303 → 2765), `app/model_bases` (502 → 1985),
`services/policy_scoring` (680 → 2193). Additive throughout: no existing
signature changed, no existing config table was edited, no route response
payload changed shape, no DDL.

- **`routers/topics.py` — the governance layer is advisory, and that is a
  decision, not an omission.** `GovernedAPIRoute(APIRoute)` is set on
  `router.route_class` and returns the original response untouched. The table
  describes the router; the planner predicts. No existing route is gated, rate
  budget is never spent by planning, and the recorder is the only piece in the
  request path. The alternative — enforcing the bounds — would have changed
  behaviour of 31 live routes in a pass whose scope was adding surface.
  Every clamp is reported, so a silent narrowing is at least visible.
- **`app/model_bases.py` — the four new mixins are for new tables only.**
  Applying `SluggableMixin` / `ApprovalMixin` / `MoneyMixin` /
  `IdempotencyMixin` to a class in `app/models.py` is a DDL change and needs a
  migration, so this pass added them and applied them to nothing. A column name
  that both a mixin and an existing table declare is therefore reported as a
  **warning that names both sides**, not blocked — a test that "passed" because
  the overlap was forbidden would be asserting a restriction the layer does not
  and should not have.
- **`app/model_bases.py` — partial schema views must say so.** `Base.metadata`
  only holds imported models. `mixin_column_provenance` reports
  `entities_module_imported` and `validate_model_bases` warns when it is false,
  following the same precedent as `schema_drift_report` in `db.py`. A report
  that read a partial view as a whole-schema audit would pass while being wrong.
- **`app/model_bases.py` — the field-sensitivity vocabulary is layered *over* the
  untouched `SERIALIZATION_DENYLIST`.** New keys are reported through
  `mixin_extensions`; the pinned `mixins` / `families` contract is never
  widened, because a caller iterating those lists would silently start seeing
  new names.
- **BUG FOUND — `SERIALIZATION_PROFILES["public"]["max_depth"]` was 2.** A list
  response is `{key: [row, ...]}`, so the row dict sits at depth 2 and the
  bound blanked every row in every list payload. The validator check is on
  *shape* (`rows and all(row for row in rows)`), not on any one field name, so
  tightening the class list cannot make it fire spuriously.
- **`services/recovery_playbooks.py` — the automated sweep is a loop and
  `credit_points` had no bound.** The credit amount is derived from the current
  dissatisfaction score, and nothing limited how often the sweep could run, so a
  customer whose sentiment stayed negative was re-credited the same goodwill
  amount every pass, indefinitely. `RECOVERY_GUARD_RULES` makes the limits
  declarative (per-run, per-day, cooldown, daily budget per budgeted metric) and
  a refusal is reported as `skipped` **with the guard that said no**, not as a
  failure — a guard that looks like an error trains people to disable it.
- **Keep-as-is decision — `app/models.py` still does not import
  `app/services/*`,** so a judgment two services would share is declared twice
  rather than imported. A duplicate table can drift; a cycle cannot be repaired
  without breaking the import graph.
- **BUG FOUND — the `policy_scoring` validator raised `ValueError` instead of
  reporting.** `dict(rule.get("priority") or {})` on a recommendation row whose
  `priority` is the string `"high"` raised out of the validator, i.e. a 500 from
  the one place a malformed config table must never take the process down. Both
  the validator and the request-path trace now type-check first; the trace falls
  back to a reported priority rather than raising.
- **`services/policy_scoring.py` — two original arithmetic asymmetries are
  preserved, not corrected.** (1) `customer_score` floors its inverted
  dissatisfaction term at 0 but does **not** ceiling it, so a negative
  dissatisfaction reading adds to the customer score. (2) `scaled_mean` divides
  by the *term count*, not by the sum of the weights; in `interest_score` the
  weights are 10/4/8/1, so reading it as a weighted mean would move the
  published score by a wide margin. Both are documented in the table comments,
  and both are asserted: every stored `CustomerPolicyScore.summary` string
  embeds the numbers they produced, so "correcting" either one silently
  invalidates historical comparisons.
- **`services/policy_scoring.py` — marker accumulation order is part of the
  contract.** For topic `depth` the five-word bonus lands *between* the routing
  and urgency marker groups, and float addition is not associative. The marker
  table therefore carries an explicit `order` and a test asserts
  `10 < bonus_order < 20`, rather than leaving the order to be an accident of
  how the list is written.
- **`policy_rule_coverage` surfaced a fact that was not visible before:** 62.9%
  of the (access, system) score grid resolves to the *default* constrained
  posture rather than to a `CONTROL_POSTURE_RULES` row. First-match-wins makes
  row order the semantics, so a shadowed row is dead configuration that still
  reads as live in the catalog. The sweep reports unreachable rows as
  **warnings, not errors**, because it is a grid and a band narrower than the
  step can hide a rule.
- **Test-side corrections made in this pass (the assertions were wrong, not the
  source):** the new-mixin column-overlap test asserts a validator *warning*
  (`test_a_column_name_shared_with_an_existing_table_is_reported_not_forbidden`);
  `test_lists_are_walked_and_bounded_too` uses `public`; an
  unknown-enum-value test no longer asserts `filters == []` (replaced by
  `test_a_half_valid_list_still_filters_and_is_still_reported` and
  `test_a_fully_unknown_list_leaves_no_filter_at_all`);
  `test_a_widened_profiles_table_is_an_error` now matches `"no longer falls back
  to excluding every class"`. **Never `del` a module attribute in a test** — an
  earlier `del topics.observed_topic_routes` permanently removed the function
  and broke 30+ tests; use `monkeypatch.setattr` / `monkeypatch.setitem`.
- **Verification approach.** For `policy_scoring` the claim is *byte-identical
  output*, so it is proved differentially rather than asserted: the original
  inline expressions were transcribed into the test file and compared against
  the table-driven engine over 24,063 topic texts, 20,000 random metric sets and
  a grid straddling every threshold. Comparing the new engine against another
  restatement of the same tables would have proved nothing.
- Code map: leaf-level growth on `services.retention`,
  `services.recovery_playbooks`, `routers.topics`, `models.model_bases`,
  `services.policy_scoring` and `infra.main`; regenerated via
  `python3 scripts/build_code_map.py` to 511 leaves / 54 internal nodes
  (was 498/54), with **zero leaves lost**. Revision stays `r5` (no new layer /
  top-level domain, rule 6).
- `/meta/` surfaces updated in the same commit (rule 5): four `/meta` features,
  four `/meta/ecosystem` `subservices` entries with `config_tables` + `notes`,
  new `GET /meta/model-bases` and `GET /meta/policy-scoring` handlers, and the
  matching `/meta/features` and `/meta/scoring-catalog` keys.
- Tests: `tests/test_retention_policy_expansion.py` (71),
  `tests/test_recovery_governance_expansion.py` (119),
  `tests/test_topic_request_governance_expansion.py` (183),
  `tests/test_model_bases_governance_expansion.py` (167),
  `tests/test_policy_scoring_expansion.py` (133) — suite grew **1240 → 1373
  passing**. `validate_model_bases()` and `validate_policy_scoring()` both
  report 0 errors against the live tables.

### Thin-group expansion, fifth pass — `app/deps.py` (2026-09-28)

`app/deps.py` 595 → 2908 LOC, 0 → 14 config tables, +19 functions, 184 new
tests. The request path is unchanged: same principal resolution, same status
codes, same denial payloads, and every existing dependency keeps its exact
signature.

- **`AUTHZ_DENIALS` is load-bearing, the other two tables are not.**
  Each `require_*` factory's `status_code` now reads `AUTHZ_DENIALS`, with
  values identical to the old inline literals. The `error` / `text` strings stay
  **inline** on purpose: clients match on them, so a table would only make them
  easier to change without noticing. `RATE_TIERS` is read only by the new
  `require_rate_tier`; the historical `require_rate_limit(capacity,
  refill_per_second)` signature and the env-driven `RATE_LIMITER` are untouched,
  and the `interactive` row documents the 60/1.0 default the env vars override.
  `STEP_UP_RANKS` documents the ranks; `require_step_up(level: str = "loa2")`
  keeps its **literal** default deliberately, so the table documents the floor
  instead of silently supplying a signature.
- **`ROLE_SCOPE_GRANTS` is advisory and must never be applied at resolution
  time.** `require_scopes` reads `Principal.scopes` and nothing else. Deriving
  scopes from roles would start authorizing calls that are denied today. It is
  reported as `facts.implied_by_roles` with `implied_by_roles_applied: False`.
  `USER_ROLES` is untouched.
- **`AUTHZ_RULES` describes routes that exist; it does not route.** 56 rows,
  most-specific-first, catch-all last. `enforced_by` names the dependency that
  actually runs; `hardening` is proposed-but-unenforced and lives in a
  **different key** on purpose, evaluated only under
  `evaluate_authz(include_hardening=True)` with every such check marked
  `enforced: False`. A second key rather than a flag inside `enforced_by`,
  because a reader scanning that column must never be able to mistake a
  proposal for something a request is checked against.
- **`app.deps` cannot import `app.main`** (cycle), so
  `authz_drift_report(routes)` and `build_authz_catalog(routes=None)` take the
  route list as an argument. All six `require_*` factories are bound to **no
  route at all**; the report lists them under `unbound_factories` rather than
  leaving them to look wired.
- **FastAPI version quirk worth remembering:** `app.routes` holds
  `_IncludedRouter` wrappers and `route.path` is **not** the effective path.
  `include_router(prefix=...)` lives in `route.include_context.prefix`, while a
  router's own `.prefix` is already baked into its child route paths.
  `iter_authz_routes` implements this and reproduces the openapi path set
  **exactly** (197 paths, zero diff) — asserted in the tests. Also, `Dependant`
  in this version has no `.dependency`; read `Depends` defaults from the
  endpoint signature and `route.dependencies`.
- **Three bugs found, all "the surface reported a fact that was not true":**
  1. `match_authz_rule` omitted the `matched_fallback` key when the deciding
     rule *was* the catch-all — so `unclassified_routes` and `in_sync` missed
     precisely the case the fallback exists to catch. The key is now always
     present; two consumers switched to `bool(match["matched_fallback"])`.
  2. `evaluate_authz` failed the `authenticated` check whenever
     `principal is None`, **including on `public` routes** that require no
     credential, so the whole public surface was reported denied. That check is
     now `not_evaluated` with a reason. Related: the `anonymous` probe was built
     as an empty `Principal`, but `roles_for_user` always yields at least one
     role, so a resolved-but-roleless principal is a state the app **cannot
     produce** — and "resolves to a principal" is exactly what the
     authenticated routes check, so the probe reported them all as *allowed*
     while its note claimed "no credential at all". Probes now carry an
     `absent` flag and `authz_probe` returns `None` for those rows, which is
     what `evaluate_authz` actually branches on. A probe row that is absent and
     still declares a subject, roles, scopes, tenant, auth method or claims is
     a validator **error**, not a shrug.
  3. The pattern compiler turned a trailing `*` into `.*`, so the rule for
     `/meta*` also claimed `/metadata`. Tightened to `(?:/.*)?`. That
     reclassified `GET /chat/admin-activity`, which had been admin-gated **by
     accident**; it now has its own `chat_admin_activity` rule.
- **`validate_authz` was validating derived indexes instead of the tables.**
  It read the import-time `SCOPE_BY_NAME`, `STEP_UP_RANK_BY_LEVEL`,
  `RATE_TIER_BY_NAME` and `AUTHZ_RULE_IDS`, so editing `STEP_UP_RANKS` to
  disagree with `security.STEP_UP_RANK` produced no complaint at all, and
  "the catch-all rule must be last" had never fired. It now builds its own
  local indexes from the lists it validates. **Two tests had been written
  against the old behaviour** — they mutated `STEP_UP_RANK_BY_LEVEL` — and were
  corrected to mutate the table, plus a new test asserting the index is left
  stale on purpose.
- **`AUTHZ_OPS["default_rate_tier"] = "interactive"`** was added: the
  default-tier lookup had been reading `default_exposure`, which yields the
  string `"public"` as though it were a tier name.
- **`policy_tier` is the one `AUTHZ_CHECK_ORDER` check with no denial.** It
  needs a `User` row that an offline `Principal` does not carry, so it is
  reported `not_evaluated` and is never a denial. Every other check in the
  order must have a denial; every denial must be claimed by a check, and one
  claimed by none is a **warning** (it is reachable from a dependency, just not
  from `evaluate_authz`).
- **Test-side traps hit in this pass, for the record:**
  - `original = list(TABLE)` is a **shallow** copy: the row dicts are the live
    ones. A test that did `row["self_service"] = True` and restored with
    `TABLE[:] = original` restored the *mutation* and silently poisoned every
    later test. Replace the row (`{**row, ...}`) rather than editing it in place.
  - `original = getattr(D, name)` **aliases the live list** — same trap, worse.
    Use `list(...)`.
  - Two tests mutated `AUTHZ_PROBES[0]` positionally, which is now the `absent`
    row; they name the probe (`"customer"`) instead.
- **Doc drift corrected while here:** `CODE_MAP.md`'s "Current Map" header still
  read `498 leaves / 54 internal nodes` from before the fourth pass, and its
  multi-line rendering was missing seven leaves the Newick had already gained
  (`model_bases_endpoint`, `policy_scoring_endpoint`, `composition`,
  `topic_metrics`, `posture_adjustment`, `tier_escalation`, `ops_catalog`). The
  rendering is now token-for-token equal to the Newick, checked by script.
  The `/meta/ecosystem` note also hardcoded "the 208 live method+path pairs";
  it is now count-free, because a note that restates a count is a note that
  will be wrong the next time a route is added.
- Code map: `auth_deps` gained 12 leaves (`scope_catalog`,
  `role_scope_grants`, `denial_contract`, `step_up_ranks`, `rate_tiers`,
  `route_rules`, `decision_engine`, `drift`, `simulation`, `validation`,
  `authz_catalog`, `rate_tier`); regenerated via
  `python3 scripts/build_code_map.py` to **523 leaves / 54 internal nodes**
  (was 511/54), **zero leaves lost**. Revision stays `r5` (no new layer /
  top-level domain, rule 6).
- `/meta/` surfaces updated in the same commit (rule 5): `authz_governance` in
  the feature list, an `/meta/ecosystem` `subservices` entry with
  `config_tables` + `notes`, a new `GET /meta/authz` handler, and the matching
  `/meta/features` and `/meta/scoring-catalog` keys. All five endpoints 200.
- Tests: `tests/test_authz_expansion.py` (184) — suite grew **1373 → 1557
  passing**. `validate_authz()` reports **0 errors and 0 warnings** against the
  live tables. `authz_denial_contract` drives all 14 `require_*` factories and
  private raisers against their declared payloads.
- Route ground truth for the next pass: **209 live method+path pairs over 197
  paths** — 79 admin, 63 authenticated, 19 policy, 48 public. (209 − 197 = 12
  methods beyond GET.) Write the count into a test, not into prose.

### Thin-group expansion, sixth pass — `app/i18n.py` (2026-09-29)

`app/i18n.py` 564 → 3211 LOC, 0 → 6 config tables, 11 → 20 public functions
(+9), 116 new tests. Verified by diff: **zero pre-existing signatures changed,
zero functions removed.** `MESSAGE_CATALOG`, `SUPPORTED_LOCALES`,
`PLURAL_CATEGORIES`, `RTL_LOCALES`, `translate`, `_render_template`,
`resolve_locale`, `negotiate_locale`, `parse_accept_language`,
`catalog_coverage` and `build_i18n_catalog` are byte-identical to
`HEAD`; the tests transcribe them into `ORIG_*` constants and assert it.

- **This group is report-only. Do not "fix" the leaks.** `_render_template`
  catches `(KeyError, IndexError, ValueError)` and returns the joined string, so
  a template that leaks `{name}` is *already being served that way* — repairing
  it changes a string clients are matching on, inside the module whose entire job
  is producing those strings. All 15 `RENDER_OPS` rows carry
  `report_only: True` and `validate_i18n` **errors** on a row that does not.
  A finding names the code path that produced it; it does not propose a patch.
- **`NUMBER_FORMATS` is keyed by region profile, not language tag** (`en_us`,
  `en_gb`, `es_es`, `es_419`, `fr_fr`, `de_de`, `ar_eg`). A language tag
  determines neither the decimal separator nor the currency position: `es` is
  `1.234,50 €` in Spain and `$1,234.50` in Latin America. Language-keyed rows
  would have been confidently wrong. Currency and date order are deliberately
  **absent** from `LOCALE_EXPECTATIONS` for the same reason. Every profile
  declares `digit_substitution: "none"` (Arabic-Indic shaping is not
  implemented) and `format_number` is **offered, not imposed** — the renderer's
  plural `#` still emits `str(int(n))`.
- **`unreached_codes` is a first-class catalog field, not a hidden gap.** 22 of
  the 36 `I18N_WARNINGS` codes are guards that only fire on a malformed config;
  the shipped catalog is clean, so they stay unreached and the catalog says so.
- **Six new functions, but the schemas are untouched.** No new pydantic model
  was added, no existing model's fields were renamed or retyped, and no route's
  response payload changed shape. The one change to an existing type is
  additive: `CatalogOverrides.keys_by_scope()`, a read-only view added so the
  coverage reports do not reach into `_layers`.
- **Every audit takes an optional `catalog=`.** A proposed catalog is checked
  before installation, so no test has to mutate the live catalog and restore it
  — which is the trap that produces three of the bugs listed below.

#### Nine verified defects, reported and deliberately not fixed

1. A missing value leaks the raw token: `translate('auth.welcome','en')` →
   `'Welcome, {name}'`. Six findings (3 locales × 2 independent detectors).
2. **`translate()` raises `AttributeError` out of the renderer** for a dotted
   field — `except (KeyError, IndexError, ValueError)` does not cover it.
   `_render_template('Value is {a.b} here','en',{'a':'v'})` propagates.
   Indexing (`{a[0]}`) *is* caught as `IndexError`. The one template shape that
   turns a copy mistake into a 500.
3. A plural with no `count` renders the **zero** branch:
   `translate('error.count','en')` is identical to `count=0` → `'no errors'`.
4. One unescaped `}` makes `str.format` raise `ValueError`, so **every**
   placeholder in that template stops interpolating: `'Welcome, {name} }'` →
   `'Welcome, {name} }'`. The escape is `}}`.
5. `#` substitution has no word boundary: `'{n, plural, other {issue #7 resolved
   with # items}}'` with `n=3` → `'issue 37 resolved with 3 items'`.
6. A missing `other` branch makes the last branch serve every unmatched count:
   `'{n, plural, one {# item}}'` with `n=99` → `'99 item'`.
7. `negotiate_locale(...).requested` is `normalize_locale` of the **whole
   header**: `'fr-CA, es;q=0.8, en;q=0.5'` → `'fr-ca, es;q=0.8, en;q=0.5'` — a
   field named `requested` that is not a tag.
8. `q=0` is still eligible (RFC 9110: zero quality means "not acceptable");
   `q` is unclamped (`q=5` → 5.0); a repeated `q` last-wins silently.
9. `resolve_locale` and `negotiate_locale` disagree on `fallback_used` for `""`,
   `es` and `fr` — same resolved locale, opposite flag — and `chain` means
   different things depending on which produced it.

Plus registry drift: `RTL_LOCALES` ∩ `SUPPORTED_LOCALES` = ∅ (so `direction` is
permanently `"ltr"`), `PLURAL_CATEGORIES["ar"]` declares six categories while
`plural_category()` produces only `one`/`other`, `SUPPORTED_LOCALES[l]
['fallback']` is reported by two payloads and read by no resolution path,
override keys are invisible to `catalog_coverage()`, and `error.*` vs `errors.*`
is a declared near-duplicate **kept as-is** because collapsing it would retire
live keys.

#### Two bugs found in the new code, and the probe traps on this module

- `locale_registry_audit` counted templates with `MESSAGE_CATALOG.get(locale)`,
  but the catalog is `{key: {locale: template}}` — the top-level lookup found
  nothing and reported all three shipped locales as translation-less. Three
  spurious findings. The count is now `sum(1 for _, locale, _ in
  _catalog_entries(MESSAGE_CATALOG))`; **never** `MESSAGE_CATALOG.get(locale)`.
- `plural_audit(catalog=X)` called `_safe_translate`, which always reads
  `MESSAGE_CATALOG` — so it audited the live catalog while claiming to audit the
  proposed one. It now renders the catalog it was handed.
- **`translate`'s `count` and `scopes` are keyword-only.**
  `translate(key, locale, "Ana")` raises `TypeError`. A first probe round
  passed the value positionally, appeared to show that overrides were being
  ignored, and would have been written up as a defect. Override probes must pass
  `scopes=(...)`.
- `_template_scan` calls `_render_template(text, locale, {})`, which is safe
  *only* because the empty-values path returns before `.format()`. That is
  exactly what makes it usable as a detector, and exactly what would break it if
  the render order ever changed.
- `I18N_PLACEHOLDER_NAME_INVALID` is reachable only for brace contents that are
  not `\w+` (e.g. `{na-me}`), and is found in the **rendered output** via
  `_BRACE_CONTENT`, not in the template via `_PLACEHOLDER`. A legal but
  differently-spelled name like `{nombre}` is a `I18N_PLACEHOLDER_MISMATCH`, not
  a name error.
- Test-restore traps, all still live: `original = getattr(D, name)` **aliases
  the live list**; `original = list(TABLE)` is **shallow**, so `row["x"] = y`
  then `TABLE[:] = original` restores the *mutation*. Replace the row
  (`{**row, ...}`) or `monkeypatch.setattr` the whole table, and restore in a
  `finally`.

#### State after this pass

- Suite green at **1673 passing** (1557 + 116).
- `build_i18n_governance_catalog()` reports **36 findings (16 defect / 16
  warning / 4 info)** across 6 audits; `validate_i18n()` is clean — 0 errors,
  0 warnings. `message_budget_report` is the one audit with no findings.
- `main.py` wiring, all four surfaces: the `localization` subservice on
  `/meta/ecosystem` gained `config_tables` (all 6) and a 1333-character
  `notes`; `endpoints.i18n_governance = "/meta/i18n"` in `/meta/features`;
  `"i18n_governance"` in the `/meta` features list; `"i18n_governance":
  build_i18n_governance_catalog()` in `/meta/scoring-catalog`; a `governance`
  key on `/meta/i18n`. All five `/meta/` endpoints return 200.
- Code map: **523 → 539 leaves**, 54 internal nodes, revision stays **r5**
  (leaf-level additions only; nothing structural). Zero prior leaves lost,
  verified by token-multiset diff against the `HEAD` Newick, and the multi-line
  rendering in `CODE_MAP.md` is token-for-token equal to `CODE_MAP.newick`
  (checked by script, not by eye — the previous pass had drifted by seven
  leaves).
- **Ground truth for the shipped catalog**, asserted in the tests so a future
  edit to `MESSAGE_CATALOG` cannot silently move it: 15 keys, 45 templates, 3
  locales, 8 prefixes; `name` in `auth.welcome` is the *only* placeholder in the
  whole catalog; 6 templates carry a plural block; the longest is
  `error.count[fr]` at 68 characters, which is why the 80-character budget report
  is clean; `booking.count` → `one`/`other` and `error.count` → `=0`/`one`/
  `other`, identical across all three locales; all locales agree per key on
  placeholders, so the divergence guard is green today.
- **Nothing is committed.** All changes across all seven expansions are
  uncommitted working-tree changes.

#### Next target

`app/schemas/audit.py` (520 LOC, 40 models, 0 functions, 0 config tables) — the
last of the 43 ranked groups. A pure pydantic model module: the expansion adds
`CONTRACT_KINDS`, `CONTRACT_AUDIENCES`, `CONTRACT_FIELD_POLICIES`,
`CONTRACT_FORBIDDEN_FIELDS`, `CONTRACT_OPS`, `CONTRACT_INVENTORY`,
`CONTRACT_WARNINGS`, plus `describe_contract_field`,
`contract_divergence_report`, `contract_redaction_report`, `validate_contracts`,
`build_contract_catalog`, new report models, and `GET /meta/audit-contracts`.
Ground truth still true for that work: **209** API routes (method+path pairs) over
**197** paths — 79 admin, 63 authenticated, 19 policy, 48 public — and
`analyze_sentiment` returns `None` offline, so any sentiment-dependent contract
must not assume a model result.

### Thin-group expansion, seventh pass — `app/schemas/audit.py` (2026-09-29)

`app/schemas/audit.py` 520 → 3222 LOC, 0 → 9 config tables, 0 → 6 public
functions, 0 → 32-code finding taxonomy, 139 new tests. The eighth and last of the
43 ranked groups. Verified by diff: the forty shipped pydantic contracts above
the governance banner are **byte-identical to `HEAD`**; the tests transcribe the
40 field lists, the 40 (fields, required) pairs, the eight comment-declared
vocabularies and the two `severity` vocabularies as `ORIG_*` constants and assert
them.

- **This group is report-only, and the reason is sharper than the i18n pass.**
  A message catalog is data a renderer consumes; a pydantic model here *is* the
  contract a client binds to. Widening `severity` to silence
  `CONTRACT_VOCABULARY_COLLISION` would delete the only place the event
  vocabulary is written down and start accepting a fourth value at the door.
  All 8 `CONTRACT_OPS` rows carry `report_only: True` and `validate_contracts`
  **errors** on a row that does not — the posture is checked, not promised.
- **Nine config tables, none of which existed before**: `CONTRACT_KINDS` (6),
  `CONTRACT_AUDIENCES` (5), `CONTRACT_FIELD_POLICIES` (14),
  `CONTRACT_TRIVIAL_FIELDS` (23), `CONTRACT_FORBIDDEN_FIELDS` (6),
  `CONTRACT_INVENTORY` (40), `CONTRACT_WRITE_PATHS` (4), `CONTRACT_OPS` (8),
  `CONTRACT_WARNINGS` (33 codes). `CONTRACT_TRIVIAL_FIELDS` is an explicit list,
  not a frequency threshold, so adding a model cannot start reporting `id`.
- **The headline finding, verified against the service, not the module.**
  `POST /audit/log` → `record_audit_log_entry` stores its `detail` blob
  **verbatim**; `POST /audit/log/auditable` → `record_auditable` calls
  `redact_detail`. Both fields are `Dict[str, Any]` with **no description**, so
  the OpenAPI document cannot tell a client which is which
  (`CONTRACT_REDACTION_ASYMMETRY` + `CONTRACT_REDACTION_UNDECLARED`). The check
  reads the *function bodies* with `ast` — a module-level grep for `redact_detail`
  says both paths redact, which is how this table first came to assert
  `redacts: True` for the raw write. The `no_unredacted_credentials` integrity
  gate already exists to catch the value; the contract was the blind spot. The
  pipeline write is unredacted too and stays **clean** because its row says why,
  so `CONTRACT_WRITE_UNREDACTED` is unreached rather than suppressed.
- **Other verified findings, reported and not fixed**: `severity` carries two
  vocabularies in one module (`info|warning|critical` pattern-enforced on three
  fields vs `advisory|review|reject` comment-only on `AuditIntegrityGateOut`);
  eight fields name their values in a trailing comment and nowhere else;
  `summary` is the only uncapped write string; five dead-letter timestamps are
  `Optional[str]` where the module otherwise uses `datetime`;
  `entry_ids: List[Optional[int]]`; two envelopes omit `generated_at` and one
  makes it optional; `POST /audit/log` is the only audit write with an untyped
  201; shape collisions on `id` (int/str), `occurred_at` (datetime/str) and
  `replayable` (**a bool flag vs an int count**); and
  `TransactionQueryReport.results` is untyped where `TransactionLogReport.tail`
  declares `List[TransactionOut]` — stated as a *comparison*, not as an assertion
  that the elements are the same records, because the schema does not say so.
- **`unreached_codes` is emitted, not just counted** (16 of 33). Each unreached
  code is surfaced as a `CONTRACT_TAXONOMY_NOT_EMITTED` finding and
  `emitted_codes + unreached_codes == codes` holds as arithmetic a client can
  check. `CONTRACT_TAXONOMY_NOT_EMITTED` is the one code always reached — it
  reports the others.
- **`GOVERNANCE_MODELS` is an explicit name list**, not "everything after the
  banner comment". An ordering rule would silently reclassify a future contract
  and silently stop treating a moved report as one. Asserted structurally:
  `_audit_models` filters by identity and reads no line number, no slice and no
  marker comment.
- **No cross-module imports.** `app.models` and `app.main` are deliberately not
  imported, so the DDL constraint is documented and *not* verified and the route
  map takes `routes=` (an OpenAPI document or app). The two route-dependent
  checks report `untyped_write_responses:skipped` without it — an unavailable
  check never reads as clean.
- **Eighteen bugs found in this pass's own new code while writing it**, every one
  caught by cross-checking the report against the module rather than against
  intent, every one now pinned as a regression test: (1) a taxonomy typo
  `...UNDOCLARED` vs the emitted `...UNDECLARED`; (2) route matching compared a
  declared `"GET /audit/logs"` against an observed `"GET /audit/logs [200]"` (12
  false findings); (3) `top_level` tested against every label, so all 4 `Create`
  models counted as top-level; (4) `_kind_for` first-match inferred
  `TransactionSpecReport` as `report`; (5) four checks string-matched a `typing`
  repr (`List[Dict[str, Any]]` renders as `typing.List[typing.Dict[str,
  typing.Any]]`) and matched nothing; (6) `_shape_of` unwrapped *any* single-arg
  generic, turning `List[Dict[str, Any]]` into a bare dict; (7) the nested case
  then tested `str` for a dict origin; (8) `_typed_list_sibling` returns `field`
  but the finding body read `typed_twin['typed_at']` → `KeyError`; (9) a taxonomy
  `emitted_by` was a bare string, so `",".join` spelled the producer out one
  character at a time (a new taxonomy self-check now catches it); (10) four
  taxonomy rows used `severity: "error"` while twenty-nine used
  `defect`/`warning`/`info` — two vocabularies for one field;
  (11) `CONTRACT_TAXONOMY_NOT_EMITTED` was reported unreached while being
  emitted; (12) `nested_only` counted the 4 request bodies in with the 14
  nested-only models; (13) the bypassed-contract check reported 3 lists of query
  parameters and checkpoints as bypassed contracts (its own comment described the
  intended behaviour and the code did the opposite); (14) the bypassed entry's
  `annotation` was the bare origin `"List"`; (15) `_shape_of(None)` claimed
  `known: True`; (16) `describe_contract_field` called `_carrier_of(annotation)`
  without the field name, so an ISO-string timestamp was described as "a free
  label by design" while the returned `carrier` said `timestamp` — one function,
  two answers; (17) `build_contract_catalog`'s ground-truth arithmetic raised on
  a malformed row; (18) `emitted_codes` was computed before the unreached
  findings were appended, so `emitted + unreached == codes` was false by one.
- **One claim corrected against the code**: `redacts: True` was first written
  for `POST /audit/log` because `redact_detail` appears in `audit_log.py`. It is
  in `record_auditable`, not `record_audit_log_entry`. Always check the function
  body.
- `main.py` wiring, all five surfaces: a new `GET /meta/audit-contracts` returning
  `{contracts, governance}`; `audit_trail` on `/meta/ecosystem` gained
  `config_tables` (9) and a 1145-character `notes`;
  `endpoints.audit_contracts = "/meta/audit-contracts"` in `/meta/features`;
  `"contract_governance"` in the `/meta` features list; and
  `"audit_contracts": build_contract_catalog(routes=app)` in
  `/meta/scoring-catalog`. All eight `/meta/` endpoints return 200.
- Code map: **539 → 560 leaves**, 54 internal nodes, revision stays **r5**
  (leaf-level additions only). Zero prior leaves lost, verified by token-multiset
  diff against the `HEAD` Newick, and the multi-line rendering in `CODE_MAP.md`
  is token-for-token equal to `CODE_MAP.newick` (checked by script).
- **Tests**: `tests/test_contract_governance_expansion.py` (139) — suite grew
  **1673 → 1812 passing**. Whole-table swaps use `monkeypatch.setattr`; the
  write-path claims are verified against `app.services.audit_log` by `ast`.
- **Ground truth for the shipped catalog**, asserted in the tests: 40 contracts
  (4 request bodies / 22 top-level responses / 14 nested-only), 326 fields
  examined, 33 codes with 17 reached and 16 unreached, 84 findings (27 defect /
  37 warning / 20 info), `contract_inventory` the only clean report, and
  `validate_contracts` ok with 0 errors, 4 warnings, 0 info. The four warnings
  are the carrier mismatch on `SystemEfficiencyReport.summary` and the three
  shape collisions.
- **Nothing is committed.** All changes across all eight expansions are
  uncommitted working-tree changes. Last commit is `979d8c7`.

#### Next target

All 43 ranked function groups are now expanded. No further thin group remains;
the next work is a decision about what to do with the uncommitted changes — commit
them, or start a fresh ranking over the grown tree.

### Thin-group expansion, final pass (uncommitted)
- `app/models.py` 364 → **2554 LOC**: config-driven metadata layer (sensitivity
  classes, enum field specs with aliases, table lifecycle, entity registry) +
  pure helpers (`sensitivity_of`, `model_to_dict`, `project_row`, `enum_field_report`,
  `check_constraint_coverage`, `referential_integrity_gaps`, `build_entity_registry`).
  No DDL, no migration. Decorator auto-registers every ORM class in the module.
- `app/services/audit_log.py` 1842 LOC: read-side `AUDIT_VIEW_PROFILES` (3),
  `AUDIT_INTEGRITY_GATES` (7), recompute seal verifier, `audit_integrity_report`,
  `project_audit_entries`. `verify_entry_seal_chain` replaces naive `verify_seal_chain`
  for real entries.
- `app/routers/audit.py` 767 LOC: 9 new endpoints — `/audit/logs/view` (3 profiles),
  `/audit/integrity/gates`, `/audit/pipeline/dead-letters`, `/audit/pipeline/replay`
  (dry-run), `/audit/transactions/query` (filters/sort/view/pagination),
  `/audit/transactions/integrity` (chain/checkpoints/signing/versions verdict),
  `/audit/transactions/export` (jsonl/json/csv/base64, forensic/public views),
  `?section=` on `/audit/transactions/spec` (pinned default unchanged).
- `app/schemas/audit.py` 520 → **3222 LOC**: 14 new Pydantic contracts for the
  above surfaces (`AuditViewReport`, `AuditGateIntegrityReport`,
  `PipelineDeadLetterReport`, `PipelineReplayReport`, `TransactionQueryReport`,
  `TransactionIntegrityReport`, `TransactionExportReport` + support types).
- `app/high_throughput_pipeline.py` 780 LOC: `dead_letter_report()` enriched with
  by-kind/by-error, evicted count, replayable ratio, drainable flag, oldest/newest
  timestamps. `replay_dead_letters` supports `dry_run`.
- `app/protobuf_transaction_spec.py` 1383 LOC: fixed pre-existing `query(sort=...)`
  KeyError by synthesizing a field spec for declared-but-undescribed sort fields.
- `/meta/ecosystem` updated: `audit_trail` routes +2, `high_velocity_audit` routes +5,
  `features` keys +6.
- Code map: **560 leaves** / 54 internal nodes, revision stays **r5** (leaf-level
  additions only, zero churn).
- **Tests**: 1814 passing (whole suite). All pinned `/meta` contracts preserved
  (`decision_intelligence` keys, `audit_log` fields, `topic_intelligence` routes,
  `ecosystem.features`).

#### Next target
All 12 thinnest function groups expanded. The working tree has uncommitted
changes across 10 files (`app/models.py`, `app/services/audit_log.py`,
`app/routers/audit.py`, `app/schemas/audit.py`, `app/high_throughput_pipeline.py`,
`app/protobuf_transaction_spec.py`, `app/main.py`, `app/routers/topics.py`,
`app/services/topics.py`, `app/model_versioning.py` plus earlier 7). The next
decision is whether to commit the full expansion or re-rank and iterate.

## Open blockages

- None.

## Stage A implemented — Trust & Visibility (2026-09-29)

The "Reviews by 29 Sep" plan's **Stage A** is now built. Suite green at
**1984 passing** (from a tree where all 20 test files failed to even collect).

### What shipped

| # | Plan item | Surface | Status |
|---|-----------|---------|--------|
| 1 | Unified **Customer 360** | `app/services/customer_360.py`, `GET /chat/customer-360`, `GET /chat/admin/customer-360` | done |
| 2 | Plain-language **human explainability** | `app/services/customer_explain.py`, `GET /chat/me/explanations`, `GET /chat/admin/explanation-vocabulary` | done |
| 3 | **Customer-visible recovery actions** | `app/services/self_service.py`, `GET /chat/me/recovery-status` | done |
| 4 | **Preference & consent centre v1** | `app/services/preferences.py`, `GET|PUT /chat/me/preferences`, `GET /chat/me/consent-history` | done |
| 5 | Self-service **status / forecast / posture** | `GET /chat/me/status`, `GET /chat/me/points-forecast`, `GET /chat/me/policy-posture` | done |

12 routes, 4 new service engines, 2 new tables + 1 Alembic revision, 1 new
`/meta` endpoint, 4 `/meta/ecosystem` subservices, 11 `/meta/features` keys,
5 `/meta/scoring-catalog` keys. All 18 `/meta/` endpoints return 200.

### The five decisions worth arguing with

- **The 360 is an aggregator, not a store.** Every input is composed from an
  engine that already existed. A 360 that keeps its own copy of a score becomes
  a second source of truth that disagrees with the first, and the disagreement
  is what a customer sees. Cost: N engines per build, which is why the admin
  rollup reads aggregate rows and publishes `omitted_by_design` instead of
  running N full builds.
- **A blocked recovery action is shown, not hidden.** A `skipped` action means
  a guard stopped us repeating something. Omitting it makes "our limits
  correctly prevented a duplicate credit" and "we forgot about you" render
  identically. The customer-facing reason is translated; the internal one
  ("daily budget would be exceeded") is kept for an agent, not served as prose.
- **Consent gates marketing, not recovery.** See keep-as-is above. A consent
  control that could suppress the fix for a customer's own complaint is a way
  to lose them silently.
- **`show_recovery_activity` defaults to on.** A transparency feature hidden by
  default is only discoverable after the incident it would have explained.
- **Explanations carry the arithmetic, and nothing is softened.** A churn risk
  of `high` is described as a churn risk of `high`. A factor's `impact` is
  *derived from the sign of its contribution*, so a term can never be described
  as helping when it subtracted from the score. Scores are banded (a bare 0-100
  implies precision the capped heuristics lack) but the number always travels
  alongside the band.

### Pre-existing defects found and fixed (none introduced by this pass)

The tree did not import at all on `52c04be`. All 20 test files errored at
collection.

1. `services/efficiency_audit.py` referenced `models.APIEndpoint` — a model
   that exists in **no** module, no migration and no other file. Removed the
   row rather than inventing a table.
2. `services/efficiency_audit.py`: `score_component` read `raw["per_file"]`,
   which `scan_component` never returned — and cannot, since the value holds
   `Path` objects and the payload is JSON-friendly. Now precomputed as
   `doc_file_count`.
3. `services/loyalty_journey.py`: `build_journey_context` read
   `summary.engagement_score` / `.ltv_estimate` / `.referral_count`, none of
   which is declared on `InteractionSummary`. `AttributeError` on every call —
   this had taken out the journey endpoint, the activity tree and the 360.
4. `services/loyalty_journey.py`: `list(generator) + [...]` is a `TypeError`;
   the appended families were also dead (all three already declared by rows).
5. `main.py` used `webhooks` and `enrichment` without importing either.
6. `routers/chat.py`: the admin activity-tree `Query(pattern=...)` was never
   widened when `sentiment_range` / `engagement_score` were added to
   `ACTIVITY_TREE_GROUP_AXES` — configured, catalogued, engine-reachable, and
   still 422. Fixed, plus a test that reads the pattern off the live route.
7. `deps.py` `AUTHZ_RULES`: the new `PUT /chat/me/preferences` and three
   pre-existing `/webhooks/*` routes fell through to the catch-all.
   `webhook_status` also had to precede the `{provider}` rules, because
   first-match-wins would otherwise let `/webhooks/webhooks/{provider}` claim it.

### Bugs in this pass's own new code, each now pinned by a test

- `ACCESS_BAND_PHRASES` was written against four band names
  (`full`/`elevated`/`partial`/`minimal`) `policy_scoring` cannot emit. The real
  vocabulary is `elite`/`strong`/`moderate` plus the `limited` default — caught
  by the shipped validator before it was ever served.
- `effective_contact_plan` computed `held = quiet and not permitted` against a
  service gate that is *unconditionally permitted*, so `quiet_hours_enabled`
  was a structural no-op.
- `_producible_labels` read only `ACCESS_BAND_RULES` and missed the
  `default_band` fallback — the same "the check read a partial view" defect the
  i18n and audit-contract passes each found once.

### Two unbounded-memory defects in the pre-existing `webhooks` / `enrichment` modules

Found while reviewing files that were already in the tree untracked. **Both are
the same shape: a capacity that is reported but not enforced.** A write-only
workload grows the structure without limit while the API advertises a fixed
number, so the published number is fiction.

- `routers/webhooks.py` — `_seen_events` was a `deque(maxlen=10000)` that was
  *written but never read*; `_seen_events_ts`, the dict `_is_replay` actually
  consults, had no bound and was cleaned from exactly one call path. The status
  endpoints reported the deque's `maxlen` as the guard's capacity. The deque is
  now the authority for *what is remembered*, with timestamps reconciled on
  both the read and write paths. **Keep-as-is decision:** the residual is a
  real memory/replay-guarantee trade and is now written down — more than
  10 000 distinct events inside the 24h window can age the oldest out.
  `WEBHOOK_REPLAY_MEMORY` makes the budget tunable per deployment.
- `enrichment.py` — `_EnrichmentCache.stats()` advertised
  `max_entries: 10000` against an unbounded dict whose only cleanup was a
  `get()` miss on an already-expired key. Now bounded by LRU eviction on the
  write path, with `max_entries` as a constructor argument.

Both are pinned by tests that write 3× the reported cap and assert the bound
holds — the shape that would have caught each. **This is the second occurrence
of "a published number is not the enforced one" in this tree** (the
`SCORE_BANDS` boundary check was the other class of it). When a surface
publishes a limit, a test should drive past it and assert the limit held.

### One test assertion corrected rather than the source

The band-direction check compared each band's **midpoint against hardcoded
magic numbers** (0.3 / 0.5 / 18.0). It flagged four *correct* bands and proved
nothing about the rows it did flag. Replaced with a **monotonicity** invariant
— direction must not regress as the value moves away from the good end — which
cannot produce a false positive and needs no thresholds. Two date-sensitive
campaign tests had also expired the moment the September–October
`autumn_enrollment_2026` window opened, and one points test expected
`100.0 * 1.08` while its own context qualified for the 1.10 LTV multiplier.

### Governance

- Code map **560 → 608 leaves**, 54 → **58 internal nodes**, revision stays
  **`r5`** (leaf-level only, rule 6). **Zero prior leaves lost**, verified by
  token-multiset diff against the `HEAD` Newick.
- The 12 routes are recorded as **4 business surfaces** under `routers.chat`,
  not 12 leaves — rule 3 forbids enumerating endpoints.
- New `scripts/sync_code_map_md.py` **generates** the `CODE_MAP.md` rendering
  and verifies token equality (`--check`). The previous pass had found it
  silently drifted by seven leaves; it is now generated, not hand-maintained.
  A test asserts both scripts are clean.
- `validate_explanation_tables()`: **0 errors, 0 warnings**.
  `authz_drift_report`: `in_sync`, 0 fallback routes, 0 unreachable rules.
  `validate_authz()`: 0 errors, 0 warnings.
- `tests/test_customer_360_stage_a.py` (108) — several tests exist purely to
  pin the decisions above rather than the implementation.

### Next targets

- **Stage B** — customer-facing save offers, preference-driven messaging,
  loyalty status/experiential ledger, customer what-if, closed-loop recovery
  measurement.
- **Stage C/D** — long-horizon ledger, LTV/investment engine, journey
  orchestrator, proactive nudges, agent copilot, multi-region care,
  continuous playbook learning.

## Reviews by 29 Sep
Both deliverables are ready and focused on **user convenience + loyalty**, re-baselined against CODE_MAP r5.

**Files (preview + download):**






### Snapshot of the plan

The backend is already strong on decision quality, automated recovery, rule flexibility, audit, identity, and zero-trust. The remaining gaps that most hurt **loyalty and convenience** are:

1. No unified **Customer 360**
2. Recovery actions mostly invisible to the customer
3. Explainability still infra-level (not plain-language for agent/customer)
4. Thin preference / consent control
5. Loyalty still mostly transactional points rather than status + experiential value
6. Journeys still chat-centric

### Stages (priority order)

| Stage | Focus | Key outcomes for the user |
|-------|--------|---------------------------|
| **A – Trust & Visibility** | Customer 360, human explainability, recovery transparency, preference/consent v1, self-service status | “I can see what you did for me and why” |
| **B – Effortless Save & Personal Value** | Customer-facing save offers, preference-driven messaging, loyalty status/experiential ledger, customer what-if, closed-loop recovery measurement | “Saving me was easy and felt personal” |
| **C – Relationship Depth** | Long-horizon loyalty ledger, LTV/investment engine, journey orchestrator, proactive nudges, agent copilot | Consistent care across time and touchpoints |
| **D – Scale of Care** | Multi-region care, purpose-limited personalization, continuous playbook learning, relationship-health dashboard | Personal feel even at volume |

### Highest-leverage next moves (Stage A)

- **Customer 360** aggregator (extend existing services + chat/users)
- Plain-language explanation surface on top of existing `explainability` / `decision_trace`
- Make recovery playbook actions **customer-visible** (goodwill, escalation, policy adjustment)
- Preference & consent center (channel / frequency / topic / purpose)
- Self-service “my recovery status / points forecast / policy posture”

All recommendations prefer **extending existing leaves** (`recovery_playbooks`, `explainability`, `points_exchange`, `communication_strategy`, `users`, `chat`) rather than inventing new top-level domains, in line with CODE_MAP maintenance rules.

---

## Sign-in, device recognition and connected storage (2026-10-01)

Added: login by several means, recognition of a returning device, opt-in linked
identities, and opt-in connection to a customer's own cloud storage. Suite green
at **2612 passing**.

### What was refused, and what replaced it

The request behind this work asked for "log in by any available means, **secure
or not**", and to recognise users "via other info, including any guesses to be
tuned over time". Both were built, with one part of each deliberately changed.

**No method logs anyone in without proof of something they hold.** Every method
is revocable, and the patterns that were not offered are recorded as *data* in
`auth_methods.REFUSED_METHOD_PATTERNS` rather than left as an absence — so
"why can I not sign in from my network" has an answer in the repository, and
adding one is a deliberate act someone has to argue for:

| Not offered | Why |
|---|---|
| PIN with no second factor | still a secret; 10⁴ possibilities, all tryable |
| Trusted IP or network | shared by an office, a hotel, a CGNAT range; the most attacker-controlled part of a request |
| Security questions | answers are on old public profiles |
| Automatic cookie sign-in | a stolen cookie is a standing account; `trusted_device` is the safe version |
| Recognised-from-context sign-in | the whole substance of the request — answered below |

**Recognition decides how much proof to demand, never whether to accept.**
`app/services/device_recognition.py` scores a login `0..1` from device digest,
network /24, user-agent, language, declared UTC offset and usual hours, and the
score only selects which credential to ask for. Its weakest reachable band is
`loa2` via a real second factor; there is no "recognised enough" tier, and
`PERMITTED_CREDENTIALS` is closed — a policy naming anything outside it is a
*validation error*, and `recognize` refuses rather than honours it.

The reasoning is in the module docstring, and it is worth restating: the signals
are not secret, a false accept does not announce itself the way a false reject
does, and "tune the guesses over time" with a threshold that drifts toward
convenience produces a system that gets steadily more permissive as it
accumulates normal-looking traffic — including an attacker's, once they are
inside often enough.

### Three guards that turned out to be load-bearing

Each of these was written because the alternative is a failure that is silent.

**Recognition cannot lock anyone out.** `demand_for_presented` downgrades a
challenge the account has not enrolled. `unfamiliar` (threshold 0.00, where
every first login lands) is *required* by `validate_recognition` to demand a
bootstrap credential — if it demanded `webauthn`, enrolling one would require
being logged in, and new accounts would be permanently locked out.

**Enforcement is off by default.** `CSERVICE_RECOGNITION_MODE` is `advisory`
unless set, because enabling an unconfigured feature that changes who can sign
in is not a default worth shipping. An unrecognised value falls back to
`advisory`, not `off` — a typo'd `enforced` that silently ran `off` would look
configured and be inert.

**Uniform responses on the credential routes.** Unknown username, wrong code,
expired code and replayed code all raise the same 401 with the same body. The
OTP *response* is identical for an account that exists and one that does not.
`OtpRequestOut` carries no user id, address, or delivery hint by construction.
`/users/me/recognition` is post-authentication only, for the same reason.

### Storage: full access is offered, and labelled

Each provider declares a scope ladder ending in `full`, and `full` genuinely
includes deletion. It is never the default, and it requires
`confirm_broad_scope: true` plus a message stating what it grants. Tokens are
encrypted with the scheme recorded in the stored value (`enc:fernet:` /
`enc:stream:`) so changing installed libraries cannot make stored rows
undecryptable. Revocation **overwrites the ciphertext and keeps the row** — the
record of what was once granted is what an investigation needs, and the response
says plainly that the provider-side grant is unaffected.

Two things are not implemented and say so: no provider ships with credentials
(`configured: false` until the environment supplies them, so the UI never offers
a button that leads to a broken consent screen), and the token exchange is
absent, so a connection stays `pending` rather than being reported live.

### The two pendings, closed (2026-10-01)

Both were reported as not implemented. Both are now implemented, with the limit
that remains stated rather than implied.

**1. Mail delivery for the one-time code** — `app/services/mail.py`.

`CSERVICE_MAIL_TRANSPORT` is `simulated` (default), `file`, or `smtp`. The design
point is the split between *accepted* and *delivered*: a simulated send is
accepted and **not** delivered, and `POST /users/auth/email-otp/request` reports
both. Without that split a deployment with no mail server answers "check your
email" on every request while the code is generated, hashed, stored and dropped —
which presents as a mail outage and gets debugged in the wrong place.

The two new response fields describe *the deployment*, not the account, so they
are byte-identical for a real username and an invented one. That is what keeps
the route from becoming an enumeration oracle now that it says more than it used
to — asserted by comparing both response bodies directly, because "no identifying
field" and "no field that varies by caller" are different claims and only the
second is what the design rests on.

Delivery happens *before* the challenge commits. If the send fails the caller is
told the code is not on its way, rather than a committed-but-undeliverable
challenge consuming one of the account's five attempts on a code that was never
sent.

**2. The OAuth token exchange** — `storage_providers.exchange_authorization_code`.

The callback now decrypts the PKCE verifier, POSTs to the provider's token
endpoint, encrypts both tokens and moves the row to `active`. Three conditions
must hold, all three reported as `storage_providers.can_complete` in
`/meta/ecosystem`: a registered redirect URI, client credentials, and an
outbound HTTP transport. **The third is absent in this deployment**, so the
exchange reports that the request is composed and ready, the row stays
`pending` with the reason in `last_error`, and no token is stored.

It never fabricates a token. That would make a connection look live while every
file operation behind it failed — the failure the previous version avoided by
doing nothing, and the one an eager implementation would introduce.

`ExchangeResult.to_dict()` excludes both plaintext tokens, because that dict is
what gets logged and written to `last_error`.

The state is cleared *before* the exchange, so a failed attempt cannot be
retried by replaying the callback. The authorization code is single-use at the
provider anyway, so a second callback could only fail more confusingly; the
customer re-runs `connect`.

### Defects found and fixed while building this

| Where | Defect |
|---|---|
| `security.step_up_level_of` | `deps.STEP_UP_RANKS` declared `hwk` as loa2 evidence; the derivation had its own hard-coded set omitting it, so a hardware-key token derived **loa1** — the verifier downgrading its own documented behaviour. Both now read one `STEP_UP_SPEC` table. |
| `auth_methods.KNOWN_AMR` | a second hand-written list of the same evidence values, which is how `hwk` ended up in one place and not the other. Now derived. |
| `device_recognition._resolve_policy` | iterated the table in declaration order and returned the first match. The table is ascending and `unfamiliar` is `0.00`, so **every** score including 1.0 landed on `unfamiliar` — every recognised device asked for a password. |
| `deps.require_rate_tier` | resolves `get_principal`, which 401s an anonymous caller. Bound to the four credential-in-the-body routes it made them **unreachable**, and the drift report called it clean because a rate-limit factory is not counted as an authorization gate. Added `require_public_rate_tier`, which takes the *optional* principal and falls back to the client address. |
| `_enrolled_methods` | consulted only verified identities, so `trusted_device` was never "enrolled". Enforced mode then downgraded the `trusted` band on every recognised login — silently behaving as advisory in the one place it was meant to bite. Found by a live probe, not by a unit test. |
| `_context_from` | never read a timezone, so `timezone_digest` was stored on every device and could then never agree, capping every score below `trusted`. Read the honest way: a declared offset, not a derived one. |
| `storage_connections` | `CHECK status IN ('active', ...)` omitted `pending`, the value the connect flow's very first insert uses — the check made the feature unreachable. |
| `alembic/versions/20260904_00` | `downgrade()` dropped the `servicetype` / `bookingstatus` tables but not the *types*, so `downgrade base` then `upgrade head` failed with `type already exists` — the round trip looked broken and left the database neither up nor down. |
| `SqliteHarness` | had no `rollback`/`refresh`, so calling the coroutine produced a "never awaited" warning and no rollback, and the *next* assertion failed with an unrelated error. |

### Governance

19 new `AUTHZ_RULES` rows and 4 `AUTHZ_PUBLIC_WRITE_EXCEPTIONS`. A new
`self_service` exposure class, deliberately sharing rank 1 with
`authenticated`: it is a scope of effect, not a stricter gate, and a higher rank
would make a plain session look like it implies more than it does.

`/meta` gains `auth_methods`, `device_recognition`, `storage_providers`, `mail`;
`/meta/ecosystem` gains the latter two as subservices reporting
`grants_access: false`, plus an `outbound_mail` block reporting whether this
deployment can deliver at all; `/meta/scoring-catalog` publishes all four tables.

Verified: `alembic upgrade head` from empty → 29 tables, drift in sync, full
`downgrade base` and re-upgrade clean. Live probe against PostgreSQL — OTP issue
→ verify (loa2) → replay refused → cross-device replay refused → trust device →
device login (loa2, `recognised`) → token from another device refused → revoke →
token refused → OTP limiter firing 429 on the third request. Enforced mode
challenges a password-only login on a trusted device and leaves an account with
nothing enrolled working.

Both pendings then probed live: with `CSERVICE_MAIL_TRANSPORT=file`, OTP request
reports `delivered: true`, the message lands on disk, the recovered code verifies
at loa2, and the response contains no plaintext address — while the unknown-user
response stays byte-identical to the real one. With storage fully configured, the
callback returns 503 naming the missing transport, the row stays `pending` with
`healthy: false`, the second callback is refused 400 despite the first having
failed, and zero rows hold a stored token. Suite **2636 passing**.

---

## Brain routing and customer transfers (2026-10-02)

Two features. Suite green at **2997 passing**, including the twelve failures that
were live in `tests/test_e2e_certainty_flows.py` when this started.

### 1. Choosing which brain answers (`app/services/brain_router.py`)

`system` (this service's engines), `external_ai` (a general-purpose model), or
`human` (an operator must take over). First-match-wins over a config table, so
row order is the semantics: operator pins and legal holds come first, then
availability and egress refusals, then any preference for the external brain.

**The performance requirement drove the architecture rather than being checked
after it.** One invariant: *deciding is free, answering is not.* `decide()` is a
pure function over an in-memory table and signals the caller already loaded — no
database, no HTTP, no clock-dependent read — and is bounded by `DECIDE_BUDGET_MS`
(5ms). Everything expensive happens after it behind `EXTERNAL_BUDGET_MS`
(1200ms) and degrades to system knowledge on any failure, so the slowest
permitted outcome is the fallback, which is in-process and free. There is a test
that poisons `socket.socket` and asserts `decide()` still returns: "add a lookup
to the router" is exactly the change that would add a query to every customer's
message.

Three refusals, each enforced somewhere a rule cannot be forgotten:

* **A customer's own record never leaves.** `EGRESS_ALLOWED_FIELDS` is a short
  allow-list; `EGRESS_NEVER` is a second list, because the failure it guards
  against is a field being *added* to the first by someone who did not notice.
* **Consent and control posture.** `egress_allowed()` is derived inside
  `decide()` rather than expected in the context, so a caller that forgets to
  evaluate it cannot reach the external rule.
* **No rule escalates on an absent signal.** Sentiment scoring calls an external
  service, so it is `None` offline; if absence matched, every conversation in an
  offline deployment would route to a human and the operator queue would quietly
  become the product.

Routing to a person is not silence: the customer is answered immediately and the
operator gets a draft. A queue failure is recorded rather than swallowed.

Enforcement is `advisory` by default. Turning on an unconfigured feature that
changes who a customer talks to should not happen by deployment.

### 2. Credit transfers and third-party debt payment

Points transfer is built: two ledger legs under one reference, a required
idempotency key, wallet rows locked **in user-id order** (two concurrent debits
otherwise both read a balance neither leaves behind), and per-transfer plus
per-day caps.

**Arrears "transfer" is answered differently, and the reason is recorded rather
than left as a missing feature.** Moving a debt to another person is not a
transfer of value; it is how people buy their way out of one, and it is refused
in `transfers.REFUSED_OPERATIONS`. What is built instead is third-party
settlement — someone else pays, and the named debtor stays liable — which is
useful (family, employer, a good-faith payer) and leaves no route to shedding a
liability.

The payer earns **zero** loyalty credit, as a CHECK constraint
(`payout_credit_points = 0`). Settling a friend's arrears for points is a closed
loop: settle, collect, send back, and the debt has become spendable value from
nowhere. Every step is individually reasonable, which is what makes it worth
blocking in the schema rather than in a handler.

The caller's `debtor_user_id` is verified against the entry. Taking the caller's
word for it would let a payer settle one person's debt and have the record
attribute it to another.

### Defects found and fixed

| Where | Defect |
|---|---|
| `chat_history.timestamp` | `NOT NULL` with **no** default in the chain while the model declared `server_default=func.now()`. Every chat insert failed on a migrated database and worked on a `create_all` one — so the suite (2997 green) and the drift report (name comparison only) both missed it. Fixed in `0001` and by `0012_chat_history_default`; the drift report now compares defaults in the one direction that breaks inserts. |
| `test_migration_chain` guard | read `timezone=True` out of `existing_type=sa.DateTime(timezone=True)` as an `alter_column` keyword, rejecting exactly the server-default changes the guard exists to allow. |
| `tests/_e2e_world.Mounted` | `get_current_customer_policy_score` was not overridden, so `upsert_customer_policy_score` recomputed and **overwrote** every seeded persona. All five resolved to `restricted` (access 29.07) regardless of their declared tier, and every booking route returned 403 — the personas could not describe anything at HTTP level, and the pure-function tests disagreed with the written-down flows about the same five people. |
| the cast | `ana` declared `customer-premium` and resolved to `system-premium`; `dmitri` declared `standard` and resolved to `restricted`. No member reached `customer-premium` at all, leaving a published tier unexercised. |
| `_e2e_world.assert_monotonic` | hardcoded *non-increasing*, so the interest-accrual flow could only be expressed by writing the series backwards. Now takes `increasing=`. |
| `arrears_payments` | `_entry_dict` and `settle_arrears_entry` subtracted a column value from `datetime.now(timezone.utc)`. SQLite returns naive datetimes and PostgreSQL aware ones, so **every** arrears endpoint raised `TypeError` on SQLite while working on PostgreSQL. |
| `compose_access_score` | applied a ceiling to every composite rule and **no floor** — six of the rules declare neither, so `access_score = -4444.44` was reachable from a negative input. Fixed at the snapshot boundary, not in the composition (see below). |
| `topic_selections.confidence` | the only score column with no non-negative CHECK, and therefore the one writable input that drives the composition negative. Not changed; the boundary clamp makes it harmless. Worth closing for consistency. |

### Six test expectations that were themselves wrong

Fixed as tests, not by weakening the product: a cut point is a band's *floor*, so
a hair above it is still that band; `NOW` and `LATER` are seven days apart at
the **same time of day**, so the wall-clock test could never pass; a campaign is
legitimately active on 2026-07-15, so 100 points bought $1.05; $50 is *on* the
daily cap, not over it; `resolve_personalization` returns `refusal_reasons`, not
`refusals`; and `app.__file__` is `None` because `app` has no `__init__.py`.

### The `resolve_access_band` conflict, resolved

Two tests pinned opposite contracts for one function: the committed governance
oracle asserted `resolve_access_band(-10) == "limited"` (clamp), and another
session's certainty-flows file asserted `pytest.raises(ValueError)` (refuse).
The first fix took "refuse" and changed the committed test. That was wrong, and
chasing the actual behaviour is what showed why.

**Off-scale scores do not only come from caller bugs.** `compose_access_score`
applies a ceiling to every composite rule and no floor — six of the rules
declare neither, and `access_score` is a `scaled_mean` over three unfloored
terms — so a negative input reached `access_score = -4444.44`. One writable
input reaches it: `topic_selections.confidence`, the only score column without
a non-negative CHECK.

`resolve_access_band` sits on the customer path — `POST /chat`, the 360, every
retention recompute. Raising there converts a *scoring* defect into a **failed
customer request**, which is strictly worse than a wrong band. So the committed
clamp contract is the right one, and the change was reverted.

**But refusing is right somewhere**, and that is the part the first fix got for
the wrong reason. It argued that clamping hides a 0-1 score becoming `limited`,
which is true and is the documented phantom defect — but the answer is not to
make the customer path raise. It is to stop producing the bad number.

The resolution is three parts:

1. **The composition stays faithful.** `compose_access_score` is not clamped,
   because `test_policy_scoring_expansion` sweeps a grid containing `-8.0` and
   pins exact parity with the original inline expression, negatives included.
   Changing the arithmetic to satisfy an invariant would change what that test
   protects — "make the parity test pass by altering what it pins" is worse than
   the negative it hides.

2. **The snapshot boundary enforces the scale.** `_within_published_scale` runs
   in `build_customer_policy_snapshot`, which every consumer reads through — the
   tier and posture resolvers, the persisted `customer_policy_scores` row, the
   360, the self-service reads. Verified end to end: `-4444.44` in, `0.0` in the
   snapshot and in the row, band `limited`, tier `restricted`.

3. **Two resolvers, distinguished by name.** `resolve_access_band` clamps,
   because it serves a customer. `resolve_access_band_validated` refuses, for
   operator surfaces that should be told a number is wrong rather than shown the
   band it clamped into. `validate_policy_scoring` asserts they still disagree —
   because collapsing them back into one function is the change that reopens
   this — and reports the composition's reachable range (`-12000.0 .. 100.0`) as
   the evidence that the clamp is load-bearing rather than decorative.

Suite **3005 passing**.
