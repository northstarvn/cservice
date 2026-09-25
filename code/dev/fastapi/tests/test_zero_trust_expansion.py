"""Tests for Stage 1B: cryptographic security & zero-trust access.

Covers HSM signing abstractions (``app/hsm_signer.py``), biometric validation
vaults (``app/biometric_vault.py``), contextual adaptive risk evaluation
(``app/risk_evaluator.py``), and fine-grained data-cell access matrices
(``app/cell_matrix.py``).
"""
import pytest

from app import biometric_vault, cell_matrix, hsm_signer, risk_evaluator


# --- HSM signing --------------------------------------------------------------


def test_hsm_sign_and_verify_round_trip():
    signer = hsm_signer.MockHsmSigner(key_id="test-key")
    payload = b"authenticate-alice"
    sig = hsm_signer.sign_bytes(payload, signer)
    assert sig.key_id == "test-key"
    assert hsm_signer.verify_bytes(payload, sig.signature, signer) is True


def test_hsm_verify_rejects_tampered_payload():
    signer = hsm_signer.MockHsmSigner()
    payload = b"authenticate-alice"
    sig = hsm_signer.sign_bytes(payload, signer)
    assert hsm_signer.verify_bytes(b"authenticate-bob", sig.signature, signer) is False


def test_hsm_envelope_round_trip_and_tamper_detection():
    data = {"user": "alice", "scope": ["read", "write"], "n": 3}
    envelope = hsm_signer.sign_envelope(data)
    assert hsm_signer.verify_envelope(envelope)
    assert hsm_signer.decode_envelope(envelope) == data

    tampered = dict(envelope)
    tampered["payload"] = hsm_signer.sign_envelope({"user": "mallory"})["payload"]
    assert hsm_signer.verify_envelope(tampered) is False
    assert hsm_signer.verify_envelope({"payload": "not-base64", "signature": "x"}) is False


def test_hsm_signer_catalog_is_introspectable():
    catalog = hsm_signer.build_hsm_signer_catalog()
    assert catalog["supported_backends"] == ["hmac", "ed25519", "mock"]
    assert catalog["key_id"] and catalog["algorithm"]


# --- biometric vault ----------------------------------------------------------


def test_biometric_register_and_validate_match():
    vault = biometric_vault.BiometricVault()
    template = b"\x01\x02\x03\x04\x05\x06\x07\x08" * 4
    vault.register_template(user_id=1, modality="fingerprint", template=template)
    result = vault.validate_template(user_id=1, modality="fingerprint", template=template)
    assert result["match"] is True
    assert result["confidence"] == 1.0


def test_biometric_validate_rejects_unknown_and_mismatched():
    vault = biometric_vault.BiometricVault()
    template = b"\x01\x02\x03\x04\x05\x06\x07\x08" * 4
    unknown = vault.validate_template(user_id=9, modality="face", template=template)
    assert unknown["match"] is False
    assert unknown["reason"] == "template_not_registered"

    vault.register_template(user_id=1, modality="face", template=template)
    other = bytes([x ^ 0xFF for x in template])
    result = vault.validate_template(user_id=1, modality="face", template=other)
    assert result["match"] is False
    assert result["reason"] == "below_threshold"
    assert 0.0 < result["confidence"] < 0.86


def test_biometric_revoke_and_list():
    vault = biometric_vault.BiometricVault()
    vault.register_template(user_id=1, modality="voice", template=b"voice-a" * 8)
    vault.register_template(user_id=2, modality="face", template=b"face-b" * 8)
    revoked = vault.revoke_template(user_id=1, modality="voice")
    assert revoked["revoked"] is True

    after = vault.validate_template(user_id=1, modality="voice", template=b"voice-a" * 8)
    assert after["match"] is False and after["reason"] == "template_revoked"

    assert len(vault.list_templates(user_id=2)) == 1
    with pytest.raises(ValueError):
        vault.revoke_template(user_id=9, modality="face")
    with pytest.raises(ValueError):
        vault.register_template(user_id=1, modality="iris", template=b"x")


def test_biometric_vault_catalog_shape():
    catalog = biometric_vault.build_biometric_vault_catalog(biometric_vault.BiometricVault())
    assert set(catalog["modalities"]) == {"fingerprint", "face", "voice"}
    assert catalog["storage"]["plaintext_templates"] is False
    assert catalog["template_count"] == 0


# --- contextual adaptive risk evaluation --------------------------------------


