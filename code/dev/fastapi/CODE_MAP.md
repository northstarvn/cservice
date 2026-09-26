# CService Backend — Code Map (Newick) Governance

Companion to [`CODE_MAP.newick`](CODE_MAP.newick). That file is a single-line,
strictly well-formed Newick tree that reflects the **entire backend logic** at
business-surface granularity. This document explains how to read it and how to
keep it in sync as the backend evolves.

## Revision Control

- Revision ID: `r5`
- Scope: backend logic only (`fastapi/app/**`); frontend and requirement
  artifacts are out of scope, matching `ARCHITECTURE_CONCEPT_MAP.md`
- Purpose: a machine-readable, diff-friendly map that mirrors module
  structure, ownership, and the business surfaces each module exposes
- Stop rule: leaves describe *business logic surfaces*, never every endpoint or
  helper; if a leaf would need route-level enumeration to be meaningful, the
  underlying code is probably overgrown (a signal for `efficiency_audit`)
- Resume rule: update the tree whenever a module gains a *new business
  surface* (new router domain, new service rule engine, new schema family, new
  infra capability), and bump the revision below

## Reading the Tree

Newick is a nested-parenthesis notation: `(child1,child2,child3)label`.
Leaves are business surfaces; internal node labels name layers (root:
`cservice_backend`, then `infra`, `models`, `routers`, `services`,
`schemas`).

| Layer | What its leaves mean |
|---|---|
| `infra` | startup/metadata (`main`), database plumbing (`db`), auth primitives (`security`), auth dependencies (`auth_deps`), localization, multi-tenant routing + partition lifecycle (`multi_tenant`), zero-trust crypto (`zero_trust`), event pipeline (`event_pipeline`), decision intelligence (`decision_intelligence`), business-rule hyper-flexibility (`business_rules`) |
| `models` | isolated polymorphic base models (`model_bases`) plus persisted domain entities (identity, booking lifecycle, chat signals, retention, policy, topics, communication, payments, points, audit trail, security events) |
| `routers` | public API domains and their grouped surfaces (`users`, `bookings`, `chat`, `topics`, `audit`) |
| `services` | the rule engines — own the business logic (scoring, retention, loyalty, activity trees, communication strategy, payments, points, audit trail) |
| `schemas` | contract families (`schemas_core`, `schemas_chat`, `schemas_audit`) |

Rules of thumb:

- A leaf appearing under `services` means the logic lives in that service, not
  in the router (thin-adapter convention).
- A leaf appearing under a router means it is a *distinct public surface*
  (e.g. `arrears_payments` under `chat` because those routes are exposed by
  the chat router).
- Entities that are only data carriers (no business rules) appear only under
  `models`, never duplicated elsewhere.

## Current Map — `CODE_MAP.newick`

Tree size: **428 leaves / 53 internal nodes** (validated, see maintenance
rule 4). Rendered multi-line for readability; the single-line Newick file is
the source of truth.

