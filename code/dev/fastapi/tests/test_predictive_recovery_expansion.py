"""Tests for Predictive Sentiment & Automated Service Recovery.

This slice automates proactive retention on top of the existing sentiment /
recovery surfaces:

- Realtime dissatisfaction indicators: live interaction window -> context
  (readiness, score, sentiment, churn/value tier, risk areas, peak intensity).
- Config-driven recovery playbooks matched by the shared when-DSL engine
  (``app.rule_engine.evaluate_when``) with ``=formula`` action params.
- Actions: auto-issue goodwill credit points (wallet + ``recovery_credit``
  ledger rows), record ticket escalations (generated ticket reference), and
  apply policy-score goodwill guardrails (recorded adjustment + preview).
- Orchestrator audit trail in ``models.RecoveryAction`` rows; dry-run mode
  writes nothing; the optional background sweep is env-gated
  (``CSERVICE_AUTO_RECOVERY``, default off) so default behavior is unchanged.

Constraints honored: additive table (``recovery_actions``), no changes to the
pinned points/wallet/ledger kinds or the recovery dashboard contracts, and no
DB-backed tests run outside the fake-DB harness.
"""
import os
import sys
import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import models  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.chat import (  # noqa: E402
    DissatisfactionRecoveryReport,
    InteractionSummary,
    RecoverySignal,
    Sentiment,
)
from app.services import policy_scoring, recovery_playbooks  # noqa: E402

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _summary(**overrides):
    defaults = dict(
        user_id=1,
        messages_analyzed=5,
        bookings_analyzed=1,
        churn_risk="high",
        loyalty_score=40.0,
        monetization_readiness=92.0,
        value_tier="premium",
        customer_classification="loyal high-value",
        top_issues=["support"],
        strengths=[],
        insights=[],
        metadata={"repeated_messages": 2, "booking_states": {"cancelled": 1}},
        generated_at=NOW,
    )
    defaults.update(overrides)
    return InteractionSummary(**defaults)


def _dissatisfaction(score=32.0, readiness="critical", signals=None):
    return DissatisfactionRecoveryReport(
        generated_at=NOW,
        window_days=30,
        dissatisfaction_score=score,
        recovery_readiness=readiness,
        primary_risks=["negative sentiment"],
        recovery_signals=signals
        or [
            RecoverySignal(
                area="support",
                intensity=4.2,
                evidence=["e"],
                evidence_summary="evidence summary",
                recommended_action="fast follow-up",
            )
        ],
        action_plan="plan",
    )


def _sentiment(label="negative", score=0.92):
    return Sentiment(label=label, score=score)


class _Empty:
    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _FakeDb:
    """Async session double: table-shaped execute routing + add/flush/commit."""

    def __init__(self, wallets=(), chat_rows=(), bookings=(), users=()):
        self.wallets = list(wallets)
        self.chat_rows = list(chat_rows)
        self.bookings = list(bookings)
        self.users = list(users)
        self.pending = []
        self.next_id = 1
        self.commits = 0

    async def execute(self, statement, *_args, **_kwargs):
        text = str(statement)
        if "points_wallets" in text:
            return _Rows(self.wallets)
        if "chat_history" in text:
            return _Rows(self.chat_rows)
        if "bookings" in text:
            return _Rows(self.bookings)
        if "FROM users" in text:
            return _Rows(self.users)
        return _Empty()

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self.next_id
            self.next_id += 1
        self.pending.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


def _inject_db_fake():
    return _FakeDb(
        wallets=[models.PointsWallet(id=5, user_id=1, point_type="loyalty_points", balance=100.0)]
    )


# ---------------------------------------------------------------------------
# Realtime dissatisfaction indicators (pure)
# ---------------------------------------------------------------------------


class TestRealtimeIndicators:
    def test_context_fields(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(), _sentiment(), _dissatisfaction()
        )
        assert context["recovery_readiness"] == "critical"
        assert context["dissatisfaction_score"] == 32.0
        assert context["sentiment_label"] == "negative"
        assert context["sentiment_score"] == 0.92
        assert context["churn_risk"] == "high"
        assert context["value_tier"] == "premium"
        assert context["primary_risks"] == ["negative sentiment"]
        assert context["risk_areas"] == ["support"]
        assert context["peak_intensity"] == 4.2
        assert context["repeated_messages"] == 2
        assert context["booking_states"] == {"cancelled": 1}

    def test_context_neutral_sentiment_defaults(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(), None, _dissatisfaction(score=4.0, readiness="low")
        )
        assert context["sentiment_label"] == "neutral"
        assert context["sentiment_score"] == 0.0
        assert context["recovery_readiness"] == "low"

    def test_indicators_payload_shape(self):
        payload = recovery_playbooks.compute_realtime_dissatisfaction_indicators(
            _summary(), _sentiment(), _dissatisfaction()
        )
        assert set(payload) == {
            "generated_at",
            "realtime_indicators",
            "auto_recovery_enabled",
            "playbook_matches",
        }
        assert payload["realtime_indicators"]["dissatisfaction_score"] == 32.0
        assert {match["playbook_id"] for match in payload["playbook_matches"]} == {
            "recovery_goodwill_points",
            "recovery_ticket_escalation",
            "recovery_policy_guardrail",
        }


