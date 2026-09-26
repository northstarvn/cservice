"""Tests for the identity/session expansion of ``app/routers/users.py``.

The historical surface — ``/register``, ``/login``, ``/me`` and
``/me/password`` — is pinned byte-for-byte here. Everything tested below it is
new: refresh-token rotation, logout, session inventory, step-up tokens,
self-service machine credentials, and non-mutating password feedback.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import deps, security
from app.routers import users as users_router


# --- pinned historical contracts ---------------------------------------------


def test_control_posture_alias_is_still_the_deps_function():
    assert users_router._current_control_posture is deps.current_control_posture


def test_historical_routes_are_still_registered():
    from app.main import app

    router_paths = {getattr(route, "path", None) for route in users_router.router.routes}
    assert {
        "/register",
        "/login",
        "/me",
        "/me/policy-score",
        "/me/can-access",
        "/me/policy-decision",
        "/me/policy-decision/report",
        "/me/password",
    } <= router_paths
    # Mounted under the historical prefix and still served.
    mounted = set(app.openapi()["paths"])
    assert {
        "/users/register",
        "/users/login",
        "/users/me",
        "/users/me/password",
        "/users/refresh",
        "/users/logout",
    } <= mounted


def test_change_password_message_still_matches_the_historical_text():
    import asyncio

    class _Db:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    class _User:
        username = "customer"
        policy_score = None
        hashed_password = security.get_password_hash("old-password")

    user = _User()
    from app.schemas import schemas

    result = asyncio.run(
        users_router.change_password(
            payload=schemas.PasswordChange(
                current_password="old-password", new_password="new-password"
            ),
            current_user=user,
            db=_Db(),
        )
    )
    assert result == {"message": "Password updated successfully under controlled access posture."}


# --- pure helpers --------------------------------------------------------------


def test_build_session_record_normalizes_claims():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    row = users_router.build_session_record(
        {
            "jti": "abc",
            "sub": "customer",
            "typ": "refresh",
            "iat": (now - timedelta(minutes=5)).isoformat(),
            "exp": (now + timedelta(days=1)).isoformat(),
        },
        now=now,
    )
    assert row["jti"] == "abc"
    assert row["token_type"] == "refresh"
    assert row["subject"] == "customer"
    assert row["issued_at"] == now - timedelta(minutes=5)
    assert row["active"] is True and row["revoked"] is False


def test_build_session_record_marks_expired_and_revoked():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    expired = users_router.build_session_record(
        {"jti": "e", "exp": (now - timedelta(seconds=1)).isoformat()}, now=now
    )
    assert expired["active"] is False and expired["revoked"] is False
    revoked = users_router.build_session_record({"jti": "r", "exp": None}, revoked=True, now=now)
    assert revoked["active"] is False and revoked["revoked"] is True
    # A token with no exp claim is treated as live until explicitly revoked.
    assert revoked["expires_at"] is None


def test_as_utc_tolerates_every_claim_shape():
    moment = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert users_router._as_utc(moment) == moment
    naive = datetime(2026, 9, 26, 12, 0)
    assert users_router._as_utc(naive).tzinfo is timezone.utc
    assert users_router._as_utc(moment.timestamp()) == moment
    assert users_router._as_utc("2026-09-26T12:00:00Z") == moment
    assert users_router._as_utc("2026-09-26T12:00:00+00:00") == moment
    assert users_router._as_utc("") is None
    assert users_router._as_utc("not-a-date") is None
    assert users_router._as_utc(None) is None


def test_build_session_inventory_orders_newest_first_and_counts():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        users_router.build_session_record(
            {"jti": "old", "sub": "customer", "iat": (now - timedelta(hours=2)).isoformat()},
            now=now,
        ),
        users_router.build_session_record(
            {"jti": "new", "sub": "customer", "iat": (now - timedelta(minutes=1)).isoformat()},
            now=now,
        ),
        users_router.build_session_record({"jti": "dead", "sub": "customer"}, revoked=True, now=now),
    ]
    inventory = users_router.build_session_inventory("customer", rows, now=now)
    # A row with no issue time sorts last: unknown age is not recency.
    assert [row["jti"] for row in inventory["sessions"]] == ["new", "old", "dead"]
    assert inventory["active_sessions"] == 2
    assert inventory["revoked_sessions"] == 1
    assert inventory["mechanism"] == "jti deny-list, TTL-bounded"
    # Rows belonging to a different subject never leak into the listing.
    other = users_router.build_session_record({"jti": "x", "sub": "someone-else"}, now=now)
    scoped = users_router.build_session_inventory("customer", rows + [other], now=now)
    assert [row["jti"] for row in scoped["sessions"]] == ["new", "old", "dead"]


def test_build_refresh_plan_carries_tenant_and_delegation_context():
    claims = security.create_refresh_token({"sub": "customer", "tenant_id": "acme"})
    validation = security.decode_token_full(claims, expected_type=security.TOKEN_TYPE_REFRESH)
    plan = users_router.build_refresh_plan(validation, rotate=True, locale="fr")
    assert plan["data"]["sub"] == "customer"
    assert plan["data"]["tenant_id"] == "acme"
    assert plan["rotate"] is True
    assert plan["previous_jti"] == validation.jti
    assert plan["access_expires_in"] == security.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert plan["refresh_expires_in"] == security.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    assert plan["locale"]["resolved"] == "fr"
    assert plan["locale"]["fallback_used"] is False


def test_build_refresh_plan_can_opt_out_of_rotation():
    token = security.create_refresh_token({"sub": "customer"})
    validation = security.decode_token_full(token, expected_type=security.TOKEN_TYPE_REFRESH)
    plan = users_router.build_refresh_plan(validation, rotate=False)
    assert plan["rotate"] is False
    assert plan["data"] == {"sub": "customer"}


def test_build_step_up_plan_reports_unknown_levels():
    plan = users_router.build_step_up_plan("loa2", method="mfa")
    assert plan["known_level"] is True
    assert plan["method_accepted"] is True
    assert "write" in plan["purpose"]

    unknown = users_router.build_step_up_plan("loa9", method="mfa")
    assert unknown["known_level"] is False
    assert unknown["method_accepted"] is False
    assert unknown["accepted_methods"] == []


def test_build_step_up_plan_reports_already_satisfied():
    assert users_router.build_step_up_plan("loa1", method="pwd", current_level="loa1")[
        "already_satisfied"
    ] is True
    assert users_router.build_step_up_plan("loa3", method="hwk", current_level="loa2")[
        "already_satisfied"
    ] is False


def test_filter_self_scopes_never_grants_the_wildcard():
    granted, rejected = users_router.filter_self_scopes(["read", "*", "audit", "read", "", "admin"])
    assert granted == ["audit", "read"]
    assert rejected == ["*", "admin"]


def test_policy_requirements_and_advice_track_the_active_policy():
    loose = security.DEFAULT_PASSWORD_POLICY
    # The shipped default is length-bounded with a letter required, nothing else.
    assert users_router.policy_requirements(loose) == [
        "at least 8 characters",
        "at most 128 characters",
        "at least one letter",
    ]
    assert users_router.policy_advice(loose) == [
        "Use at least 8 characters so length, not symbols, carries the strength."
    ]
    strict = loose.with_overrides(
        min_length=12, require_digit=True, require_special=True, forbid_common=True, history_depth=3
    )
    requirements = users_router.policy_requirements(strict)
    assert "at least 12 characters" in requirements
    assert "at least one digit" in requirements
    assert "at least one special character" in requirements
    assert "not a commonly used password" in requirements
    advice = users_router.policy_advice(strict)
    assert advice[0].startswith("Use at least 12 characters")
    assert any("have not used" in text for text in advice)
    assert any("digits" in text for text in advice)


def test_build_password_feedback_is_non_mutating_and_advisory():
    weak = users_router.build_password_feedback("abc")
    assert weak["accepted"] is False
    assert weak["issues"]
    assert weak["score"] == 0

    strong = users_router.build_password_feedback("Tr0ub4dor&3-quartz-jaguar")
    assert strong["accepted"] is True
    assert strong["score"] >= 3
    assert strong["issues"] == []
    assert strong["policy"]["hash_scheme"] == "bcrypt"


def test_build_password_feedback_flags_username_and_reuse():
    embedded = users_router.build_password_feedback("alexandra-2026!", username="alexandra")
    assert embedded["contains_username"] is True
    assert "must not contain your username" in embedded["issues"]

    history = security.build_password_history(depth=3)
    history.remember(security.get_password_hash("Zx9!panel-rocket"))
    reused = users_router.build_password_feedback(
        "Zx9!panel-rocket", username="customer", history=history
    )
    assert reused["reused"] is True
    assert "has been used on this account before" in reused["issues"]
    # The default (depth 0) history never reports reuse.
    assert users_router.build_password_feedback(
        "Zx9!panel-rocket", history=security.DEFAULT_PASSWORD_HISTORY
    )["reused"] is False


def test_build_api_key_payload_only_carries_the_secret_when_given():
    record = security.ApiKeyRecord(
        key_id="k1",
        name="ci",
        subject="customer",
        scopes=("read",),
        hash_prefix="deadbeefcafe",
        expires_at=None,
        created_at="2026-09-26T12:00:00+00:00",
    )
    listed = users_router.build_api_key_payload(record)
    assert listed["plaintext"] is None
    assert "csk_" not in str(listed)
    issued = users_router.build_api_key_payload(record, "csk_secret")
    assert issued["plaintext"] == "csk_secret"


def test_build_api_key_inventory_splits_active_expired_revoked():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

    def record(key_id, *, expires_at=None, revoked=False, subject="customer"):
        return security.ApiKeyRecord(
            key_id=key_id,
            name=key_id,
            subject=subject,
            scopes=("read",),
            hash_prefix="x",
            expires_at=expires_at,
            created_at="2026-09-26T00:00:00+00:00",
            revoked=revoked,
        )

    inventory = users_router.build_api_key_inventory(
        "customer",
        [
            record("live"),
            record("gone", expires_at=(now - timedelta(days=1)).isoformat()),
            record("later", expires_at=(now + timedelta(days=1)).isoformat()),
            record("dead", revoked=True),
            record("other", subject="someone-else"),
        ],
        now=now,
    )
    assert inventory["total"] == 4  # the other subject is filtered out
    assert inventory["expired"] == 1
    assert inventory["revoked"] == 1
    assert inventory["active"] == 2


def test_build_security_posture_emits_templated_recommendations():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    posture = users_router.build_security_posture(
        "customer",
        control_posture="constrained",
        step_up_level="none",
        auth_method="user",
        sessions=[],
        keys=[],
        now=now,
    )
    conditions = {row["condition"] for row in posture["recommendations"]}
    assert {"no_active_sessions", "no_api_keys", "no_step_up", "low_trust"} <= conditions
    assert all(row["severity"] in {"info", "warning"} for row in posture["recommendations"])
    assert posture["control_posture"] == "constrained"
    assert posture["active_sessions"] == 0
    assert posture["highest_known_step_up"] == "none"
    assert posture["signed_in_as_admin"] is False


def test_build_security_posture_reads_assurance_off_the_claims():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    live = security.with_step_up({"sub": "customer"}, "loa3", ["hwk"])
    plain = security.decode_access_token(security.create_access_token({"sub": "customer"}))
    posture = users_router.build_security_posture(
        "customer",
        step_up_level="loa1",
        sessions=[
            {"claims": live, "revoked": False},
            {"claims": plain, "revoked": True},
        ],
        now=now,
    )
    assert posture["highest_known_step_up"] == "loa3"
    assert posture["active_sessions"] == 1
    assert "no_api_keys" in {row["condition"] for row in posture["recommendations"]}
    assert "no_active_sessions" not in {row["condition"] for row in posture["recommendations"]}


def test_users_catalog_is_introspectable():
    catalog = users_router.build_users_catalog()
    assert catalog["lifecycle"]["steps"] == ["register", "login", "refresh", "logout"]
    assert catalog["lifecycle"]["rotation_default"] is True
    assert catalog["step_up"]["levels"] == list(security.STEP_UP_LEVELS)
    assert catalog["api_keys"]["wildcard_self_assignable"] is False
    assert "*" not in catalog["api_keys"]["self_service_scopes"]
    assert catalog["password_policy"]["hash_scheme"] == "bcrypt"
    assert catalog["password_requirements"] == users_router.policy_requirements()


# --- endpoint behaviour ---------------------------------------------------------


class _User:
    username = "customer"
    email = "customer@example.com"
    full_name = "Customer"
    is_admin = False
    policy_score = None


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _clear_overrides():
    from app.main import app

    for dep in (deps.get_db, deps.get_current_user, deps.get_principal):
        app.dependency_overrides.pop(dep, None)


def _client_with_user(user=None):
    """Mount the app with the current user resolved, and return a client."""
    from app.main import app

    resolved = user or _User()

    async def _db():
        yield None

    app.dependency_overrides[deps.get_db] = _db
    app.dependency_overrides[deps.get_current_user] = lambda: resolved
    app.dependency_overrides[deps.get_principal] = lambda: deps.Principal(
        subject=resolved.username,
        user=resolved,
        roles=frozenset({"customer"}),
        scopes=frozenset(),
        auth_method=deps.AUTH_METHOD_USER,
    )
    return TestClient(app)


def test_refresh_rotates_and_burns_the_presented_token():
    original = security.create_refresh_token({"sub": "customer", "tenant_id": "acme"})
    response = _client_with_user().post(
        "/users/refresh", json={"refresh_token": original, "locale": "es"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rotated"] is True
    assert body["previous_jti_revoked"] is True
    assert body["refresh_token"]
    assert body["refresh_expires_in"] == security.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    assert body["locale"]["resolved"] == "es"

    # The replacement is a real access token for the same subject.
    access = security.decode_access_token(body["access_token"])
    assert access["sub"] == "customer"
    assert access["tenant_id"] == "acme"
    # The new refresh token works, the old one does not.
    assert (
        security.decode_token_full(
            body["refresh_token"], expected_type=security.TOKEN_TYPE_REFRESH
        ).valid
        is True
    )
    assert (
        security.decode_token_full(
            original, expected_type=security.TOKEN_TYPE_REFRESH
        ).reason
        == "revoked"
    )


def test_refresh_without_rotation_leaves_the_token_usable():
    original = security.create_refresh_token({"sub": "customer"})
    response = _client_with_user().post(
        "/users/refresh", json={"refresh_token": original, "rotate_refresh_token": False}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rotated"] is False
    assert body["previous_jti_revoked"] is False
    assert body["refresh_token"] is None
    assert (
        security.decode_token_full(original, expected_type=security.TOKEN_TYPE_REFRESH).valid
        is True
    )


def test_refresh_rejects_a_non_refresh_token():
    access = security.create_access_token({"sub": "customer"})
    response = _client_with_user().post("/users/refresh", json={"refresh_token": access})
    assert response.status_code == 401
    assert "wrong_token_type" in response.json()["detail"]


def test_refresh_rejects_garbage():
    response = _client_with_user().post("/users/refresh", json={"refresh_token": "not-a-jwt"})
    assert response.status_code == 401


def test_logout_revokes_a_single_token_idempotently():
    token = security.create_access_token({"sub": "customer"})
    client = _client_with_user()
    first = client.post("/users/logout", json={"token": token})
    assert first.status_code == 200
    body = first.json()
    assert body["revoked"] is True and body["scope"] == "token"
    assert body["newly_revoked"] == 1

    # A second logout succeeds; the token is already on the deny-list.
    second = client.post("/users/logout", json={"token": token})
    assert second.status_code == 200
    assert second.json()["newly_revoked"] == 0
    assert security.decode_token_full(token).reason == "revoked"


def test_logout_tolerates_an_undecodable_token():
    response = _client_with_user().post("/users/logout", json={"token": "garbage"})
    assert response.status_code == 200
    assert response.json()["revoked"] is False


def test_logout_all_sessions_revokes_every_tracked_token():
    client = _client_with_user()
    access = security.create_access_token({"sub": "customer"})
    security.REVOCATIONS.revoke("seeded-jti", subject="customer")
    response = client.post("/users/logout", json={"token": access, "all_sessions": True})
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "all_sessions"
    assert body["newly_revoked"] >= 1
    assert "customer" in body["message"]


def test_logout_all_sessions_requires_a_token():
    response = _client_with_user().post("/users/logout", json={"all_sessions": True})
    assert response.status_code == 400


def test_logout_without_a_token_is_rejected():
    response = _client_with_user().post("/users/logout", json={})
    assert response.status_code == 400


def test_me_sessions_lists_the_presented_token():
    token = security.create_access_token({"sub": "customer"})
    response = _client_with_user().get("/users/me/sessions", headers=_bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "customer"
    assert body["active_sessions"] == 1
    assert body["mechanism"] == "jti deny-list, TTL-bounded"


def test_me_sessions_marks_a_revoked_credential():
    token = security.create_access_token({"sub": "customer"})
    security.revoke_token(token)
    response = _client_with_user().get("/users/me/sessions", headers=_bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert body["active_sessions"] == 0
    # Listed exactly once, even though it is both presented and denied.
    assert body["revoked_sessions"] == 1
    assert [row["jti"] for row in body["sessions"]].count(
        security.decode_token_full(token).jti
    ) == 1


def test_step_up_endpoints_mint_and_describe_assurance():
    client = _client_with_user()
    plan = client.get("/users/me/step-up?target_level=loa3")
    assert plan.status_code == 200
    assert plan.json()["requested_level"] == "loa3"
    assert "hwk" in plan.json()["accepted_methods"]

    minted = client.post("/users/me/step-up", json={"level": "loa2", "method": "otp"})
    assert minted.status_code == 200
    body = minted.json()
    assert body["level"] == "loa2" and body["satisfied"] is True
    claims = security.decode_access_token(body["access_token"])
    assert security.step_up_level_of(claims) == "loa2"
    assert claims["amr"] == ["otp"]


def test_step_up_rejects_unknown_levels_and_weak_methods():
    client = _client_with_user()
    assert client.post("/users/me/step-up", json={"level": "loa9"}).status_code == 400
    # "pwd" is not evidence for loa3.
    assert client.post("/users/me/step-up", json={"level": "loa3", "method": "pwd"}).status_code == 400


def test_api_key_crud_round_trip():
    client = _client_with_user()
    created = client.post(
        "/users/me/api-keys", json={"name": "ci-runner", "scopes": ["read", "audit"]}
    )
    assert created.status_code == 200
    body = created.json()
    assert body["plaintext"].startswith(security.API_KEY_PREFIX)
    assert body["scopes"] == ["audit", "read"]
    assert body["revoked"] is False
    key_id = body["key_id"]

    listed = client.get("/users/me/api-keys").json()
    assert listed["total"] >= 1
    assert all(key["plaintext"] is None for key in listed["keys"])
    assert any(key["key_id"] == key_id for key in listed["keys"])

    revoked = client.delete(f"/users/me/api-keys/{key_id}")
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert client.delete(f"/users/me/api-keys/{key_id}").json()["revoked"] is False


def test_api_key_creation_refuses_wildcards_and_absurd_ttls():
    client = _client_with_user()
    assert client.post("/users/me/api-keys", json={"name": "root", "scopes": ["*"]}).status_code == 400
    assert (
        client.post(
            "/users/me/api-keys",
            json={"name": "forever", "scopes": ["read"], "expires_in_days": 9999},
        ).status_code
        == 400
    )


def test_api_key_revocation_is_scoped_to_the_owner():
    other = security.API_KEYS.issue(
        name="someone-elses", subject="other-subject", scopes=["read"]
    )[1]
    response = _client_with_user().delete(f"/users/me/api-keys/{other.key_id}")
    assert response.status_code == 404
    # The other subject's key is untouched.
    assert [row["revoked"] for row in security.API_KEYS.list_keys() if row["subject"] == "other-subject"] == [False]


def test_password_policy_endpoint_is_public_and_describes_the_active_policy():
    from app.main import app

    response = TestClient(app).get("/users/password-policy")
    assert response.status_code == 200
    body = response.json()
    assert body["policy"]["hash_scheme"] == "bcrypt"
    assert body["scale"] == [0, 4]
    assert body["requirements"] == users_router.policy_requirements()


def test_password_feedback_scores_without_mutating_anything():
    response = _client_with_user().post(
        "/users/me/password-feedback", json={"candidate": "qwerty"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False
    assert "common_password" in body["penalties"] or body["issues"]
    assert body["policy"]["min_length"] == security.DEFAULT_PASSWORD_POLICY.min_length


def test_security_posture_endpoint_joins_the_pieces():
    response = _client_with_user().get("/users/me/security-posture")
    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "customer"
    assert body["control_posture"] == "observed"
    assert body["step_up_level"] == "none"
    assert body["auth_method"] == "user"
    assert body["signed_in_as_admin"] is False
    assert body["recommendations"]
    assert all(row["severity"] in {"info", "warning"} for row in body["recommendations"])


# --- metadata surfaces ----------------------------------------------------------


def test_meta_features_documents_the_identity_endpoints():
    from app.main import app

    endpoints = TestClient(app).get("/meta/features").json()["endpoints"]
    assert endpoints["session_refresh"] == "/users/refresh"
    assert endpoints["session_logout"] == "/users/logout"
    assert endpoints["session_inventory"] == "/users/me/sessions"
    assert endpoints["step_up"] == "/users/me/step-up"
    assert endpoints["api_keys"] == "/users/me/api-keys"
    assert endpoints["password_policy"] == "/users/password-policy"
    assert endpoints["password_feedback"] == "/users/me/password-feedback"
    assert endpoints["security_posture"] == "/users/me/security-posture"


def test_meta_ecosystem_documents_the_session_lifecycle():
    from app.main import app

    identity = TestClient(app).get("/meta/ecosystem").json()["subservices"]["identity"]
    assert "/users/refresh" in identity["routes"]
    assert "/users/logout" in identity["routes"]
    assert "/users/me/sessions" in identity["routes"]


def test_scoring_catalog_publishes_the_identity_catalog():
    from app.main import app

    catalog = TestClient(app).get("/meta/scoring-catalog").json()
    assert catalog["identity"] == users_router.build_users_catalog()
    assert catalog["identity"]["step_up"]["levels"] == list(security.STEP_UP_LEVELS)


# --- isolation ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registry_isolation():
    """Keep API keys, revocations and dependency overrides from leaking."""
    security.API_KEYS.reset()
    security.REVOCATIONS.reset()
    try:
        yield
    finally:
        security.API_KEYS.reset()
        security.REVOCATIONS.reset()
        _clear_overrides()
