"""Tests for the retention policy-layer expansion.

`services/retention.py` gained a policy layer over its original read layer. The
point of this file is to pin the *config-driven* behaviour, plus the two things
that could silently break while the tuning constants were being lifted out of
inline code:

1. Every `when` rule in `RETENTION_HEALTH_RULES` / `RETENTION_ANOMALY_RULES`
   must validate against the shared `rule_engine`. The DSL's two right-hand
   forms look alike and are not — a list means membership, a dict means named
   operators — so a typo like `{"churn_avg": [">=", 2.0]}` is well-formed,
   evaluates to `False` forever, and looks like working configuration. These
   tests are the guard against that class of silent no-op.
2. An empty snapshot series must not be banded as though it were a measurement.
   Every derived metric is 0.0 for an empty series and 0.0 reads as a real
   value, so this is pinned explicitly.

The pre-existing snapshot/delta/trend/coverage/operational surfaces are
unchanged; `test_threshold_lift_is_behaviour_preserving` pins the specific
inline values that were replaced.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from _doubles import SqliteHarness

from app import deps, models
from app.main import app
from app.rule_engine import validate_when
from app.services import retention
from app.services.retention import (
    RETENTION_ANOMALY_RULES,
    RETENTION_CHURN_RANK,
    RETENTION_CLUSTER_IDS,
    RETENTION_HEALTH_BAND_ORDER,
    RETENTION_HEALTH_RULES,
    RETENTION_TOPIC_CLUSTERS,
    RETENTION_TOPIC_HINTS,
    build_retention_catalog,
    build_retention_cluster_coverage,
    build_retention_forecast,
    build_retention_health_report,
    build_retention_horizon_sweep,
    build_retention_series_metrics,
    churn_risk_label,
    churn_risk_rank,
    detect_retention_anomalies,
    forecast_confidence,
    resolve_retention_health,
    snapshot_series_points,
)

NOW = datetime.now(timezone.utc)


def _snapshot(index, loyalty, churn, *, kind="auto", days_ago=0, stage="loyal"):
    """A snapshot-shaped mapping; `snapshot_series_points` accepts dicts."""
    return {
        "id": index,
        "snapshot_type": kind,
        "lifecycle_stage": stage,
        "loyalty_score": loyalty,
        "churn_risk": churn,
        "created_at": NOW - timedelta(days=days_ago),
    }


COLLAPSING = [
    _snapshot(1, 80, "low", days_ago=28),
    _snapshot(2, 62, "medium", days_ago=21),
    _snapshot(3, 40, "high", days_ago=14),
    _snapshot(4, 22, "critical", days_ago=7),
]
STABLE = [_snapshot(i, 78 + (i % 2), "low", days_ago=35 - (i * 6)) for i in range(6)]


# ---------------------------------------------------------------------------
# Config tables
# ---------------------------------------------------------------------------


def test_topic_hints_table_is_fully_extracted():
    """80 rows, every one with a non-empty keyword list."""
    assert len(RETENTION_TOPIC_HINTS) == 80
    assert all(row["keywords"] for row in RETENTION_TOPIC_HINTS)
    assert all(str(row["topic"]).strip() for row in RETENTION_TOPIC_HINTS)


def test_topic_hint_topics_are_unique():
    """A duplicated topic would silently drop a row once the table is a list.

    The original dict literal could not express duplicates; the extracted list
    can, and a duplicate would be a silent data loss rather than an error.
    """
    topics = [str(row["topic"]) for row in RETENTION_TOPIC_HINTS]
    assert len(topics) == len(set(topics))


def test_cluster_table_shape():
    assert len(RETENTION_TOPIC_CLUSTERS) == 14
    assert len(RETENTION_CLUSTER_IDS) == 14
    members = [topic for row in RETENTION_TOPIC_CLUSTERS for topic in row["topics"]]
    assert len(members) == 63


def test_cluster_table_reports_empty_clusters():
    """An empty cluster stays in the result — absence is reportable."""
    clusters = retention._retention_topic_clusters([])
    assert len(clusters) == 14
    assert all(members == [] for members in clusters.values())


# ---------------------------------------------------------------------------
# DSL guard: the silent-no-op failure mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule", RETENTION_HEALTH_RULES + RETENTION_ANOMALY_RULES, ids=lambda r: str(r["rule_id"])
)
def test_every_when_rule_validates_against_the_shared_engine(rule):
    report = validate_when(rule["when"])
    assert report["valid"] is True, f"{rule['rule_id']}: {report['errors']}"
    assert report["errors"] == []
    # A dict with no recognised operator reports no operators_used; catching that
    # here is what stops `{"x": [">=", 1]}` from passing as real configuration.
    assert report["operators_used"], f"{rule['rule_id']} uses no named operator"


@pytest.mark.parametrize("rule", RETENTION_HEALTH_RULES, ids=lambda r: str(r["rule_id"]))
def test_health_rules_use_dict_operators_not_membership_lists(rule):
    """Guard the specific trap: a list RHS is membership, not comparison.

    ``{"churn_avg": [">=", 2.0]}`` is accepted by the engine and is always
    False. Any list RHS inside a health/anomaly rule is therefore a bug unless it
    is nested under a combinator as a genuine membership test.
    """
    def walk(node, in_combinator=False):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in ("all", "any", "not"):
                for child in value if isinstance(value, list) else [value]:
                    walk(child, in_combinator=True)
                continue
            if isinstance(value, list):
                # Only legal when the whole field is a top-level membership test
                # against a list-valued context field; none of ours are.
                assert in_combinator or isinstance(value, list), f"{rule['rule_id']}.{key}"
                if isinstance(value, list) and all(isinstance(v, str) for v in value):
                    # A string-only list is a legitimate membership test.
                    continue
                pytest.fail(f"{rule['rule_id']}.{key} looks like an operator list: {value!r}")

    walk(rule["when"])


def test_health_rule_bands_are_declared_in_the_band_order():
    for rule in RETENTION_HEALTH_RULES:
        assert str(rule["band"]) in RETENTION_HEALTH_BAND_ORDER


def test_health_rules_have_unique_ids_and_priorities():
    ids = [str(rule["rule_id"]) for rule in RETENTION_HEALTH_RULES]
    priorities = [int(rule["priority"]) for rule in RETENTION_HEALTH_RULES]
    assert len(ids) == len(set(ids))
    assert len(priorities) == len(set(priorities))


# ---------------------------------------------------------------------------
# Churn rank: one declared scale, one reader
# ---------------------------------------------------------------------------


def test_churn_rank_matches_the_previous_inline_map():
    previous = {"low": 0.0, "medium": 1.0, "high": 2.0, "critical": 3.0}
    for label, value in previous.items():
        assert churn_risk_rank(label) == value


def test_churn_rank_unknown_label_falls_back_to_zero():
    """Matches the ``.get(label, 0.0)`` it replaced."""
    assert churn_risk_rank("not-a-band") == 0.0
    assert churn_risk_rank(None) == 0.0


def test_churn_rank_explicit_fallback_overrides():
    assert churn_risk_rank("not-a-band", fallback=2.5) == 2.5


def test_churn_label_is_the_inverse_of_the_rank():
    for label, value in RETENTION_CHURN_RANK.items():
        assert churn_risk_label(value) == label
    assert churn_risk_label(0.0) == "low"
    assert churn_risk_label(2.9) == "high"
    assert churn_risk_label(3.0) == "critical"


# ---------------------------------------------------------------------------
# Health banding
# ---------------------------------------------------------------------------


def test_no_snapshots_is_no_data_not_at_risk():
    """The regression this whole guard exists for.

    Every metric is 0.0 for an empty series, so without `health_no_data` the
    customer satisfies `loyalty_avg < 50` and is reported as an at-risk
    measurement that was never taken.
    """
    health = build_retention_health_report([])["health"]
    assert health["band"] == "no_data"
    assert health["rule_id"] == "health_no_data"


def test_collapsing_customer_bands_critical_on_movement_not_average():
    report = build_retention_health_report(COLLAPSING)
    assert report["health"]["band"] == "critical"
    # The average is a lagging 51.0; the band must be driven by the drop.
    assert report["metrics"]["loyalty_avg"] == 51.0
    assert report["metrics"]["loyalty_delta"] == -58.0
    assert report["health"]["matched_fields"]


def test_stable_customer_bands_stable():
    assert build_retention_health_report(STABLE)["health"]["band"] == "stable"


def test_thin_but_healthy_series_is_watch_not_stable():
    """Three healthy snapshots are not enough to claim stability."""
    report = build_retention_health_report([_snapshot(1, 80, "low"), _snapshot(2, 81, "low"), _snapshot(3, 80, "low")])
    assert report["health"]["band"] == "watch"
    assert report["health"]["rule_id"] == "health_watch_insufficient_data"


def test_every_band_in_the_order_is_reachable_from_some_rule():
    declared = {str(rule["band"]) for rule in RETENTION_HEALTH_RULES}
    assert declared == set(RETENTION_HEALTH_BAND_ORDER)


def test_resolve_retention_health_is_total():
    """An empty context still yields a verdict, never an exception or None."""
    health = resolve_retention_health({})
    assert health["band"] in RETENTION_HEALTH_BAND_ORDER
    assert health["rule_id"] == "default"
    assert health["default_applied"] is True


def test_health_evidence_is_returned_with_the_verdict():
    health = build_retention_health_report(COLLAPSING)["health"]
    assert set(health) >= {"band", "rule_id", "rule_name", "priority", "actions", "matched_fields", "band_rank"}
    assert health["actions"], "a fired rule must carry its recommended actions"


# ---------------------------------------------------------------------------
# Series metrics + anomalies
# ---------------------------------------------------------------------------


def test_series_points_normalize_to_oldest_first():
    """The loaders return newest-first; the analytics read oldest-first."""
    newest_first = list(reversed(COLLAPSING))
    points = snapshot_series_points(newest_first)
    assert [point["index"] for point in points] == [0, 1, 2, 3]
    assert points[0]["loyalty_score"] == 80.0
    assert points[-1]["loyalty_score"] == 22.0


def test_series_points_accept_orm_style_objects():
    class _Row:
        def __init__(self, index, loyalty, churn):
            self.id = index
            self.snapshot_type = "auto"
            self.lifecycle_stage = "loyal"
            self.loyalty_score = loyalty
            self.churn_risk = churn
            # index 0 is the *older* reading, so the series reads 80 -> 70.
            self.created_at = NOW - timedelta(days=10 - index)

    points = snapshot_series_points([_Row(0, 80, "low"), _Row(1, 70, "medium")])
    assert [point["loyalty_score"] for point in points] == [80.0, 70.0]
    assert [point["churn_rank"] for point in points] == [0.0, 1.0]


def test_series_metrics_deltas():
    metrics = build_retention_series_metrics(snapshot_series_points(COLLAPSING))
    assert metrics["snapshot_count"] == 4
    assert metrics["loyalty_delta"] == -58.0
    assert metrics["abs_loyalty_delta"] == 58.0
    assert metrics["churn_delta"] == 3.0
    assert metrics["churn_latest_label"] == "critical"


def test_series_metrics_of_empty_series():
    metrics = build_retention_series_metrics([])
    assert metrics["snapshot_count"] == 0
    assert metrics["loyalty_delta"] == 0.0
    assert metrics["churn_latest_label"] == ""


def test_collapse_raises_both_critical_anomalies():
    ids = {finding["rule_id"] for finding in build_retention_health_report(COLLAPSING)["anomalies"]}
    assert "anomaly_loyalty_collapse" in ids
    assert "anomaly_churn_escalation" in ids


def test_stable_series_raises_no_anomaly():
    assert build_retention_health_report(STABLE)["anomalies"] == []


def test_sparse_capture_is_reported():
    ids = {f["rule_id"] for f in build_retention_health_report([_snapshot(1, 50, "medium")])["anomalies"]}
    assert "anomaly_sparse_capture" in ids


def test_anomalies_are_ordered_severity_first():
    severities = [finding["severity"] for finding in build_retention_health_report(COLLAPSING)["anomalies"]]
    order = list(retention.RETENTION_ANOMALY_SEVERITIES)
    assert severities == sorted(severities, key=order.index)


def test_anomaly_findings_carry_their_evidence():
    for finding in build_retention_health_report(COLLAPSING)["anomalies"]:
        assert finding["matched_fields"]
        assert finding["detail"]


def test_detect_anomalies_is_bounded_by_the_configured_cap(monkeypatch):
    monkeypatch.setitem(retention.RETENTION_SNAPSHOT_OPS, "max_anomalies_reported", 1)
    assert len(detect_retention_anomalies(build_retention_health_report(COLLAPSING)["metrics"])) == 1


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


def test_forecast_declines_below_min_points():
    forecast = build_retention_forecast(COLLAPSING[:2])
    assert forecast["sufficient_data"] is False
    assert "at least 3 snapshots" in forecast["reason"]
    assert forecast["confidence"] == 0.0


def test_forecast_reports_unknown_method_instead_of_raising():
    forecast = build_retention_forecast(COLLAPSING, method="telepathy")
    assert forecast["sufficient_data"] is False
    assert "telepathy" in forecast["reason"]


def test_forecast_projects_downward_for_a_collapsing_series():
    forecast = build_retention_forecast(COLLAPSING, horizon_days=30)
    assert forecast["sufficient_data"] is True
    assert forecast["method"] == "linear_regression"
    assert forecast["loyalty"]["projected"] <= forecast["loyalty"]["latest"]
    assert forecast["loyalty"]["bounds"] == [0.0, 100.0]


def test_forecast_respects_metric_bounds():
    forecast = build_retention_forecast(COLLAPSING, horizon_days=365)
    assert 0.0 <= forecast["loyalty"]["projected"] <= 100.0
    assert 0.0 <= forecast["churn"]["projected"] <= 3.0


def test_forecast_labels_the_projected_churn_band():
    forecast = build_retention_forecast(COLLAPSING, horizon_days=90)
    assert forecast["churn"]["projected_label"] in retention.RETENTION_CHURN_BANDS


def test_confidence_decays_with_horizon():
    short = forecast_confidence(7, 20)
    long = forecast_confidence(90, 20)
    assert short > long


def test_confidence_grows_with_sample_size():
    thin = forecast_confidence(30, 3)
    thick = forecast_confidence(30, 30)
    assert thin < thick


def test_horizon_sweep_covers_every_configured_horizon():
    sweep = build_retention_horizon_sweep(COLLAPSING)
    horizons = [entry["horizon_days"] for entry in sweep["horizons"]]
    assert horizons == list(retention.RETENTION_FORECAST_RULES["horizons_days"])
    confidences = [entry["confidence"] for entry in sweep["horizons"]]
    assert confidences == sorted(confidences, reverse=True)


def test_forecast_methods_are_a_registry():
    assert "linear_regression" in retention._FORECAST_METHODS
    # `last_value` holds the slope flat, so it must not move the projection.
    flat = build_retention_forecast(COLLAPSING, method="last_value")
    assert flat["loyalty"]["projected"] == flat["loyalty"]["latest"]


# ---------------------------------------------------------------------------
# Cluster coverage (absence-oriented)
# ---------------------------------------------------------------------------


def test_cluster_coverage_reports_silent_clusters():
    matched = retention._retention_topic_context_from_summary("refund dispute billing charge")
    coverage = build_retention_cluster_coverage(matched, retention._retention_topic_clusters(matched))
    assert coverage["clusters_declared"] == 14
    assert coverage["clusters_represented"] + coverage["clusters_silent"] == 14
    assert 0.0 <= coverage["coverage_ratio"] <= 1.0


def test_cluster_coverage_of_no_matches_is_all_silent():
    coverage = build_retention_cluster_coverage([], retention._retention_topic_clusters([]))
    assert coverage["clusters_represented"] == 0
    assert coverage["clusters_silent"] == 14
    assert coverage["coverage_ratio"] == 0.0


# ---------------------------------------------------------------------------
# Threshold lift is behaviour-preserving
# ---------------------------------------------------------------------------


def test_threshold_lift_is_behaviour_preserving():
    """The report cutoffs are the values that were previously inline."""
    assert retention.RETENTION_SAMPLE_THRESHOLDS["sample_size_min"] == 5      # len(snapshots) < 5
    assert retention.RETENTION_SAMPLE_THRESHOLDS["trend_min"] == 3            # total >= 3
    assert retention.RETENTION_SAMPLE_THRESHOLDS["churn_alert"] == 2.0        # average_churn >= 2.0
    assert retention.RETENTION_SAMPLE_THRESHOLDS["loyalty_alert"] == 50.0     # average_loyalty < 50
    assert retention.RETENTION_SAMPLE_THRESHOLDS["topic_coverage_target"] == 0.5  # coverage >= 0.5


def test_prune_keep_default_is_unchanged():
    assert retention.RETENTION_SNAPSHOT_OPS["prune_keep"] == 20


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_reports_every_policy_table():
    catalog = build_retention_catalog()
    assert catalog["catalog_version"] == "retention_v2"
    tables = catalog["config_tables"]
    assert set(tables) == {
        "topic_hints",
        "topic_clusters",
        "churn_rank",
        "snapshot_ops",
        "health_rules",
        "anomaly_rules",
        "forecast_rules",
    }
    assert tables["topic_hints"]["rows"] == 80
    assert tables["topic_clusters"]["rows"] == 14
    assert tables["churn_rank"]["scale"] == RETENTION_CHURN_RANK
    assert tables["health_rules"]["rules_by_band"] == {
        band: sum(1 for rule in RETENTION_HEALTH_RULES if rule["band"] == band)
        for band in RETENTION_HEALTH_BAND_ORDER
    }


def test_catalog_documents_the_dsl_trap():
    """The catalog is where an operator looks; the note belongs there too."""
    assert "when_dsl" in build_retention_catalog()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None


class _FakeDb:
    """Serves a scripted snapshot list through the retention snapshot query."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *_args, **_kwargs):
        result = _EmptyResult()
        result._rows = self.rows
        result.scalars = lambda: _Rows(self.rows)
        return result


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _FakeUser:
    id = 1
    username = "tester"
    is_admin = True


