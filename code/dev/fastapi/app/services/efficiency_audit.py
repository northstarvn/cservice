"""Conceptual system self-audit / efficiency classification.

Hypothesis
----------
The service should be able to look at its own system — the codebase layout,
generated reports/logs, and (when reachable) database scale — and classify each
component by efficiency, then propose a prioritized enhancement list. The goal
is a *conceptual* health check that is decent enough to run from day one:
no AST parsing, no external AI, just deterministic heuristics over files and
any available operational data.

Implementation
--------------
- `COMPONENT_TARGETS` — config table mapping each logical component to the
  paths it lives in (relative to the repo root) and which file extensions to
  count. Adding a component is config-only.
- Pure core: `_scan_component`, `score_component`, `propose_enhancements`,
  `build_full_report` (the sync payload assembly).
- Async orchestrator: `build_system_efficiency_report` — collects optional DB
  system data and keeps the report usable when DB/logs are unavailable.
- `build_efficiency_audit_catalog` feeds `/meta/scoring-catalog` and the
  public `/audit/catalog` endpoint so the rule tables are discoverable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.audit import (
    ComponentMetrics,
    EnhancementListReport,
    EnhancementProposal,
    LogScanResult,
    SystemDataPoint,
    SystemDataSection,
    SystemEfficiencyReport,
)

RULE_VERSION = "efficiency_audit_v1"
SCOPE = "cservice_system"
METHOD = "conceptual static analysis (deterministic heuristics; no parsing)"

# ---------------------------------------------------------------------------
# Config tables
# ---------------------------------------------------------------------------

# Directories never scanned (case-insensitive path-part match).
EXCLUDED_DIR_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    ".next",
}

# Components are described relative to the repo root. `kind` drives which
# efficiency rules apply (source components are expected to carry tests).
COMPONENT_TARGETS: list[dict[str, Any]] = [
    {
        "component": "backend_api_core",
        "kind": "source",
        "description": "FastAPI wiring: main, db, deps, models, security, i18n.",
        "paths": ["code/dev/fastapi/app"],
        "exts": [".py"],
        "recursive": False,
        "test_path": ["code/dev/fastapi/tests"],
        "owner": "backend engineering",
    },
    {
        "component": "backend_routers",
        "kind": "source",
        "description": "HTTP routers exposing the API surface.",
        "paths": ["code/dev/fastapi/app/routers"],
        "exts": [".py"],
        "test_path": ["code/dev/fastapi/tests"],
        "owner": "backend engineering",
    },
    {
        "component": "backend_services",
        "kind": "source",
        "description": "Domain services and rule engines.",
        "paths": ["code/dev/fastapi/app/services"],
        "exts": [".py"],
        "test_path": ["code/dev/fastapi/tests"],
        "owner": "backend engineering",
    },
    {
        "component": "backend_schemas",
        "kind": "source",
        "description": "Pydantic contracts between API and services.",
        "paths": ["code/dev/fastapi/app/schemas"],
        "exts": [".py"],
        "test_path": ["code/dev/fastapi/tests"],
        "owner": "backend engineering",
    },
    {
        "component": "backend_migrations",
        "kind": "schema",
        "description": "Alembic migration scripts.",
        "paths": ["code/dev/fastapi/alembic"],
        "exts": [".py", ".ini", ".mako", ".md"],
        "owner": "backend engineering",
    },
    {
        "component": "backend_tests",
        "kind": "tests",
        "description": "Pytest suites covering the backend.",
        "paths": ["code/dev/fastapi/tests"],
        "exts": [".py"],
        "owner": "qa / backend engineering",
    },
    {
        "component": "backend_reports",
        "kind": "data",
        "description": "Generated reports and operational output (treated as logs).",
        "paths": ["code/dev/fastapi/reports"],
        "exts": [".json", ".log"],
        "owner": "ops / analytics",
    },
    {
        "component": "frontend_web",
        "kind": "source",
        "description": "React web application (dev frontend).",
        "paths": ["code/dev/src"],
        "exts": [".js", ".jsx", ".ts", ".tsx", ".css"],
        "test_path": ["code/dev/src/__tests__"],
        "owner": "frontend engineering",
    },
    {
        "component": "requirements_docs",
        "kind": "docs",
        "description": "Requirement specs and product documentation.",
        "paths": ["requirement"],
        "exts": [".json", ".txt", ".js", ".yaml", ".html", ".md"],
        "owner": "product",
    },
]

# Marker patterns counted as visible tech-debt in source lines.
MARKER_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)
# Error-level lines: explicit failures. A JSON report counter like
# `"failed": 0` must NOT count as an error, only genuine failures do.
ERROR_RE = re.compile(
    r"\b(error|exception|traceback|critical|crash)\b"
    r"|\bfailed\b(?!\s*[\"']?\s*[:=]\s*(0|false|null|none)\b)",
    re.IGNORECASE,
)
WARNING_RE = re.compile(r"\b(warning|warn(?:ing)?s?|deprecated)\b", re.IGNORECASE)

# Line-based efficiency penalties (declining marginal returns on size).
LARGE_FILE_RULES = [
    {"min_lines": 1200, "penalty": 12.0},
    {"min_lines": 800, "penalty": 8.0},
    {"min_lines": 500, "penalty": 5.0},
]

MARKER_DENSITY_RULES = [
    {"min_per_1000": 15.0, "penalty": 10.0, "priority": "high"},
    {"min_per_1000": 5.0, "penalty": 6.0, "priority": "medium"},
]

CLASSIFICATION_THRESHOLDS = [
    {"min_score": 80.0, "label": "efficient"},
    {"min_score": 65.0, "label": "needs_attention"},
    {"min_score": 0.0, "label": "at_risk"},
]

# System-data metrics collected when the DB session is reachable.
SYSTEM_DATA_METRICS: list[tuple[str, Any]] = [
    ("users", models.User),
    ("bookings", models.Booking),
    ("chat_messages", models.ChatHistory),
    ("interaction_signals", models.InteractionSignal),
    ("retention_snapshots", models.RetentionSnapshot),
    ("recovery_outcomes", models.RecoveryOutcome),
]

MAX_FILE_READ_BYTES = 2 * 1024 * 1024  # guard against huge/binary files
MAX_UPDATE_SAMPLES = 5

PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
IMPACT_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


@dataclass
class _Finding:
    """Internal structured finding; mapped to an EnhancementProposal."""

    code: str
    component: str
    detail: str
    recommendation: str
    owner_hint: str
    impact: str
    effort: str
    priority: str
    signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------

def _detect_repo_root() -> Path:
    """Resolve the repo root walking up from this file.

    Prefers an ancestor that looks like the cservice layout (contains
    `requirement` and `code`); falls back to the dev folder.
    """
    override = os.getenv("CSERVICE_AUDIT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "requirement").is_dir() and (parent / "code").is_dir():
            return parent
        if (parent / "code").is_dir():
            return parent
    # services -> app -> fastapi -> dev
    return current.parents[3]


def resolve_repo_root(repo_root: Optional[Path] = None) -> Path:
    return Path(repo_root).expanduser().resolve() if repo_root else _detect_repo_root()


def _resolve_target_paths(target: dict[str, Any], root: Path) -> list[Path]:
    paths = []
    for rel in target.get("paths", []):
        candidate = root / rel
        if candidate.exists():
            paths.append(candidate)
    return paths


# ---------------------------------------------------------------------------
# Scanning (conceptual)
# ---------------------------------------------------------------------------

def _iter_files(root: Path):
    """Walk `root` yielding files, pruning excluded/hidden directories eagerly."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in EXCLUDED_DIR_PARTS and not d.startswith(".")
        ]
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def _collect_files(paths: list[Path], exts: set[str], recursive: bool = True) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix in exts:
                files.append(path)
        elif path.is_dir():
            if recursive:
                for candidate in _iter_files(path):
                    if candidate.suffix in exts:
                        files.append(candidate)
            else:
                for candidate in path.iterdir():
                    if candidate.is_file() and candidate.suffix in exts:
                        files.append(candidate)
    return sorted(files)


