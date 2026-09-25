# CService Backend — Code Map (Newick) Governance

Companion to [`CODE_MAP.newick`](CODE_MAP.newick). That file is a single-line,
strictly well-formed Newick tree that reflects the **entire backend logic** at
business-surface granularity. This document explains how to read it and how to
keep it in sync as the backend evolves.

## Revision Control

- Revision ID: `r2`
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
| `infra` | startup/metadata (`main`), database plumbing (`db`), auth primitives (`security`), auth dependencies (`auth_deps`), localization, multi-tenant routing + partition lifecycle (`multi_tenant`), zero-trust crypto (`zero_trust`), event pipeline (`event_pipeline`) |
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

Tree size: **230 leaves / 44 internal nodes** (validated, see maintenance
rule 4). Rendered multi-line for readability; the single-line Newick file is
the source of truth.

```
(((startup_lifespan,cors,exception_handler,meta,health,runtime,scoring_catalog,i18n_endpoint,
    features,ecosystem,tenants,partitions,zero_trust)main,
  (engine,pool_config,session,ping,retry)db,
  (verify_password,hash_password,access_token,refresh_token,decode_token,password_policy)security,
  (current_user,optional_user,admin_user,policy_or_admin,policy_score,control_posture)auth_deps,
  (locale_resolution,locale_payload,message_catalog,translate,catalog_builder)localization,
  ((registry,route_for_tenant,tenant_session,declared_tenants,catalog)tenant_router,
   (policies,ensure_partition,archive,drop_expired,worker,catalog)partition_manager)multi_tenant,
  ((hmac_signer,ed25519_signer,sign_bytes,verify_bytes,signature_envelope,catalog)hsm_signer,
   (modalities,register,validate,revoke,list_templates,catalog)biometric_vault,
   (risk_levels,risk_rules,evaluate,adaptive_weights,catalog)risk_evaluator,
   (access_levels,cell_matrix,evaluate,resolve_roles,catalog)cell_matrix)zero_trust,
  ((workers,batch,submit,drain,dead_letter,stats,catalog)high_throughput_pipeline,
   (wire_format,transaction,hash_chain,append_only_log,spec_catalog)protobuf_transaction_spec)event_pipeline)infra,
 ((timestamp,tenant_scoped,partitioned,security_event)model_bases,user,booking,booking_event,
  booking_assignment,chat_history,interaction_signal,retention_snapshot,recovery_outcome,
  customer_policy_score,topic_selection,communication_override,arrears_entry,points_wallet,
  points_transaction,audit_log_entry)models,
 ((register,login,me,policy_score,can_access,policy_decision,change_password)users,
  (lifecycle_crud,analytics,assignment_report,audit_history,export)bookings,
  (history,sentiment,insights,recovery,loyalty_journey,retention_dashboard,snapshot_operations,
   activity_tree,communication_strategy,arrears_payments,points_exchange,topic_policy)chat,
  (catalog,themes,intelligence,coverage,portfolio,workspace,overview,search,suggestions,
   recommendations,current_selection,history)topics,
  (efficiency,enhancements,audit_catalog,trail_catalog,log,logs,summary,pipeline_stats,
   pipeline_event,transactions,protobuf_spec)audit)routers,
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
  (interest_policies,quote,open,settle,waive_interest)arrears_payments,
  (rate_rules,quote,wallet,ledger,admin_rollup)points_exchange,
  (record,list,summary,action_catalog)audit_log)services,
 ((user,token,booking,policy_score,topic_selection)schemas_core,
  (insight,recovery,retention,snapshot_ops,topic_ranking,topic_policy)schemas_chat,
  (efficiency,enhancements,audit_trail)schemas_audit)schemas)cservice_backend;
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