```
(((startup_lifespan,cors,exception_handler,meta,health,runtime,scoring_catalog,i18n_endpoint,
      features,ecosystem,tenants,tenants_policy,partitions,zero_trust,decisions,
      regional_endpoint)main,
    (engine,pool_config,session,ping,retry,retry_policy,retry_classification,circuit_breaker,
      statement_timeouts,advisory_lock,transaction,pool_status,db_health,readiness_probe,
      test_connection,catalog)db,
    (verify_password,hash_password,access_token,refresh_token,decode_token,password_policy,
      key_rotation,token_revocation,token_validation,step_up,api_keys,password_strength,
      password_history,catalog)security,
    (current_user,optional_user,admin_user,policy_or_admin,policy_score,control_posture,
      principal,optional_principal,require_principal,correlation_id,require_scopes,require_roles,
      require_step_up,require_cell_access,require_tenant,rate_limit,catalog)auth_deps,
    (locale_resolution,locale_payload,message_catalog,translate,catalog_builder,
      locale_normalization,accept_language,negotiation,plural_rules,fallback_chain,coverage,
      overrides,translate_many)localization,
((registry,route_for_tenant,tenant_session,declared_tenants,catalog,tenant_validation,
        request_resolution,dsn_redaction,routing_policy,tenant_health,provisioning_plan)tenant_router,
      (policies,ensure_partition,archive,drop_expired,worker,catalog,identifier_validation,
        period_keys,sql_builders,policy_validation,catalog_reads,legal_holds,lifecycle_plan,
        adopt_partitions,attach_restore,dry_run)partition_manager)multi_tenant,
((hmac_signer,ed25519_signer,sign_bytes,verify_bytes,signature_envelope,catalog,
        backend_registry,signing_key_ring,key_fingerprint,detailed_verification,co_signature,
        multi_signature,replay_guard,self_test,health)hsm_signer,
      (modalities,register,validate,revoke,list_templates,catalog,adaptive_thresholds,
        slot_enrollment,challenges,attempt_lockout,match_decision,history,state_export,
        state_import,snapshot)biometric_vault,
      (risk_levels,risk_rules,evaluate,adaptive_weights,catalog,signal_normalization,
        context_gaps,required_controls,action_mapping,counterfactuals,weight_history,
        weight_versions,outcome_batches,risk_decisions)risk_evaluator,
      (access_levels,cell_matrix,evaluate,resolve_roles,catalog,row_scopes,cell_overrides,
        masking,resource_plans,override_simulation,temporary_grants)cell_matrix)zero_trust,
((workers,batch,submit,drain,dead_letter,stats,catalog)high_throughput_pipeline,
      (wire_format,transaction,hash_chain,append_only_log,spec_catalog)protobuf_transaction_spec)event_pipeline,
((registry,snapshots,canary,shadow_scoring,promote,catalog)model_versioning,
      (what_if,risk_scenarios,retention_scenarios,reports,catalog)simulation_engine,
      (decision_trace,trace_store,explain,factor_trail,catalog)explainability,
      (version_guard,compare_and_swap,stale_conflict,conflict_policy,versioned_records,
        versioned_store,field_diffs,json_merge_patch,retry_updates,lock_sets,catalog)optimistic_locking)decision_intelligence,
((field_dsl,combinators,date_ops,expressions,catalog)rule_engine,
      (calendars,labor_rules,tax_rules,booking_assessment,catalog)regional_policy)business_rules)infra,
((timestamp,tenant_scoped,partitioned,security_event,soft_delete,row_version,expiring,
      actor_audit,serialization,security_event_extensions,entity_registry,catalog)model_bases,
    user,booking,booking_event,booking_assignment,chat_history,interaction_signal,
    retention_snapshot,recovery_outcome,recovery_action,customer_policy_score,topic_selection,
    communication_override,arrears_entry,points_wallet,points_transaction,audit_log_entry)models,
((register,login,me,policy_score,can_access,policy_decision,change_password,session_refresh,
      logout,session_inventory,step_up,api_keys,password_policy,password_feedback,
      security_posture,catalog)users,
    (lifecycle_crud,analytics,assignment_report,audit_history,export)bookings,
    (history,sentiment,insights,recovery,recovery_playbooks,loyalty_journey,retention_dashboard,
      snapshot_operations,activity_tree,communication_strategy,arrears_payments,points_exchange,
      topic_policy)chat,
    (catalog,themes,intelligence,coverage,portfolio,workspace,overview,search,suggestions,
      recommendations,current_selection,history)topics,
    (efficiency,enhancements,audit_catalog,trail_catalog,log,logs,summary,pipeline_stats,
      pipeline_event,transactions,protobuf_spec,governed_log,integrity,actors,timeline,anomalies,
      retention,export)audit)routers,
((area_scoring,sentiment,insights,recovery,topic_ranking,topic_policy,capabilities)chat_analytics,
    (catalog,search,suggestion,theme,selection,workspace,intelligence,coverage)topics,
    (lifecycle,normalization,transitions,ownership,assignment_report)bookings,
    (policy_tier,control_posture,access_band,can_access,decision_reports)policy_scoring,
    (snapshots,trends,deltas,retention_dashboard)retention,
    (freshness,health,operations_status,compliance,posture,automation,launch_readiness,go_no_go)retention_snapshot_ops,
    (scenario_catalog,matching,journey_plan,admin_report)loyalty_journey,
    (grouping,ranking,smart_filter,anomaly_rules,admin_trees)activity_tree,
    (override,policy,culture,profile,mood,decision_trail)communication_strategy,
    (component_scoring,classifications,enhancement_proposals,catalog)efficiency_audit,
    (interest_policies,late_fees,quote,open,settle,waive_interest,waive_fees,waiver_policy)arrears_payments,
    (rate_rules,rate_multipliers,campaigns,quote,wallet,ledger,admin_rollup)points_exchange,
    (realtime_context,playbook_rules,credit_points,escalation,policy_guardrail,orchestrator,
      auto_sweep,catalog)recovery_playbooks,
    (record,list,summary,action_catalog,governed_record,action_aliases,action_specs,
      severity_policy,justification,sensitive_detail,redaction,change_detail,paging,count,
      retention_plan,retention_advice,actor_activity,entity_timeline,anomaly_detection,
      seal_chain,chain_verification,trail_export,catalog)audit_log)services,
((user,token,booking,policy_score,topic_selection,session_lifecycle,machine_credentials,
      password_feedback,security_posture,step_up)schemas_core,
    (insight,recovery,recovery_playbooks,retention,snapshot_ops,topic_ranking,topic_policy)schemas_chat,
    (efficiency,enhancements,audit_trail,governed_write,integrity,actors,timeline,anomalies,
      retention,export)schemas_audit)schemas)cservice_backend;
```

