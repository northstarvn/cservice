"""Tests for the thin-infrastructure expansion.

IOW These tests verify that the pinned contract key sets of the six expanded modules are unchanged, and that the new sibling policy catalogs are accessible from the metadata surfaces.

The repo's house style is: config tables + pure helpers + a ``build_*_catalog()`` /
``build_*_policy()`` surface. This pass adds a ``build_*_policy()`` function and a
``"policy"`` pointer inside the *corresponding* ``build_*_catalog()``, without
altering the catalog's top-level key set. Each test below asserts that the pinned
keys are preserved.
"""
from fastapi.testclient import TestClient

from app.main import app


def test_meta_endpoints_preserve_pinned_key_sets():
    """The expanded modules' sibling policy surfaces do not move any keys inside
    the pinned catalogs."""

    client = TestClient(app)

    # /meta/zero-trust keeps its eight keys unchanged.
    zero_trust = client.get("/meta/zero-trust").json()
    assert set(zero_trust) == {
        "name",
        "version",
        "environment",
        "generated_at",
        "hsm_signer",
        "biometric_vault",
        "risk_evaluator",
        "cell_matrix",
    }

    # /meta/scoring-catalog["zero_trust"] keeps the same four.
    scoring = client.get("/meta/scoring-catalog").json()
    assert set(scoring["zero_trust"]) == {
        "hsm_signer",
        "biometric_vault",
        "risk_evaluator",
        "cell_matrix",
    }

    # /meta/tenants keeps its seven keys unchanged.
    tenants = client.get("/meta/tenants").json()
    assert set(tenants) == {
        "declared_tenants",
        "default_tenant",
        "dsn_template_configured",
        "lookup_order",
        "mode",
        "pool",
        "registered",
    }

    # /meta/partitions["plan"] keeps its three keys unchanged.
    partitions = client.get("/meta/partitions").json()
    assert set(partitions["plan"]) == {"create", "drop_count", "held_count"}


def test_new_policy_endpoints_are_reachable():
    """The new sibling policy endpoints exist and return valid JSON."""

    client = TestClient(app)

    # Partitions lifecycle policy.
    r = client.get("/meta/partitions/policy")
    assert r.status_code == 200
    payload = r.json()
    assert payload["default_tier"] == "hot"
    assert "tiers" in payload
    assert "guard_bands" in payload
    assert payload["guard_bands"]["daily"] == 7

    # Zero-trust governance policy.
    r = client.get("/meta/zero-trust/policy")
    assert r.status_code == 200
    payload = r.json()
    assert payload["note"]
    assert payload["hsm_signer"]
    assert payload["biometric_vault"]
    assert payload["risk_evaluator"]
    assert payload["cell_matrix"]