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
