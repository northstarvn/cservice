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

## Open blockages
- None.