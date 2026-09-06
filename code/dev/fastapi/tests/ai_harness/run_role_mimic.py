from __future__ import annotations

import argparse
import pathlib
import sys

if __package__ in {None, ""}:
    sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

from tests.ai_harness.role_mimic import RoleMimicHarness, build_ai_prompt, save_report


def _default_scorer(scenario):
    mapping = {
        "guest": ("public_access", "Public pages are visible."),
        "customer": ("booking_flow", "Booking flow reached confirmation."),
        "support_agent": ("support_workbench", "Open issues surfaced."),
        "manager": ("management_view", "Team summary available."),
        "admin": ("admin_controls", "Retention audit available."),
    }
    return mapping.get(scenario.role, ("unknown", "No mapping available."))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a compact AI-style role mimic harness.")
    parser.add_argument("--output-dir", default="reports", help="Directory for the JSON report.")
    args = parser.parse_args()

    harness = RoleMimicHarness()
    report = harness.run(_default_scorer)
    report_path = save_report(report, args.output_dir)
    prompt = build_ai_prompt(report)

    print(report.to_compact_text())
    print(f"report={report_path}")
    print("ai_prompt=")
    print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