# ---------------------------------------------------------------------------
# Playbook matching (when-DSL rules)
# ---------------------------------------------------------------------------


class TestPlaybookMatching:
    def test_critical_negative_premium_matches_all_three(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(), _sentiment(), _dissatisfaction()
        )
        matched = recovery_playbooks.evaluate_recovery_playbooks(context)
        assert [entry["playbook_id"] for entry in matched] == [
            "recovery_goodwill_points",
            "recovery_ticket_escalation",
            "recovery_policy_guardrail",
        ]

    def test_moderate_readiness_matches_nothing(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(), _sentiment(), _dissatisfaction(score=8.0, readiness="moderate")
        )
        assert recovery_playbooks.evaluate_recovery_playbooks(context) == []

    def test_high_neutral_growth_matches_guardrail_only(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(churn_risk="medium", value_tier="growth"),
            _sentiment(label="neutral", score=0.5),
            _dissatisfaction(score=24.0, readiness="high"),
        )
        matched = recovery_playbooks.evaluate_recovery_playbooks(context)
        assert [entry["playbook_id"] for entry in matched] == ["recovery_policy_guardrail"]

    def test_critical_negative_premium_low_churn_matches_points_only(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(churn_risk="low"),
            _sentiment(),
            _dissatisfaction(),
        )
        matched = recovery_playbooks.evaluate_recovery_playbooks(context)
        assert [entry["playbook_id"] for entry in matched] == ["recovery_goodwill_points"]

    def test_guardrail_requires_high_value_tier(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(value_tier="standard"),
            _sentiment(label="neutral", score=0.5),
            _dissatisfaction(score=24.0, readiness="high"),
        )
        assert recovery_playbooks.evaluate_recovery_playbooks(context) == []

    def test_formula_action_params_resolved(self):
        context = recovery_playbooks.build_realtime_recovery_context(
            _summary(), _sentiment(), _dissatisfaction()
        )
        playbook = recovery_playbooks.evaluate_recovery_playbooks(context)[0]
        params = recovery_playbooks.resolve_params(playbook["actions"][0]["params"], context)
        assert playbook["actions"][0]["action"] == "credit_points"
        assert params["point_type"] == "loyalty_points"
        # 25 + round(32.0 * 2.5) = 105, capped at 300.
        assert params["points"] == pytest.approx(105.0)
        assert params["reference"] == "recovery:goodwill"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestOrchestrator:
    def test_fulfills_credit_escalation_and_guardrail(self):
        db = _inject_db_fake()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
            )
        )
        assert [item["playbook_id"] for item in report["matched_playbooks"]] == [
            "recovery_goodwill_points",
            "recovery_ticket_escalation",
            "recovery_policy_guardrail",
        ]
        statuses = [(item["action"], item["status"]) for item in report["executed_actions"]]
        assert statuses == [
            ("credit_points", "executed"),
            ("escalate_ticket", "executed"),
            ("adjust_policy_score", "executed"),
        ]
        assert db.commits == 1

        # Wallet credited 105 points (25 + 32*2.5) on top of the seeded 100.
        wallet = db.wallets[0]
        assert round(wallet.balance, 2) == 205.0

        ledger = [row for row in db.pending if isinstance(row, models.PointsTransaction)]
        assert len(ledger) == 1
        assert ledger[0].kind == "recovery_credit"
        assert ledger[0].points_delta == 105.0
        assert ledger[0].reference == "recovery:goodwill"

        actions = [row for row in db.pending if isinstance(row, models.RecoveryAction)]
        assert len(actions) == 3
        by_action = {row.action: row for row in actions}
        assert by_action["escalate_ticket"].reference == "ESC-1-001"
        assert by_action["escalate_ticket"].status == "executed"
        assert '"ticket_reference": "ESC-1-001"' in by_action["escalate_ticket"].result_json
        guardrail = by_action["adjust_policy_score"]
        assert guardrail.playbook_id == "recovery_policy_guardrail"
        assert guardrail.status == "executed"

        # Report carries the escalation result + audit view.
        assert report["executed_actions"][1]["reference"] == "ESC-1-001"
        assert report["summary"].startswith("3 playbook(s) matched")

    def test_dry_run_writes_nothing(self):
        db = _inject_db_fake()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
                dry_run=True,
            )
        )
        assert all(item["status"] == "would_execute" for item in report["executed_actions"])
        assert db.commits == 0
        assert db.pending == []
        assert db.wallets[0].balance == 100.0

    def test_no_match_writes_nothing(self):
        db = _FakeDb()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(churn_risk="low", value_tier="standard"),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(score=8.0, readiness="moderate"),
            )
        )
        assert report["matched_playbooks"] == []
        assert report["executed_actions"] == []
        assert db.commits == 0
        assert db.pending == []

    def test_failed_action_is_recorded_not_raised(self, monkeypatch):
        async def _boom_credit(db, user_id, point_type, points, reference="recovery"):
            raise ValueError("injected failure")

        monkeypatch.setattr(recovery_playbooks, "credit_recovery_points", _boom_credit)
        db = _inject_db_fake()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
            )
        )
        by_action = {item["action"]: item for item in report["executed_actions"]}
        assert by_action["credit_points"]["status"] == "failed"
        assert by_action["credit_points"]["failure_reason"] == "injected failure"
        assert by_action["escalate_ticket"]["status"] == "executed"
        assert by_action["adjust_policy_score"]["status"] == "executed"
        action_row = [row for row in db.pending if isinstance(row, models.RecoveryAction)
                      and row.action == "credit_points"][0]
        assert action_row.status == "failed"
        assert action_row.failure_reason == "injected failure"
        assert db.commits == 1

    def test_policy_adjustment_preview_included(self):
        snapshot = policy_scoring.PolicyScoreSnapshot(
            system_score=70.0,
            customer_score=60.0,
            access_score=65.0,
            interest_score=50.0,
            closeness_score=60.0,
            community_closeness_score=65.0,
            policy_tier="standard",
            control_posture="constrained",
            summary="snapshot",
        )
        db = _inject_db_fake()
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                summary=_summary(),
                sentiment=_sentiment(),
                dissatisfaction=_dissatisfaction(),
                policy_snapshot=snapshot,
            )
        )
        preview = report["policy_adjustment_preview"]
        assert preview is not None
        # Access is lifted by the guardrail formula min(10, round(32*0.15,2))=4.8,
        # so 65 -> 69.8 (recomputed tier/posture via the canonical helpers).
        assert preview["access_score"] == 69.8
        assert preview["customer_score"] == 62.0
        assert preview["policy_tier"] == "standard"
        assert preview["control_posture"] == "observed"
        assert snapshot.access_score == 65.0  # frozen snapshot never mutated

    def test_builds_summary_from_passed_rows(self):
        row = models.ChatHistory(
            id=1,
            user_id=1,
            message="billing is wrong and support is slow",
            response="understood",
            timestamp=NOW,
        )
        db = _FakeDb(chat_rows=[row])
        report = asyncio.run(
            recovery_playbooks.run_recovery_playbooks(
                db,
                1,
                chat_rows=[row],
                sentiment=_sentiment(),
                window_days=30,
            )
        )
        assert report["user_id"] == 1
        assert isinstance(report["matched_playbooks"], list)
        assert "dissatisfaction_score" in report["context"]
        assert "recovery_readiness" in report["context"]

    def test_auto_recovery_pass_scans_users(self):
        db = _FakeDb(users=[1, 2])
        payload = asyncio.run(recovery_playbooks.run_auto_recovery_pass(db))
        assert payload["users_scanned"] == 2
        assert payload["users_recovered"] == 0
        assert payload["outcomes"] == []


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class TestActions:
    def test_credit_recovery_points_updates_wallet_and_ledger(self):
        db = _inject_db_fake()
        result = asyncio.run(
            recovery_playbooks.credit_recovery_points(
                db, 1, "loyalty_points", 50.0, reference="recovery:test"
            )
        )
        assert result["kind"] == "recovery_credit"
        assert result["points_credited"] == 50.0
        assert result["new_balance"] == 150.0
        assert result["reference"] == "recovery:test"
        ledger = [row for row in db.pending if isinstance(row, models.PointsTransaction)]
        assert ledger[0].points_delta == 50.0
        assert db.wallets[0].balance == 150.0

    def test_credit_creates_wallet_when_missing(self):
        db = _FakeDb()
        result = asyncio.run(
            recovery_playbooks.credit_recovery_points(db, 1, "loyalty_points", 25.0)
        )
        assert result["new_balance"] == 25.0
        wallets = [row for row in db.pending if isinstance(row, models.PointsWallet)]
        assert len(wallets) == 1
        assert wallets[0].user_id == 1
        assert wallets[0].point_type == "loyalty_points"

    def test_escalation_reference_uses_sequence(self):
        first = recovery_playbooks.build_escalation_result(7, {"priority": "high"}, 1)
        assert first["ticket_reference"] == "ESC-7-001"
        assert first["priority"] == "high"
        second = recovery_playbooks.build_escalation_result(7, {"priority": "urgent"}, 12)
        assert second["ticket_reference"] == "ESC-7-012"

    def test_apply_policy_adjustment_recomputes_tier(self):
        snapshot = policy_scoring.PolicyScoreSnapshot(
            system_score=70.0,
            customer_score=60.0,
            access_score=65.0,
            interest_score=50.0,
            closeness_score=60.0,
            community_closeness_score=65.0,
            policy_tier="standard",
            control_posture="constrained",
            summary="snapshot",
        )
        preview, applied = recovery_playbooks.apply_policy_adjustment(
            snapshot, {"access_delta": 10.0, "customer_delta": 2.0}
        )
        assert applied == {"access_delta": 10.0, "customer_delta": 2.0, "system_delta": 0.0}
        assert preview["access_score"] == 75.0
        assert preview["customer_score"] == 62.0
        assert preview["policy_tier"] == "customer-premium"
        assert snapshot.access_score == 65.0

    def test_apply_policy_adjustment_clamps_to_100(self):
        snapshot = policy_scoring.PolicyScoreSnapshot(
            system_score=98.0,
            customer_score=99.0,
            access_score=96.0,
            interest_score=90.0,
            closeness_score=90.0,
            community_closeness_score=90.0,
            policy_tier="system-premium",
            control_posture="system_trusted",
            summary="snapshot",
        )
        preview, _applied = recovery_playbooks.apply_policy_adjustment(
            snapshot, {"access_delta": "=98", "customer_delta": "=20"}
        )
        assert preview["access_score"] == 100.0
        assert preview["customer_score"] == 100.0

    def test_apply_policy_adjustment_without_snapshot(self):
        preview, applied = recovery_playbooks.apply_policy_adjustment(
            None, {"access_delta": 5.0}
        )
        assert preview is None
        assert applied["access_delta"] == 5.0


