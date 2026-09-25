import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app import deps
from app.schemas.audit import (
    ComponentMetrics,
    EnhancementListReport,
    SystemEfficiencyReport,
)
from app.services import efficiency_audit
from app.services.efficiency_audit import (
    build_efficiency_audit_catalog,
    build_enhancement_list_report,
    build_full_report,
    build_system_efficiency_report,
    class_for_score,
    collect_system_data,
    scan_logs,
    score_component,
)


def test_class_for_score_thresholds():
    assert class_for_score(95.0) == "efficient"
    assert class_for_score(80.0) == "efficient"
    assert class_for_score(70.0) == "needs_attention"
    assert class_for_score(65.0) == "needs_attention"
    assert class_for_score(40.0) == "at_risk"


def test_score_component_missing_returns_missing_classification():
    raw = {
        "component": "ghost_component",
        "kind": "source",
        "present": False,
        "file_count": 0,
        "max_file_lines": 0,
        "largest_file": "",
        "avg_file_lines": 0.0,
        "marker_count": 0,
        "marker_density_per_1000": 0.0,
        "test_files": 0,
        "test_ratio": 0.0,
    }
    score, classification, findings = score_component(raw)
    assert score == 0.0
    assert classification == "missing"
    assert any(f.code == "missing_component" for f in findings)


def test_score_component_penalizes_oversized_files():
    raw = {
        "component": "backend_services",
        "kind": "source",
        "present": True,
        "file_count": 14,
        "max_file_lines": 2100,
        "largest_file": "monster.py",
        "avg_file_lines": 420.0,
        "marker_count": 0,
        "marker_density_per_1000": 0.0,
        "test_files": 12,
        "test_ratio": 0.86,
    }
    score, classification, findings = score_component(raw)
    assert score < 100.0
    assert any(f.code == "large_files" for f in findings)
    large = next(f for f in findings if f.code == "large_files")
    assert large.priority == "high"
    assert large.component == "backend_services"


def test_score_component_penalizes_missing_tests():
    raw = {
        "component": "backend_routers",
        "kind": "source",
        "present": True,
        "file_count": 4,
        "max_file_lines": 400,
        "largest_file": "a.py",
        "avg_file_lines": 200.0,
        "marker_count": 0,
        "marker_density_per_1000": 0.0,
        "test_files": 0,
        "test_ratio": 0.0,
    }
    score, classification, findings = score_component(raw)
    assert any(f.code == "missing_tests" for f in findings)
    assert score <= 85.0


def test_score_component_penalizes_marker_debt():
    raw = {
        "component": "backend_api_core",
        "kind": "source",
        "present": True,
        "file_count": 6,
        "max_file_lines": 350,
        "largest_file": "b.py",
        "avg_file_lines": 120.0,
        "marker_count": 12,
        "marker_density_per_1000": 16.0,
        "test_files": 6,
        "test_ratio": 1.0,
    }
    score, classification, findings = score_component(raw)
    assert any(f.code == "marker_debt" for f in findings)
    marker = next(f for f in findings if f.code == "marker_debt")
    assert marker.priority == "high"


def test_scan_logs_detects_error_and_warning_lines(tmp_path: Path):
    log_file = tmp_path / "sample.log"
    log_file.write_text(
        "INFO startup complete\n"
        "ERROR booking timeout for user 1\n"
        "WARNING slow query detected\n"
        "Traceback (most recent call last):\n"
        "INFO healthy\n"
    )
    target = {
        "component": "backend_reports",
        "kind": "data",
        "paths": [str(tmp_path)],
        "exts": [".log"],
        "owner": "ops",
    }
    result = scan_logs(tmp_path, targets=[target])
    assert result.available is True
    assert result.file_count == 1
    assert result.error_lines == 2
    assert result.warning_lines == 1
    assert any("ERROR" in sample or "Traceback" in sample for sample in result.samples)


def test_scan_logs_unavailable_when_no_files(tmp_path: Path):
    result = scan_logs(tmp_path, targets=[])
    assert result.available is False
    assert result.file_count == 0


@pytest.mark.asyncio
async def test_collect_system_data_unavailable_without_db():
    section = await collect_system_data(None)
    assert section.available is False
    assert section.points == []


@pytest.mark.asyncio
async def test_collect_system_data_fails_open_on_db_errors():
    class _BrokenSession:
        async def execute(self, query):
            raise RuntimeError("connection refused")

    section = await collect_system_data(_BrokenSession())
    assert section.available is False
    assert "connection refused" in (section.error or "")