## Maintenance Rules

1. **Add a leaf** when a module exposes a genuinely new business surface
   (new router domain, new service engine, new persisted domain entity).
2. **Move a leaf** when ownership changes (e.g. topic intelligence moves from
   the chat router into a dedicated router — update the tree the same commit).
3. **Never enumerate endpoints as leaves.** If a router gains a fifth
   sub-surface under the same domain (e.g. another `/chat/admin/*` report),
   fold it into the existing leaf instead of adding a row.
4. **Keep the single-line format in `CODE_MAP.newick`.** Do not hand-edit it;
   regenerate via `python3 scripts/build_code_map.py` (nested tuples →
   serialize → parse-validate; prints `leaves=N internal_nodes=M`) and re-run
   the validation. `CODE_MAP.md` may keep a readable multi-line rendering, but
   the Newick file is the source of truth.
5. **Update `/meta/` surfaces in the same commit**: when the map gains a leaf,
   the corresponding `main.py` ecosystem/features/scoring-catalog entries
   should already reflect it — the map documents what the backend already says.
6. **Bump the revision** on structural changes only (new layer, new top-level
   domain). Leaf-level additions are recorded in the log without a bump.

## Revision Log

### r5 — flexible-core expansion (2026-09-26)

Thin-group expansion pass: the fourteen smallest function groups (LOC ranked)
were grown into configurable, introspectable surfaces. No new layer and no new
top-level domain, so per maintenance rule 6 the revision stays `r5`; the tree
grew 278/53 → **428 leaves / 53 internal nodes** and no r1–r5 leaf was lost.
Every change is additive — existing signatures, returned payloads and pinned
catalog key sets are untouched, and no DDL was added to an existing table.

- **Infra — resilience & observability**
  - `db`: `retry_policy` (frozen `RetryPolicy` with classification, backoff,
    jitter; `policy=None` keeps the exact legacy `retry_async` behavior),
    `circuit_breaker` (`CircuitBreaker` / `CircuitOpenError`),
    `statement_timeouts`, `advisory_lock`, `pool_status`, `db_health`,
    `readiness_probe`, `test_connection`, `catalog`.
  - `security`: `key_rotation` (kid-bearing ring, `SECRET_KEY_PREVIOUS`),
    `token_revocation` (`TokenRevocationRegistry`, TTL-bounded jti deny-list),
    `token_validation` (`TokenValidation` explains *why* a token failed),
    `step_up` (`acr`/`amr` claims, `STEP_UP_LEVELS`), `api_keys`
    (digest-backed `ApiKeyRegistry` with hierarchical scopes),
    `password_strength`, `password_history` (bcrypt-verify reuse ring),
    `catalog`.
  - `auth_deps`: `principal` (frozen `Principal` unifying user, API-key and
    delegated credentials), `optional_principal`, `require_principal`,
    `correlation_id`, `require_scopes`, `require_roles`, `require_step_up`,
    `require_cell_access`, `require_tenant`, `rate_limit`, `catalog`.
  - `localization`: `locale_normalization`, `accept_language`,
    `negotiation` (`negotiate_locale`), `plural_rules`, `fallback_chain`,
    `coverage` (`catalog_coverage`), `overrides` (`CatalogOverrides`),
    `translate_many`.