def _read_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_FILE_READ_BYTES:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _count_markers(text: str) -> int:
    return len(MARKER_RE.findall(text))


def _analyze_files(files: list[Path], markers: bool = True) -> dict[str, Any]:
    line_count = 0
    marker_count = 0
    per_file: list[tuple[int, Path]] = []
    for path in files:
        if markers:
            text = _read_text(path)
            marker_count += _count_markers(text)
            lines = text.count("\n") + 1
        else:
            # Cheap line estimate from size when we do not need content.
            try:
                line_count_fallback = sum(1 for _ in path.open("r", errors="ignore"))
            except OSError:
                line_count_fallback = 0
            lines = line_count_fallback
        line_count += lines
        per_file.append((lines, path))
    return {"line_count": line_count, "marker_count": marker_count, "per_file": per_file}


def _count_test_files(test_paths: list[Path]) -> int:
    files = _collect_files(test_paths, {".py"})
    test_files = [f for f in files if f.stem.startswith("test_") or f.name.startswith("test_")]
    return len(test_files)


def scan_component(
    target: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    """Scan one component target into raw, JSON-friendly metrics."""
    paths = _resolve_target_paths(target, repo_root)
    exts = {str(e) for e in target.get("exts", [])}
    files = _collect_files(
        paths, exts, recursive=bool(target.get("recursive", True))
    )
    analysis = _analyze_files(files, markers=target.get("kind") != "data")

    per_file = analysis["per_file"]
    max_lines = max((entry[0] for entry in per_file), default=0)
    largest = ""
    if per_file:
        largest_file_entry = max(per_file, key=lambda entry: entry[0])
        largest = largest_file_entry[1].name

    test_files = 0
    if target.get("kind") == "source" and target.get("test_path"):
        test_paths = [repo_root / rel for rel in target["test_path"] if (repo_root / rel).exists()]
        test_files = _count_test_files(test_paths)

    file_count = len(files)
    line_count = analysis["line_count"]
    avg = round(line_count / file_count, 2) if file_count else 0.0
    return {
        "component": target["component"],
        "kind": target["kind"],
        "description": target["description"],
        "paths_used": [str(p) for p in paths],
        "present": bool(files),
        "file_count": file_count,
        "line_count": line_count,
        "avg_file_lines": avg,
        "max_file_lines": max_lines,
        "largest_file": largest,
        "marker_count": analysis["marker_count"],
        "marker_density_per_1000": round(analysis["marker_count"] * 1000.0 / max(line_count, 1), 2),
        "test_files": test_files,
        "test_ratio": round(test_files / max(file_count, 1), 2),
    } | {"findings_text": [], "score": 0.0, "classification": "missing"}


# ---------------------------------------------------------------------------
# Scoring (deterministic)
# ---------------------------------------------------------------------------

def score_component(raw: dict[str, Any]) -> tuple[float, str, list[_Finding]]:
    """Turn raw metrics into an efficiency score, classification, and findings."""
    findings: list[_Finding] = []
    component = raw["component"]
    owner = _owner_hint(component)

    if not raw["present"] or raw["file_count"] == 0:
        return (
            0.0,
            "missing",
            [
                _Finding(
                    code="missing_component",
                    component=component,
                    detail=f"No files found for component '{component}'.",
                    recommendation="Verify the component's path or deployment checkout; or create the component skeleton.",
                    owner_hint=owner,
                    impact="high",
                    effort="high",
                    priority="high",
                    signals=[f"component='{component}' has 0 scannable files"],
                )
            ],
        )

    score = 100.0
    max_lines = float(raw["max_file_lines"])
    findings_text: list[str] = []

    # Oversized files.
    for rule in LARGE_FILE_RULES:
        if max_lines >= rule["min_lines"]:
            score -= rule["penalty"]
            findings_text.append(
                f"oversized file '{raw['largest_file']}' ({int(max_lines)} lines) may hide complexity"
            )
            if max_lines >= 1200:
                findings.append(
                    _Finding(
                        code="large_files",
                        component=component,
                        detail=f"'{raw['largest_file']}' has {int(max_lines)} lines.",
                        recommendation="Split the largest file into cohesive modules and move shared logic into services.",
                        owner_hint=owner,
                        impact="high",
                        effort="medium",
                        priority="high",
                        signals=[f"max_file_lines={int(max_lines)}"],
                    )
                )
            elif max_lines >= 800:
                findings.append(
                    _Finding(
                        code="large_files",
                        component=component,
                        detail=f"'{raw['largest_file']}' has {int(max_lines)} lines.",
                        recommendation="Split the largest file into cohesive modules.",
                        owner_hint=owner,
                        impact="medium",
                        effort="medium",
                        priority="medium",
                        signals=[f"max_file_lines={int(max_lines)}"],
                    )
                )
            else:
                findings.append(
                    _Finding(
                        code="large_files",
                        component=component,
                        detail=f"'{raw['largest_file']}' has {int(max_lines)} lines.",
                        recommendation="Consider splitting the largest file into cohesive modules.",
                        owner_hint=owner,
                        impact="medium",
                        effort="low",
                        priority="low",
                        signals=[f"max_file_lines={int(max_lines)}"],
                    )
                )
            break

    # High average density of code per file.
    if raw["avg_file_lines"] > 600:
        score -= 6.0
        findings_text.append(
            f"average file size {raw['avg_file_lines']:.0f} lines suggests dense modules"
        )

    # Marker debt density.
    density = float(raw["marker_density_per_1000"])
    for rule in MARKER_DENSITY_RULES:
        if density >= rule["min_per_1000"]:
            score -= rule["penalty"]
            findings_text.append(
                f"{raw['marker_count']} TODO/FIXME markers ({density:.1f} per 1k lines)"
            )
            findings.append(
                _Finding(
                    code="marker_debt",
                    component=component,
                    detail=f"{raw['marker_count']} TODO/FIXME/HACK markers found.",
                    recommendation="Triage the marked items: fix quick wins, convert the rest into tracked backlog tickets.",
                    owner_hint=owner,
                    impact="medium",
                    effort="low",
                    priority=rule["priority"],
                    signals=[f"marker_density_per_1000={density:.2f}"],
                )
            )
            break

    # Test coverage expectations for source components.
    if raw["kind"] == "source" and raw.get("test_files", 0) == 0:
        score -= 15.0
        findings_text.append("no automated tests found for this source component")
        findings.append(
            _Finding(
                code="missing_tests",
                component=component,
                detail="Source component has no test files in the expected test path.",
                recommendation="Add a pytest suite covering the component's public builders and edge cases.",
                owner_hint="qa / " + owner,
                impact="high",
                effort="medium",
                priority="high",
                signals=[f"test_files=0 while source file_count={raw['file_count']}"],
            )
        )
    elif raw["kind"] == "source" and raw["test_ratio"] < 0.5:
        score -= 5.0
        findings_text.append("test-to-source ratio is low")
        findings.append(
            _Finding(
                code="low_test_ratio",
                component=component,
                detail=f"Only {raw['test_files']} test files for {raw['file_count']} source files.",
                recommendation="Extend test coverage for the least-covered paths of this component.",
                owner_hint="qa / " + owner,
                impact="medium",
                effort="low",
                priority="medium",
                signals=[f"test_ratio={raw['test_ratio']:.2f}"],
            )
        )

    score = max(0.0, round(score, 2))
    classification = class_for_score(score)
    raw["findings_text"] = findings_text
    raw["score"] = score
    raw["classification"] = classification
    return score, classification, findings


def class_for_score(score: float) -> str:
    for rule in sorted(CLASSIFICATION_THRESHOLDS, key=lambda r: r["min_score"], reverse=True):
        if score >= rule["min_score"]:
            return rule["label"]
    return "at_risk"


def _owner_hint(component: str) -> str:
    for target in COMPONENT_TARGETS:
        if target["component"] == component:
            return target.get("owner", "engineering")
    return "engineering"


# ---------------------------------------------------------------------------
# Log scan
# ---------------------------------------------------------------------------

def scan_logs(repo_root: Path, targets: Optional[list[dict[str, Any]]] = None) -> LogScanResult:
    """Scan report/log files under the configured data components and any *.log files.

    Gracefully reports `available=False` when nothing log-like exists.
    """
    candidates: list[Path] = []
    data_targets = [t for t in (targets or COMPONENT_TARGETS) if t.get("kind") == "data"]
    for target in data_targets:
        paths = _resolve_target_paths(target, repo_root)
        candidates.extend(_collect_files(paths, {".json", ".log"}))
    # Any stray *.log file anywhere under the repo root.
    log_root = repo_root
    if log_root.is_dir():
        for candidate in _iter_files(log_root):
            if candidate.suffix != ".log" or candidate in candidates:
                continue
            candidates.append(candidate)

    error_lines = 0
    warning_lines = 0
    total_lines = 0
    samples: list[str] = []
    scanned: list[str] = []
    for path in sorted(set(candidates)):
        text = _read_text(path)
        if not text:
            continue
        scanned.append(str(path))
        total_lines += text.count("\n") + 1
        for line in text.splitlines()[:2000]:
            line = line.strip()
            if not line:
                continue
            if ERROR_RE.search(line):
                error_lines += 1
                if len(samples) < MAX_UPDATE_SAMPLES:
                    samples.append(line[:240])
            elif WARNING_RE.search(line):
                warning_lines += 1

    available = bool(scanned)
    return LogScanResult(
        available=available,
        paths_scanned=scanned,
        file_count=len(scanned),
        error_lines=error_lines,
        warning_lines=warning_lines,
        error_density_per_1000=round(error_lines * 1000.0 / max(total_lines, 1), 2),
        samples=samples,
    )


# ---------------------------------------------------------------------------
# System data (optional DB)
# ---------------------------------------------------------------------------

async def collect_system_data(db: Optional[AsyncSession]) -> SystemDataSection:
    if db is None:
        return SystemDataSection(available=False, source="database", error="no database session provided")
    try:
        points: list[SystemDataPoint] = []
        for metric, model in SYSTEM_DATA_METRICS:
            result = await db.execute(select(func.count(model.id)))
            value = int(result.scalar() or 0)
            points.append(SystemDataPoint(metric=metric, value=value))
        return SystemDataSection(available=True, source="database", points=points)
    except Exception as exc:  # DB unreachable should not break the report
        return SystemDataSection(
            available=False,
            source="database",
            error=f"database query failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Enhancements
# ---------------------------------------------------------------------------

def propose_enhancements(
    findings: dict[str, list[_Finding]],
    logs: LogScanResult,
) -> list[_Finding]:
    flat: list[_Finding] = []
    for component_findings in findings.values():
        flat.extend(component_findings)

    if logs.available and logs.error_lines > 0:
        priority = "high" if logs.error_density_per_1000 >= 5.0 else "medium"
        flat.append(
            _Finding(
                code="log_errors_present",
                component="backend_reports",
                detail=f"{logs.error_lines} error-level lines in {logs.file_count} log/report files.",
                recommendation="Inspect the sampled error lines, add alerting, and fix the recurring failure paths.",
                owner_hint="ops / backend engineering",
                impact="high" if priority == "high" else "medium",
                effort="medium",
                priority=priority,
                signals=[f"error_lines={logs.error_lines}"] + logs.samples[:3],
            )
        )
    elif not logs.available:
        flat.append(
            _Finding(
                code="no_logs_available",
                component="backend_reports",
                detail="No log/report files were found to inspect.",
                recommendation="Enable structured logging/report output so operational issues become visible.",
                owner_hint="ops",
                impact="low",
                effort="low",
                priority="low",
                signals=["logs_available=False"],
            )
        )

    return flat


def _finding_to_proposal(index: int, finding: _Finding) -> EnhancementProposal:
    title_by_code = {
        "missing_component": "Restore or scaffold missing component",
        "large_files": "Split oversized modules",
        "marker_debt": "Clear TODO/FIXME tech debt",
        "missing_tests": "Add automated test coverage",
        "low_test_ratio": "Improve test-to-source coverage",
        "log_errors_present": "Investigate error-level log lines",
        "no_logs_available": "Enable structured logging",
    }
    return EnhancementProposal(
        id=f"{finding.code}_{index}",
        code=finding.code,
        component=finding.component,
        title=title_by_code.get(finding.code, finding.code.replace("_", " ").title()),
        priority=finding.priority,
        impact=finding.impact,
        effort=finding.effort,
        rationale=[
            f"{finding.component}: {finding.detail}",
            f"Recommended owner: {finding.owner_hint}.",
        ],
        recommended_action=finding.recommendation,
        owner_hint=finding.owner_hint,
        signals=finding.signals,
    )


def sort_and_limit_proposals(
    findings: list[_Finding],
    limit: int = 20,
) -> list[EnhancementProposal]:
    def sort_key(finding: _Finding):
        return (
            PRIORITY_RANK.get(finding.priority, 9),
            IMPACT_RANK.get(finding.impact, 9),
            0 if finding.priority == "high" else (PRIORITY_RANK.get(finding.priority, 9)),
        )

    ordered = sorted(findings, key=sort_key)
    return [
        _finding_to_proposal(index, finding)
        for index, finding in enumerate(ordered[: max(1, limit)])
    ]


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _build_summary(
    components: list[ComponentMetrics],
    proposals: list[EnhancementProposal],
    data_sources: dict[str, str],
) -> dict[str, Any]:
    scored = [c for c in components if c.present]
    counts: dict[str, int] = {}
    for component in scored:
        counts[component.classification] = counts.get(component.classification, 0) + 1
    overall = (
        round(sum(c.efficiency_score for c in scored) / max(len(scored), 1), 2)
        if scored
        else 0.0
    )
    worst = min(scored, key=lambda c: c.efficiency_score) if scored else None
    by_priority: dict[str, int] = {}
    for proposal in proposals:
        by_priority[proposal.priority] = by_priority.get(proposal.priority, 0) + 1
    return {
        "overall_efficiency_score": overall,
        "overall_classification": class_for_score(overall),
        "components_scanned": len(components),
        "components_present": len(scored),
        "by_classification": counts,
        "worst_component": worst.component if worst else None,
        "worst_score": worst.efficiency_score if worst else 0.0,
        "enhancement_count": len(proposals),
        "enhancements_by_priority": by_priority,
        "data_sources": data_sources,
    }


def build_full_report(
    components_metrics: list[dict[str, Any]],
    findings_by_component: dict[str, list[_Finding]],
    logs: LogScanResult,
    system_data: SystemDataSection,
    repo_root: Path,
    limit: int = 20,
) -> SystemEfficiencyReport:
    components = [
        ComponentMetrics(
            component=raw["component"],
            kind=raw["kind"],
            description=raw["description"],
            paths_used=raw["paths_used"],
            present=raw["present"],
            file_count=raw["file_count"],
            line_count=raw["line_count"],
            avg_file_lines=raw["avg_file_lines"],
            max_file_lines=raw["max_file_lines"],
            largest_file=raw["largest_file"],
            marker_count=raw["marker_count"],
            marker_density_per_1000=raw["marker_density_per_1000"],
            test_files=raw["test_files"],
            test_ratio=raw["test_ratio"],
            efficiency_score=raw["score"],
            classification=raw["classification"],
            findings=raw["findings_text"],
        )
        for raw in components_metrics
    ]

    all_findings = propose_enhancements(findings_by_component, logs)
    proposals = sort_and_limit_proposals(all_findings, limit=limit)

    data_sources = {
        "codebase": "available" if any(c.present for c in components) else "partial",
        "logs": "available" if logs.available else "unavailable",
        "system_data": "available" if system_data.available else "unavailable",
    }
    summary = _build_summary(components, proposals, data_sources)

    return SystemEfficiencyReport(
        generated_at=datetime.now(timezone.utc),
        scope=SCOPE,
        method=METHOD,
        rule_version=RULE_VERSION,
        data_sources=data_sources,
        components=components,
        system_data=system_data,
        logs=logs,
        summary=summary,
        enhancements=proposals,
    )


# ---------------------------------------------------------------------------
# Async entry point (router-facing)
# ---------------------------------------------------------------------------

async def build_system_efficiency_report(
    db: Optional[AsyncSession] = None,
    limit: int = 20,
    include_logs: bool = True,
    repo_root: Optional[Path] = None,
) -> SystemEfficiencyReport:
    """Scan the repo, score components, and produce the full efficiency report."""
    root = resolve_repo_root(repo_root)
    components_metrics: list[dict[str, Any]] = []
    findings_by_component: dict[str, list[_Finding]] = {}
    for target in COMPONENT_TARGETS:
        raw = scan_component(target, root)
        score, classification, findings = score_component(raw)
        raw["score"] = score
        raw["classification"] = classification
        components_metrics.append(raw)
        findings_by_component[target["component"]] = findings

    logs = scan_logs(root) if include_logs else LogScanResult()
    system_data = await collect_system_data(db)
    return build_full_report(
        components_metrics,
        findings_by_component,
        logs,
        system_data,
        root,
        limit=limit,
    )


async def build_enhancement_list_report(
    db: Optional[AsyncSession] = None,
    limit: int = 20,
    include_logs: bool = True,
    repo_root: Optional[Path] = None,
) -> EnhancementListReport:
    """Extract just the prioritized enhancement list from a full scan."""
    full = await build_system_efficiency_report(
        db, limit=limit, include_logs=include_logs, repo_root=repo_root
    )
    by_priority: dict[str, int] = {}
    for proposal in full.enhancements:
        by_priority[proposal.priority] = by_priority.get(proposal.priority, 0) + 1
    return EnhancementListReport(
        generated_at=full.generated_at,
        rule_version=RULE_VERSION,
        total=len(full.enhancements),
        by_priority=by_priority,
        items=full.enhancements,
    )


# ---------------------------------------------------------------------------
# Catalog (discoverability)
# ---------------------------------------------------------------------------

def build_efficiency_audit_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog` and `/audit/catalog`."""
    return {
        "catalog_version": RULE_VERSION,
        "method": METHOD,
        "components": [
            {
                "component": target["component"],
                "kind": target["kind"],
                "description": target["description"],
                "paths": list(target["paths"]),
                "exts": list(target.get("exts", [])),
                "recursive": bool(target.get("recursive", True)),
                "owner": target.get("owner", "engineering"),
            }
            for target in COMPONENT_TARGETS
        ],
        "scoring": {
            "large_file_lines": [rule["min_lines"] for rule in LARGE_FILE_RULES],
            "marker_density_thresholds_per_1000": [
                rule["min_per_1000"] for rule in MARKER_DENSITY_RULES
            ],
            "expected_test_ratio_min": 0.5,
            "classification_thresholds": {
                rule["label"]: rule["min_score"] for rule in CLASSIFICATION_THRESHOLDS
            },
        },
        "system_data_metrics": [metric for metric, _ in SYSTEM_DATA_METRICS],
        "endpoints": {
            "report": "/audit/efficiency",
            "enhancements": "/audit/enhancements",
            "catalog": "/audit/catalog",
        },
    }