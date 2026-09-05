from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.routers.chat import _prune_retention_snapshots, _build_retention_dashboard


@dataclass
class FakeSnapshot:
    id: int
    user_id: int
    snapshot_type: str
    window_days: int
    loyalty_score: float
    churn_risk: str
    lifecycle_stage: str
    summary_json: str
    created_at: datetime


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, snapshots=None):
        self.snapshots = snapshots or []
        self.deleted = []
        self.commits = 0
        self.executed_queries = []

    async def execute(self, query):
        self.executed_queries.append(str(query))
        if "retention_snapshots" in str(query):
            return FakeResult(list(self.snapshots))
        return FakeResult([])

    async def delete(self, item):
        self.deleted.append(item)
        if item in self.snapshots:
            self.snapshots.remove(item)

    async def commit(self):
        self.commits += 1


def _snapshot(snapshot_id: int, snapshot_type: str = "chat_response", created_at=None):
    created_at = created_at or datetime.now(timezone.utc)
    return FakeSnapshot(
        id=snapshot_id,
        user_id=1,
        snapshot_type=snapshot_type,
        window_days=30,
        loyalty_score=80.0 - snapshot_id,
        churn_risk="low",
        lifecycle_stage="loyal",
        summary_json="{}",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_prune_retention_snapshots_keeps_newest_items():
    snapshots = [
        _snapshot(1, created_at=datetime.now(timezone.utc) - timedelta(days=1)),
        _snapshot(2, created_at=datetime.now(timezone.utc) - timedelta(days=2)),
        _snapshot(3, created_at=datetime.now(timezone.utc) - timedelta(days=3)),
    ]
    db = FakeSession(snapshots=snapshots)
    expected_deleted_id = snapshots[2].id

    await _prune_retention_snapshots(db, user_id=1, window_days=30, keep=2)

    assert len(db.snapshots) == 2
    assert [item.id for item in db.deleted] == [expected_deleted_id]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_build_retention_dashboard_returns_composed_sections():
    db = FakeSession([_snapshot(1), _snapshot(2)])

    dashboard = await _build_retention_dashboard(db, user_id=1, window_days=30)

    assert dashboard.window_days == 30
    assert dashboard.summary.user_id == 1
    assert dashboard.snapshot_report.window_days == 30
    assert dashboard.snapshot_delta.window_days == 30
    assert dashboard.snapshot_trends.window_days == 30
    assert dashboard.snapshot_report.snapshots[0].id == 1
    assert dashboard.snapshot_trends.trends[0].snapshot_type == "chat_response"