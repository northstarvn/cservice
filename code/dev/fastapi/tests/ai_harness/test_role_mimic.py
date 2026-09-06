from .role_mimic import RoleMimicHarness, build_ai_prompt


def test_role_mimic_harness_covers_all_roles():
    harness = RoleMimicHarness()
    scenarios = harness.build_scenarios()

    assert [scenario.role for scenario in scenarios] == ["guest", "customer", "support_agent", "manager", "admin"]


def test_role_mimic_harness_produces_compact_report():
    harness = RoleMimicHarness()

    report = harness.run(lambda scenario: (scenario.expected_signal, "ok"))

    assert report.passed == 5
    assert report.failed == 0
    assert "roles=5 passed=5 failed=0" in report.to_compact_text()


def test_ai_prompt_is_short_and_actionable():
    harness = RoleMimicHarness()
    report = harness.run(lambda scenario: ("unknown", "missing mapping"))
    prompt = build_ai_prompt(report)

    assert "failed" in prompt
    assert "concise" in prompt