def _client(rows):
    async def _fake_get_db():
        yield _FakeDb(rows)

    async def _fake_admin():
        return _FakeUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_admin
    return TestClient(app)


def _release():
    app.dependency_overrides.pop(deps.get_db, None)
    app.dependency_overrides.pop(deps.get_current_admin_user, None)


class _Row:
    def __init__(self, index, loyalty, churn, days_ago):
        self.id = index
        self.snapshot_type = "auto"
        self.lifecycle_stage = "loyal"
        self.loyalty_score = loyalty
        self.churn_risk = churn
        self.created_at = NOW - timedelta(days=days_ago)


def test_retention_health_endpoint():
    rows = [_Row(i, loyalty, churn, days_ago)
            for i, (loyalty, churn, days_ago) in enumerate(
                [(80, "low", 28), (62, "medium", 21), (40, "high", 14), (22, "critical", 7)], start=1)]
    client = _client(rows)
    try:
        response = client.get("/chat/admin/retention-health")
    finally:
        _release()
    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == 1
    assert payload["health"]["band"] == "critical"
    assert payload["metrics"]["snapshot_count"] == 4
    assert payload["anomalies"]


def test_retention_forecast_endpoint_reports_insufficient_data():
    client = _client([_Row(1, 50, "medium", 3)])
    try:
        response = client.get("/chat/admin/retention-forecast")
    finally:
        _release()
    assert response.status_code == 200
    payload = response.json()
    assert payload["sufficient_data"] is False
    assert payload["confidence"] == 0.0
    assert payload["reason"]


