from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable
import json
import os


DEFAULT_ROLES = (
    "guest",
    "customer",
    "support_agent",
    "manager",
    "admin",
)


@dataclass(frozen=True)
class RoleScenario:
    role: str
    intent: str
    request: str
    expected_signal: str


@dataclass
class ScenarioResult:
    role: str
    intent: str
    passed: bool
    signal: str
    detail: str


@dataclass
class ScenarioReport:
    generated_at: str
    role_count: int
    passed: int
    failed: int
    results: list[ScenarioResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "role_count": self.role_count,
            "passed": self.passed,
            "failed": self.failed,
            "results": [result.__dict__ for result in self.results],
        }

    def to_compact_text(self) -> str:
        lines = [f"roles={self.role_count} passed={self.passed} failed={self.failed}"]
        for result in self.results:
            status = "ok" if result.passed else "fail"
            lines.append(f"{status}|{result.role}|{result.intent}|{result.signal}")
        return "\n".join(lines)


class RoleMimicHarness:
    def __init__(self, roles: Iterable[str] | None = None) -> None:
        self.roles = tuple(roles or DEFAULT_ROLES)

    def build_scenarios(self) -> list[RoleScenario]:
        base_scenarios = [
            RoleScenario(role="guest", intent="discover", request="Show public entry points.", expected_signal="public_access"),
            RoleScenario(role="customer", intent="book", request="Create a booking and confirm details.", expected_signal="booking_flow"),
            RoleScenario(role="support_agent", intent="triage", request="Inspect open issues and suggest next steps.", expected_signal="support_workbench"),
            RoleScenario(role="manager", intent="review", request="Summarize team performance and escalation risk.", expected_signal="management_view"),
            RoleScenario(role="admin", intent="govern", request="Audit retention and operational health.", expected_signal="admin_controls"),
        ]
        return [scenario for scenario in base_scenarios if scenario.role in self.roles]

    def run(self, scorer) -> ScenarioReport:
        scenarios = self.build_scenarios()
        results: list[ScenarioResult] = []
        for scenario in scenarios:
            signal, detail = scorer(scenario)
            passed = signal == scenario.expected_signal
            results.append(
                ScenarioResult(
                    role=scenario.role,
                    intent=scenario.intent,
                    passed=passed,
                    signal=signal,
                    detail=detail,
                )
            )

        passed = sum(1 for item in results if item.passed)
        return ScenarioReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            role_count=len(scenarios),
            passed=passed,
            failed=len(scenarios) - passed,
            results=results,
        )


def build_ai_prompt(report: ScenarioReport) -> str:
    compact = report.to_compact_text()
    return (
        "You are improving a customer service system.\n"
        "Review the role coverage results and return only concise, actionable fixes.\n"
        "Focus on failed roles first, then missing behaviors, then the smallest code changes.\n"
        f"\n{compact}\n"
    )


def save_report(report: ScenarioReport, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"role_mimic_report_{report.generated_at.replace(':', '_')}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
    return path
