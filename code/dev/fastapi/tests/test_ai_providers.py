"""Several AI services behind one external brain, and the failover between them.

The feature makes one promise worth testing: **a quota running out is a detour,
not an outage**. So the groups below are organised around the ways that promise
can break, not around the provider table:

* **Classifying a refusal** -- a rate limit and an exhausted monthly quota arrive
  with the same status code from several providers, and treating the second as
  the first is how a deployment retries a provider it cannot pay for, once a
  minute, forever. The classification is pure, so it is tested directly.
* **Failing over.** Every one of these tests installs a transport that returns a
  scripted answer. None of them touches the internet, and none of them sleeps.
* **The credential rules.** The environment is how a deployment is configured;
  the database copy is how a session is rotated. Which one wins, and what a
  cleared credential means, are the two claims worth pinning.
* **Persistence.** The pool is in memory on the hot path and durable across a
  restart, and those two facts have to agree.
* **The admin surface.** Rotate, clear, reset, probe -- behind the existing
  admin gate, and never returning a secret.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.future import select

from app import deps, models
from app.main import app
from app.services import ai_providers as ai
from app.routers import chat as chat_router

from tests._doubles import SqliteHarness


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _openai_ok(text: str) -> ai.AiResponse:
    return ai.AiResponse(
        status=200, body={"choices": [{"message": {"content": text}}]}
    )


# ===========================================================================
# Classification: a refusal is not one thing
# ===========================================================================


class TestClassifyingARefusal:
    def test_a_2xx_is_success(self):
        assert ai.classify_failure(200, "") == ai.OK
        assert ai.classify_failure(204, "") == ai.OK

    def test_a_rate_limit_and_a_spent_quota_are_told_apart(self):
        """Same status, different future. This is the whole point of the split."""
        plain = ai.classify_failure(429, '{"error":{"message":"slow down"}}')
        spent = ai.classify_failure(
            429, '{"error":{"code":"insufficient_quota","type":"insufficient_quota"}}'
        )
        assert plain == ai.RATE_LIMITED
        assert spent == ai.QUOTA_EXHAUSTED

    def test_the_quota_body_tokens_cover_the_common_provider_wording(self):
        for body in (
            '{"error":{"code":"insufficient_quota"}}',
            '{"error":{"status":"RESOURCE_EXHAUSTED"}}',
            "You exceeded your current quota, please check your plan",
            '{"error":{"message":"Your credit balance is too low"}}',
        ):
            assert ai.classify_failure(429, body) == ai.QUOTA_EXHAUSTED, body

    def test_payment_required_is_a_quota_even_with_perfect_wording(self):
        assert ai.classify_failure(402, "payment required") == ai.QUOTA_EXHAUSTED

    def test_a_rejected_credential_is_its_own_kind(self):
        assert ai.classify_failure(401, "") == ai.AUTH_FAILED
        assert ai.classify_failure(403, "") == ai.AUTH_FAILED

    def test_server_and_client_faults_are_separated(self):
        assert ai.classify_failure(500, "") == ai.SERVER_ERROR
        assert ai.classify_failure(503, "") == ai.SERVER_ERROR
        assert ai.classify_failure(400, "") == ai.BAD_REQUEST
        assert ai.classify_failure(404, "") == ai.BAD_REQUEST
        assert ai.classify_failure(408, "") == ai.TIMEOUT


class TestCooldownDuration:
    def test_a_provider_supplied_retry_after_is_honoured_but_bounded(self):
        assert ai.cooldown_seconds(ai.RATE_LIMITED, retry_after=30) == 30
        # A provider asking for a day is not really rate-limiting.
        assert ai.cooldown_seconds(ai.RATE_LIMITED, retry_after=999999) == 3600

    def test_a_rejected_credential_waits_for_a_rotation_not_a_minute(self):
        assert ai.cooldown_seconds(ai.AUTH_FAILED) == ai.AUTH_COOLDOWN_SECONDS
        assert ai.AUTH_COOLDOWN_SECONDS > ai.cooldown_seconds(ai.RATE_LIMITED)

    def test_an_exhausted_quota_outlasts_a_rate_limit(self):
        assert ai.cooldown_seconds(ai.QUOTA_EXHAUSTED) > ai.cooldown_seconds(
            ai.RATE_LIMITED
        )


class TestRetryAfterParsing:
    def test_reads_the_delta_seconds_form(self):
        assert ai.parse_retry_after({"Retry-After": "12"}) == 12
        assert ai.parse_retry_after({"retry-after": "0"}) == 0

    def test_ignores_the_http_date_form_rather_than_guessing(self):
        assert ai.parse_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
        assert ai.parse_retry_after(None) is None
        assert ai.parse_retry_after({}) is None


# ===========================================================================
# The provider table
# ===========================================================================


class TestTheProviderTable:
    def test_the_table_is_self_consistent(self):
        report = ai.validate_ai_providers()
        assert report["ok"] is True, report["problems"]
        assert report["providers"] == len(ai.AI_PROVIDERS)

    def test_provider_names_are_unique(self):
        names = [p.provider for p in ai.AI_PROVIDERS]
        assert len(names) == len(set(names))

    def test_every_provider_declares_models_and_an_auth_mode(self):
        for spec in ai.AI_PROVIDERS:
            assert spec.models, spec.provider
            assert spec.auth_modes, spec.provider
            assert set(spec.auth_modes) <= set(ai.AUTH_MODES)

    def test_browser_providers_are_marked_best_effort(self):
        """The caveat has to be visible in the data, not only in a docstring."""
        for spec in ai.AI_PROVIDERS:
            if ai.AUTH_BROWSER_SESSION in spec.auth_modes:
                assert spec.browser_best_effort is True, spec.provider
                assert spec.session_envs, spec.provider

    def test_provider_order_prefers_the_table_unless_configured(self, monkeypatch):
        monkeypatch.delenv("CSERVICE_AI_PROVIDER_ORDER", raising=False)
        assert ai.provider_order() == ai.provider_ids()

    def test_a_configured_order_reorders_and_ignores_unknown_names(self, monkeypatch):
        monkeypatch.setenv(
            "CSERVICE_AI_PROVIDER_ORDER", "anthropic,not_a_provider,openai"
        )
        order = ai.provider_order()
        assert order[0] == "anthropic"
        assert order[1] == "openai"
        assert "not_a_provider" not in order
        # A stale variable must not hide a provider that is still in the table.
        assert set(order) == set(ai.provider_ids())


# ===========================================================================
# Failing over
# ===========================================================================


class _Transport:
    """A scripted transport: records what was called, returns what it is told."""

    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: ai.AiRequest) -> ai.AiResponse:
        self.calls.append((request.provider, request.model))
        return self._behaviour(request)


class TestFailover:
    def test_an_exhausted_quota_moves_to_the_next_provider(self):
        def behaviour(request):
            if request.kind == "openai":
                return ai.AiResponse(
                    status=429,
                    text='{"error":{"code":"insufficient_quota"}}',
                )
            return ai.AiResponse(
                status=200, body={"content": [{"type": "text", "text": "from anthropic"}]}
            )

        transport = _Transport(behaviour)
        ai.POOL.set_transport(transport)
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        ai.POOL.set_credential("anthropic", ai.AUTH_API_KEY, "sk-a")

        answer = ai.POOL.complete("a general question", {}, 5000)

        assert answer == "from anthropic"
        # Both of openai's models were tried and cooled before the move.
        assert transport.calls[0] == ("openai", "gpt-4o-mini")
        assert ("anthropic", "claude-3-5-haiku-latest") in transport.calls
        assert ai.POOL.cooldown_until("openai", "gpt-4o-mini") is not None

    def test_a_rejected_credential_cools_every_model_of_that_provider(self):
        seen: list[tuple[str, str]] = []

        def behaviour(request):
            seen.append((request.provider, request.model))
            if request.provider == "openai":
                return ai.AiResponse(status=401, text="bad key")
            return ai.AiResponse(
                status=200, body={"content": [{"type": "text", "text": "ok"}]}
            )

        ai.POOL.set_transport(_Transport(behaviour))
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-bad")
        ai.POOL.set_credential("anthropic", ai.AUTH_API_KEY, "sk-ok")

        assert ai.POOL.complete("q", {}, 5000) == "ok"
        # The second openai model was never attempted -- the credential is wrong
        # for the provider, not for one model.
        assert ("openai", "gpt-4o") not in seen
        assert ai.POOL.cooldown_until("openai", "gpt-4o") is not None

    def test_every_provider_failing_raises_with_the_attempt_trail(self):
        ai.POOL.set_transport(_Transport(lambda r: ai.AiResponse(status=503, text="down")))
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")

        with pytest.raises(ai.AllProvidersExhausted) as raised:
            ai.POOL.complete("q", {}, 5000)

        assert "openai/gpt-4o-mini" in str(raised.value)
        assert ai.SERVER_ERROR in str(raised.value)

    def test_a_transport_timeout_is_a_timeout_and_fails_over(self):
        def behaviour(request):
            raise TimeoutError("too slow")

        ai.POOL.set_transport(_Transport(behaviour))
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")

        with pytest.raises(ai.AllProvidersExhausted):
            ai.POOL.complete("q", {}, 5000)
        record = {r["model"]: r for r in ai.POOL.health_records()}
        assert record["gpt-4o-mini"]["failure_kind"] == ai.TIMEOUT

    def test_an_empty_completion_is_a_soft_failure_not_an_answer(self):
        ai.POOL.set_transport(
            _Transport(lambda r: ai.AiResponse(status=200, body={"choices": []}))
        )
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        with pytest.raises(ai.AllProvidersExhausted):
            ai.POOL.complete("q", {}, 5000)

    def test_with_no_provider_configured_there_is_nothing_to_call(self):
        with pytest.raises(ai.AllProvidersExhausted):
            ai.POOL.complete("q", {}, 5000)

    def test_a_cooled_model_is_skipped_without_being_called(self):
        transport = _Transport(lambda r: _openai_ok("hi"))
        ai.POOL.set_transport(transport)
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        ai.POOL.record_failure("openai", "gpt-4o-mini", ai.QUOTA_EXHAUSTED)

        assert ai.POOL.complete("q", {}, 5000) == "hi"
        assert transport.calls == [("openai", "gpt-4o")]

    def test_a_success_clears_a_previous_failure(self):
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        ai.POOL.record_failure("openai", "gpt-4o-mini", ai.SERVER_ERROR)
        assert ai.POOL.cooldown_until("openai", "gpt-4o-mini") is not None
        ai.POOL.record_success("openai", "gpt-4o-mini")
        assert ai.POOL.cooldown_until("openai", "gpt-4o-mini") is None
        kinds = {r["model"]: r["failure_kind"] for r in ai.POOL.health_records()}
        assert kinds.get("gpt-4o-mini", "") == ""


class TestPlanIsPure:
    def test_missing_credentials_are_dropped(self):
        plan = ai.plan_attempts(
            order=ai.provider_ids(),
            model_lists={"openai": ("gpt-4o-mini",), "anthropic": ("haiku",)},
            has_credential=lambda p: p == "anthropic",
            cooldown_until=lambda p, m: None,
            now=_utcnow(),
        )
        assert plan == [("anthropic", "haiku")]

    def test_a_future_cooldown_excludes_a_model(self):
        until = _utcnow().replace(year=_utcnow().year + 1)
        plan = ai.plan_attempts(
            order=("openai",),
            model_lists={"openai": ("gpt-4o-mini", "gpt-4o")},
            has_credential=lambda p: True,
            cooldown_until=lambda p, m: until if m == "gpt-4o-mini" else None,
            now=_utcnow(),
        )
        assert plan == [("openai", "gpt-4o")]

    def test_an_expired_cooldown_re_admits_a_model(self):
        past = _utcnow().replace(year=_utcnow().year - 1)
        plan = ai.plan_attempts(
            order=("openai",),
            model_lists={"openai": ("gpt-4o-mini",)},
            has_credential=lambda p: True,
            cooldown_until=lambda p, m: past,
            now=_utcnow(),
        )
        assert plan == [("openai", "gpt-4o-mini")]


# ===========================================================================
# Credentials: environment vs a runtime rotation
# ===========================================================================


class TestCredentialResolution:
    def test_no_credential_means_no_external_brain(self):
        assert ai.default_external_brain() is None

    def test_a_configured_provider_installs_the_brain(self):
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        brain = ai.default_external_brain()
        assert brain is not None
        assert callable(brain)

    def test_the_environment_is_read_when_nothing_was_rotated(self, monkeypatch):
        monkeypatch.setenv("AI_OPENAI_API_KEY", "sk-env")
        assert ai.POOL.credential_for("openai") == (ai.AUTH_API_KEY, "sk-env")
        assert ai.POOL.credential_source("openai") == "environment"

    def test_a_rotated_credential_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("AI_OPENAI_API_KEY", "sk-env")
        ai.POOL.set_credential("openai", ai.AUTH_BROWSER_SESSION, "session-x")
        assert ai.POOL.credential_for("openai") == (
            ai.AUTH_BROWSER_SESSION,
            "session-x",
        )
        assert ai.POOL.credential_source("openai") == "database"

    def test_a_browser_session_provider_reads_its_own_variable(self, monkeypatch):
        monkeypatch.setenv("AI_CHATGPT_WEB_SESSION", "web-token")
        assert ai.POOL.credential_for("chatgpt_web") == (
            ai.AUTH_BROWSER_SESSION,
            "web-token",
        )

    def test_clearing_a_credential_lets_the_environment_back_in(self, monkeypatch):
        monkeypatch.setenv("AI_OPENAI_API_KEY", "sk-env")
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-rotated")
        ai.POOL.clear_credential("openai")
        assert ai.POOL.credential_for("openai") == (ai.AUTH_API_KEY, "sk-env")

    def test_an_empty_secret_installation_suppresses_the_environment(self, monkeypatch):
        """What a disabled credential means: the env value is not a fallback."""
        monkeypatch.setenv("AI_OPENAI_API_KEY", "sk-env")
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "", status="disabled")
        assert ai.POOL.credential_for("openai") is None


# ===========================================================================
# The seam into the chat router
# ===========================================================================


class TestChatRouterSeam:
    def test_default_behaviour_is_unchanged_without_a_provider(self):
        assert chat_router._external_brain_for() is None

    def test_an_installed_brain_still_wins(self):
        sentinel = lambda q, c, b: "installed"  # noqa: E731
        chat_router.set_external_brain(sentinel)
        try:
            assert chat_router._external_brain_for() is sentinel
        finally:
            chat_router.set_external_brain(None)

    def test_a_configured_provider_becomes_the_fallback(self):
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        brain = chat_router._external_brain_for()
        assert brain is not None
        assert callable(brain)


# ===========================================================================
# Persistence: the durable shadow of the in-memory pool
# ===========================================================================


class TestPersistence:
    def test_credentials_and_cooldowns_round_trip_through_the_database(self):
        harness = SqliteHarness()
        harness.loop.run_until_complete(harness.setup())
        try:
            async def scenario():
                session = harness.session
                session.add(
                    models.AiProviderCredential(
                        provider="openai",
                        auth_mode=ai.AUTH_API_KEY,
                        secret_encrypted=ai.seal_secret("sk-stored"),
                        status="active",
                        credential_source="database",
                    )
                )
                await session.commit()

                loaded = await ai.load_persisted_state(session)
                assert loaded["credentials"] == 1
                assert ai.POOL.credential_for("openai") == (
                    ai.AUTH_API_KEY,
                    "sk-stored",
                )

                ai.POOL.record_failure("openai", "gpt-4o-mini", ai.QUOTA_EXHAUSTED)
                written = await ai.persist_health(session)
                assert written == 1

                rows = (
                    await session.execute(select(models.AiModelHealth))
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].failure_kind == ai.QUOTA_EXHAUSTED
                assert rows[0].cooldown_until is not None

            harness.loop.run_until_complete(scenario())
        finally:
            harness.loop.run_until_complete(harness.teardown())
            harness.loop.close()

    def test_a_stored_secret_is_not_plaintext(self):
        sealed = ai.seal_secret("sk-super-secret")
        assert "sk-super-secret" not in sealed
        assert ai.unseal_secret(sealed) == "sk-super-secret"

    def test_an_unreadable_secret_reads_as_absent(self):
        assert ai.unseal_secret("not-ciphertext") == ""


# ===========================================================================
# The admin surface
# ===========================================================================


class _FakeUser:
    id = 1
    username = "admin"
    is_admin = True


class _Result:
    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return []


class _DeleteResult:
    rowcount = 0


class _AdminDb:
    """A session double shaped to the four calls the admin router makes."""

    def __init__(self, credential=None):
        self.credential = credential
        self.added: list[object] = []
        self.commits = 0
        self.deletes = 0

    async def execute(self, statement):
        if str(statement).strip().upper().startswith("DELETE"):
            self.deletes += 1
            return _DeleteResult()
        return _Result(self.credential)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _admin_overrides(db):
    async def _fake_user():
        return _FakeUser()

    async def _fake_db():
        yield db

    app.dependency_overrides[deps.get_current_admin_user] = _fake_user
    app.dependency_overrides[deps.get_db] = _fake_db


def _clear_overrides():
    app.dependency_overrides.pop(deps.get_current_admin_user, None)
    app.dependency_overrides.pop(deps.get_db, None)


class TestAdminSurface:
    def test_the_snapshot_lists_every_provider_without_a_secret(self):
        _admin_overrides(_AdminDb())
        try:
            response = TestClient(app).get("/chat/admin/ai-providers")
        finally:
            _clear_overrides()
        assert response.status_code == 200
        body = response.json()
        assert body["version"] == ai.AI_PROVIDERS_VERSION
        assert {p["provider"] for p in body["providers"]} == set(ai.provider_ids())
        assert "secret" not in response.text.lower()

    def test_the_catalog_is_a_public_shape_behind_the_admin_gate(self):
        _admin_overrides(_AdminDb())
        try:
            response = TestClient(app).get("/chat/admin/ai-providers/catalog")
        finally:
            _clear_overrides()
        assert response.status_code == 200
        body = response.json()
        assert body["providers"]
        assert body["browser_sessions_are_best_effort"] is True

    def test_rotating_a_credential_stores_ciphertext_and_arms_the_pool(self):
        db = _AdminDb()
        _admin_overrides(db)
        try:
            response = TestClient(app).post(
                "/chat/admin/ai-providers/openai/credential",
                json={"auth_mode": "api_key", "secret": "sk-new", "label": "primary"},
            )
        finally:
            _clear_overrides()
        assert response.status_code == 200
        assert response.json()["configured"] is True
        assert db.commits == 1
        assert db.added, "a new credential row should have been added"
        assert "sk-new" not in str(db.added[0].secret_encrypted)
        assert ai.POOL.credential_for("openai") == (ai.AUTH_API_KEY, "sk-new")

    def test_rotating_an_unknown_provider_is_a_404(self):
        _admin_overrides(_AdminDb())
        try:
            response = TestClient(app).post(
                "/chat/admin/ai-providers/nope/credential",
                json={"auth_mode": "api_key", "secret": "sk"},
            )
        finally:
            _clear_overrides()
        assert response.status_code == 404

    def test_an_auth_mode_the_provider_does_not_accept_is_a_400(self):
        _admin_overrides(_AdminDb())
        try:
            response = TestClient(app).post(
                "/chat/admin/ai-providers/openai/credential",
                json={"auth_mode": "browser_session", "secret": "sk"},
            )
        finally:
            _clear_overrides()
        assert response.status_code == 400

    def test_clearing_a_credential_deletes_the_row_and_the_pool_copy(self):
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        db = _AdminDb()
        _admin_overrides(db)
        try:
            response = TestClient(app).delete(
                "/chat/admin/ai-providers/openai/credential"
            )
        finally:
            _clear_overrides()
        assert response.status_code == 200
        assert db.deletes == 1
        assert response.json()["configured"] is False
        assert ai.POOL.credential_for("openai") is None

    def test_resetting_cooldowns_clears_memory_and_disk(self):
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        ai.POOL.record_failure("openai", "gpt-4o-mini", ai.QUOTA_EXHAUSTED)
        db = _AdminDb()
        _admin_overrides(db)
        try:
            response = TestClient(app).post(
                "/chat/admin/ai-providers/openai/reset", json={}
            )
        finally:
            _clear_overrides()
        assert response.status_code == 200
        assert response.json()["cleared_in_memory"] >= 1
        assert db.deletes == 1
        assert ai.POOL.cooldown_until("openai", "gpt-4o-mini") is None

    def test_the_failover_probe_returns_the_attempt_trail(self):
        ai.POOL.set_transport(
            _Transport(lambda r: ai.AiResponse(status=503, text="down"))
        )
        ai.POOL.set_credential("openai", ai.AUTH_API_KEY, "sk-o")
        db = _AdminDb()
        _admin_overrides(db)
        try:
            response = TestClient(app).post(
                "/chat/admin/ai-providers/failover",
                json={"question": "what can you do?", "budget_ms": 2000},
            )
        finally:
            _clear_overrides()
        assert response.status_code == 200
        body = response.json()
        assert body["exhausted"] is True
        assert body["attempts"]
        assert "openai/gpt-4o-mini" in body["attempts"][0]
