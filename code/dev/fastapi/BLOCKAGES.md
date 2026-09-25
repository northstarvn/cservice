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

## Open blockages
- None.