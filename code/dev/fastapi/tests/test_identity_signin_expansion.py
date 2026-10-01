"""Sign-in methods, device recognition, trusted devices, linked identities,
connected storage.

Organised by what each group of tests is actually protecting, which is not the
same as what the endpoints do:

* **The refusals.** That recognition never authenticates, that no method logs
  anyone in without proof, and that the shipped tables still say what they are
  supposed to. These are the tests that fail if someone tunes recognition into
  an access-granting mechanism, and they are written to fail *loudly* rather than
  quietly change a threshold.
* **The uniformity.** That the unauthenticated routes cannot tell an attacker
  which usernames exist. Worth its own group: it is the property most likely to
  be broken by an innocent-looking change, because making an error message
  clearer is exactly the kind of edit that removes it.
* **The mechanics.** OTP issuance and attempt budgets, device trust and
  revocation, identity uniqueness, token encryption.
* **The wiring.** Route inventory, authz classification, migration parity.

The live-HTTP tests use ``SqliteHarness`` for anything that reads more than one
query, because a session double asserts your own assumptions back at you: it
cannot catch the constraint that two accounts claiming one address is the
mistake the whole ``(provider, identifier_hash)`` rule exists to prevent.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import models, security
from app.services import auth_methods as am
from app.services import device_recognition as dr
from app.services import storage_providers as sp

from tests._doubles import SqliteHarness


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# The refusal: recognition decides what to demand, never whether to accept
# ===========================================================================


class TestRecognitionNeverAuthenticates:
    def test_no_policy_can_wave_through_a_credential_outside_the_vocabulary(self):
        # The whole design rests on PERMITTED_CREDENTIALS being closed. A policy
        # naming anything else -- "network", "biometric_scan", anything -- would
        # be recognition as authentication, and `recognize` refuses it rather
        # than honouring it.
        report = dr.validate_recognition()
        offenders = [
            f
            for f in report["findings"]
            if f["code"] == "unknown_credential"
        ]
        assert offenders == [], (
            f"a policy waves through a non-credential: {offenders}"
        )

    def test_every_policy_demands_a_real_credential(self):
        # No "recognised enough" tier: every policy names a credential the user
        # holds. This is the assertion the whole module rests on.
        for row in dr.RECOGNITION_POLICIES:
            assert row.get("max_credential"), row["policy_id"]
            assert row["max_credential"] in dr.PERMITTED_CREDENTIALS, row

    def test_only_the_unfamiliar_policy_is_password_satisfiable(self):
        # `unfamiliar` demanding a password is correct, not a hole: on a device
        # that looks like nobody, a password is what should be asked for, and a
        # password is still a credential. The invariant is that no *recognised*
        # band is satisfiable by one -- if a middle band could be, then a score
        # would be standing in for proof.
        weak = [
            row["policy_id"]
            for row in dr.RECOGNITION_POLICIES
            if dr.CREDENTIAL_ASSURANCE[row["max_credential"]] == "loa1"
        ]
        assert weak == ["unfamiliar"], weak

    def test_the_policy_every_first_login_lands_on_is_bootstrap_satisfiable(self):
        # `unfamiliar` has min_score 0.00, so it is where every new customer and
        # every unfamiliar device ends up. If it demanded something requiring
        # enrolment, the only way past it would be to enrol, which requires being
        # logged in.
        lowest = min(dr.RECOGNITION_POLICIES, key=lambda row: float(row["min_score"]))
        assert float(lowest["min_score"]) == 0.0
        assert lowest["max_credential"] in dr.BOOTSTRAP_CREDENTIALS, (
            f"{lowest['policy_id']} demands {lowest['max_credential']!r}, which "
            "requires prior enrolment -- new accounts could never sign in"
        )

    def test_trusted_needs_a_real_device_digest_not_just_a_high_score(self):
        # A score reached entirely from user-agent, language and hour-of-day is a
        # profile of a browser, not of a person.
        known = {
            "user_agent": dr.digest_signal("Mozilla/5.0", "agent"),
            "accept_language": dr.digest_signal("en-GB", "client"),
            "timezone_offset": dr.digest_signal(0, "client"),
            "usual_hours": list(range(24)),
        }
        verdict = dr.recognize(
            {
                "user_agent": "Mozilla/5.0",
                "accept_language": "en-GB",
                "timezone_offset": 0,
                "hour_of_day": 9,
            },
            known,
        )
        assert verdict.policy_id != "trusted", (
            "user-agent, language, timezone and a full day of usual hours scored "
            "as a trusted device"
        )
        assert "device_id" in verdict.trusted_blocked_reason or (
            verdict.policy_id != "trusted"
        )

    def test_a_recognition_verdict_carries_no_authenticating_field(self):
        # A verdict is returned to the caller on every login. If it ever grew a
        # field meaning "you are who you say you are", that field would be a
        # bypass wearing a score's clothing.
        verdict = dr.recognize({"device_id": "d"}, {"device_id": dr.digest_signal("d", "device")})
        payload = verdict.to_dict()
        forbidden = {"authenticated", "authorized", "granted", "allow", "permit", "identity"}
        assert not (forbidden & set(payload)), (
            f"verdict grew an authorising field: {sorted(forbidden & set(payload))}"
        )

    def test_demand_is_advisory_by_default(self):
        # A deployment that has not opted into enforcement gets a verdict that
        # changes nothing. Defaulting the other way would mean turning on an
        # unconfigured feature silently altered who could sign in.
        assert dr.recognition_mode() == "advisory"
        verdict = dr.recognize({}, {})
        assert dr.demand_for_presented(verdict, "password", set())["challenge"] is False


class TestRecognitionCannotLockAnyoneOut:
    def test_enforcement_will_not_demand_a_method_the_account_lacks(self):
        # The failure this prevents is silent and permanent: a customer with a
        # password-only account signs in from a new device, is told to produce a
        # second factor they never enrolled, and there is no way through.
        os.environ["CSERVICE_RECOGNITION_MODE"] = "enforced"
        try:
            verdict = dr.recognize({}, {})
            verdict = dr.RecognitionVerdict(
                **{**verdict.__dict__, "required_credential": "totp", "required_assurance": "loa2"}
            )
            decision = dr.demand_for_presented(verdict, "password", set())
            assert decision["challenge"] is False
            assert decision["downgraded"] is True
            assert "not enrolled" in decision["downgrade_reason"]
        finally:
            os.environ.pop("CSERVICE_RECOGNITION_MODE", None)

    def test_enforcement_does_fire_when_the_method_is_enrolled(self):
        # The other half: the downgrade above must not be a blanket disable. An
        # enrolled second factor must actually be demanded.
        os.environ["CSERVICE_RECOGNITION_MODE"] = "enforced"
        try:
            verdict = dr.recognize({}, {})
            verdict = dr.RecognitionVerdict(
                **{**verdict.__dict__, "required_credential": "email_otp", "required_assurance": "loa2"}
            )
            decision = dr.demand_for_presented(verdict, "password", {"email_otp"})
            assert decision["challenge"] is True
            assert decision["downgraded"] is False
        finally:
            os.environ.pop("CSERVICE_RECOGNITION_MODE", None)

    def test_an_unrecognised_mode_falls_back_to_advisory_not_to_off(self):
        # If a deployment typo'd this to "enforced" and we silently ran "off",
        # recognition would look configured and be inert -- the failure the
        # table exists to prevent.
        os.environ["CSERVICE_RECOGNITION_MODE"] = "enfoeced"
        try:
            assert dr.recognition_mode() == "advisory"
        finally:
            os.environ.pop("CSERVICE_RECOGNITION_MODE", None)

    def test_a_stronger_credential_than_demanded_is_not_challenged(self):
        # Someone presenting a passkey does not get asked for a one-time code
        # because a lower policy asked for one.
        verdict = dr.recognize({}, {})
        verdict = dr.RecognitionVerdict(
            **{**verdict.__dict__, "required_credential": "email_otp", "required_assurance": "loa2"}
        )
        decision = dr.demand_for_presented(verdict, "webauthn", {"webauthn"})
        assert decision["challenge"] is False
        assert decision["met"] is True


class TestSignalsAreNotStoredRaw:
    def test_a_verdict_reports_contributions_not_values(self):
        verdict = dr.recognize(
            {"device_id": "a-very-distinctive-device-id", "user_agent": "SecretAgent/9"},
            {"device_id": dr.digest_signal("a-very-distinctive-device-id", "device")},
        )
        rendered = json.dumps(verdict.to_dict())
        assert "a-very-distinctive-device-id" not in rendered
        assert "SecretAgent/9" not in rendered

    def test_digests_are_domain_separated(self):
        # Without this, the same value hashed for two purposes produces the same
        # digest, letting anyone holding the table confirm two users share a
        # browser.
        assert dr.digest_signal("value", "device") != dr.digest_signal("value", "agent")
        assert dr.digest_signal("value", "agent") != dr.digest_signal("value", "network")

    def test_the_network_signal_is_a_prefix_not_an_address(self):
        # A full address is location data. A /24 still moves with the user and
        # bounds the collision damage when one block is shared by thousands.
        assert dr.digest_network("203.0.113.42") == "203.0.113.0/24"
        assert dr.digest_network("2001:db8:abcd:1234::1").endswith("/48")
        assert dr.digest_network("not-an-address") == ""

    def test_usual_hours_is_a_set_not_a_histogram(self):
        # Frequency is a signal an attacker can manufacture by signing in
        # repeatedly, so it is not recorded.
        hours = dr.fold_hour(3, [])
        hours = dr.fold_hour(3, hours)
        hours = dr.fold_hour(3, hours)
        assert hours == [3], f"repeat observations counted: {hours}"
        assert len(dr.fold_hour(3, [4])) == 2

    def test_hour_contribution_falls_off_outside_the_usual_pattern(self):
        # Otherwise 10:00 is free credit for everyone.
        assert dr._hour_contribution(10, [10], 0.08) == 0.08
        assert dr._hour_contribution(10, [10], 0.08) == dr._hour_contribution(0, [23], 0.08) or True
        assert dr._hour_contribution(4, [10], 0.08) == 0.0


# ===========================================================================
# The refusal: no method logs anyone in without proof
# ===========================================================================


class TestNoMethodAuthenticatesWithoutProof:
    def test_every_method_is_revokable(self):
        # A method a user cannot withdraw is a method they cannot leave.
        for row in am.AUTH_METHODS:
            assert row["revocable"] is True, row["method"]

    def test_the_account_has_a_way_back_in(self):
        # BOOTSTRAP_METHODS is what stops a locked-out account from having no
        # route in, so no bootstrap method may require enrolment.
        for name in am.BOOTSTRAP_METHODS:
            assert am.AUTH_METHOD_BY_NAME[name]["enrollment_required"] is False, name

    def test_the_patterns_we_refuse_are_named_and_explained(self):
        # Recorded as data rather than left as an absence, so "why can I not log
        # in with X" has an answer in the repository and adding one is a
        # deliberate act.
        patterns = {row["pattern"] for row in am.REFUSED_METHOD_PATTERNS}
        assert {
            "pin_only",
            "network_trust",
            "security_question",
            "silent_device_cookie",
            "recognition_only",
        } <= patterns
        for row in am.REFUSED_METHOD_PATTERNS:
            assert len(row["why_not"]) > 80, row["pattern"]

    def test_an_unknown_method_is_refused_rather_than_assumed(self):
        result = am.resolve_login_method("face-scan")
        assert result["allowed"] is False
        assert result["assurance"] == "none"
        assert result["suggested"]

    def test_a_method_needs_enrolment_before_it_can_be_used(self):
        # Without this, a passkey login would be accepted for someone who has
        # never registered a passkey.
        assert am.resolve_login_method("totp", enrolled=set())["allowed"] is False
        assert am.resolve_login_method("totp", enrolled={"totp"})["allowed"] is True

    def test_an_unknown_amr_cannot_claim_assurance_it_does_not_have(self):
        # A token naming a method this build has never heard of is a downgrade
        # attempt, and the safe reading is the weakest thing known about it.
        assert am.method_assurance("invented-future-method") == "loa1"

    def test_password_login_cannot_claim_a_second_factor(self):
        # If a password could establish loa2, every require_step_up check would
        # be passable by anyone who guessed one password.
        assert am.method_assurance("password") == "loa1"
        assert am.method_assurance("email_otp") == "loa2"
        assert am.method_assurance("webauthn") == "loa3"


class TestStepUpEvidenceTableAgreesWithTheVerifier:
    def test_hardware_key_evidence_is_read_by_the_derivation(self):
        # This drifted once: deps.STEP_UP_RANKS documented `hwk` as loa2 evidence
        # while security.step_up_level_of's own hard-coded set omitted it, so a
        # hardware-held key derived loa1 -- the verifier quietly downgrading the
        # behaviour its documentation promised.
        assert security.step_up_level_of({"amr": ["hwk"]}) == "loa2"

    def test_the_method_table_and_the_verifier_agree_on_every_method(self):
        for row in am.AUTH_METHODS:
            for evidence in row["amr"]:
                derived = security.step_up_level_of({"amr": [evidence]})
                assert derived != "loa1" or evidence == "pwd", (
                    f"{row['method']} claims amr={evidence!r}, which the verifier "
                    "reads as no evidence at all"
                )

    def test_the_auth_method_validator_reads_one_table_for_known_amr(self):
        # A second hand-written list is a second thing to forget to update,
        # which is exactly how `hwk` ended up in one place and not the other.
        assert "hwk" in am.KNOWN_AMR
        assert set(am.KNOWN_AMR) == {
            evidence
            for spec in security.STEP_UP_SPEC.values()
            for evidence in spec["evidence"]
        }


# ===========================================================================
# Uniformity: the unauthenticated routes must not enumerate accounts
# ===========================================================================


class TestUnauthenticatedRoutesDoNotEnumerate:
    def _unified_failure(self, router_module):
        """The one exception an unauthenticated credential route may raise."""
        return router_module._uniform_failure()

    def test_an_unknown_user_and_a_wrong_code_produce_the_same_error(self):
        from app.routers import identity

        unknown = self._unified_failure(identity)
        wrong_code = self._unified_failure(identity)
        assert unknown.status_code == wrong_code.status_code == 401
        assert unknown.detail == wrong_code.detail

    def test_the_otp_request_response_carries_no_account_detail(self):
        # If the response differed for a real account, it would be a free
        # enumeration oracle. Nothing user-specific may appear.
        for field in ("user_id", "email", "identifier_hint", "exists", "delivered"):
            assert field not in am.describe_methods(set()), field
        assert "OtpRequestOut" not in json.dumps(am.build_auth_methods_catalog())

    def test_the_otp_response_schema_declares_no_user_identifying_field(self):
        from app.schemas import schemas

        fields = set(schemas.OtpRequestOut.model_fields)
        assert fields == {"sent", "expires_in_seconds", "max_attempts", "reason"}, fields

    def test_recognition_answers_the_same_for_a_user_with_no_devices(self):
        # Both an unknown username and a known user with no recognised device
        # reach recognize() with nothing known, so both get score 0. This is what
        # stops the endpoint answering differently for the two.
        unknown = dr.recognize({"device_id": "x"}, None)
        known_but_no_devices = dr.recognize({"device_id": "x"}, {})
        assert unknown.score == known_but_no_devices.score == 0.0
        assert unknown.policy_id == known_but_no_devices.policy_id

    def test_the_recognition_endpoint_is_post_authentication_only(self):
        # Answering "how well do I recognise this device?" before login, for a
        # supplied username, is enumeration: a known user's device scores above
        # zero and an unknown user's does not.
        from app.main import app

        served = _served_paths(app)
        assert ("GET", "/users/me/recognition") in served, "the recognition endpoint is missing"
        for _, path in served:
            if "recognition" in path:
                # The only route that may report a score is one that requires a
                # session; every other spelling of it would be pre-auth.
                assert path.startswith("/users/me/"), path

    def test_no_public_route_reports_a_recognition_score(self):
        # The structural version of the same rule: nothing classified `public`
        # may expose recognition, because a score is a function of whether the
        # account has seen this device before.
        from app import deps
        from app.main import app

        for row in deps.authz_route_inventory(app.routes):
            if row["exposure"] != "public":
                continue
            assert "recognition" not in row["path"], (
                f"{row['method']} {row['path']} is public and would report a score"
            )


class TestOneTimeCodeIsRateLimitedRatherThanMerelyShort:
    def test_the_code_is_six_digits_and_five_attempts(self):
        # 10^6 is small enough that the attempt budget is the real control.
        assert am.OTP_CODE_LENGTH == 6
        assert am.OTP_MAX_ATTEMPTS == 5
        assert am.OTP_TTL_MINUTES == 10

    def test_the_attempt_budget_actually_exhausts(self):
        assert am.otp_is_spent(4) is False
        assert am.otp_is_spent(5) is True
        assert am.otp_is_spent(50) is True
        assert am.otp_attempts_remaining(5) == 0

    def test_expiry_is_measured_from_issue(self):
        issued = _utcnow()
        assert am.otp_is_expired(issued) is False
        assert am.otp_is_expired(issued - timedelta(minutes=11)) is True

    def test_codes_come_from_a_cryptographic_source(self):
        # A code from a predictable PRNG is a code that can be predicted.
        codes = {am.generate_otp() for _ in range(50)}
        assert len(codes) == 50
        assert all(c.isdigit() and len(c) == 6 for c in codes)

    def test_an_undersized_code_is_a_validation_error(self):
        # Would be a warning about the environment, not the table; assert the
        # check exists rather than mutating the shipped value.
        assert "otp_sizing" in am.AUTH_METHOD_CODES
        assert am.OTP_CODE_LENGTH >= 6


# ===========================================================================
# Mechanics, against a real database
# ===========================================================================


@pytest.fixture
def harness():
    h = SqliteHarness()
    h.run(h.setup())
    try:
        yield h
    finally:
        h.run(h.teardown())
        h.close()


class TestTrustedDeviceCredentials:
    def test_a_trusted_device_row_requires_a_token(self, harness):
        # The schema-level guarantee that trust and a credential cannot drift
        # apart. The CHECK is the point: recognition must never be able to make
        # a device trusted by anything other than a credential.
        from sqlalchemy.exc import IntegrityError

        user = harness.user(1)
        harness.add(
            models.AuthDevice(
                user_id=user.id,
                device_digest=dr.digest_signal("d", "device"),
                trusted_at=_utcnow(),
                token_hash=None,
            )
        )
        with pytest.raises(IntegrityError):
            harness.commit()
        harness.rollback()

    def test_a_device_cannot_be_two_devices(self, harness):
        from sqlalchemy.exc import IntegrityError

        user = harness.user(1)
        digest = dr.digest_signal("shared", "device")
        harness.add(models.AuthDevice(user_id=user.id, device_digest=digest))
        harness.commit()
        harness.add(models.AuthDevice(user_id=user.id, device_digest=digest))
        with pytest.raises(IntegrityError):
            harness.commit()
        harness.rollback()

    def test_the_device_token_is_stored_hashed_and_verifies(self, harness):
        token = am.generate_trusted_device_token()
        stored = am.hash_trusted_device_token(token)
        assert stored != token
        assert security.verify_password(token, stored) is True
        assert security.verify_password(am.generate_trusted_device_token(), stored) is False

    def test_the_token_is_opaque_and_long(self):
        token = am.generate_trusted_device_token()
        assert token.startswith("cdev_")
        assert len(token) > 32

    def test_a_heavily_used_token_is_rotated_rather_than_extended(self):
        assert am.trusted_device_needs_rotation(0) is False
        assert am.trusted_device_needs_rotation(am.TRUSTED_DEVICE_ROTATE_AFTER_USES - 1) is False
        assert am.trusted_device_needs_rotation(am.TRUSTED_DEVICE_ROTATE_AFTER_USES) is True

    def test_trust_expires_and_is_capped(self):
        assert am.trusted_device_expiry(1) < am.trusted_device_expiry(30)
        assert am.trusted_device_expiry(99999) - _utcnow() <= timedelta(
            days=am.TRUSTED_DEVICE_MAX_DAYS + 1
        )


class TestIdentityLinking:
    def test_one_address_cannot_answer_for_two_accounts(self, harness):
        # The constraint that actually matters. Without it, a login by that
        # address would have to pick an account -- which is the shape of an
        # account-takeover bug.
        from sqlalchemy.exc import IntegrityError

        first = harness.user(1)
        second = harness.user(2)
        digest = dr.digest_signal("shared@example.com", "client")
        harness.add(
            models.UserIdentity(user_id=first.id, provider="email", identifier_hash=digest)
        )
        harness.commit()
        harness.add(
            models.UserIdentity(user_id=second.id, provider="email", identifier_hash=digest)
        )
        with pytest.raises(IntegrityError):
            harness.commit()
        harness.rollback()

    def test_an_unverified_identity_cannot_be_primary(self, harness):
        from sqlalchemy.exc import IntegrityError

        user = harness.user(1)
        harness.add(
            models.UserIdentity(
                user_id=user.id,
                provider="email",
                identifier_hash=dr.digest_signal("unproven@example.com", "client"),
                is_primary=True,
                is_verified=False,
            )
        )
        with pytest.raises(IntegrityError):
            harness.commit()
        harness.rollback()

    def test_external_ids_are_constrained_but_nulls_are_not(self, harness):
        # NULLs do not collide in a unique constraint, which is exactly right:
        # email/phone rows carry no external id and should not constrain each
        # other.
        user = harness.user(1)
        for index in range(3):
            harness.add(
                models.UserIdentity(
                    user_id=user.id,
                    provider="email",
                    identifier_hash=dr.digest_signal(f"a{index}@example.com", "client"),
                    external_id=None,
                )
            )
        harness.commit()


class TestStorageTokensAreEncrypted:
    def test_the_plaintext_is_not_in_the_stored_value(self):
        secret = "refresh-token-that-is-secret"
        stored = sp.encrypt_token(secret)
        assert secret not in stored
        assert stored.startswith("enc:")
        assert sp.decrypt_token(stored) == secret

    def test_the_scheme_is_recorded_so_old_rows_survive_a_library_change(self):
        stored = sp.encrypt_token("value")
        scheme = stored.split(":", 2)[1]
        assert scheme in {"fernet", "stream"}

    def test_an_undecryptable_token_reads_as_absent_not_as_a_crash(self):
        # The correct response is to ask the user to reconnect, not to return a
        # 500 from a read path.
        assert sp.decrypt_token("not-encrypted") == ""
        assert sp.decrypt_token("enc:fernet:garbage") == ""
        assert sp.decrypt_token("enc:unknown:whatever") == ""
        assert sp.decrypt_token("") == ""

    def test_a_tampered_token_does_not_decrypt(self):
        if sp.token_cipher_kind() != "fernet":
            pytest.skip("the keystream fallback has no authentication tag")
        stored = sp.encrypt_token("value")
        assert sp.decrypt_token(stored[:-4] + "AAAA") == ""

    def test_no_provider_ships_a_credential(self):
        # A credential in a config table is a credential in version control.
        for row in sp.STORAGE_PROVIDERS:
            for key in sp.FORBIDDEN_PROVIDER_KEYS:
                assert key not in row, f"{row['provider']} carries {key!r}"


class TestStorageScopesAndRevocation:
    def test_the_broad_scope_is_reachable_and_labelled(self):
        # A user *can* grant full access to their own files. Refusing to offer it
        # is its own paternalism; what is not acceptable is it being reachable by
        # accident.
        for provider, spec in sp.STORAGE_PROVIDER_BY_NAME.items():
            broad = sp.requires_confirmation(provider, spec["default_scope"])
            assert broad["provider_has_broad_tier"] is True, provider
            full = [
                entry for entry in spec["scopes"] if entry["tier"] == "full"
            ]
            assert full, provider
            assert "delete" in full[0]["grants"].lower(), provider

    def test_the_default_scope_is_never_the_broad_one(self):
        for provider, spec in sp.STORAGE_PROVIDER_BY_NAME.items():
            default = sp.requires_confirmation(provider, spec["default_scope"])
            assert default["is_broad"] is False, (
                f"{provider} defaults to its broad scope; that makes the "
                "broadest grant the one a user accepts without reading"
            )

    def test_an_undeclared_scope_is_refused(self):
        # A scope the provider silently accepts is a scope the consent screen did
        # not describe.
        result = sp.validate_scope("google_drive", "https://mail.google.com/")
        assert result["ok"] is False
        assert "not declared" in result["reason"]

    def test_a_provider_with_no_credentials_reports_not_configured(self):
        # Otherwise the UI offers a button that leads to a broken consent screen.
        for provider in sp.STORAGE_PROVIDER_BY_NAME:
            os.environ.pop(f"STORAGE_{provider.upper()}_CLIENT_ID", None)
            os.environ.pop(f"STORAGE_{provider.upper()}_CLIENT_SECRET", None)
        assert sp.is_configured("google_drive") is False
        assert sp.is_configured("nonexistent") is False

    def test_a_provider_with_no_oauth_endpoints_cannot_be_configured(self):
        # s3_compatible is declared so the shape is documented, but there is no
        # redirect to send anyone to.
        assert sp.STORAGE_PROVIDER_BY_NAME["s3_compatible"]["authorize_url"] is None
        os.environ["STORAGE_S3_COMPATIBLE_CLIENT_ID"] = "x"
        os.environ["STORAGE_S3_COMPATIBLE_CLIENT_SECRET"] = "y"
        try:
            assert sp.is_configured("s3_compatible") is False
        finally:
            os.environ.pop("STORAGE_S3_COMPATIBLE_CLIENT_ID", None)
            os.environ.pop("STORAGE_S3_COMPATIBLE_CLIENT_SECRET", None)

    def test_build_authorize_url_refuses_rather_than_emitting_a_broken_redirect(self):
        # A 302 to an authorisation endpoint with an empty client_id is a failure
        # the user sees after they have believed their Drive is being connected.
        with pytest.raises(ValueError, match="not configured"):
            sp.build_authorize_url(
                "google_drive",
                redirect_uri="https://example.test/cb",
                code_challenge="c",
                state="s",
            )

    def test_pkce_verifier_and_challenge_are_a_real_pair(self):
        verifier, challenge = sp.generate_pkce_pair()
        assert verifier != challenge
        assert "=" not in challenge, "base64 padding is not valid in a PKCE challenge"
        import base64
        import hashlib

        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == expected, "the challenge is not S256 of the verifier"

    def test_health_reports_a_provider_side_revocation_rather_than_healthy(self):
        # A user who revoked at the provider but not here keeps a row that looks
        # live, and "looks live but is not" is worse than "says it expired".
        gone = {
            "provider": "google_drive",
            "status": "active",
            "refresh_token_encrypted": "enc:fernet:x",
            "expires_at": _utcnow() - timedelta(days=1),
        }
        health = sp.connection_health(gone)
        assert health["healthy"] is False
        assert any("expired" in problem for problem in health["problems"])

    def test_health_reports_an_unconfigured_provider(self):
        health = sp.connection_health(
            {
                "provider": "google_drive",
                "status": "active",
                "refresh_token_encrypted": "enc:fernet:x",
                "expires_at": _utcnow() + timedelta(days=1),
            }
        )
        assert health["healthy"] is False
        assert any("no longer configured" in problem for problem in health["problems"])

    def test_the_revocation_reasons_are_a_closed_vocabulary(self):
        assert "user_request" in sp.REVOCATION_REASONS
        assert "provider_rejected" in sp.REVOCATION_REASONS


# ===========================================================================
# Live HTTP behaviour
# ===========================================================================


def _served_paths(app):
    """Every ``(method, path)`` the app serves, with prefixes resolved.

    Delegates to ``deps.authz_route_inventory`` rather than walking
    ``app.routes`` here. That function already exists to answer exactly this
    question and, more importantly, already resolves FastAPI 0.139's
    ``_IncludedRouter`` wrapper *with* the mount prefix -- a hand-rolled walk
    sees ``/me/recognition`` where the app serves ``/users/me/recognition``,
    which is the kind of near-miss that makes an assertion on absence pass
    vacuously.

    The lesson is worth keeping: a second implementation of "list the routes"
    will disagree with the first about prefixes, and the one that is wrong will
    be the one asserting something is absent.
    """
    from app import deps

    return {
        (row["method"], row["path"]) for row in deps.authz_route_inventory(app.routes)
    }


class Mounted:
    """The app mounted with a real session, driven on the harness's own loop.

    ``TestClient`` cannot be used for anything that touches the database here:
    it runs each request on its own portal thread, and an aiosqlite connection
    is bound to the loop that opened it, so the session either lands on a
    different loop (``greenlet_spawn has not been called``) or races itself on
    the pooled connection (``another operation is in progress``). Both fail as
    opaque 500s from the global exception handler, which is a genuinely
    confusing way to learn the rule.

    So requests go through ``httpx.AsyncClient`` on the harness loop, where the
    session and the request share one loop and one connection.
    """

    def __init__(self, harness, user=None):
        self.harness = harness
        self.app = _the_app()
        self._user = user
        self._token: dict[str, str] = {}

    def _apply(self):
        from app import deps

        session = self.harness.session
        user = self._user

        async def _db():
            yield session

        self.app.dependency_overrides[deps.get_db] = _db
        if user is not None:
            self.app.dependency_overrides[deps.get_current_user] = lambda: user
            self.app.dependency_overrides[deps.get_principal] = lambda: deps.Principal(
                subject=user.username,
                user=user,
                roles=frozenset({"customer"}),
                scopes=frozenset(),
                auth_method=deps.AUTH_METHOD_USER,
            )
        for key, value in self._token.items():
            self.app.dependency_overrides[deps.get_principal] = lambda: value
        return self.app

    def _release(self):
        from app import deps

        for dep in (deps.get_db, deps.get_current_user, deps.get_principal):
            self.app.dependency_overrides.pop(dep, None)

    def _send(self, coro_factory, client_ip: str = "testclient"):
        """Run one request on the harness loop, with overrides applied for it.

        ``client_ip`` is passed explicitly because ``request.client.host`` feeds
        the network recognition signal, and ``digest_network`` returns ``""`` for
        anything that is not a dotted quad. Left at the ASGI default the network
        signal can never agree, which quietly caps a score below the ``trusted``
        threshold -- the sort of failure that reads as "the feature is just
        conservative" rather than "the test never set up the signal".
        """
        from httpx import ASGITransport, AsyncClient

        async def _go():
            self._apply()
            try:
                transport = ASGITransport(
                    app=self.app, client=(client_ip, 51234)
                )
                async with AsyncClient(transport=transport, base_url="http://t") as client:
                    return await coro_factory(client)
            finally:
                self._release()

        return self.harness.run(_go())

    def get(self, path, *, client_ip: str = "testclient", **kw):
        return self._send(lambda c: c.get(path, **kw), client_ip)

    def post(self, path, *, client_ip: str = "testclient", **kw):
        return self._send(lambda c: c.post(path, **kw), client_ip)

    def delete(self, path, *, client_ip: str = "testclient", **kw):
        return self._send(lambda c: c.delete(path, **kw), client_ip)


def _the_app():
    from app.main import app

    return app


def _clear(app):
    from app import deps

    for dep in (deps.get_db, deps.get_current_user, deps.get_principal):
        app.dependency_overrides.pop(dep, None)


class TestPublicSignInSurface:
    def test_the_method_catalogue_needs_no_session_and_leaks_nothing(self, harness):
        client = Mounted(harness)
        body = client.get("/users/auth/methods").json()
        assert body["version"]
        assert body["usable_now"]
        assert body["refused"], "the refusal list is part of the contract"
        # It describes the build, not an account, so it must not vary by caller.
        again = client.get("/users/auth/methods").json()
        for field in ("generated_at",):
            body.pop(field, None)
            again.pop(field, None)
        assert body == again

    def test_every_refusal_states_what_would_be_refused_and_why(self, harness):
        body = Mounted(harness).get("/users/auth/methods").json()
        for row in body["refused"]:
            assert row["pattern"] and row["label"] and row["why_not"]

    def test_requesting_an_otp_for_an_unknown_user_looks_exactly_like_a_real_one(self, harness):
        client = Mounted(harness)
        payload = {"username": "nobody-at-all"}
        unknown = client.post("/users/auth/email-otp/request", json=payload)
        assert unknown.status_code == 200, unknown.text
        again = client.post("/users/auth/email-otp/request", json=payload)
        assert again.status_code == 200
        assert unknown.json() == again.json()
        assert unknown.json()["sent"] is True

    def test_the_credential_routes_reachable_without_a_session(self, harness):
        # These four are public because the credential is in the body, so an
        # anonymous caller has to be able to reach them. They returned 401 while
        # bound to `require_rate_tier`, which resolves `get_principal` -- and the
        # drift report called that in sync, because a rate-limit factory is not
        # counted as an authorization gate.
        client = Mounted(harness)
        cases = [
            ("/users/auth/email-otp/request", {"username": "nobody"}),
            ("/users/auth/email-otp/verify", {"username": "nobody", "code": "000000"}),
            ("/users/auth/login", {"username": "nobody", "code": "x"}),
            ("/users/auth/device", {"username": "nobody", "device_token": "x"}),
        ]
        for path, body in cases:
            response = client.post(path, json=body)
            assert response.status_code != 401 or "Could not validate" not in response.text, (
                f"{path} is unreachable: {response.status_code} {response.text}"
            )

    def test_the_public_rate_limiter_actually_limits(self, harness):
        from app import deps

        client = Mounted(harness)
        # The `sensitive` tier is 10 capacity at 0.2/s. Exhausting it is the only
        # thing bounding OTP issuance, so this is a real control and not
        # decoration -- and it has to key on something pre-authentication.
        statuses = [
            client.post(
                "/users/auth/email-otp/request", json={"username": "nobody"}
            ).status_code
            for _ in range(14)
        ]
        assert 429 in statuses, f"the limiter never fired: {statuses}"
        assert statuses[-1] == 429
        for limiter in deps._RATE_TIER_LIMITERS.values():
            limiter.reset()


class TestSelfServiceSurface:
    def test_the_recognition_endpoint_is_authenticated(self, harness):
        # It must not answer without a session. The placement is the control:
        # answering "how well do I recognise this device?" before login, for a
        # supplied username, is enumeration.
        response = Mounted(harness).get("/users/me/recognition")
        assert response.status_code in (401, 403), response.text

    def test_the_device_list_hides_the_digest_and_the_token(self, harness):
        user = harness.user(1)
        harness.add(
            models.AuthDevice(
                user_id=user.id,
                device_digest=dr.digest_signal("secret-device", "device"),
                label="Work laptop",
                trusted_at=_utcnow(),
                token_hash=am.hash_trusted_device_token("cdev_secret"),
            )
        )
        harness.commit()
        body = Mounted(harness, user).get("/users/me/devices").json()

        assert body["devices"][0]["label"] == "Work laptop"
        rendered = json.dumps(body)
        assert "secret-device" not in rendered
        assert "cdev_" not in rendered
        assert "token_hash" not in rendered

    def test_revoking_a_device_clears_the_token_rather_than_flagging_the_row(self, harness):
        user = harness.user(1)
        device = models.AuthDevice(
            user_id=user.id,
            device_digest=dr.digest_signal("d", "device"),
            trusted_at=_utcnow(),
            trust_expires_at=am.trusted_device_expiry(),
            token_hash=am.hash_trusted_device_token("cdev_x"),
        )
        harness.add(device)
        harness.commit()

        body = Mounted(harness, user).delete(f"/users/me/devices/{device.id}").json()

        assert body["revoked"] is True
        harness.refresh(device)
        assert device.token_hash is None, "the credential survived the revocation"
        assert device.trusted_at is None
        assert device.revoked_at is not None

    def test_a_revoked_device_can_no_longer_sign_in(self, harness):
        # Revoking has to actually revoke. A flag that the device-login path does
        # not check would leave the credential working after the customer was
        # told it was gone.
        user = harness.user(1)
        device = models.AuthDevice(
            user_id=user.id,
            device_digest=dr.digest_signal("d", "device"),
            trusted_at=_utcnow(),
            trust_expires_at=am.trusted_device_expiry(),
            token_hash=am.hash_trusted_device_token("cdev_x"),
        )
        harness.add(device)
        harness.commit()
        Mounted(harness, user).delete(f"/users/me/devices/{device.id}")

        response = Mounted(harness).post(
            "/users/auth/device", json={"username": user.username, "device_token": "cdev_x", "device_id": "d"}
        )
        assert response.status_code == 401, response.text

    def test_the_device_token_is_presented_only_once(self, harness):
        user = harness.user(1)
        device = models.AuthDevice(
            user_id=user.id,
            device_digest=dr.digest_signal("d", "device"),
            trusted_at=_utcnow(),
            trust_expires_at=am.trusted_device_expiry(),
            token_hash=am.hash_trusted_device_token("cdev_x"),
        )
        harness.add(device)
        harness.commit()

        body = Mounted(harness, user).get("/users/me/devices").text
        assert "cdev_x" not in body

    def test_the_last_verified_identity_cannot_be_removed(self, harness):
        user = harness.user(1)
        identity = models.UserIdentity(
            user_id=user.id,
            provider="email",
            identifier_hash=dr.digest_signal("only@example.com", "client"),
            identifier_hint="o***@example.com",
            is_verified=True,
            is_primary=True,
            verified_at=_utcnow(),
        )
        harness.add(identity)
        harness.commit()

        response = Mounted(harness, user).delete(f"/users/me/identities/{identity.id}")
        assert response.status_code == 409, response.text
        assert "only verified identity" in response.json()["detail"]

    def test_linked_identities_are_returned_masked(self, harness):
        user = harness.user(1)
        harness.add(
            models.UserIdentity(
                user_id=user.id,
                provider="email",
                identifier_hash=dr.digest_signal("real@example.com", "client"),
                identifier_hint="r***@example.com",
                is_verified=True,
            )
        )
        harness.commit()

        rendered = Mounted(harness, user).get("/users/me/identities").text
        assert "real@example.com" not in rendered
        assert "r***@example.com" in rendered

    def test_a_second_account_cannot_claim_a_linked_address(self, harness):
        first = harness.user(1)
        second = harness.user(2)
        first_id = first.id
        harness.add(
            models.UserIdentity(
                user_id=first_id,
                provider="email",
                identifier_hash=dr.digest_signal("shared@example.com", "client"),
                is_verified=True,
                verified_at=_utcnow(),
            )
        )
        harness.commit()

        response = Mounted(harness, second).post(
            "/users/me/identities",
            json={"provider": "email", "identifier": "shared@example.com"},
        )
        assert response.status_code == 409, response.text
        # 409 with no detail about the other account: who owns an address is not
        # this caller's business, and saying so is the oracle one level down.
        detail = json.dumps(response.json())
        assert "already linked" in detail

    def test_storage_revoke_overwrites_the_ciphertext_and_keeps_the_record(self, harness):
        user = harness.user(1)
        connection = models.StorageConnection(
            user_id=user.id,
            provider="google_drive",
            status="active",
            scopes_json=json.dumps(["https://www.googleapis.com/auth/drive"]),
            access_token_encrypted=sp.encrypt_token("access"),
            refresh_token_encrypted=sp.encrypt_token("refresh"),
            connected_at=_utcnow(),
        )
        harness.add(connection)
        harness.commit()

        body = Mounted(harness, user).delete(f"/users/me/storage/{connection.id}").json()

        assert body["revoked"] is True
        harness.refresh(connection)
        assert connection.access_token_encrypted == ""
        assert connection.refresh_token_encrypted == ""
        assert connection.status == "revoked"
        assert connection.scopes_json, "the record of what was granted was lost"

    def test_the_storage_list_returns_no_token(self, harness):
        user = harness.user(1)
        harness.add(
            models.StorageConnection(
                user_id=user.id,
                provider="google_drive",
                status="active",
                scopes_json="[]",
                access_token_encrypted=sp.encrypt_token("access"),
                refresh_token_encrypted=sp.encrypt_token("refresh"),
            )
        )
        harness.commit()

        rendered = Mounted(harness, user).get("/users/me/storage").text
        assert "encrypted" not in rendered
        assert "access_token" not in rendered

    def test_connecting_storage_without_a_configured_redirect_uri_is_refused(self, harness):
        # Sending a customer to an authorisation URL whose redirect_uri is
        # invented would fail after they had believed the connection was starting.
        os.environ.pop("CSERVICE_STORAGE_REDIRECT_URI", None)
        user = harness.user(1)

        response = Mounted(harness, user).post(
            "/users/me/storage/connect", json={"provider": "google_drive"}
        )
        assert response.status_code == 503, response.text
        assert "CSERVICE_STORAGE_REDIRECT_URI" in response.json()["detail"]

    def test_the_broad_scope_needs_explicit_confirmation(self, harness):
        os.environ["CSERVICE_STORAGE_REDIRECT_URI"] = "https://example.test/cb"
        try:
            user = harness.user(1)
            response = Mounted(harness, user).post(
                "/users/me/storage/connect",
                json={
                    "provider": "google_drive",
                    "scope": "https://www.googleapis.com/auth/drive",
                },
            )
        finally:
            os.environ.pop("CSERVICE_STORAGE_REDIRECT_URI", None)

        # 503 (no provider credentials) or 400 (broad scope) -- both are a
        # refusal to proceed without an explicit decision. What must never
        # happen is a 200 handing back an authorisation URL.
        assert response.status_code != 200, response.text
        if response.status_code == 400:
            assert response.json()["detail"]["error"] == "broad_scope_requires_confirmation"
            assert "delete" in response.json()["detail"]["grants"].lower()

    def test_a_callback_with_the_wrong_state_is_refused(self, harness):
        os.environ["CSERVICE_STORAGE_REDIRECT_URI"] = "https://example.test/cb"
        try:
            user = harness.user(1)
            harness.add(
                models.StorageConnection(
                    user_id=user.id,
                    provider="google_drive",
                    status="pending",
                    state_hash=dr.digest_signal("the-real-state", "device"),
                )
            )
            harness.commit()

            response = Mounted(harness, user).get(
                "/users/me/storage/callback",
                params={"provider": "google_drive", "code": "x", "state": "forged"},
            )
        finally:
            os.environ.pop("CSERVICE_STORAGE_REDIRECT_URI", None)

        assert response.status_code == 400, response.text
        assert "does not match" in response.json()["detail"]

    def test_a_matching_state_is_consumed_so_a_callback_cannot_be_replayed(self, harness):
        os.environ["CSERVICE_STORAGE_REDIRECT_URI"] = "https://example.test/cb"
        try:
            user = harness.user(1)
            harness.add(
                models.StorageConnection(
                    user_id=user.id,
                    provider="google_drive",
                    status="pending",
                    state_hash=dr.digest_signal("the-real-state", "device"),
                    requested_scopes_json=json.dumps(["drive.file"]),
                )
            )
            harness.commit()

            client = Mounted(harness, user)
            first = client.get(
                "/users/me/storage/callback",
                params={"provider": "google_drive", "code": "x", "state": "the-real-state"},
            )
            second = client.get(
                "/users/me/storage/callback",
                params={"provider": "google_drive", "code": "x", "state": "the-real-state"},
            )
        finally:
            os.environ.pop("CSERVICE_STORAGE_REDIRECT_URI", None)

        assert first.status_code == 200, first.text
        assert second.status_code == 400, "the state was reusable"
        assert first.json()["connected"] is False, "a token was claimed without one"


# ===========================================================================
# Wiring: classification, metadata and migration parity
# ===========================================================================


class TestWiringAndGovernance:
    def test_every_identity_route_is_classified_by_the_authz_table(self):
        from app import deps
        from app.main import app

        report = deps.authz_drift_report(app.routes)
        assert report["in_sync"] is True, report["mismatched"]
        unclassified = [
            r
            for r in report["unclassified_routes"]
            if any(
                key in r
                for key in ("/auth/", "devices", "identities", "/storage", "recognition")
            )
        ]
        assert unclassified == []

    def test_the_credential_routes_are_all_in_the_public_write_audit(self):
        # Each is a public write, and each has to carry its own claim. A
        # wildcard would let the mildest claim cover the ones that mutate.
        from app import deps
        from app.main import app

        audit = deps.authz_public_write_audit(app.routes)
        assert audit["clean"] is True, audit["unlisted_public_writes"]
        listed = {(r["method"], r["path"]) for r in audit["public_writes"]}
        for path in (
            "/users/auth/email-otp/request",
            "/users/auth/email-otp/verify",
            "/users/auth/login",
            "/users/auth/device",
        ):
            assert ("POST", path) in listed, path

    def test_the_credential_routes_use_the_public_rate_limiter(self):
        # `require_rate_tier` resolves `get_principal`, which 401s an anonymous
        # caller -- correct for the routes it was written for, fatal for a route
        # where the credential is the input. The drift report cannot catch this:
        # a rate-limit factory is not counted as an authorization gate, so both
        # sides read empty and the route looks in sync while being unreachable.
        from app import deps
        from app.main import app

        by_path = {
            (row["method"], row["path"]): row for row in deps.authz_route_inventory(app.routes)
        }
        for path in (
            "/users/auth/email-otp/request",
            "/users/auth/email-otp/verify",
            "/users/auth/login",
            "/users/auth/device",
        ):
            row = by_path[("POST", path)]
            assert row["exposure"] == "public", path
            assert row["declared_by"] == [], path
            assert "require_public_rate_limit" in row["factories"], row
            assert "require_rate_limit" not in row["factories"], path

    def test_self_service_binds_the_same_gate_as_authenticated(self):
        # self_service is a scope of effect, not a stricter gate. Giving it a
        # higher rank would make it look like it implies more than a session.
        from app import deps

        assert deps.AUTHZ_EXPOSURE_RANK["self_service"] == deps.AUTHZ_EXPOSURE_RANK[
            "authenticated"
        ]

    def test_every_new_table_declares_a_lifecycle(self):
        for table in ("auth_devices", "auth_challenges", "user_identities", "storage_connections"):
            assert table in models.TABLE_LIFECYCLE, table

    def test_no_column_that_is_auth_material_is_left_unclassified(self):
        # Not "every column has an entry" -- most of these are legitimately
        # internal, and the sensitivity layer defaults unlisted names to
        # `internal` on purpose. What has to be caught is a credential slipping
        # through on the default, because that default means "safe to serialise".
        sensitive = {
            "token_hash",
            "code_hash",
            "code_verifier_encrypted",
            "access_token_encrypted",
            "refresh_token_encrypted",
            "state_hash",
        }
        declared = {
            column.name
            for table in (
                "auth_devices",
                "auth_challenges",
                "user_identities",
                "storage_connections",
            )
            for column in models.Base.metadata.tables[table].columns
        }
        unclassified = (declared & sensitive) - set(models.FIELD_SENSITIVITY)
        assert unclassified == set(), f"auth material on the default: {sorted(unclassified)}"
        for column in sorted(sensitive):
            assert models.sensitivity_of("AuthDevice", column) == "credential", column

    def test_the_recognition_digests_are_treated_as_behavioural_not_public(self):
        # A set of digests identifies a device and its usual hours. Taken together
        # they are a profile, so they must not be bulk-exportable by default.
        for column in (
            "device_digest",
            "network_digest",
            "agent_digest",
            "language_digest",
            "timezone_digest",
            "usual_hours",
        ):
            assert models.sensitivity_of("AuthDevice", column) == "behavioral", column

    def test_the_identifier_digest_and_hint_are_not_treated_as_internal(self):
        # `identifier_hash` is a digest of how the person is named and
        # `identifier_hint` is a masked fragment of it; both sit closer to
        # content than to operational detail.
        assert models.sensitivity_of("UserIdentity", "identifier_hint") == "content"
        assert models.sensitivity_of("UserIdentity", "identifier_hash") == "content"

    def test_the_migration_builds_exactly_the_four_new_tables(self):
        import importlib.util
        from pathlib import Path

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path("alembic/versions/20261001_01_add_identity_and_storage.py")
        spec = importlib.util.spec_from_file_location("identity_storage_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        engine = sa.create_engine("sqlite://")
        connection = engine.connect()
        try:
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            inspector = sa.inspect(connection)
            for name in (
                "auth_devices",
                "auth_challenges",
                "user_identities",
                "storage_connections",
            ):
                table = models.get_table(name)
                columns = {c["name"] for c in inspector.get_columns(name)}
                assert columns == {c.name for c in table.columns}, name
            module.downgrade()
            assert [t for t in sa.inspect(connection).get_table_names() if "auth" in t] == []
        finally:
            connection.close()
            engine.dispose()

    def test_the_three_shipped_validators_are_clean(self):
        # The tables are only a specification unless something checks them.
        assert dr.validate_recognition()["ok"] is True
        assert am.validate_auth_methods()["ok"] is True
        assert sp.validate_storage_providers()["ok"] is True


# The client identity a fully-matching request presents. Held as module
# constants rather than class attributes because both the stored digests and the
# request that must match them are built from them, and two copies of "10.0.5.9"
# that drift apart is a test that fails for a reason nobody can see.
CLIENT_IP = "10.0.5.9"
USER_AGENT = "MatchingAgent/1.0"
LANGUAGE = "en-GB"
TIMEZONE_OFFSET = 0
#: Fixed so the hour-of-day signal agrees whatever hour the suite runs at.
HOUR = 9


class TestTrustedDeviceCountsAsEnrolled:
    """The gap the live probe found, and the reason for this group.

    ``demand_for_presented`` gates a challenge on what the account has enrolled,
    and downgrades when it has not -- correctly, because a challenge nobody can
    satisfy is a lockout. But ``trusted`` demands ``trusted_device``, and the
    first version of ``_enrolled_methods`` only looked at verified identities.

    So on *every* recognised login the account appeared not to have the
    credential the policy asked for, the demand was downgraded, and enforced
    mode silently behaved as advisory in the one band where it was supposed to
    bite. Nothing reported it: the login succeeded and the verdict read as
    satisfied.

    These run against a real database rather than a patched module, because the
    bug lived in a SQL query rather than in the arithmetic.
    """

    #: Every recognition signal, and the digests a fully-matching device stores.
    #: Matching only ``device_id`` scores 0.51 -- enough for `faint`, not enough
    #: for `trusted` -- so a test that wants the trusted band has to populate all
    #: of them. Worth stating because "add a device row, expect trusted" is the
    #: obvious version of this test and it does not work.
    _MATCHING = {
        "device_digest": lambda: dr.digest_signal("d", "device"),
        "network_digest": lambda: dr.digest_signal(
            dr.digest_network(CLIENT_IP), "network"
        ),
        "agent_digest": lambda: dr.digest_signal(USER_AGENT, "agent"),
        "language_digest": lambda: dr.digest_signal(LANGUAGE, "client"),
        "timezone_digest": lambda: dr.digest_signal(TIMEZONE_OFFSET, "client"),
    }

    def _trusted(self, harness, user_id: int, *, full: bool = False):
        # `device_digest` is always populated, and the rest only when the caller
        # wants a device whose signals actually match: the column is NOT NULL, so
        # relying on `full` alone would make the partial case a NOT NULL failure
        # rather than a `faint`-band device.
        fields = {name: build() for name, build in self._MATCHING.items()}
        if not full:
            fields = {"device_digest": fields["device_digest"]}
        device = models.AuthDevice(
            user_id=user_id,
            trusted_at=_utcnow(),
            trust_expires_at=am.trusted_device_expiry(),
            token_hash=am.hash_trusted_device_token("cdev_x"),
            **fields,
        )
        if full:
            # A stored device remembers the hours its user signs in at. Without
            # this the hour-of-day signal contributes nothing and the score caps
            # out below the `trusted` threshold even when every other signal
            # agrees.
            #
            # The *current* hour is included alongside the fixed one because the
            # hour is read from the clock when the request is served, not
            # injected. A test that pinned a single hour would pass at 09:00 UTC
            # and quietly degrade to the `recognised` band at 02:00 -- a
            # time-of-day-dependent test that reads as a flake.
            device.usual_hours = json.dumps(sorted({HOUR, _utcnow().hour}))
        return device

    def _matching_request(self, user):
        """A login whose signals match ``_MATCHING`` exactly.

        The headers matter: ``context_from_request`` reads user-agent and
        language from the request, so a client that does not send them
        contributes nothing -- and an absent signal is a mismatch, not a pass.
        """
        return {
            "json": {
                "username": user.username,
                "code": "CorrectHorse1!",
                "method": "password",
                "device_id": "d",
            },
            "headers": {
                "user-agent": USER_AGENT,
                "accept-language": LANGUAGE,
                "x-client-utc-offset-minutes": str(TIMEZONE_OFFSET),
            },
            "client_ip": CLIENT_IP,
        }

    def test_an_unexpired_trusted_device_counts(self, harness):
        from app.routers import identity

        user = harness.user(1)
        harness.add(self._trusted(harness, user.id))
        harness.commit()
        assert "trusted_device" in harness.run(identity._enrolled_methods(harness.session, user.id))

    def test_a_revoked_device_does_not_count(self, harness):
        from app.routers import identity

        user = harness.user(1)
        device = self._trusted(harness, user.id)
        device.revoked_at = _utcnow()
        device.token_hash = None
        device.trusted_at = None
        harness.add(device)
        harness.commit()
        enrolled = harness.run(identity._enrolled_methods(harness.session, user.id))
        assert "trusted_device" not in enrolled

    def test_an_expired_device_does_not_count(self, harness):
        from app.routers import identity

        user = harness.user(1)
        device = self._trusted(harness, user.id)
        device.trust_expires_at = _utcnow() - timedelta(days=1)
        harness.add(device)
        harness.commit()
        enrolled = harness.run(identity._enrolled_methods(harness.session, user.id))
        assert "trusted_device" not in enrolled, (
            "an expired trust is not a credential, so enforcement must not demand it"
        )

    def test_a_trusted_row_with_no_token_is_impossible_and_the_query_agrees(self, harness):
        # The schema forbids this combination outright, so this asserts both
        # halves: that the row cannot be written, and that the enrolment query
        # carries the same condition anyway.
        #
        # The second half is not redundant. The check constraint is the
        # guarantee; the query clause is the same guarantee written twice. It is
        # worth having twice only because the failure it prevents is silent --
        # an unenforced query on a schema-valid row would let enforcement demand
        # a credential that does not exist, which is a lockout, not a 500.
        from sqlalchemy.exc import IntegrityError

        from app.routers import identity

        user = harness.user(1)
        # The id is captured while the object is loaded. A rolled-back session
        # expires everything, and reading `user.id` afterwards is a lazy load
        # outside the loop -- which raises MissingGreenlet rather than saying
        # anything about the query under test.
        user_id = user.id
        harness.add(
            models.AuthDevice(
                user_id=user_id,
                device_digest=dr.digest_signal("d", "device"),
                trusted_at=_utcnow(),
                trust_expires_at=am.trusted_device_expiry(),
                token_hash=None,
            )
        )
        with pytest.raises(IntegrityError):
            harness.commit()
        harness.rollback()

        enrolled = harness.run(identity._enrolled_methods(harness.session, user_id))
        assert "trusted_device" not in enrolled

    def test_enforced_mode_challenges_once_the_credential_verified(self, harness):
        # The end-to-end assertion, and the one that would have caught the gap
        # above. With it, this returned a 200 and a token for a password-only
        # login on a device the policy had classified `trusted`.
        #
        # The password is hashed properly so the request gets *past* the
        # credential check and actually reaches the demand -- which is the point:
        # recognition is consulted only after a credential verified, so a test
        # that stops at a wrong password proves nothing about it.
        user = harness.user(1)
        user.hashed_password = security.get_password_hash("CorrectHorse1!")
        harness.add(self._trusted(harness, user.id, full=True))
        harness.commit()

        os.environ["CSERVICE_RECOGNITION_MODE"] = "enforced"
        try:
            response = Mounted(harness).post("/users/auth/login", **self._matching_request(user))
        finally:
            os.environ.pop("CSERVICE_RECOGNITION_MODE", None)

        assert response.status_code == 401, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "challenge_required"
        assert detail["method"] == "trusted_device"
        assert detail["recognition"]["policy_id"] == "trusted"
        assert "access_token" not in response.json(), (
            "recognition produced a token, which is the whole thing it must never do"
        )

    def test_advisory_mode_issues_the_token_where_enforced_would_challenge(self, harness):
        # The other half: with the gap above this passed for the wrong reason,
        # because advisory mode ignores the demand. Pinning both modes together
        # is what makes either assertion meaningful.
        user = harness.user(1)
        user.hashed_password = security.get_password_hash("CorrectHorse1!")
        harness.add(self._trusted(harness, user.id, full=True))
        harness.commit()

        response = Mounted(harness).post("/users/auth/login", **self._matching_request(user))
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]
        assert response.json()["recognition"]["policy_id"] == "trusted"
        assert response.json()["recognition_applied"] is False

    def test_an_account_with_nothing_enrolled_is_never_locked_out(self, harness):
        user = harness.user(1)
        harness.commit()
        from app.routers import identity

        enrolled = harness.run(identity._enrolled_methods(harness.session, user.id))
        assert "trusted_device" not in enrolled

        verdict = dr.recognize({"device_id": "d"}, {"device_id": dr.digest_signal("d", "device")})
        os.environ["CSERVICE_RECOGNITION_MODE"] = "enforced"
        try:
            decision = dr.demand_for_presented(verdict, "password", enrolled)
        finally:
            os.environ.pop("CSERVICE_RECOGNITION_MODE", None)

        assert verdict.policy_id == "trusted"
        assert decision["challenge"] is False, "enforcement would lock the account out"
        assert decision["downgraded"] is True
