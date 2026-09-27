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

## Expansion log

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

## Open blockages
- None.