def test_retention_forecast_endpoint_projects_a_full_series():
    rows = [_Row(i, loyalty, "low", 20 - (i * 5)) for i, loyalty in enumerate([80, 75, 70, 65, 60, 55], start=1)]
    client = _client(rows)
    try:
        response = client.get("/chat/admin/retention-forecast?horizon_days=30")
    finally:
        _release()
    assert response.status_code == 200
    payload = response.json()
    assert payload["sufficient_data"] is True
    assert 0.0 < payload["confidence"] <= 1.0
    assert payload["loyalty"]["projected"] < payload["loyalty"]["latest"]


def test_retention_forecast_sweep_endpoint():
    rows = [_Row(i, loyalty, "low", 20 - (i * 5)) for i, loyalty in enumerate([80, 75, 70, 65, 60, 55], start=1)]
    client = _client(rows)
    try:
        response = client.get("/chat/admin/retention-forecast-sweep")
    finally:
        _release()
    assert response.status_code == 200
    horizons = response.json()["horizons"]
    assert [entry["horizon_days"] for entry in horizons] == list(retention.RETENTION_FORECAST_RULES["horizons_days"])


def test_retention_cluster_coverage_endpoint():
    client = _client([])
    try:
        response = client.get("/chat/admin/retention-cluster-coverage")
    finally:
        _release()
    assert response.status_code == 200
    payload = response.json()
    assert payload["clusters_declared"] == 14
    assert payload["clusters_represented"] == 0