# ---------------------------------------------------------------------------
# Catalog + main.py wiring
# ---------------------------------------------------------------------------


class TestCatalogAndWiring:
    def test_catalog_shape(self):
        catalog = recovery_playbooks.build_recovery_playbook_catalog()
        assert catalog["catalog_version"] == "recovery_playbooks_v1"
        assert catalog["credit_action_kind"] == "recovery_credit"
        assert catalog["auto_recovery_env"] == "CSERVICE_AUTO_RECOVERY"
        assert isinstance(catalog["auto_recovery_enabled"], bool)
        assert {p["playbook_id"] for p in catalog["playbooks"]} == {
            "recovery_goodwill_points",
            "recovery_ticket_escalation",
            "recovery_policy_guardrail",
        }
        goodwill = next(p for p in catalog["playbooks"] if p["playbook_id"] == "recovery_goodwill_points")
        assert "when" in goodwill
        assert goodwill["actions"][0]["action"] == "credit_points"

    def test_scoring_catalog_exposes_recovery_playbooks(self):
        response = TestClient(app).get("/meta/scoring-catalog")
        assert response.status_code == 200
        payload = response.json()
        assert payload["recovery_playbooks"]["catalog_version"] == "recovery_playbooks_v1"

    def test_ecosystem_lists_recovery_automation(self):
        response = TestClient(app).get("/meta/ecosystem")
        assert response.status_code == 200
        subservices = response.json()["subservices"]
        assert "recovery_automation" in subservices
        assert subservices["recovery_automation"]["routes"] == [
            "/chat/recovery/playbooks",
            "/chat/admin/recovery/playbooks",
        ]

    def test_root_meta_lists_recovery_feature(self):
        response = TestClient(app).get("/meta")
        assert response.status_code == 200
        assert "recovery_playbooks" in response.json()["features"]

    def test_feature_summary_lists_recovery_endpoints(self):
        response = TestClient(app).get("/meta/features")
        assert response.status_code == 200
        endpoints = response.json()["endpoints"]
        assert endpoints["recovery_playbooks"] == "/chat/recovery/playbooks"
        assert endpoints["recovery_playbooks_admin"] == "/chat/admin/recovery/playbooks"