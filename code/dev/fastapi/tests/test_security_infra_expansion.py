"""Regression + behavior tests for the infrastructure expansion.

Covers the small backend components that were expanded from their original
skeletons:

1. ``app.security`` — access vs refresh token types, a decode/validate helper,
   and a configurable password-strength policy.
2. ``app.db`` — env-tunable pool settings, a non-raising ping probe, and a
   retry helper for idempotent recovery paths.
3. ``app.i18n`` — the French locale, the message catalog, and the translate
   helper with fallback behavior.

All existing contracts (locale payload shape, token encoding, engine/session
creation) are preserved.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app import db, i18n, security


# --- security: token types and decode -----------------------------------------


def test_access_token_carries_access_type_claim():
    token = security.create_access_token(data={"sub": "alice"})
    payload = jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])
    assert payload["sub"] == "alice"
    assert payload["typ"] == security.TOKEN_TYPE_ACCESS


def test_refresh_token_carries_refresh_type_claim_and_longer_lifetime():
    access = security.create_access_token(data={"sub": "alice"})
    refresh = security.create_refresh_token(data={"sub": "alice"})

    def _lifetime(token):
        payload = jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])
        return datetime.fromtimestamp(payload["exp"], tz=timezone.utc) - datetime.fromtimestamp(
            payload["iat"], tz=timezone.utc
        )

    assert jwt.decode(refresh, security.SECRET_KEY, algorithms=[security.ALGORITHM])["typ"] == security.TOKEN_TYPE_REFRESH
    assert _lifetime(refresh) > _lifetime(access)


def test_decode_access_token_round_trips_and_rejects_wrong_type():
    token = security.create_access_token(data={"sub": "bob"})
    payload = security.decode_access_token(token)
    assert payload["sub"] == "bob"

    refresh = security.create_refresh_token(data={"sub": "bob"})
    with pytest.raises(ValueError):
        security.decode_access_token(refresh)  # refresh token is not an access token


def test_decode_access_token_rejects_garbage_and_missing_subject():
    with pytest.raises(ValueError):
        security.decode_access_token("not-a-jwt")
    token = security.create_access_token(data={})  # no subject
    with pytest.raises(ValueError):
        security.decode_access_token(token)


# --- security: password policy -------------------------------------------------


def test_password_policy_validates_accept_and_reject():
    policy = security.PasswordPolicy(min_length=8, require_digit=True)
    assert security.validate_password_strength("Abcdef12!", policy) == []
    issues = security.validate_password_strength("short", policy)
    assert any("8 characters" in issue for issue in issues)
    issues2 = security.validate_password_strength("abcdefgh", policy)
    assert any("digit" in issue for issue in issues2)


def test_password_policy_payload_is_introspectable():
    payload = security.password_policy_payload()
    assert payload["min_length"] == security.DEFAULT_PASSWORD_POLICY.min_length
    assert payload["hash_scheme"] == "bcrypt"
    assert security.ACCESS_TOKEN_EXPIRE_MINUTES == 120  # historical default kept


# --- db: retry helper ----------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient_failures():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    result = await db.retry_async(flaky, attempts=3, delay_seconds=0)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_async_raises_last_error_when_exhausted():
    async def always_fails():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await db.retry_async(always_fails, attempts=2, delay_seconds=0)


@pytest.mark.asyncio
async def test_ping_database_never_raises():
    class _FailingConn:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("no db here")

    class _FailingEngine:
        def begin(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return _FailingConn()

                async def __aexit__(self_inner, *exc):
                    return False

            return _Ctx()

    assert await db.ping_database(_FailingEngine()) is False


# --- db: env-tunable pool defaults --------------------------------------------


def test_db_pool_settings_have_stable_defaults():
    assert db.DB_POOL_SIZE >= 1
    assert db.DB_MAX_OVERFLOW >= 0
    assert db.DB_POOL_TIMEOUT >= 1


# --- i18n: fr locale, catalog, translate ---------------------------------------


def test_fr_locale_is_supported():
    resolution = i18n.resolve_locale("fr")
    assert resolution.resolved == "fr"
    assert resolution.fallback_used is False
    payload = i18n.locale_payload("fr")
    assert payload["display_name"] == "Fran\u00e7ais"


def test_translate_localizes_and_interpolates():
    assert i18n.translate("auth.welcome", "es", name="Ana") == "Bienvenido, Ana"
    assert i18n.translate("auth.welcome", "fr", name="Ana") == "Bienvenue, Ana"
    assert i18n.translate("auth.welcome", "en", name="Ana") == "Welcome, Ana"


def test_translate_falls_back_to_english_and_unknown_key_passthrough():
    # Missing template key -> English fallback.
    assert i18n.translate("common.welcome", "pt-BR") == "Welcome to Customer Service"
    # Unknown key -> the key itself.
    assert i18n.translate("does.not.exist", "fr") == "does.not.exist"


def test_i18n_catalog_exposes_locales_and_messages():
    catalog = i18n.build_i18n_catalog()
    assert catalog["locales"] == ["en", "es", "fr"]
    assert catalog["fallback_locale"] == "en"
    assert catalog["message_count"] == len(i18n.MESSAGE_CATALOG)
    assert "ai_chat.greeting" in catalog["messages"]
    assert catalog["messages"]["ai_chat.greeting"]["fr"]


# --- end-to-end: /meta/i18n and scoring catalog --------------------------------


def test_meta_i18n_endpoint_contract():
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/meta/i18n")
    assert response.status_code == 200
    payload = response.json()
    assert payload["catalog"]["message_count"] > 0
    assert payload["resolution"]["resolved"] == "en"

    es = TestClient(app).get("/meta/i18n?locale=es")
    assert es.status_code == 200
    assert es.json()["resolution"]["resolved"] == "es"


def test_scoring_catalog_exposes_new_infra_catalogs():
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/meta/scoring-catalog")
    assert response.status_code == 200
    payload = response.json()
    assert payload["i18n"]["locales"] == ["en", "es", "fr"]
    assert payload["password_policy"]["min_length"] >= 8