def test_risk_evaluator_flags_risky_context():
    risky = {
        "device_proven": False,
        "biometric_valid": False,
        "hsm_verified": False,
        "geo_velocity_kmh": 1200,
        "auth_velocity_per_min": 8,
        "odd_hour": True,
        "target_sensitivity": "high",
        "ip_reputation": "poor",
    }
    result = risk_evaluator.evaluate_risk(risky)
    assert result["score"] >= 80
    assert result["level"] in {"high", "critical"}
    assert all(f["present"] for f in result["factors"])


def test_risk_evaluator_benign_context_scores_low():
    benign = {
        "device_proven": True,
        "biometric_valid": True,
        "hsm_verified": True,
        "geo_velocity_kmh": 40,
        "auth_velocity_per_min": 1,
        "odd_hour": False,
        "target_sensitivity": "low",
        "ip_reputation": "good",
    }
    result = risk_evaluator.evaluate_risk(benign)
    assert result["score"] == 0
    assert result["level"] == "low"


def test_risk_adaptive_weights_drift_with_outcomes():
    risk_evaluator.reset_weight_state()
    base = risk_evaluator.current_weights()["new_device"]

    raised = risk_evaluator.adapt_risk_weight("new_device", "fraud")
    assert raised["weight"] > base

    lowered = risk_evaluator.adapt_risk_weight("new_device", "false_positive")
    assert lowered["weight"] == base

    with pytest.raises(ValueError):
        risk_evaluator.adapt_risk_weight("does_not_exist", "fraud")


def test_risk_evaluator_catalog_shape():
    risk_evaluator.reset_weight_state()
    catalog = risk_evaluator.build_risk_evaluator_catalog()
    assert [b["level"] for b in catalog["levels"]] == ["low", "moderate", "high", "critical"]
    assert "device_proven" in catalog["context_keys"]
    assert all("current_weight" in rule for rule in catalog["rules"])
    assert catalog["adaptive"]["weight_version"] >= 1


# --- data-cell access matrix --------------------------------------------------


def test_cell_access_respects_role_and_cell():
    assert cell_matrix.can_write_cell("customer_profile", "full_name", ["admin"])
    assert cell_matrix.can_read_cell("customer_profile", "phone", ["agent"])
    assert cell_matrix.cell_access("customer_profile", "phone", ["auditor"]) == "none"
    assert not cell_matrix.can_read_cell("customer_profile", "biometric_status", ["agent"])
    assert cell_matrix.can_read_cell("customer_profile", "risk_score", ["owner"])
    assert not cell_matrix.can_write_cell("customer_profile", "risk_score", ["owner"])


def test_cell_access_admin_group_implies_lower_roles():
    # admin group expands to admin+agent+auditor+owner -> writes everything.
    assert cell_matrix.can_write_cell("payments", "instrument_last4", ["admin"])
    # agent may read instrument_last4 but never full_instrument / audit detail.
    assert cell_matrix.can_read_cell("payments", "instrument_last4", ["agent"])
    assert not cell_matrix.can_read_cell("payments", "full_instrument", ["agent"])
    assert not cell_matrix.can_read_cell("audit_trail", "actor", ["agent"])


def test_evaluate_cell_matrix_returns_full_per_cell_map():
    matrix = cell_matrix.evaluate_cell_matrix("customer_profile", ["agent"])
    assert set(matrix) >= {"full_name", "email", "phone", "risk_score", "biometric_status"}
    assert matrix["full_name"] == "read"
    assert matrix["risk_score"] == "none"
    assert all(v in ("none", "read", "write") for v in matrix.values())


def test_cell_matrix_catalog_shape():
    catalog = cell_matrix.build_cell_matrix_catalog()
    assert set(catalog["resources"]) == {"customer_profile", "booking", "payments", "audit_trail"}
    assert catalog["access_levels"] == ["none", "read", "write"]
    assert "full_name" in catalog["cells"]["customer_profile"]


# --- metadata surfaces --------------------------------------------------------


def test_meta_zero_trust_and_scoring_catalog_document_surfaces():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    zero_trust = client.get("/meta/zero-trust")
    assert zero_trust.status_code == 200
    payload = zero_trust.json()
    assert set(payload) == {
        "name",
        "version",
        "environment",
        "generated_at",
        "hsm_signer",
        "biometric_vault",
        "risk_evaluator",
        "cell_matrix",
    }

    ecosystem = client.get("/meta/ecosystem").json()["subservices"]
    assert ecosystem["zero_trust"]["status"] == "ready"
    assert ecosystem["zero_trust"]["signer_algorithm"]

    scoring = client.get("/meta/scoring-catalog").json()["zero_trust"]
    assert set(scoring) == {"hsm_signer", "biometric_vault", "risk_evaluator", "cell_matrix"}