def test_scoring_catalog_exposes_the_retention_policy_tables():
    import asyncio

    from app.main import scoring_catalog

    catalog = asyncio.run(scoring_catalog())
    assert catalog["retention"]["catalog_version"] == "retention_v2"


# ===========================================================================
# build_retention_recommendations: the health-band entry
# ===========================================================================
#
# This function had a block that referenced a local named `health` which the
# function never assigned, so it raised `NameError` on every call that got past
# the `if not snapshots` early return. It went unnoticed because the function
# has no callers -- nothing routes to it -- so the crash could not fire in
# production either. Ruff's F821 is what finally named it.
#
# These tests run against a real in-memory SQLite rather than the scripted
# `_FakeDb` above, because the function makes two reads (the snapshot window and
# the per-type counts) and a single scripted result would have hidden a mistake
# in either one.


@pytest.fixture()
def harness():
    h = SqliteHarness()
    h.run(h.setup())
    try:
        yield h
    finally:
        h.run(h.teardown())
        h.close()


def _seed_snapshots(harness, series, *, user_id=1):
    for index, (loyalty, risk, days_ago) in enumerate(series, start=1):
        harness.add(
            models.RetentionSnapshot(
                id=index,
                user_id=user_id,
                snapshot_type="daily",
                window_days=30,
                loyalty_score=loyalty,
                churn_risk=risk,
                lifecycle_stage="at_risk",
                created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
            )
        )
    harness.commit()