- **Models** — `model_bases` gains `soft_delete`, `row_version`, `expiring`,
  `actor_audit`, `serialization`, `security_event_extensions` (extra STI
  families live in their own table so the pinned 3-key `families` catalog stays
  intact), `entity_registry`, `catalog`. No existing table changed.
- **Multi-tenancy** — `tenant_router`: `tenant_validation`,
  `request_resolution`, `dsn_redaction`, `routing_policy`
  (`build_tenant_routing_policy` + `GET /meta/tenants/policy`), `tenant_health`,
  `provisioning_plan`. `partition_manager`: `identifier_validation`
  (`IDENTIFIER_PATTERN` strict / `PARTITION_NAME_PATTERN` allows `-` in period
  keys), `period_keys`, `sql_builders` (attach/detach/restore/archive/drop),
  `policy_validation`, `catalog_reads`, `legal_holds`,
  `lifecycle_plan` (`plan_lifecycle`), `adopt_partitions`, `attach_restore`,
  `dry_run`.
- **Zero-trust** — `hsm_signer`: `backend_registry`, `signing_key_ring`
  (rotation), `key_fingerprint`, `detailed_verification`, `co_signature`
  (`sign_multi` / `verify_multi`), `multi_signature`, `replay_guard`
  (`ReplayGuard`), `self_test`, `health`. `biometric_vault`:
  `adaptive_thresholds` (config-driven per-modality bands), `slot_enrollment`,
  `challenges` (replay-gated), `attempt_lockout`, `match_decision`,
  `history`, `state_export` / `state_import`, `snapshot`.
  `risk_evaluator`: `signal_normalization`, `context_gaps`,
  `required_controls`, `action_mapping` (`RISK_ACTIONS`),
  `counterfactuals` (+ `cheapest_clearing_signal`), `weight_history`,
  `weight_versions` (`weights_at`), `outcome_batches` (`apply_risk_outcomes`,
  one version bump per batch), `risk_decisions` (`evaluate_risk_decision` +
  `RiskDecisionStore`). `evaluate_risk` score semantics are unchanged.
  `cell_matrix`: `row_scopes`, `cell_overrides`, `masking`, `resource_plans`,
  `override_simulation` (thread-safe), `temporary_grants`.
- **Decision intelligence** — `optimistic_locking`: `versioned_records`,
  `versioned_store`, `field_diffs`, `json_merge_patch`, `retry_updates`,
  `lock_sets`, `catalog`.
- **Audit trail** — `services.audit_log` (186 → 1326 LOC) gains
  `governed_record` (`record_auditable`: justification, sensitive-detail
  redaction), `action_aliases`, `action_specs`, `severity_policy`,
  `justification`, `sensitive_detail`, `redaction`, `change_detail`
  (`audit_diff`), `paging` (`iter_audit_pages`), `count`, `retention_plan`,
  `retention_advice`, `actor_activity`, `entity_timeline`,
  `anomaly_detection`, `seal_chain` (`SealChain`), `chain_verification`,
  `trail_export` (CSV + NDJSON). `routers.audit` gains `governed_log`,
  `integrity`, `actors`, `timeline`, `anomalies`, `retention`, `export`.
- **Identity** — `routers.users` (185 → ~880 LOC) gains `session_refresh`
  (`POST /users/refresh`, rotation burns the presented refresh token),
  `logout` (single-token and `all_sessions`), `session_inventory`,
  `step_up` (mint + describe `acr`/`amr`), `api_keys` (self-service issue/list/
  revoke; the `*` wildcard is never self-assignable), `password_policy`,
  `password_feedback` (non-mutating strength/advice), `security_posture`,
  `catalog`. `/register`, `/login`, `/me` and `/me/password` are byte-identical.
- **Meta wiring** — `GET /meta/tenants/policy`; 8 new `/meta/features` keys;
  identity routes in the `/meta/ecosystem` `identity` subservice; 16 new
  `schemas_core` / `schemas_audit` leaves.
- **Tests**: `tests/test_flexible_core_expansion.py` (77),
  `tests/test_risk_partition_expansion.py` (29),
  `tests/test_users_identity_expansion.py` (42) — suite grew 552 → 700 passing.

### r5 (2026-09-26)

Predictive sentiment & automated service recovery — realtime dissatisfaction
indicators and config-driven recovery playbooks that auto-trigger credit /
escalation / policy-guardrail actions. Tree grew 267/52 → **278 leaves / 53
internal nodes**; no r1–r4 leaf was lost.