@pytest.mark.asyncio
async def test_build_full_report_shapes_payload(tmp_path: Path):
    raw = {
        "component": "sample_component",
        "kind": "source",
        "description": "synthetic",
        "paths_used": [str(tmp_path)],
        "present": True,
        "file_count": 2,
        "line_count": 60,
        "avg_file_lines": 30.0,
        "max_file_lines": 45,
        "largest_file": "m.py",
        "marker_count": 0,
        "marker_density_per_1000": 0.0,
        "test_files": 1,
        "test_ratio": 0.5,
        "findings_text": [],
        "score": 90.0,
        "classification": "efficient",
    }
    report = build_full_report(
        components_metrics=[raw],
        findings_by_component={"sample_component": []},
        logs=scan_logs(tmp_path, targets=[]),
        system_data=await collect_system_data(None),
        repo_root=tmp_path,
        limit=10,
    )
    assert isinstance(report, SystemEfficiencyReport)
    assert report.rule_version == "efficiency_audit_v1"
    assert report.components[0].classification == "efficient"
    assert report.summary["overall_efficiency_score"] == 90.0
    assert report.data_sources["system_data"] == "unavailable"
    assert report.data_sources["logs"] == "unavailable"
    # No logs + no findings -> the "enable structured logging" low-priority item.
    assert any(p.code and p.priority == "low" for p in report.enhancements)
    assert report.enhancements[0].id.startswith("no_logs_available_")


@pytest.mark.asyncio
async def test_build_system_efficiency_report_scans_repo(tmp_path: Path):
    # Build a tiny fake repo layout matching the cservice component paths.
    (tmp_path / "code" / "dev" / "fastapi" / "app").mkdir(parents=True)
    (tmp_path / "code" / "dev" / "fastapi" / "tests").mkdir(parents=True)
    (tmp_path / "requirement").mkdir(parents=True)
    (tmp_path / "code" / "dev" / "fastapi" / "app" / "main.py").write_text("def x():\n    pass\n")
    (tmp_path / "code" / "dev" / "fastapi" / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    (tmp_path / "requirement" / "spec.json").write_text('{"ok": true}\n')

    report = await build_system_efficiency_report(repo_root=tmp_path)
    assert isinstance(report, SystemEfficiencyReport)
    components = {c.component: c for c in report.components}
    assert components["backend_api_core"].present is True
    assert components["backend_api_core"].file_count >= 1
    assert components["requirements_docs"].present is True
    assert report.data_sources["codebase"] == "available"


@pytest.mark.asyncio
async def test_build_enhancement_list_report_orders_by_priority():
    class _EmptySession:
        async def execute(self, query):
            class _Result:
                def scalar(self_inner):
                    return 0

            return _Result()

    report = await build_enhancement_list_report(db=_EmptySession())
    assert isinstance(report, EnhancementListReport)
    assert report.total == len(report.items)
    assert "high" in report.by_priority or "low" in report.by_priority
    priorities = [item.priority for item in report.items]
    assert priorities == sorted(priorities, key=lambda p: {"high": 0, "medium": 1, "low": 2}[p])


def test_efficiency_catalog_exposes_rule_tables():
    catalog = build_efficiency_audit_catalog()
    assert catalog["catalog_version"] == "efficiency_audit_v1"
    components = {entry["component"] for entry in catalog["components"]}
    assert "backend_services" in components
    assert "frontend_web" in components
    assert catalog["endpoints"]["report"] == "/audit/efficiency"
    assert catalog["scoring"]["expected_test_ratio_min"] == 0.5


@dataclass
class FakeAuditUser:
    id: int = 1
    username: str = "admin"
    email: str = "admin@example.com"
    full_name: str | None = "Admin"
    hashed_password: str = "hash"
    is_admin: bool = True
    created_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)


def test_audit_catalog_endpoint_is_public():
    response = TestClient(app).get("/audit/catalog")
    assert response.status_code == 200
    payload = response.json()
    assert payload["catalog_version"] == "efficiency_audit_v1"
    assert payload["endpoints"]["report"] == "/audit/efficiency"


def test_audit_efficiency_endpoint_returns_report_with_fakes():
    class _CountSession:
        def __init__(self):
            self.calls = 0

        async def execute(self, query):
            class _Result:
                def scalar(self_inner):
                    return 42

            self.calls += 1
            return _Result()

    async def _fake_get_db():
        yield _CountSession()

    async def _fake_get_current_admin_user():
        return FakeAuditUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/audit/efficiency")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["rule_version"] == "efficiency_audit_v1"
    assert payload["system_data"]["available"] is True
    assert payload["system_data"]["points"][0]["value"] == 42
    assert any(c["component"] == "backend_services" for c in payload["components"])
    assert "codebase" in payload["data_sources"]


def test_audit_enhancements_endpoint_returns_list():
    async def _fake_get_db():
        yield None

    async def _fake_get_current_admin_user():
        return FakeAuditUser()

    app.dependency_overrides[deps.get_db] = _fake_get_db
    app.dependency_overrides[deps.get_current_admin_user] = _fake_get_current_admin_user
    try:
        response = TestClient(app).get("/audit/enhancements")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_current_admin_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert "items" in payload
    assert payload["total"] == len(payload["items"])


def test_meta_ecosystem_exposes_efficiency_audit_subservice():
    response = TestClient(app).get("/meta/ecosystem")
    assert response.status_code == 200
    payload = response.json()
    assert "efficiency_audit" in payload["subservices"]
    assert "/audit/efficiency" in payload["subservices"]["efficiency_audit"]["routes"]


def test_meta_scoring_catalog_includes_efficiency_audit():
    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    payload = response.json()
    assert payload["efficiency_audit"]["catalog_version"] == "efficiency_audit_v1"


def test_detects_existing_repo_root():
    root = efficiency_audit._detect_repo_root()
    assert root.is_dir()