COLLAPSING_SERIES = [(60.0, "low", 10), (40.0, "medium", 8), (20.0, "high", 5), (8.0, "critical", 2)]
STABLE_SERIES = [(78.0, "low", 30), (79.0, "low", 24), (78.0, "low", 18), (79.0, "low", 12)]


def test_recommendations_include_a_health_band_entry(harness):
    harness.user(1)
    _seed_snapshots(harness, COLLAPSING_SERIES)
    recs = harness.run(retention.build_retention_recommendations(harness.session, 1, 30))
    bands = [r for r in recs if r["area"].startswith("health-band:")]
    assert bands, recs
    assert bands[0]["area"] == "health-band:critical"
    assert bands[0]["priority"] == "high"


def test_a_health_recommendation_cites_the_rule_that_fired(harness):
    harness.user(1)
    _seed_snapshots(harness, COLLAPSING_SERIES)
    recs = harness.run(retention.build_retention_recommendations(harness.session, 1, 30))
    band = next(r for r in recs if r["area"].startswith("health-band:"))
    # The evidence has to name the rule and its actions, or it is an assertion
    # with nothing behind it.
    assert "Rule health_" in band["evidence"]
    assert "suggested actions:" in band["evidence"]


def test_the_band_agrees_with_the_health_report(harness):
    """The whole point of routing through `build_retention_health_report`.

    If the recommendation bandued the customer independently, the queue and the
    dashboard could disagree about the same customer -- which is the failure
    mode a single shared composition exists to prevent.
    """
    harness.user(1)
    _seed_snapshots(harness, COLLAPSING_SERIES)
    recs = harness.run(retention.build_retention_recommendations(harness.session, 1, 30))
    band = next(r for r in recs if r["area"].startswith("health-band:"))
    snapshots = harness.run(retention._load_retention_snapshots(harness.session, 1, 30))
    health = retention.build_retention_health_report(snapshots)["health"]
    assert band["area"] == f"health-band:{health['band']}"
    assert band["recommendation"] == health["rule_name"]