- **New service** `recovery_playbooks.py` (all under `services`):
  - `realtime_context` — `build_realtime_recovery_context` + 
    `compute_realtime_dissatisfaction_indicators`: derives a live context
    (readiness, dissatisfaction score, sentiment, churn risk, value tier, risk
    areas, peak intensity, repeated messages, booking states) from the current
    interaction window rather than persisted dashboard snapshots.
  - `playbook_rules` — config-driven `RECOVERY_PLAYBOOKS` table evaluated by
    the shared when-DSL engine (`evaluate_when` + `=formula`
    `resolve_params`): `recovery_goodwill_points`, `recovery_ticket_escalation`,
    `recovery_policy_guardrail` (priority-ordered; adding a playbook is data,
    not code).
  - `credit_points` — `credit_recovery_points` reuses the canonical wallet
    (`get_or_create_wallet`) and appends a `recovery_credit` ledger row.
  - `escalation` — `build_escalation_result` records a senior-support
    escalation with a generated `ESC-<user>-<seq>` ticket reference.
  - `policy_guardrail` — `apply_policy_adjustment` applies score deltas to a
    frozen `PolicyScoreSnapshot` (preview only; tier/posture recomputed via the
    canonical policy helpers; persisted scores never mutated).
  - `orchestrator` — `run_recovery_playbooks` loads the interaction window,
    matches playbooks, executes actions, and writes one `RecoveryAction` audit
    row per action (`dry_run` mode writes nothing; failures are recorded, never
    raised through the sweep).
  - `auto_sweep` — `run_auto_recovery_pass` +
    `auto_recovery_worker_forever`, gated by `CSERVICE_AUTO_RECOVERY` (default
    off) and wired into the app lifespan like the partition worker.
- **New model** `recovery_action` (`recovery_actions` table): additive audit
  rows for every automatically triggered playbook action (payload/result JSON,
  `reference`, `failure_reason`) + `User.recovery_actions` relationship.
- **New schemas** (`schemas_chat` leaf `recovery_playbooks`):
  `RecoveryPlaybookRunRequest`, `RecoveryPlaybookRunReport`,
  `RecoveryPlaybookMatchResult`, `RecoveryAutomationActionResult`.
- **New routers** (`chat` leaf `recovery_playbooks`): self-service
  `POST /chat/recovery/playbooks` (runs the orchestrator against the realtime
  context, optional `dry_run`) and admin `GET /chat/admin/recovery/playbooks`
  (read-only catalog).
- **Meta wiring**: `recovery_automation` subservice in `/meta/ecosystem`
  (`auto_recovery_enabled` flag), `recovery_playbooks` key in
  `/meta/scoring-catalog`, feature listed under `/meta` and enough
  `/meta/features`; `recovery_actions` added to the efficiency-audit system
  metrics.
- **Tests**: `tests/test_predictive_recovery_expansion.py` (27) — suite grew
  525 → 552 passing.

### r4 (2026-09-26)

Stage 1 — business-rule hyper-flexibility & granular customization (R1 shared
`when`-DSL core, R2 financial flexibility in points + arrears, R3 regional
booking & tax). Tree grew 251/49 → **267 leaves / 52 internal nodes**; no r1–r3
leaf was lost.

