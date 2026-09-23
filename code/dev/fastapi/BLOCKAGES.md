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

### Dynamic rule engines (uncommitted until this batch)
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

## Open blockages
- None.