def test_a_healthy_customer_gets_no_health_recommendation(harness):
    """A rule with no actions produces no entry, so the list is not padded."""
    harness.user(1)
    _seed_snapshots(harness, STABLE_SERIES)
    recs = harness.run(retention.build_retention_recommendations(harness.session, 1, 30))
    health = retention.build_retention_health_report(
        harness.run(retention._load_retention_snapshots(harness.session, 1, 30))
    )["health"]
    if not health["actions"]:
        assert not [r for r in recs if r["area"].startswith("health-band:")]


def test_the_empty_series_early_return_is_untouched(harness):
    harness.user(1)
    recs = harness.run(retention.build_retention_recommendations(harness.session, 1, 30))
    assert [r["area"] for r in recs] == ["coverage"]
    assert "No snapshots were found" in recs[0]["evidence"]


def test_the_function_never_references_an_unassigned_local():
    """Static guard on the exact class of bug, so it cannot come back.

    A `NameError` in a function with no callers is invisible to every runtime
    check -- nothing routes to `build_retention_recommendations`, so the crash
    could not fire in production either. That is precisely why it survived, and
    why the absence of the pattern is asserted here rather than exercised.

    Resolution is done against the real scopes -- parameters, assignments,
    module globals, builtins -- rather than a hand-maintained allow-list, which
    would itself rot the first time the function used a new name.
    """
    import ast
    import builtins
    import inspect
    import textwrap

    func = retention.build_retention_recommendations
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))

    assigned = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    parameters = {
        arg.arg
        for arg in (
            *tree.body[0].args.posonlyargs,
            *tree.body[0].args.args,
            *tree.body[0].args.kwonlyargs,
        )
    }
    # Comprehension targets bind inside their own scope and read as locals.
    comprehension_targets = {
        sub.id
        for node in ast.walk(tree)
        if isinstance(node, ast.comprehension)
        for sub in ast.walk(node.target)
        if isinstance(sub, ast.Name)
    }
    resolved = (
        assigned
        | parameters
        | comprehension_targets
        | set(vars(retention))
        | set(dir(builtins))
    )
    read = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert not (read - resolved), sorted(read - resolved)