- **New infra group** `business_rules` (all under `infra`):
  - `rule_engine.py` — the shared `when`-DSL core that the four service engines
    (loyalty_journey, communication_strategy, arrears_payments,
    points_exchange) now delegate to (behavior-identical; the `risk_evaluator`
    `(op, value)` engine is a different DSL and stays as-is). Adds combinators
    (`any`/`all`/`not`), the reserved `_date` key bound to the evaluation date,
    date-window operators (`between_dates`, `month_in`, `weekday_in`,
    `on_date`, `within_days`), and safe `=formula` expression params
    (AST-walked calculator; imports/attributes/subscripts rejected).
  - `regional_policy.py` — pure regional engine: `REGIONAL_CALENDARS`
    (timezone, weekends, fixed + annual holidays), `REGIONAL_LABOR_RULES`
    (max shift hours / consecutive days / minimum rest), `TAX_RULES` per
    jurisdiction × service type, `is_working_day`,
    `evaluate_labor_compliance`, `compute_taxed_amount`,
    `assess_booking_dates`. `bookings.py` lifecycle untouched (pinned routes).
  - `main` gained the `regional_endpoint` leaf: `GET /meta/regional`
    (`?region=` resolves today's working-day verdict, labor rule, sample tax).
- **R2 — points flexibility** (`points_exchange`): `rate_multipliers` +
  `campaigns` leaves — tier (`POINTS_TIER_MULTIPLIERS`), LTV-proxy
  (`POINTS_LTV_MULTIPLIERS`), seasonal campaign rules (`POINTS_CAMPAIGN_RULES`,
  date-window DSL). Multipliers are applied **only** when the quote is
  explicitly driven via `effective_date` / `param_overrides` (governed
  what-if/seasonal evaluation), so default quotes and pinned rate contracts are
  byte-identical; `param_overrides` may carry `=formula` `points_per_unit`
  resolved by the shared rule engine.
- **R2 — arrears flexibility** (`arrears_payments`): `late_fees`,
  `waive_fees`, `waiver_policy` leaves — optional late-fee terms
  (`late_fee_amount` flat / `late_fee_pct` of principal) snapshotted onto rows
  at open and charged at settlement when past due (unless waived);
  `ARREARS_WAIVER_POLICY` gates interest/fee waivers on the operator's
  `PolicyScoreSnapshot` (tier rank + access score); legacy un-scored calls
  stay un-gated. Five additive `ArrearsEntry` columns
  (`late_fee_amount`, `late_fee_pct`, `late_fee_charged`, `fees_waived`,
  `waived_fees`) with `getattr` defaults, so existing fake-row tests are safe.
- **Meta wiring**: `rule_engine` + `regional_policy` subservices in
  `/meta/ecosystem`; `rule_engine` / `regional_policy` keys in
  `/meta/scoring-catalog`; both features listed under `/meta` and
  `/meta/features`.
- **Tests**: `tests/test_rule_hyper_flexibility.py` (70) — suite grew
  455 → 525 passing.

### r3 (2026-09-25)

Phase 1 — decision quality & consistency (S-02 simulation, S-03 model
versioning + canary, C-05 explainability, M-03 optimistic concurrency). Tree
grew 230/44 → **251 leaves / 49 internal nodes**; no prior leaf was lost.

- **New infra group** `decision_intelligence` (all under `infra`):
  - `model_versioning.py` — immutable model version registry (`risk_rules` +
    `partition_policies` pre-seeded from live config) and `CanaryRunner`
    shadow-mode scoring: served decisions come from the active model, the
    candidate scores in shadow, divergence/confidence accumulate; promotion is
    optimistic-lock guarded; auto-promotion behind `CSERVICE_CANARY_AUTOPROMOTE`
    (default off).
  - `simulation_engine.py` — pure what-if engines: `simulate_risk_whatif`
    (weight overrides / disabled rules / level retune on a copy of the rule
    table) and `simulate_retention_whatif` (replay drop/keep decisions under
    modified retention, no DDL); `run_simulation` records an explainability
    trace.
  - `explainability.py` — `DecisionTrace` + bounded `DecisionTraceStore`
    (factor trail, thresholds, model version, outcome), filterable listing,
    `explain(decision_id)`, risk-result conversion.
  - `optimistic_locking.py` — `VersionedRecord` CAS guard + `StaleVersionError`
    (expected/current versions), used by model promotion/rollback.
  - `main` gained the `decisions` leaf: `/meta/decisions`,
    `POST /meta/decisions/simulate`, `POST /meta/decisions/canary/run`,
    `POST /meta/decisions/canary/promote` (409 on stale version),
    `GET /meta/decisions/explanations[_/{decision_id}]`.
- **Meta wiring**: `decision_intelligence` subservice in `/meta/ecosystem`;
  six new features keys; `decision_intelligence` scoring-catalog key with all
  four catalogs.
- **Additive risk-evaluator surfaces** (folded under the existing `evaluate`
  leaf): `score_with_config` (pure config-parameterized scoring) and
  `effective_risk_rules` (config + learned weights snapshot) — live scoring
  behavior untouched.
- **Tests**: `tests/test_decision_quality_expansion.py` — suite grew
  427 → 455 passing.

### r2 (2026-09-25)

Backend evolution pass — multi-tenant routing + partition lifecycle (1A),
zero-trust crypto foundations (1B), high-velocity async audit pipeline with
immutable protobuf transactions (1C). Tree grew 174/32 → **230 leaves /
44 internal nodes**; no r1 leaf was lost.

- **New infra groups** (all under `infra`, opt-in via env flags, single-tenant
  defaults unchanged):
  - `multi_tenant`: `tenant_router.py` (`TenantRouter` — runtime
    register/deregister, DSN template/declarations, per-tenant sessions,
    catalog) + `partition_manager.py` (`PARTITION_POLICIES` config,
    ensure/archive/drop-expired, `run_partition_cycle_forever` worker,
    `CSERVICE_PARTITION_WORKER`).
  - `zero_trust`: `hsm_signer.py` (`HsmSigner` protocol; hmac default,
    ed25519/mock backends; sign/verify, envelopes, catalog),
    `biometric_vault.py` (salted-digest template vault, similarity matching),
    `risk_evaluator.py` (`RISK_RULES` config, levels, adaptive weights),
    `cell_matrix.py` (`CELL_MATRIX` config, role groups, cell access).
  - `event_pipeline`: `high_throughput_pipeline.py` (bounded queue, workers,
    batch, drain, dead-letter, stats; `CSERVICE_PIPELINE_AUTOSTART`) +
    `protobuf_transaction_spec.py` (self-contained protobuf wire encoder —
    varint + length-delimited, no protoc; SHA-256 prev_hash frame chain,
    append-only transaction log, spec catalog).
  - `main` gained `tenants`, `partitions`, `zero_trust` leaves (new meta
    endpoints `/meta/tenants`, `/meta/partitions`, `/meta/zero-trust`).
- **New models group**: `model_bases.py` (`TimestampMixin`,
  `TenantScopedMixin`, `PartitionedMixin`, polymorphic `SecurityEvent`
  family) — isolated from the flat `models.py` (which now derives from it and
  re-exports); existing tables untouched (no alembic risk, prior Base-naming
  decision kept).
- **Audit router expansion**: `pipeline_stats`, `pipeline_event`,
  `transactions`, `protobuf_spec` leaves — `POST /audit/pipeline/event`
  (202, async — events queue until workers or `drain()` flush),
  `GET /audit/pipeline/stats`, `GET /audit/transactions`,
  `GET /audit/transactions/spec`.
- **Meta wiring**: `tenant_routing`, `partition_management`, `zero_trust`,
  `high_velocity_audit` subservices in `/meta/ecosystem`; features keys +
  scoring-catalog keys for all four.
- **Code map tooling**: builder now lives in-repo at
  `scripts/build_code_map.py` (source of truth; regenerates + validates
  `CODE_MAP.newick`, prints leaf/internal counts).
- **Tests**: `tests/test_tenant_router_expansion.py` (14),
  `tests/test_zero_trust_expansion.py` (17),
  `tests/test_high_velocity_audit_expansion.py` (18) — suite grew
  378 → 427 passing.

### r1 (2026-09-25)

Initial revision capturing the full backend logic after the small-component
expansion pass:

- **Expanded infra components** (previously the smallest in the repo):
  - `security.py` (25 → 121 LOC): access vs refresh token types, decode +
    validate helper, configurable password-strength policy.
  - `db.py` (35 → 103 LOC): env-tunable pool settings, non-raising
    `ping_database`, `retry_async` for idempotent recovery paths.
  - `i18n.py` (42 → 152 LOC): French locale (requirements specify en/es/fr),
    message catalog, `translate()` with fallback, `build_i18n_catalog()`.
  - `deps.py` (78 → 95 LOC): decode via `security.decode_access_token`,
    new `get_current_user_optional`.
- **New audit-trail domain** (smallest router `routers/audit.py` 43 LOC was
  the least mature public surface):
  - `models.AuditLogEntry` (persisted, severity-constrained).
  - `services/audit_log.py`: record / list / rollup / action catalog.
  - Routes: `POST /audit/log`, `GET /audit/logs`, `GET /audit/logs/summary`,
    `GET /audit/trail-catalog`.
  - `main.py`: `audit_trail` + `localization` subservices in
    `/meta/ecosystem`, new endpoints in `/meta/features`, and `audit_log` /
    `i18n` / `password_policy` catalogs in `/meta/scoring-catalog`,
    plus `GET /meta/i18n`.
- **Tests**: `tests/test_security_infra_expansion.py` (13),
  `tests/test_audit_log_expansion.py` (14) — suite grew 351 → 378 passing.