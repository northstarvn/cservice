import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import platform

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, engine, get_db
from app import db as db_infra
from app import i18n
from app.i18n import locale_payload
from app import model_bases
from app import models
from app.schemas import audit as audit_schemas
from app import deps
from app import enrichment
from app.routers import (
    ai_providers as ai_providers_admin,
    audit,
    bookings,
    chat,
    complaints,
    identity,
    kaizen,
    offers,
    topics,
    transfers,
    users,
    webhooks,
)
from app import kaizen_runner
from app.services import audit_log, chat_analytics, customer_offers, loyalty_journey, policy_scoring, activity_tree, communication_strategy, arrears_payments, points_exchange, recovery_playbooks, retention, customer_360, customer_explain, preferences as preferences_service, self_service, auth_methods, device_recognition, storage_providers, mail, brain_router, ai_providers
# `transfers` names both a router and a service, and the router arrives in the
# `app.routers` import above. Aliasing the service follows the existing
# `preferences as preferences_service` convention rather than leaving a bare
# `transfers` for whichever import ran last to define it.
from app.services import transfers as transfers_service
from app.services.efficiency_audit import build_efficiency_audit_catalog
from app import security
from app import (
    biometric_vault,
    cell_matrix,
    explainability,
    high_throughput_pipeline,
    hsm_signer,
    model_versioning,
    optimistic_locking,
    partition_manager,
    protobuf_transaction_spec,
    regional_policy,
    real_life_flows,
    release_ladder,
    risk_evaluator,
    rule_engine,
    shadow_env,
    simulation_engine,
    tenant_router,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_NAME = os.getenv("CSERVICE_APP_NAME", "CService Booking Backend")
APP_VERSION = os.getenv("CSERVICE_APP_VERSION", "0.1.0")
APP_ENV = os.getenv("CSERVICE_ENV", os.getenv("ENV", "development"))
APP_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CSERVICE_CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173,http://localhost:8080,http://localhost:5174",
    ).split(",")
    if origin.strip()
]


# --- Decision-intelligence tooling contracts (Phase 1) -----------------------


class DecisionSimulateRequest(BaseModel):
    """What-if simulation payload: pick a kind, pass kind-specific overrides."""

    kind: str = Field(..., pattern="^(risk|retention)$")
    context: dict = Field(default_factory=dict, description="risk kind: request context to re-score")
    weight_overrides: dict[str, float] | None = Field(
        default=None, description="risk kind: rule_id -> replacement weight on the variant table"
    )
    disabled_rules: list[str] | None = Field(
        default=None, description="risk kind: rule_ids removed from the variant table"
    )
    level_max_overrides: dict[str, int] | None = Field(
        default=None, description="risk kind: level -> new max score (threshold retune)"
    )
    retention_overrides: dict[str, int] | None = Field(
        default=None, description="retention kind: policy_id -> days for the variant run"
    )
    active_partitions: list[dict] | None = Field(
        default=None, description="retention kind: partition records to replay (defaults to live active set)"
    )
    moment: str | None = Field(
        default=None, description="retention kind: ISO-8601 moment; defaults to now"
    )
    label: str = Field(default="what-if", max_length=120)


class CanaryRunRequest(BaseModel):
    """Shadow-mode canary run: serve from active, score with the candidate."""

    model_name: str = Field(..., min_length=1, max_length=80)
    context: dict = Field(default_factory=dict)
    entity_ref: str = Field(default="", max_length=200)


class CanaryPromoteRequest(BaseModel):
    """Explicit canary promotion (optimistic: stale expected_version -> 409)."""

    model_name: str = Field(..., min_length=1, max_length=80)
    expected_version: int | None = Field(default=None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        async with engine.begin() as conn:
            # Test connection first
            await conn.execute(text("SELECT 1"))
            logger.info("Database connection verified")
            
            # Create tables
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created successfully")
            
    except Exception as e:
        logger.error(f"Database startup error: {e}", exc_info=True)
        raise

    # Rehydrate the external-brain pool: operator-rotated credentials and the
    # cooldown state a previous process learned. Its own try/except, because a
    # failure here must not stop the app -- with nothing restored, the pool falls
    # back to the environment, which is exactly the pre-feature behaviour.
    try:
        async with db_infra.SessionLocal() as session:
            restored = await ai_providers.load_persisted_state(session)
        logger.info("AI provider state restored: %s", restored)
    except Exception as e:
        logger.warning(f"AI provider state could not be restored: {e}", exc_info=True)

    # Optional background workers (off by default so single-tenant behavior and
    # the test suite stay untouched; enable per deployment via env).
    partition_worker = None
    recovery_worker = None
    kaizen_worker = None
    try:
        if partition_manager.PARTITION_WORKER_ENABLED:
            partition_worker = asyncio.create_task(
                partition_manager.run_partition_cycle_forever(),
                name="partition-lifecycle",
            )
            logger.info("Partition lifecycle worker started")
        if recovery_playbooks.AUTO_RECOVERY_ENABLED:
            recovery_worker = asyncio.create_task(
                recovery_playbooks.auto_recovery_worker_forever(),
                name="recovery-playbooks",
            )
            logger.info("Recovery playbook worker started")
        if high_throughput_pipeline.PIPELINE_AUTOSTART:
            await high_throughput_pipeline.get_default_pipeline().start()
        if kaizen_runner.KAIZEN_AUTORUN:
            # The self-check. Off by default, like the three above, and for the
            # same reason plus one more: it costs 21 flow simulations, twelve
            # module resolutions and a walk of every served route, so a developer
            # boot should not pay for it unasked.
            #
            # It runs the sweep *immediately* rather than after the first
            # interval, so a deployment that enables it and then crashes still
            # leaves evidence that it ever started. And it never writes to
            # BLOCKAGES.md unless the deployment opted in -- see
            # app/kaizen_runner.py for why an automatic writer is worse than no
            # writer.
            kaizen_worker = asyncio.create_task(
                kaizen_runner.sweep_forever(),
                name="kaizen-sweep",
            )
            logger.info(
                "Kaizen sweep worker started (every %ss, append=%s)",
                kaizen_runner.KAIZEN_INTERVAL_SECONDS,
                kaizen_runner.KAIZEN_APPEND,
            )
    except Exception as e:
        logger.error(f"Background worker startup error: {e}", exc_info=True)

    yield  # App runs here
    
    # Shutdown
    try:
        if partition_worker is not None:
            partition_worker.cancel()
            try:
                await partition_worker
            except asyncio.CancelledError:
                pass
        if recovery_worker is not None:
            recovery_worker.cancel()
            try:
                await recovery_worker
            except asyncio.CancelledError:
                pass
        if kaizen_worker is not None:
            kaizen_worker.cancel()
            try:
                await kaizen_worker
            except asyncio.CancelledError:
                pass
        await high_throughput_pipeline.get_default_pipeline().stop()
        await tenant_router.registry.dispose_all()
    except Exception as e:
        logger.error(f"Background worker shutdown error: {e}", exc_info=True)
    await engine.dispose()
    logger.info("Database engine disposed")

app = FastAPI(
    lifespan=lifespan,
    title=APP_NAME,
    version=APP_VERSION,
    description="Backend services for customer support, bookings, chat analytics, and retention workflows.",
)

# Enhanced CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=APP_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.error(f"Global exception handler caught: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )

app.include_router(users.router, prefix="/users", tags=["users"])
# Sign-in methods, device recognition, trusted devices, linked identities and
# connected storage. Mounted under the same /users prefix as the router above
# rather than given its own top-level one: these are all operations *on the
# signed-in account*, so a client holding a session should not have to know that
# the sign-in surface lives somewhere else. Two routers on one prefix is fine --
# every path here is a distinct literal, so nothing shadows anything above.
app.include_router(identity.router, prefix="/users", tags=["identity"])
# Credit transfers and third-party debt settlement. Its own prefix, not
# folded into /chat or /users: these are the only routes where one
# authenticated customer moves value out of their own account on behalf of
# another, which is a different exposure and gets its own authz rule.
app.include_router(transfers.router, prefix="/transfers", tags=["transfers"])
app.include_router(bookings.router, prefix="/bookings", tags=["bookings"])
app.include_router(chat.router, tags=["chat"])
# External-brain provider pool: inspect the providers, rotate a credential,
# reset a cooldown, force one failover. The router carries its own
# /chat/admin/ai-providers prefix so it inherits the existing admin rule by
# prefix rather than adding a new AUTHZ_RULES row.
app.include_router(ai_providers_admin.router, tags=["ai-providers"])
app.include_router(topics.router, tags=["topics"])
app.include_router(audit.router, prefix="/audit", tags=["audit"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(complaints.router, tags=["complaints"])
# Customer-facing recovery offers (Stage B). No prefix: the paths already carry
# /chat, and a prefix here produced /chat/chat/me/offers on the first attempt.
# Authorization is inherited from the existing /chat wildcards *by prefix* --
# chat_reads / chat_writes for the customer's half, chat_admin (which carries
# loa2 step-up, correct for a human committing a credit) for the operator's --
# so a route added under either prefix inherits its gate rather than having to
# remember it. No new AUTHZ_RULES rows, for the same reason.
app.include_router(offers.router, tags=["offers"])
# The release surface. Admin-gated on every route, and deliberately NOT under /meta*:
# /meta* is public by intent and three of these endpoints mutate. See the note in
# app/routers/kaizen.py and the BLOCKAGES entry for this subsystem.
# No prefix: the router's own paths already carry /kaizen, and adding a prefix here
# produced /kaizen/kaizen/... on the first attempt, which the authz drift report
# caught immediately as 16 unclassified routes.
app.include_router(kaizen.router, tags=["kaizen"])


@app.get("/meta")
async def app_metadata():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "cors_origins": APP_CORS_ORIGINS,
        "locale": locale_payload(),
        "features": [
            "auth",
            "bookings",
            "booking_events",
            "chat",
            "chat_history_summary",
            "retention",
            "topic_intelligence_overview",
            "efficiency_audit",
            "regional_policy",
            "rule_engine",
            "recovery_playbooks",
            "topic_request_governance",
            "model_bases_governance",
            "policy_scoring_governance",
            "authz_governance",
            "i18n_governance",
            "contract_governance",
            "thin_infra_governance",
            "customer_360",
            "customer_explainability",
            "preference_consent_center",
            "customer_visible_recovery",
            "self_service_status",
            "recovery_offers",
            "loyalty_status_stage_c",
            "care_loop_stage_d",
            "regional_care_stage_e",
            "policy_motion_stage_f",
            "kaizen_release_surface",
            "complaint_learning",
            # The sign-in surface is listed as three features rather than one
            # "auth" because they fail independently and are governed
            # separately: the methods available, how much proof a login is asked
            # for, and what storage a customer may connect.
            "auth_methods",
            "device_recognition",
            "storage_providers",
            # Mail is listed separately because the one-time-code path's
            # trustworthiness is conditional on it: a deployment that cannot
            # deliver must be able to discover that from /meta rather than from a
            # customer reporting they never received a code.
            "mail",
            # Which brain answers a customer, and the budgets that keep
            # that decision from being the slow part of a reply.
            "brain_router",
            # The list of AI services the external brain can answer from, and
            # the quota failover that moves between them. Separate from
            # brain_router: choosing *that* a model should answer is the router's
            # job, choosing *which* one still can is this feature's.
            "ai_provider_failover",
            "customer_transfers",
        ],
    }


@app.get("/meta/capabilities")
async def app_capabilities():
    return chat._build_capabilities_payload()


@app.get("/meta/ecosystem")
async def app_ecosystem():
    capabilities = chat._build_capabilities_payload()
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "subservices": {
            "identity": {
                "routes": [
                    "/users/me",
                    "/users/password",
                    "/users/refresh",
                    "/users/logout",
                    "/users/me/sessions",
                    "/users/me/step-up",
                    "/users/me/api-keys",
                    "/users/auth/methods",
                    "/users/auth/email-otp/request",
                    "/users/auth/email-otp/verify",
                    "/users/auth/login",
                    "/users/auth/device",
                ],
                "purpose": "authentication, account management, and session lifecycle",
                "status": "ready",
            },
            # Split from `identity` because the useful question about these is
            # different: not "is sign-in working" but "which credentials are on
            # offer, and how much proof does this login get asked for".
            "device_recognition": {
                "routes": [
                    "/users/me/devices",
                    "/users/me/recognition",
                    "/users/me/identities",
                ],
                "purpose": (
                    "how much proof a login is asked for. Scored from device, network "
                    "and habit signals; decides which credential to demand, never "
                    "whether to admit"
                ),
                "status": "ready",
                "mode": device_recognition.recognition_mode(),
                "modes": list(device_recognition.RECOGNITION_MODES),
                "grants_access": False,
            },
            "storage_providers": {
                "routes": [
                    "/users/me/storage",
                    "/users/me/storage/providers",
                    "/users/me/storage/connect",
                    "/users/me/storage/callback",
                ],
                "purpose": "connecting a customer's own cloud storage under their own authority",
                "status": "ready",
                "configured": [
                    name
                    for name in storage_providers.STORAGE_PROVIDER_BY_NAME
                    if storage_providers.is_configured(name)
                ],
                # Whether a connection can actually reach `active`, decided the
                # same way the handler decides it rather than being asserted.
                # `implemented` alone would read as "this works" while a
                # deployment still holds at `pending`.
                "token_exchange_implemented": True,
                "outbound_http_transport": bool(
                    identity.transport_hook_is_installed()
                ),
                "can_complete": (
                    bool(
                        [
                            name
                            for name in storage_providers.STORAGE_PROVIDER_BY_NAME
                            if storage_providers.is_configured(name)
                        ]
                    )
                    and identity.transport_hook_is_installed()
                    and bool(os.getenv("CSERVICE_STORAGE_REDIRECT_URI", ""))
                ),
                "note": (
                    "the authorization-code exchange is implemented and verified up to "
                    "the network call, but this deployment has no outbound HTTP client, "
                    "so the callback reports 503 and leaves the connection 'pending' "
                    "with the reason recorded rather than reporting a connection live "
                    "that cannot reach the provider"
                ),
            },
            "outbound_mail": {
                "transport": mail.configured_transport(),
                "reports_delivery": mail.transport_reports_delivery(),
                "smtp_configured": bool(os.getenv("CSERVICE_SMTP_HOST", "")),
                "note": (
                    "the simulated transport is the default and cannot deliver. It "
                    "exists so the one-time-code path works end to end without a mail "
                    "server; POST /users/auth/email-otp/request reports delivered=false "
                    "in that configuration instead of claiming a code is on its way."
                ),
            },
            "ai_provider_failover": {
                "routes": [
                    "/chat/admin/ai-providers",
                    "/chat/admin/ai-providers/catalog",
                    "/chat/admin/ai-providers/{provider}/credential",
                    "/chat/admin/ai-providers/{provider}/reset",
                    "/chat/admin/ai-providers/failover",
                ],
                "purpose": (
                    "answering general questions from a list of AI services and "
                    "moving to the next when one runs out of quota, with browser-"
                    "session and API-key credentials both first-class"
                ),
                "status": "ready",
                "configured": ai_providers.POOL.available_providers(),
                "providers": list(ai_providers.provider_ids()),
                "auth_modes": list(ai_providers.AUTH_MODES),
                # Named rather than implied: a browser-session request is against
                # a consumer web app that changes without notice, and an operator
                # deserves to know which providers carry that caveat.
                "browser_providers_best_effort": [
                    spec.provider
                    for spec in ai_providers.AI_PROVIDERS
                    if spec.browser_best_effort
                ],
                "note": (
                    "credentials come from the environment and can be rotated at "
                    "runtime through the admin surface; cooldowns persist so a quota "
                    "that ended is not re-probed on every restart"
                ),
            },
            "booking_core": {
                "routes": ["/bookings", "/bookings/analytics/summary", "/bookings/analytics/events"],
                "purpose": "booking lifecycle, change tracking, and analytics",
                "status": "ready",
            },
            "chat_intelligence": {
                "routes": [
                    "/chat/history",
                    "/chat/insights",
                    "/chat/system-priorities",
                    "/chat/trends",
                    "/chat/retention-dashboard",
                    "/chat/loyalty-journey",
                ],
                "purpose": "conversation memory, sentiment analysis, and retention scoring",
                "status": "ready",
            },
            "retention_policy": {
                "routes": [
                    "/chat/admin/retention-health",
                    "/chat/admin/retention-forecast",
                    "/chat/admin/retention-forecast-sweep",
                    "/chat/admin/retention-cluster-coverage",
                ],
                "purpose": (
                    "config-driven health banding over the retention snapshot series, "
                    "series anomaly detection, forward projection with an explicit "
                    "confidence decay, and topic-cluster coverage including the "
                    "clusters a customer never speaks to"
                ),
                "status": "ready",
                "config_tables": [
                    "RETENTION_TOPIC_HINTS",
                    "RETENTION_TOPIC_CLUSTERS",
                    "RETENTION_CHURN_RANK",
                    "RETENTION_SAMPLE_THRESHOLDS",
                    "RETENTION_HEALTH_RULES",
                    "RETENTION_ANOMALY_RULES",
                    "RETENTION_FORECAST_RULES",
                ],
            },
            "retention_ops": {
                "routes": [
                    "/chat/admin/snapshot-health-score",
                    "/chat/admin/snapshot-operations-report",
                    "/chat/admin/snapshot-operations-status",
                    "/chat/admin/snapshot-operations-compliance",
                    "/chat/admin/snapshot-operations-launch-readiness",
                    "/chat/admin/snapshot-operations-gonogo",
                ],
                "purpose": "snapshot health, compliance, launch readiness, and go/no-go checks",
                "status": "ready" if capabilities["coverage"]["status"] == "ready" else "partial",
                "coverage": {
                    "routes": 7,
                    "freshness_window_days": capabilities["coverage"]["freshness_window_days"],
                },
            },
            "portfolio_intelligence": {
                "routes": ["/chat/admin/monetization-cohorts", "/meta/capabilities"],
                "purpose": "cross-segment monetization, capability discovery, and study-driven role coverage",
                "status": "ready" if capabilities["coverage"]["status"] == "ready" else "partial",
                "roles": [
                    "youth_conversion_intelligence",
                    "market_penetration_adoption",
                    "device_experience_optimizer",
                    "cpc_economics_profiler",
                    "older_adult_value_model",
                    "low_penetration_engagement_engine",
                ],
            },
            "topic_intelligence": {
                "routes": [
                    "/topics/search",
                    "/topics/suggestions",
                    "/topics/workspace",
                    "/topics/overview",
                    "/topics/intelligence",
                    "/topics/taxonomy",
                    "/topics/ranked",
                    "/topics/governance",
                    "/topics/integrity",
                    "/topics/match",
                    "/topics/validate",
                    "/topics/drift",
                    "/topics/lifecycle",
                ],
                "purpose": "topic catalog search, ranked suggestions, workspace portfolio, selection governance, and cross-service topic intelligence",
                "status": "ready",
            },
            "activity_monitoring": {
                "routes": ["/chat/activity-tree", "/chat/admin/activity-tree"],
                "purpose": "tree-structured customer activity monitoring with configurable grouping, ranking, smart filtering, and anomaly highlighting",
                "status": "ready",
            },
            "communication_strategy": {
                "routes": [
                    "/chat/communication-strategy",
                    "/chat/admin/communication-strategy",
                    "/chat/admin/communication-overrides",
                ],
                "purpose": "communicate with users by precedence-ordered criteria: admin select, policy defined, culture, user profile (incl. history stats), and session mood",
                "status": "ready",
            },
            "arrears_payments": {
                "routes": [
                    "/chat/payments/arrears/quote",
                    "/chat/payments/arrears",
                    "/chat/admin/payments/arrears",
                    "/chat/admin/payments/arrears/{entry_id}/settle",
                    "/chat/admin/payments/arrears/{entry_id}/waive-interest",
                ],
                "purpose": "pay in arrears with interest applied by policy-selected terms (premium gets the best rates, high-risk gets short capped deferrals)",
                "status": "ready",
            },
            "points_exchange": {
                "routes": [
                    "/chat/points/exchange/rates",
                    "/chat/points/wallet",
                    "/chat/points/transactions",
                    "/chat/points/exchange/quote",
                    "/chat/points/exchange",
                    "/chat/admin/points/exchange",
                ],
                "purpose": "convert/exchange back and forth between certain types of points and money/currencies with config-driven rates, fees, and daily caps",
                "status": "ready",
            },
            "efficiency_audit": {
                "routes": [
                    "/audit/efficiency",
                    "/audit/enhancements",
                    "/audit/catalog",
                ],
                "purpose": "classify system components by efficiency and propose prioritized enhancements",
                "status": "ready",
            },
            "audit_trail": {
                "routes": [
                    "/audit/log",
                    "/audit/logs",
                    "/audit/logs/summary",
                    "/audit/trail-catalog",
                    "/audit/logs/view",
                    "/audit/integrity/gates",
                ],
                "purpose": "immutable operator/system action trail for explainability, with action/severity catalog, rollups, per-audience read profiles, and a config-driven integrity gate table",
                "status": "ready",
                "config_tables": [
                    "CONTRACT_KINDS",
                    "CONTRACT_AUDIENCES",
                    "CONTRACT_FIELD_POLICIES",
                    "CONTRACT_TRIVIAL_FIELDS",
                    "CONTRACT_FORBIDDEN_FIELDS",
                    "CONTRACT_INVENTORY",
                    "CONTRACT_WRITE_PATHS",
                    "CONTRACT_OPS",
                    "CONTRACT_WARNINGS",
                ],
                "notes": (
                    "the 40 pydantic models in app/schemas/audit.py are the client-facing "
                    "contract for every /audit/* route, and the nine tables above describe "
                    "them: which route serves each, which audience it is for, and where its "
                    "allowed values are actually enforced. The posture is report, never "
                    "repair -- a model in that module is not a rendering of a policy, it is "
                    "the policy a client binds to, and widening one to silence a finding "
                    "would delete the only place a vocabulary was written down. So a "
                    "non-empty finding list is the expected state, not a broken endpoint. "
                    "Two things it deliberately does not verify: the DDL, because the "
                    "audit_log_entries.severity CHECK constraint is documented in "
                    "CONTRACT_FIELD_POLICIES and not checked from a schemas module; and the "
                    "route table without an openapi document, in which case the two "
                    "route-level checks report themselves as skipped rather than passing. "
                    "The headline finding is that POST /audit/log stores its detail blob "
                    "verbatim while POST /audit/log/auditable scrubs it, and the two "
                    "declare the field identically -- Dict[str, Any], no description -- so "
                    "a client reading the OpenAPI document cannot tell which is which."
                ),
            },
            "localization": {
                "routes": [
                    "/meta/i18n",
                    "/meta/scoring-catalog",
                ],
                "purpose": "locale resolution and message translation across en/es/fr with fallback to English",
                "status": "ready",
                "config_tables": [
                    "MESSAGE_NAMESPACES",
                    "PLACEHOLDER_POLICIES",
                    "LOCALE_EXPECTATIONS",
                    "NUMBER_FORMATS",
                    "RENDER_OPS",
                    "I18N_WARNINGS",
                ],
                "notes": (
                    "the resolution and render path is unchanged: SUPPORTED_LOCALES, "
                    "MESSAGE_CATALOG, PLURAL_CATEGORIES, RTL_LOCALES, translate, "
                    "_render_template, resolve_locale, negotiate_locale and "
                    "parse_accept_language all keep their exact signatures, payloads and "
                    "behaviour, and build_i18n_catalog keeps its key set. The governance "
                    "layer reports and never repairs. Every RENDER_OPS row is report_only, "
                    "because the renderer catches KeyError and returns the joined string, so "
                    "fixing a leak changes a string that is already being served. format_number "
                    "is offered, not wired in: the plural '#' still renders as str(int(n)). The "
                    "catalog ships 17 defect-severity findings, all verified against the shipped "
                    "functions and asserted in the tests -- a missing value leaks the raw "
                    "{name} token, a plural message with no count renders its zero branch, a "
                    "dotted field such as {a.b} raises AttributeError straight out of "
                    "translate() because the renderer's except clause does not cover it, "
                    "negotiate_locale reports the whole Accept-Language header as 'requested', "
                    "and resolve_locale and negotiate_locale disagree on fallback_used for the "
                    "same request. RTL_LOCALES names four locales and PLURAL_CATEGORIES six "
                    "categories for Arabic, none of which the registry ships, so direction is "
                    "always 'ltr' and the Arabic branches are unreachable; that is reported, "
                    "not repaired"
                ),
            },
            "tenant_routing": {
                "routes": [
                    "/meta/tenants",
                    "/meta/tenants/policy",
                ],
                "purpose": "runtime multi-tenant database connection routing: per-tenant pooled engines, registration/deregistration, header-driven tenant scope",
                "status": "ready",
                "mode": tenant_router.build_tenant_router_catalog()["mode"],
            },
            "partition_management": {
                "routes": [
                    "/meta/partitions",
                    "/meta/partitions/policy",
                ],
                "purpose": "dynamic data partition lifecycle: config-driven partition creation, archival, and retention-based expiry for append-only volumes",
                "status": "ready",
                "worker_enabled": partition_manager.PARTITION_WORKER_ENABLED,
            },
            "zero_trust": {
                "routes": [
                    "/meta/zero-trust",
                    "/meta/zero-trust/policy",
                ],
                "purpose": "cryptographic security & zero-trust access: HSM signing abstractions, biometric validation vaults, contextual risk evaluation, and data-cell access matrices",
                "status": "ready",
                "signer_algorithm": hsm_signer.build_hsm_signer_catalog()["algorithm"],
            },
            "thin_infra_governance": {
                "routes": [
                    "/meta/partitions/policy",
                    "/meta/tenants/policy",
                    "/meta/zero-trust/policy",
                ],
                "purpose": (
                    "the policy layer over the five thinnest infrastructure "
                    "modules: a named retention tier and guard band per "
                    "partitioned table, a legal-transition state machine and a "
                    "data-residency allowlist per tenant, a total order on key "
                    "strength with scheduled rotation and overlap-gated "
                    "retirement, enrollment-quality and presentation-attack "
                    "bounds, explicit risk composition with escalation and "
                    "expiring suppression, and a narrowing-only access layer of "
                    "purpose tags, read budgets and masking profiles"
                ),
                "status": "ready",
                "config_tables": [
                    "PARTITION_RETENTION_TIERS",
                    "PARTITION_GUARD_BANDS",
                    "PARTITION_DROP_POLICY",
                    "TENANT_RESIDENCY",
                    "TENANT_LIFECYCLE",
                    "KEY_STRENGTH_RANKS",
                    "KEY_ROTATION_SCHEDULE",
                    "KEY_RETIREMENT_RULES",
                    "ENROLLMENT_RULES",
                    "PAD_PROFILES",
                    "RISK_COMPOSERS",
                    "RISK_LEVEL_OVERRIDES",
                    "RISK_ESCALATION",
                    "RISK_SUPPRESSION",
                    "CELL_PURPOSES",
                    "CELL_BUDGETS",
                    "MASKING_PROFILES",
                ],
                "notes": (
                    "no new module and no new table. Every capability is a "
                    "module-level config table plus a pure helper plus a sibling "
                    "policy catalog, so the pre-existing catalogs keep their "
                    "exact key sets and a rule is a data change. The six "
                    "expanded modules previously reported only what existed; "
                    "they now also report the rule that bounds it"
                ),
            },
            "high_velocity_audit": {
                "routes": [
                    "/audit/pipeline/event",
                    "/audit/pipeline/stats",
                    "/audit/pipeline/dead-letters",
                    "/audit/pipeline/replay",
                    "/audit/transactions",
                    "/audit/transactions/spec",
                    "/audit/transactions/query",
                    "/audit/transactions/integrity",
                    "/audit/transactions/export",
                ],
                "purpose": "async high-throughput audit pipeline encoding immutable, append-only protobuf transactions for security signals, with a bounded dead-letter ring that can be inspected and replayed",
                "status": "ready",
                "autostart": high_throughput_pipeline.PIPELINE_AUTOSTART,
            },
            "decision_intelligence": {
                "routes": [
                    "/meta/decisions",
                    "/meta/decisions/simulate",
                    "/meta/decisions/canary/run",
                    "/meta/decisions/canary/promote",
                    "/meta/decisions/explanations",
                    "/meta/decisions/explanations/{decision_id}",
                ],
                "purpose": "decision quality & consistency: formal model versioning with shadow-mode canary, what-if simulation, explainability traces, optimistic concurrency",
                "status": "ready",
                "canary_auto_promote": model_versioning.CANARY_AUTOPROMOTE,
                "notes": (
                    "the two canary routes are admin-gated, unlike the rest of "
                    "/meta*: canary/run moves the model's divergence counters and "
                    "canary/promote decides which model scores every customer, so "
                    "'public by intent' -- a statement about discovery -- cannot "
                    "cover them. See AUTHZ_RULES['decision_canary_admin']"
                ),
            },
            "rule_engine": {
                "routes": [
                    "/meta/scoring-catalog",
                ],
                "purpose": "shared when-DSL rule engine core: unified condition evaluation (combinators, date-window operators, expression params) that the loyalty, communication, points, and arrears engines delegate to",
                "status": "ready",
            },
            "data_model": {
                "routes": [
                    "/meta/schema",
                ],
                "purpose": "declarative catalog of the ORM schema: per-column sensitivity and redaction presets, the vocabularies string columns are meant to hold but do not enforce, per-table write mode and retention advisory, the relationship graph, and an advisory report of the constraints the schema does not have",
                "status": "ready",
                "advisory_sections": [
                    "schema_gaps.check_constraint_coverage",
                    "schema_gaps.referential_integrity_gaps",
                    "schema_gaps.unbacked_enum_like_columns",
                ],
            },
            "regional_policy": {
                "routes": [
                    "/meta/regional",
                    "/meta/scoring-catalog",
                ],
                "purpose": "regional booking & tax policy: per-region calendars (holidays/weekends), labor-compliance guardrails, and per-jurisdiction tax rates for service types",
                "status": "ready",
            },
            "recovery_automation": {
                "routes": [
                    "/chat/recovery/playbooks",
                    "/chat/admin/recovery/playbooks",
                ],
                "purpose": "predictive sentiment & automated service recovery: realtime dissatisfaction indicators matched against config-driven when-DSL playbooks that auto-issue credit points, escalate tickets, and apply policy-score guardrails",
                "status": "ready",
                "auto_recovery_enabled": recovery_playbooks.AUTO_RECOVERY_ENABLED,
            },
            "recovery_governance": {
                "routes": [
                    "/chat/admin/recovery-guards",
                    "/chat/admin/recovery-analytics",
                    "/chat/admin/recovery-outreach-plan",
                ],
                "purpose": (
                    "the guard layer that bounds automated recovery: declarative "
                    "per-run, cooldown, per-day and per-metric budget limits "
                    "evaluated before dispatch, a registry-backed action policy "
                    "table, an aggregate spend/effectiveness rollup, and a "
                    "composed outreach preview that issues nothing"
                ),
                "status": "ready",
                "governance_version": recovery_playbooks.RECOVERY_GOVERNANCE_VERSION,
                "config_tables": [
                    "RECOVERY_OUTREACH_PLAYBOOKS",
                    "RECOVERY_ACTION_SPECS",
                    "RECOVERY_GUARD_RULES",
                    "RECOVERY_CALLBACK_PLANS",
                    "RECOVERY_SAVE_INCENTIVES",
                    "RECOVERY_REVIEW_RULES",
                    "RECOVERY_STAGE_RULES",
                ],
                "notes": (
                    "guards exist because the automated sweep is a loop: "
                    "credit_points derives its amount from the current "
                    "dissatisfaction score, so an unbounded sweep re-credits a "
                    "persistently-negative customer the same goodwill amount "
                    "every pass"
                ),
            },
            "topic_request_governance": {
                "routes": [
                    "/topics/governance/routes",
                    "/topics/governance/params",
                    "/topics/governance/limits",
                    "/topics/governance/capacity",
                    "/topics/governance/requests",
                    "/topics/governance/validation",
                    "/topics/governance/drift",
                    "/topics/plan",
                ],
                "purpose": (
                    "the governance layer over this router's caller-supplied "
                    "input: declared per-parameter bounds with clamp-vs-reject "
                    "semantics, per-route response caps and cache classes, rate "
                    "tiers, a cost/SLO model, a table-vs-router drift check, and "
                    "an offline planner that answers what a request would do "
                    "without sending it"
                ),
                "status": "ready",
                "advisory": True,
                "config_tables": [
                    "TOPIC_ROUTE_POLICIES",
                    "TOPIC_PARAM_BOUNDS",
                    "TOPIC_RESPONSE_LIMITS",
                    "TOPIC_CACHE_POLICIES",
                    "TOPIC_RATE_TIERS",
                    "TOPIC_CAPACITY_RULES",
                    "TOPIC_ERROR_MAP",
                ],
                "notes": (
                    "advisory by design: the table describes the router and the "
                    "planner predicts, but no existing route is gated. Planning "
                    "never spends rate budget, and the recorder is the only "
                    "piece in the request path"
                ),
            },
            "model_bases_governance": {
                "routes": [
                    "/meta/model-bases",
                    "/meta/scoring-catalog",
                    "/meta/schema",
                ],
                "purpose": (
                    "the governance layer under the shared model bases: a field "
                    "sensitivity vocabulary layered over the untouched "
                    "serialization denylist, named payload profiles with an "
                    "unknown-name fallback to the most restrictive one, four "
                    "mixins for new tables (slug, approval state machine, minor "
                    "units, idempotency fingerprint), and a validator that says "
                    "which mixin column names existing tables already own"
                ),
                "status": "ready",
                "new_tables_only": True,
                "config_tables": [
                    "MODEL_BASES_OPS",
                    "SLUG_RESERVED",
                    "SERIALIZATION_CLASSES",
                    "SERIALIZATION_PROFILES",
                    "MASK_OPS",
                    "APPROVAL_STATES",
                    "CURRENCY_EXPONENTS",
                    "MONEY_OPS",
                    "SEVERITY_BANDS",
                    "SECURITY_EVENT_SOURCES",
                    "SECURITY_EVENT_QUERY_OPS",
                    "MIXIN_CATEGORIES",
                ],
                "notes": (
                    "no existing table gained a column and no existing config "
                    "table changed. The four mixins are available for new tables "
                    "only: applying one to a mapped table is a DDL change. A "
                    "column name that both a mixin and an existing table declare "
                    "is reported as a warning, not blocked"
                ),
            },
            "authz_governance": {
                "routes": [
                    "/meta/authz",
                    "/meta/scoring-catalog",
                ],
                "purpose": (
                    "the authorization layer as data: the scope vocabulary "
                    "grounded in what app.routers.users will actually issue, the "
                    "role-to-scope relationship kept advisory and never applied "
                    "at resolution time, the denial contract for all six "
                    "require_* factories, the assurance and rate tiers, a "
                    "per-route description of the gate that actually runs, and a "
                    "pure decision engine that can answer who-may-do-what "
                    "without a request or a database"
                ),
                "status": "ready",
                "config_tables": [
                    "SCOPE_CATALOG",
                    "ROLE_SCOPE_GRANTS",
                    "AUTHZ_DENIALS",
                    "STEP_UP_RANKS",
                    "RATE_TIERS",
                    "AUTHZ_RULES",
                    "AUTHZ_PROBES",
                    "AUTHZ_CHECK_ORDER",
                    "AUTHZ_CHECK_DENIALS",
                    "AUTHZ_EXPOSURE_RANK",
                    "AUTHZ_DEPENDENCY_NAMES",
                    "AUTHZ_FACTORY_PREFIXES",
                    "AUTHZ_OPS",
                    "AUTHZ_VERSION",
                ],
                "notes": (
                    "the request path is unchanged: same principal resolution, "
                    "same status codes, same denial payloads, and every existing "
                    "dependency keeps its exact signature. The status codes the "
                    "factories raise now read AUTHZ_DENIALS, and "
                    "authz_denial_contract proves the declaration against the "
                    "live code by invoking each factory -- so a payload that "
                    "drifts from its declaration is an error, not a surprise. "
                    "USER_ROLES is untouched. ROLE_SCOPE_GRANTS is deliberately "
                    "NOT applied at resolution time: require_scopes reads granted "
                    "scopes only, so synthesizing them from roles would start "
                    "authorizing calls that are denied today. Each rule's "
                    "hardening block is a proposal and nothing reads it at "
                    "request time. authz_drift_report reports every live "
                    "method+path pair against the table while listing all "
                    "six require_* factories as bound to no route at all"
                ),
            },
            "customer_360": {
                "routes": [
                    "/chat/customer-360",
                    "/chat/admin/customer-360",
                ],
                "purpose": (
                    "one aggregated view of everything already known about a "
                    "customer: profile, interactions, sentiment, recovery, journey, "
                    "communication, payments, points and preferences"
                ),
                "status": "ready",
                "composition_only": True,
                "sections": list(customer_360.CUSTOMER_360_SECTIONS),
                "notes": (
                    "an aggregator, not a store: every input is composed from an "
                    "engine that already existed (chat_analytics, loyalty_journey, "
                    "recovery_playbooks, communication_strategy, points_exchange, "
                    "arrears_payments, policy_scoring), because a Customer 360 that "
                    "keeps its own copy of a score becomes a second source of truth "
                    "that disagrees with the first. A section that cannot be built is "
                    "reported in the summary as incomplete rather than defaulted to a "
                    "healthy value, so a failed payments query does not read as 'no "
                    "open payments'. The admin rollup is built from aggregate rows and "
                    "publishes omitted_by_design: it is not N per-user 360 builds, "
                    "which would be O(users x 8 engines) with a model call per user"
                ),
            },
            "customer_explainability": {
                "routes": [
                    "/chat/me/explanations",
                    "/chat/admin/explanation-vocabulary",
                ],
                "purpose": (
                    "plain-language explanation of the decisions a customer already "
                    "has, with the arithmetic that produced each one"
                ),
                "status": "ready",
                "complements": "decision_intelligence",
                "config_tables": [
                    "SCORE_BANDS",
                    "INVERSE_SCORE_BANDS",
                    "CHURN_RISK_PHRASES",
                    "RECOVERY_READINESS_PHRASES",
                    "POLICY_TIER_PHRASES",
                    "CONTROL_POSTURE_PHRASES",
                    "ACCESS_BAND_PHRASES",
                    "VALUE_TIER_PHRASES",
                    "LIFECYCLE_STAGE_PHRASES",
                    "RECOVERY_ACTION_CUSTOMER_IMPACT",
                    "RECOVERY_STATUS_PHRASES",
                    "NEXT_STEP_TEMPLATES",
                    "EXPLANATION_CODES",
                ],
                "notes": (
                    "a second, business vocabulary over the decisions a customer would "
                    "ask about. app/explainability.py narrates infrastructure decisions "
                    "(risk score, canary, model promotion) and its 'customer' audience "
                    "is a redaction profile over that infra vocabulary; neither can "
                    "render 'why is my loyalty score 62'. Three properties make this "
                    "useful rather than marketing copy: every explanation carries the "
                    "real number and the threshold it was compared against, nothing is "
                    "softened (a churn risk of high is described as a churn risk of "
                    "high), and a factor's impact is derived from the sign of its "
                    "contribution so a term can never be described as helping when it "
                    "subtracted from the score. Scores are banded because a bare 0-100 "
                    "number implies a precision build_summary's capped heuristics do not "
                    "have -- but the number always travels alongside the band, since "
                    "banding without it would be the dishonest version"
                ),
            },
            "preference_consent_center": {
                "routes": [
                    "/chat/me/preferences",
                    "/chat/me/consent-history",
                    "/chat/admin/preference-catalog",
                ],
                "purpose": (
                    "customer-owned communication preferences and consent grants, with "
                    "a provable append-only trail of every grant and revocation"
                ),
                "status": "ready",
                "config_tables": [
                    "PREFERENCE_CATALOG",
                    "PREFERENCE_CATEGORIES",
                    "CONSENT_PURPOSES",
                    "FREQUENCY_COOLDOWN_HOURS",
                    "PREFERENCE_CATALOG_VERSION",
                ],
                "notes": (
                    "this fills the layer the communication ladder was missing. "
                    "resolve_communication_strategy picks a channel in precedence order "
                    "from four layers (admin select, policy, culture, profile) and none "
                    "of them is the customer's stated wish. The preference is applied at "
                    "the presentation point rather than inside that engine, whose output "
                    "is pinned by existing consumers. Two deliberate decisions: the "
                    "consent gate applies to marketing and analytics but NOT to service or "
                    "recovery, because a customer who reported a problem must not have "
                    "the fix suppressed by a marketing setting -- CONSENT_GATED_PURPOSES "
                    "publishes which purposes actually gate so that is a stated property "
                    "and not a gap. And show_recovery_activity defaults to True, because a "
                    "transparency feature hidden by default is only discoverable after the "
                    "incident it would have explained"
                ),
            },
            "policy_motion_stage_f": {
                "routes": [
                    "/kaizen/admin/policy-motion",
                    "/kaizen/admin/policy-motion/ledger",
                    "/kaizen/admin/trust-continuity",
                    "/kaizen/admin/value-trajectory",
                ],
                "purpose": (
                    "Stage F: record why a rule changed, and whether what we "
                    "promise still holds for somebody who believed the old version"
                ),
                "why": (
                    "the exposure arrived with Stages C and D. this codebase is "
                    "full of tables that decide things about people, none of them "
                    "left a trace when edited, and that was survivable while the "
                    "edits were rare and reviewed. it stops being survivable the "
                    "moment something learns -- care weights derive emphasis from "
                    "observed complaints, offer outcomes rank generosity rules by "
                    "measured fulfilment -- because now a table can change itself "
                    "and nobody downstream can tell what moved or why"
                ),
                "invariants": [
                    "a motion records and never applies; an engine that retunes its "
                    "own table from its own outputs makes that table unauditable",
                    "the evidence bar is per class of change, and a change to what "
                    "somebody is owed needs a comparison rather than a count",
                    "opinion never justifies anything on its own",
                    "an unclassifiable change is measured against the strictest "
                    "class as a floor, and clearing that floor is not a verdict",
                    "a motion with no reason and no proposer is refused",
                    "the ledger lists insufficient motions first",
                    "an unchecked promise is not a held one: 'nothing is broken' "
                    "and 'nothing was looked at' must not be the same answer",
                    "a broken promise names the commitment it broke",
                    "no status movement lowers a rank without evidence, and only the "
                    "declared risk movement may lower one at all",
                ],
            },
            "regional_care_stage_e": {
                "routes": [
                    "/kaizen/admin/regions",
                    "/kaizen/admin/care-personalization",
                    "/kaizen/admin/relationship-catalog",
                    "/kaizen/admin/relationship-view",
                ],
                "purpose": (
                    "Stage E: contact is a regional question, personalization is a "
                    "per-purpose one, and an agent should land on the copilot "
                    "rather than on a table"
                ),
                "why": (
                    "the finding is a defect in code this project already shipped. "
                    "The Stage D gate read float(moment.hour) off a UTC datetime, so "
                    "a customer in Auckland had their stated 09:00-17:00 window "
                    "checked against UTC hours and the entire Stage D suite stayed "
                    "green -- because every persona in it lived in the server's "
                    "timezone. The arithmetic now lives in one exported function, "
                    "so it cannot be inlined back into UTC by a later refactor"
                ),
                "invariants": [
                    "quiet hours are evaluated in the customer's local hour, never "
                    "the server's",
                    "an unresolved region is unattributed rather than closed: an "
                    "absence of a region has no office hours to exclude contact",
                    "region is never derived from an IP address",
                    "a recognised device shortens authentication and never "
                    "permission; it cannot unlock unattended contact",
                    "a contact refusal must not stop a customer acting on their own "
                    "account",
                    "the copilot is the default pane and moving off it requires an "
                    "explicit request",
                    "acceptance and fulfilment stay separable columns",
                ],
            },
            "care_loop_stage_d": {
                "routes": [
                    "/kaizen/admin/care-gate",
                    "/kaizen/admin/care-gate/consult",
                    "/kaizen/admin/care-weights",
                    "/kaizen/admin/offer-outcomes",
                ],
                "purpose": (
                    "Stage D, the closed loop: one fail-closed gate every "
                    "proactive path asks (with structural proof that each "
                    "declared path's source calls it), offer outcomes ranked into "
                    "recommendations for the playbook tiers and the generosity "
                    "rules, journey completion and stuckness made actionable, and "
                    "learned complaint weights turned into care emphasis"
                ),
                "status": "ready",
                "config_tables": [
                    "PROACTIVE_PATHS",
                    "SERVICE_CRITICAL_PURPOSES",
                    "CARE_DIMENSIONS",
                    "CARE_WEIGHT_RULES",
                    "REFUSED_LEARNED_POWERS",
                    "WILSON_CONFIDENCE_Z",
                    "PROVISIONAL_OUTCOME_COUNT",
                    "MIN_OUTCOMES_TO_RANK",
                    "RETIRE_BOUND",
                    "RETIRE_MIN_OUTCOMES",
                    "DECIDED_STATUSES",
                    "DELIVERED_STATUSES",
                ],
                "notes": (
                    "the finding behind care_gate is a COUNT, not a theory: before "
                    "it, customer_offers was the only service that consulted the "
                    "preference centre -- recovery outreach picked a channel, a "
                    "tone and a framing for somebody who had already reported a "
                    "problem, and a callback carried an owner team and a deadline. "
                    "the gate fails CLOSED in three distinct modes, because "
                    "'could not read their preferences', 'they have expressed "
                    "nothing' and 'this surface forgot to register' are genuinely "
                    "different facts. coverage is proven by reading each path's "
                    "SOURCE, not by a docstring: a property nobody checks is a "
                    "comment. offer_outcomes ranks on a Wilson lower bound because a "
                    "naive rate rewards making fewer offers (1/1 is 100%), and it "
                    "separates acceptance from fulfilment because averaging them "
                    "points the operator at the offer volume instead of at us. "
                    "care_weights moves emphasis and never verdict: severity, tier "
                    "and auto-escalation are refused by reading "
                    "complaint_learning.LEARNED_WEIGHT_AUTHORITY rather than "
                    "restating it"
                ),
            },
            "loyalty_status_stage_c": {
                "routes": [
                    "/kaizen/admin/loyalty-status",
                    "/kaizen/admin/loyalty-status/preview",
                    "/kaizen/admin/agent-copilot/preview",
                ],
                "purpose": (
                    "Stage C in four services: loyalty status and an experiential "
                    "ledger, the LTV/investment signal that sizes a goodwill "
                    "offer, a journey orchestrator that carries at-risk -> offer -> "
                    "follow-up -> status across passes, and an agent copilot that "
                    "assembles 360 + plain-language why + next-best-action into "
                    "one card an agent can read"
                ),
                "status": "ready",
                "config_tables": [
                    "LOYALTY_STATUS_RULES",
                    "EXPERIENTIAL_SIGNALS",
                    "EXPERIENTIAL_TIER_RULES",
                    "INVESTMENT_BANDS",
                    "RELATIONSHIP_HEALTH_BANDS",
                    "HEALTH_SIGNALS",
                    "HEALTH_OVERRIDES",
                    "ORCHESTRATION_STAGES",
                    "ORCHESTRATION_TRANSITIONS",
                    "COPILOT_DO_NOT_LEAD_WITH",
                ],
                "notes": (
                    "status is EARNED and never decays for inactivity: a "
                    "customer-visible demotion for going quiet punishes the exact "
                    "pause a loyalty programme should forgive. relationship health "
                    "is the opposite -- live, and allowed to fall -- so a customer "
                    "can be Trusted and critical at once, which is the combination "
                    "that deserves the fastest response. the investment band is "
                    "ORDINAL, not monetary: there is no revenue column in this "
                    "schema and any LTV figure built from it would be a guess "
                    "wearing a float. the orchestrator's repeat rule compares a "
                    "precondition hash rather than a stage or a count, so cycling "
                    "the loop does not permit a fourth identical offer"
                ),
            },
            "recovery_offers": {
                "routes": [
                    "/chat/me/offers",
                    "/chat/me/offers/{reference}",
                    "/chat/me/offers/{reference}/accept",
                    "/chat/me/offers/{reference}/decline",
                    "/chat/admin/recovery/offers",
                    "/chat/admin/recovery/offers/{reference}/outcome",
                ],
                "purpose": (
                    "Stage B: composing an offer and giving it to a customer. "
                    "recovery_playbooks already knew what to offer -- the "
                    "RECOVERY_SAVE_INCENTIVES tiers and the ARREARS_WAIVER_POLICY "
                    "gate -- and said so, composing previews that issue nothing. "
                    "This issues them, hands them over, and records what came "
                    "back. Three kinds, each naming the upstream rule that "
                    "produced it so an offer says which policy fired rather than "
                    "merely that one was made"
                ),
                "status": "ready",
                "config_tables": [
                    "CUSTOMER_OFFER_KINDS",
                    "OFFER_STATUSES",
                    "OFFER_TRANSITIONS",
                    "OFFER_EVENT_KINDS",
                    "OFFER_GENEROSITY_RULES",
                    "OFFER_EXPLANATIONS",
                ],
                "notes": (
                    "the preference gate governs *contact*, not existence: a "
                    "reactive-only customer still receives the offer and can "
                    "accept it in one tap, because a preference about how often "
                    "to interrupt is not a preference about whether to be told. "
                    "what the gate withholds is the notification, and it is "
                    "deferred with a time rather than dropped. consent never "
                    "withholds a service or recovery offer -- a customer who "
                    "reported a problem does not get told we had a fix but they "
                    "declined marketing. the offer's internal tier name never "
                    "reaches the customer: 'Critical save offer' would tell them "
                    "our churn model graded them critical"
                ),
            },
            "customer_visible_recovery": {
                "routes": [
                    "/chat/me/recovery-status",
                    "/chat/me/status",
                    "/chat/me/points-forecast",
                    "/chat/me/policy-posture",
                ],
                "purpose": (
                    "the customer's own view of the recovery program: what it decided, "
                    "what it did, and what it deliberately did not do"
                ),
                "status": "ready",
                "config_tables": [
                    "POINTS_FORECAST_WINDOW_DAYS",
                    "POINTS_FORECAST_HORIZON_DAYS",
                    "POINTS_FORECAST_MIN_ROWS",
                ],
                "notes": (
                    "the recovery program was already thoroughly auditable internally -- "
                    "every action is a RecoveryAction row and /chat/admin/recovery-analytics "
                    "rolls it up -- but none of that reached the person it happened to. A "
                    "blocked action is shown rather than hidden, naming the guard that "
                    "stopped it, because omitting it makes 'our limits correctly prevented "
                    "a duplicate credit' and 'we forgot about you' render identically. The "
                    "points forecast is a scaled repeat of observed ledger movement, not a "
                    "prediction model, and says so via method and basis: a forecast on a "
                    "surface a customer might act on that turned out to be a guess would be "
                    "worse than no forecast"
                ),
            },
            "policy_scoring_governance": {
                "routes": [
                    "/meta/policy-scoring",
                    "/meta/scoring-catalog",
                ],
                "purpose": (
                    "the score layer under the policy tier rules: the six "
                    "published signals composed by a pure ordered engine from "
                    "declared weights instead of six inline expressions, the "
                    "topic breadth/complexity/depth marker tables with an "
                    "explicit accumulation order, declarative posture "
                    "adjustments, tier escalations and recommendation rules with "
                    "reportable predicates, and a what-if surface for tuning a "
                    "threshold against a real metric set without a database"
                ),
                "status": "ready",
                "config_tables": [
                    "SCORE_SIGNALS",
                    "SCORE_INPUTS",
                    "COMPOSITE_RULES",
                    "POSTURE_ADJUSTMENTS",
                    "TIER_ESCALATIONS",
                    "TOPIC_MARKER_WEIGHTS",
                    "TOPIC_SCORE_RULES",
                    "HEALTH_THRESHOLDS",
                    "RECOMMENDATION_RULES",
                    "RECOMMENDATION_CONDITIONS",
                    "POLICY_SCORING_OPS",
                    "POLICY_SCORING_VERSION",
                ],
                "notes": (
                    "the pinned tier/posture/band contract is unchanged and is "
                    "reproduced under tier_catalog; build_policy_tier_catalog "
                    "keeps its exact key set. Every constant lifted out of the "
                    "score formula kept its value, so published scores and every "
                    "stored summary string are unchanged -- the differential "
                    "tests assert that against the original expressions. Two "
                    "asymmetries in the original arithmetic are preserved rather "
                    "than corrected, and say so in the table comments: the "
                    "inverted dissatisfaction term is floored but not ceilinged, "
                    "and scaled_mean divides by the term count rather than the "
                    "sum of the weights"
                ),
            },
            "complaint_learning": {
                "routes": [
                    "/complaints/admin/learning/catalog",
                    "/complaints/admin/learning/weights",
                    "/complaints/admin/learning/observe",
                    "/complaints/admin/learning/detect",
                    "/complaints/admin/learning/raise",
                    "/complaints/admin/learning/system-raised",
                    "/complaints/admin/learning/clusters",
                    "/complaints/admin/learning/proposals",
                    "/complaints/admin/learning/publish",
                    "/complaints/admin/learning/blockages",
                ],
                "purpose": (
                    "complaints as a system-originated event stream: detectors that "
                    "open a case for a customer who never complained (gated by "
                    "confidence, cooldown and dedupe), contribution weights learned "
                    "from whether the customer was actually retained, and stacked "
                    "complaints turned into improvement proposals that enter the "
                    "release ladder at l0_draft. The learned weights are advisory: "
                    "they reorder and explain, and never set a tier. Nothing here "
                    "writes BLOCKAGES.md -- it renders a pasteable section and a "
                    "diff instead."
                ),
                "status": "ready",
            },
            "kaizen_release_surface": {
                "routes": [
                    "/kaizen/admin/catalog",
                    "/kaizen/admin/flows",
                    "/kaizen/admin/flows/run",
                    "/kaizen/admin/blockages",
                    "/kaizen/admin/completeness",
                    "/kaizen/admin/sweep",
                    "/kaizen/admin/shadow",
                    "/kaizen/admin/levels",
                    "/kaizen/admin/candidates",
                    "/kaizen/admin/safe-levels",
                    "/kaizen/admin/deployments",
                ],
                "purpose": (
                    "how a change to *this* code gets tested and released: the "
                    "real-life flow simulation that exercises every subflow "
                    "against varied personas and appends findings to "
                    "BLOCKAGES.md, the one-way shadow environment whose impact on "
                    "live is structurally impossible, and the graded maturity "
                    "ladder an admin promotes along one measured rung at a time. "
                    "Plus the completeness grading that answers *is it built yet* "
                    "-- six graded states, and what counts as fatal depends on the "
                    "rung, so an immature backend passes at l0_draft. "
                    "`python3 scripts/run_kaizen.py` runs all of it in one command; "
                    "CSERVICE_KAIZEN_AUTORUN=1 runs the same sweep on a timer. "
                    "Backend-only for now; the frontend is out of scope"
                ),
                "status": "ready",
                "config_tables": [
                    "SHADOW_ENVIRONMENTS",
                    "SHADOW_FEEDS",
                    "ISOLATION_CHECKS",
                    "NEVER_REPLICATED_FIELDS",
                    "MATURITY_LEVELS",
                    "PROMOTION_GATES",
                    "EVENT_KINDS",
                    "PERSONAS",
                    "PROBES",
                    "FLOW_CATALOG",
                    "BLOCKAGE_POLICY",
                    "CAPABILITY_STATES",
                    "PROBE_CAPABILITY",
                    "ENGINES",
                ],
                "notes": (
                    "NOT under /meta*, deliberately: /meta* is public by intent "
                    "and this prefix carries three mutating endpoints (append to "
                    "BLOCKAGES.md, advance a candidate, roll back a deployment). "
                    "Gated as one block by AUTHZ_RULES['kaizen_admin'] so a route "
                    "added under the prefix inherits the gate instead of "
                    "remembering it -- including the three added here. Code and "
                    "data version move as one promotable pair, and the rollback "
                    "window is the last five deployment events -- a rollback "
                    "appends rather than "
                    "rewrites, so the history stays honest. `python3 scripts/"
                    "run_kaizen.py` runs every check in this entry in one command, "
                    "and CSERVICE_KAIZEN_AUTORUN=1 runs the same sweep on a timer"
                ),
            },
        },
        "capabilities": capabilities,
    }


@app.get("/meta/features")
async def app_feature_summary():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "endpoints": {
            "health": "/health",
            "metadata": "/meta",
            "ecosystem": "/meta/ecosystem",
            "scoring_catalog": "/meta/scoring-catalog",
            "schema_catalog": "/meta/schema",
            "booking_analytics": "/bookings/analytics/summary",
            "booking_events": "/bookings/analytics/events",
            "booking_export": "/bookings/analytics/export",
            "chat_history": "/chat/history",
            "ai_providers": "/chat/admin/ai-providers",
            "ai_provider_catalog": "/chat/admin/ai-providers/catalog",
            "ai_provider_failover": "/chat/admin/ai-providers/failover",
            "retention_dashboard": "/chat/retention-dashboard",
            "retention_maintenance": "/retention/maintenance",
            "retention_health": "/chat/admin/retention-health",
            "retention_forecast": "/chat/admin/retention-forecast",
            "retention_forecast_sweep": "/chat/admin/retention-forecast-sweep",
            "retention_cluster_coverage": "/chat/admin/retention-cluster-coverage",
            "loyalty_journey": "/chat/loyalty-journey",
            "loyalty_admin_journey": "/chat/admin/loyalty-journey",
            "activity_tree": "/chat/activity-tree",
            "activity_tree_admin": "/chat/admin/activity-tree",
            "communication_strategy": "/chat/communication-strategy",
            "communication_strategy_admin": "/chat/admin/communication-strategy",
            "communication_overrides": "/chat/admin/communication-overrides",
            "arrears_quote": "/chat/payments/arrears/quote",
            "arrears_self": "/chat/payments/arrears",
            "arrears_admin": "/chat/admin/payments/arrears",
            "points_rates": "/chat/points/exchange/rates",
            "points_wallet": "/chat/points/wallet",
            "points_transactions": "/chat/points/transactions",
            "points_quote": "/chat/points/exchange/quote",
            "points_exchange": "/chat/points/exchange",
            "points_admin_exchange": "/chat/admin/points/exchange",
            "efficiency_audit": "/audit/efficiency",
            "efficiency_enhancements": "/audit/enhancements",
            "efficiency_audit_catalog": "/audit/catalog",
            "audit_log": "/audit/log",
            "audit_logs": "/audit/logs",
            "audit_log_summary": "/audit/logs/summary",
            "audit_log_governed": "/audit/log/auditable",
            "audit_log_integrity": "/audit/logs/integrity",
            "audit_log_actors": "/audit/logs/actors",
            "audit_log_timeline": "/audit/logs/timeline",
            "audit_log_anomalies": "/audit/logs/anomalies",
            "audit_log_retention": "/audit/logs/retention",
            "audit_log_export": "/audit/logs/export",
            "audit_log_view": "/audit/logs/view",
            "audit_integrity_gates": "/audit/integrity/gates",
            "audit_trail_catalog": "/audit/trail-catalog",
            "i18n_catalog": "/meta/i18n",
            "i18n_governance": "/meta/i18n",
            "audit_contracts": "/meta/audit-contracts",
            "tenant_routing": "/meta/tenants",
            "tenant_routing_policy": "/meta/tenants/policy",
            "partition_management": "/meta/partitions",
            "partition_lifecycle_policy": "/meta/partitions/policy",
            "zero_trust": "/meta/zero-trust",
            "zero_trust_policy": "/meta/zero-trust/policy",
            "pipeline_event": "/audit/pipeline/event",
            "pipeline_stats": "/audit/pipeline/stats",
            "pipeline_dead_letters": "/audit/pipeline/dead-letters",
            "pipeline_replay": "/audit/pipeline/replay",
            "transactions": "/audit/transactions",
            "transactions_spec": "/audit/transactions/spec",
            "transactions_query": "/audit/transactions/query",
            "transactions_integrity": "/audit/transactions/integrity",
            "transactions_export": "/audit/transactions/export",
            "decisions": "/meta/decisions",
            "decision_simulate": "/meta/decisions/simulate",
            "decision_canary_run": "/meta/decisions/canary/run",
            "decision_canary_promote": "/meta/decisions/canary/promote",
            "kaizen_catalog": "/kaizen/admin/catalog",
            "kaizen_flows": "/kaizen/admin/flows",
            "kaizen_flow_simulate": "/kaizen/admin/flows/{flow_id}/simulate",
            "kaizen_flow_run": "/kaizen/admin/flows/run",
            "kaizen_blockages": "/kaizen/admin/blockages",
            "kaizen_blockages_append": "/kaizen/admin/blockages/append",
            "kaizen_shadow": "/kaizen/admin/shadow",
            "kaizen_shadow_verify": "/kaizen/admin/shadow/verify",
            "kaizen_levels": "/kaizen/admin/levels",
            "kaizen_care_gate": "/kaizen/admin/care-gate",
            "kaizen_care_gate_consult": "/kaizen/admin/care-gate/consult",
            "kaizen_care_weights": "/kaizen/admin/care-weights",
            "kaizen_offer_outcomes": "/kaizen/admin/offer-outcomes",
            "kaizen_regions": "/kaizen/admin/regions",
            "kaizen_care_personalization": "/kaizen/admin/care-personalization",
            "kaizen_relationship_catalog": "/kaizen/admin/relationship-catalog",
            "kaizen_relationship_view": "/kaizen/admin/relationship-view",
            "kaizen_policy_motion": "/kaizen/admin/policy-motion",
            "kaizen_policy_motion_ledger": "/kaizen/admin/policy-motion/ledger",
            "kaizen_trust_continuity": "/kaizen/admin/trust-continuity",
            "kaizen_value_trajectory": "/kaizen/admin/value-trajectory",
            "kaizen_loyalty_status": "/kaizen/admin/loyalty-status",
            "kaizen_loyalty_status_preview": "/kaizen/admin/loyalty-status/preview",
            "kaizen_agent_copilot_preview": "/kaizen/admin/agent-copilot/preview",
            "kaizen_completeness": "/kaizen/admin/completeness",
            "kaizen_sweep": "/kaizen/admin/sweep",
            "kaizen_candidates": "/kaizen/admin/candidates",
            "kaizen_candidate_measure": "/kaizen/admin/candidates/{candidate_id}/measure",
            "kaizen_candidate_advance": "/kaizen/admin/candidates/{candidate_id}/advance",
            "kaizen_safe_levels": "/kaizen/admin/safe-levels",
            "kaizen_deployments": "/kaizen/admin/deployments",
            "kaizen_rollback": "/kaizen/admin/deployments/{event_id}/rollback",
            "explanations": "/meta/decisions/explanations",
            "explanation_detail": "/meta/decisions/explanations/{decision_id}",
            "rule_engine_core": "/meta/scoring-catalog",
            "regional_policy": "/meta/regional",
            "recovery_playbooks": "/chat/recovery/playbooks",
            "recovery_playbooks_admin": "/chat/admin/recovery/playbooks",
            "customer_offers": "/chat/me/offers",
            "customer_offer_accept": "/chat/me/offers/{reference}/accept",
            "customer_offer_decline": "/chat/me/offers/{reference}/decline",
            "offer_report_admin": "/chat/admin/recovery/offers",
            "offer_issue_admin": "/chat/admin/recovery/offers",
            "offer_outcome_admin": "/chat/admin/recovery/offers/{reference}/outcome",
            "recovery_guards": "/chat/admin/recovery-guards",
            "recovery_analytics": "/chat/admin/recovery-analytics",
            "recovery_outreach_plan": "/chat/admin/recovery-outreach-plan",
            "topic_route_policies": "/topics/governance/routes",
            "topic_param_bounds": "/topics/governance/params",
            "topic_response_limits": "/topics/governance/limits",
            "topic_capacity": "/topics/governance/capacity",
            "topic_request_analytics": "/topics/governance/requests",
            "topic_policy_validation": "/topics/governance/validation",
            "topic_route_drift": "/topics/governance/drift",
            "topic_request_plan": "/topics/plan",
            "model_bases_catalog": "/meta/model-bases",
            "policy_scoring_catalog": "/meta/policy-scoring",
            "authz_catalog": "/meta/authz",
            "session_refresh": "/users/refresh",
            "session_logout": "/users/logout",
            "session_inventory": "/users/me/sessions",
            "step_up": "/users/me/step-up",
            "api_keys": "/users/me/api-keys",
            "password_policy": "/users/password-policy",
            "password_feedback": "/users/me/password-feedback",
            "security_posture": "/users/me/security-posture",
            "customer_360": "/chat/customer-360",
            "customer_360_admin": "/chat/admin/customer-360",
            "customer_recovery_status": "/chat/me/recovery-status",
            "self_service_status": "/chat/me/status",
            "customer_points_forecast": "/chat/me/points-forecast",
            "customer_policy_posture": "/chat/me/policy-posture",
            "customer_preferences": "/chat/me/preferences",
            "customer_consent_history": "/chat/me/consent-history",
            "customer_explanations": "/chat/me/explanations",
            "explanation_vocabulary": "/chat/admin/explanation-vocabulary",
            "preference_catalog": "/chat/admin/preference-catalog",
            "customer_offers": "/chat/me/offers",
            "customer_offer_detail": "/chat/me/offers/{reference}",
            "customer_offer_accept": "/chat/me/offers/{reference}/accept",
            "customer_offer_decline": "/chat/me/offers/{reference}/decline",
            "offer_report_admin": "/chat/admin/recovery/offers",
            "offer_issue_admin": "/chat/admin/recovery/offers",
            "offer_outcome_admin": "/chat/admin/recovery/offers/{reference}/outcome",
        },
    }


@app.get("/meta/scoring-catalog")
async def scoring_catalog():
    """Expose the live, data-driven rule engines behind interaction scoring.

    Future services can discover which policy areas are scored (keywords, weights,
    caps), which keywords map to which areas, the active policy tier / posture
    / access-band thresholds, and the loyalty-journey scenario rules (new ->
    loyal next-best-action engine) — all driven by config tables instead of
    hardcoded branches.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "area_scoring": chat_analytics.build_area_scoring_catalog(),
        "area_keywords": chat_analytics.build_area_keyword_catalog(),
        "policy_tiers": policy_scoring.build_policy_tier_catalog(),
        "loyalty_scenarios": loyalty_journey.build_loyalty_scenario_catalog(),
        "activity_monitoring": activity_tree.build_activity_tree_catalog(),
        "communication_strategy": communication_strategy.build_communication_strategy_catalog(),
        "arrears_payments": arrears_payments.build_arrears_catalog(),
        "points_exchange": points_exchange.build_points_exchange_catalog(),
        "efficiency_audit": build_efficiency_audit_catalog(),
        "audit_log": audit_log.build_audit_log_catalog(),
        "enrichment": enrichment.build_enrichment_catalog(),
        "webhooks": webhooks.build_webhook_catalog(),
        "i18n": i18n.build_i18n_catalog(),
        "i18n_governance": i18n.build_i18n_governance_catalog(),
        "audit_contracts": audit_schemas.build_contract_catalog(routes=app),
        "password_policy": security.password_policy_payload(),
        "tenant_routing": tenant_router.build_tenant_router_catalog(),
        "partition_lifecycle": partition_manager.build_partition_manager_catalog(),
        "zero_trust": {
            "hsm_signer": hsm_signer.build_hsm_signer_catalog(),
            "biometric_vault": biometric_vault.build_biometric_vault_catalog(),
            "risk_evaluator": risk_evaluator.build_risk_evaluator_catalog(),
            "cell_matrix": cell_matrix.build_cell_matrix_catalog(),
        },
        "event_pipeline": {
            "pipeline": high_throughput_pipeline.build_pipeline_catalog(),
            "protobuf_transaction_spec": protobuf_transaction_spec.build_transaction_spec_catalog(),
        },
        "decision_intelligence": {
            "model_versioning": model_versioning.build_model_versioning_catalog(),
            "simulation": simulation_engine.build_simulation_engine_catalog(),
            "explainability": explainability.build_explainability_catalog(),
            "optimistic_locking": optimistic_locking.build_optimistic_locking_catalog(),
        },
        "rule_engine": rule_engine.build_rule_engine_catalog(),
        "regional_policy": regional_policy.build_regional_policy_catalog(),
        "recovery_playbooks": recovery_playbooks.build_recovery_playbook_catalog(),
        "retention": retention.build_retention_catalog(),
        "topic_request_governance": topics.build_topic_request_governance_catalog(),
        "model_bases": model_bases.build_model_bases_ops_catalog(),
        "policy_scoring_governance": policy_scoring.build_policy_scoring_catalog(),
        "authz_governance": deps.build_authz_catalog(app.routes),
        "identity": users.build_users_catalog(),
        # The sign-in surface publishes its tables rather than hard-coding them
        # at the call site, for two reasons. An operator can see which methods
        # exist, what each one establishes, and what is deliberately absent --
        # which is the only way "why can I not log in with X" gets an answer
        # from the running service rather than from whoever wrote it. And the
        # refusal lists are data, so shipping a method nobody decided on is not
        # something that can happen by omission.
        "auth_methods": auth_methods.build_auth_methods_catalog(),
        "device_recognition": device_recognition.build_recognition_catalog(),
        "storage_providers": storage_providers.build_storage_catalog(),
        # The list of AI services the external brain may answer from, and the
        # failover policy between them. Published here rather than only under the
        # admin surface because the catalog carries no secret: it is what *could*
        # be configured, so an operator can learn the option set from a running
        # deployment. What *is* configured stays behind the admin gate.
        "ai_providers": ai_providers.build_ai_providers_catalog(),
        # Mail is published because the OTP endpoint's honesty depends on it: a
        # deployment whose transport cannot deliver needs to be able to see that
        # without reading the source.
        "mail": mail.build_mail_catalog(),
        "brain_router": brain_router.build_brain_catalog(),
        "transfers": transfers_service.build_transfers_catalog(),
        "database_ops": db_infra.build_db_ops_catalog(),
        "customer_360": {
            "sections": list(customer_360.CUSTOMER_360_SECTIONS),
            "composition_only": True,
        },
        "customer_explainability": customer_explain.build_explainability_vocabulary_catalog(),
        "customer_explainability_validation": customer_explain.validate_explanation_tables(),
        "preference_consent": {
            **preferences_service.preference_catalog(),
            "consent": preferences_service.consent_report(
                preferences_service.default_consents()
            ),
        },
    }


@app.get("/meta/schema")
async def schema_catalog(section: str | None = Query(default=None)):
    """Describe the ORM schema and, more usefully, what it does not protect.

    Everything here is derived from ``Base.metadata`` at call time plus a set of
    config tables that add the judgments the DDL cannot express: which columns
    are sensitive, which string columns are *supposed* to hold an enum value,
    which tables are append-only, and which quantities are legitimately signed.

    Two sections are advisory reports rather than plain descriptions:

    * ``schema_gaps.check_constraint_coverage`` -- float columns with no
      non-negative check, split into "guarded", "signed by design", and the
      residual.
    * ``schema_gaps.referential_integrity_gaps`` -- ``*_id`` columns with no
      foreign key, split by whether the target table exists at all.

    Both report; neither fixes. Closing either gap is new DDL and needs a
    migration, so the answer here is a description an operator can act on, not a
    silent change to a running schema.

    Pass ``?section=`` for one part of the catalog (``sensitivity``,
    ``json_columns``, ``enum_fields``, ``schema_gaps``, ``lifecycle``,
    ``relationships``, ``columns``). An unknown section is a 422 rather than an
    empty payload, so a typo in a script fails loudly.
    """
    catalog = models.build_model_catalog()
    envelope = {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if section:
        if section not in catalog:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"unknown schema section {section!r}; expected one of "
                    f"{', '.join(sorted(catalog))}"
                ),
            )
        return {**envelope, "section": section, "catalog": catalog[section]}
    return {**envelope, **catalog}


@app.get("/meta/model-bases")
async def model_bases_catalog():
    """Describe the shared model bases and the governance layer over them.

    ``/meta/schema`` describes the *mapped* schema; this describes the layer the
    models are built from. The most useful section is ``validation``: it names
    every mixin that has no mapped-table consumer, every new-mixin column name
    an existing table already owns, and whether the process has imported
    ``app.models`` at all — a partial ``Base.metadata`` view read as a whole
    schema is how a partial audit passes.

    Warnings here mean "available but not yet used". Errors mean the layer
    cannot be trusted and should not be read as a statement about the schema.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **model_bases.build_model_bases_ops_catalog(),
    }


@app.get("/meta/policy-scoring")
async def policy_scoring_catalog():
    """Describe the score layer under the policy tier rules.

    ``/meta/scoring-catalog`` exposes ``policy_tiers``: the tier, posture and
    access-band thresholds. Those were already config tables. What sat *below*
    them was not — the six published signals were six arithmetic expressions
    written inline inside the snapshot builder, and the topic breadth, complexity
    and depth scores were long inline marker lists. This endpoint describes the
    tables those expressions now read, and none of the numbers in them changed.

    Three sections are worth reading first:

    * ``validation`` — which configured rules cannot run as written. A
      composition rule that reads a signal produced later, a marker group for a
      metric that does not exist, or a recommendation predicate with a typo in
      its condition name all produce a plausible score and no signal that a rule
      did not fire.
    * ``coverage`` — which tier and band row actually decides, and for how much
      of the score space. First-match-wins makes row order the semantics, so a
      shadowed row is dead configuration that still reads as live.
    * ``tier_catalog`` — the pinned tier/posture/band contract, reproduced
      unchanged for consumers that already know it.

    Warnings mean something is configured but not doing anything. Errors mean
    the layer should not be read as a statement about any customer's score.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **policy_scoring.build_policy_scoring_catalog(),
    }


@app.get("/meta/authz")
async def authz_catalog():
    """Describe the authorization layer: what each route enforces, and what it does not.

    ``app.deps`` holds the scope vocabulary, the role/scope relationship, the
    denial contract, the assurance levels, the rate tiers, and a per-route
    description of the gate that actually runs. None of that changed the request
    path -- same principal resolution, same status codes, same denial payloads --
    but it used to be invisible: the only way to ask "who can reach this?" was to
    read a FastAPI signature, and the six ``require_*`` dependency factories were
    bound to no route at all.

    Four sections are worth reading first:

    * ``drift`` -- the described posture against the routes the app actually
      registers. ``in_sync`` is a claim that can be falsified, and a new route
      landing on the fallback rule is reported by name.
    * ``validation`` -- which configured rules cannot run as written. A scope
      nobody can be issued, a step-up rank that disagrees with ``app.security``,
      or a denial whose declared key order no longer matches the payload a
      client receives are all errors here rather than surprises in production.
    * ``backlog`` -- the requirements that are *proposed and not enforced*, kept
      in a different key from the rules on purpose. 34 of the 55 rules ask for a
      scope that no dependency checks, and reading that as a guarantee is exactly
      the failure mode this separation exists to prevent.
    * ``unbound_factories`` -- which ``require_*`` factories no route calls. All
      six are working, tested, and unused.

    Warnings mean something is configured but not doing anything. Errors mean the
    layer should not be read as a statement about any real route's posture.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **deps.build_authz_catalog(app.routes),
    }


@app.get("/meta/i18n")
async def i18n_catalog(locale: str | None = None):
    """Expose the localization surface: supported locales and message catalog.

    The payload is locale-aware: pass ``?locale=es`` to see how the catalog
    will resolve for that audience, otherwise English is used as the baseline.

    ``governance`` is the rules *around* the messages -- the key namespaces and
    their placeholder and length policies, what the renderer actually does with
    a missing value, an unbalanced brace, or a plural block, and where the two
    locale resolvers disagree. It is a report, not a repair: every finding names
    the code path that produced it, and a non-empty finding list is the expected
    state rather than a sign the endpoint is broken.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "catalog": i18n.build_i18n_catalog(),
        "resolution": i18n.locale_payload(locale),
        "governance": i18n.build_i18n_governance_catalog(),
    }


@app.get("/meta/audit-contracts")
async def audit_contract_catalog():
    """Expose the audit-trail contract surface and the governance over it.

    40 pydantic models are the client-facing contract for every ``/audit/*``
    route, and until now nothing in the tree said which route each one served,
    which audience it was for, or where its allowed values were actually
    enforced. ``contracts`` answers that from the pinned inventory; ``governance``
    is the report *over* those contracts.

    The posture is report, never repair -- a model in that module is not a
    rendering of a policy, it is the policy a client binds to, and widening one
    to silence a finding would delete the only place a vocabulary was written
    down. So the interesting output is a non-empty finding list: which field
    names its allowed values in a comment instead of a ``pattern=``, that
    ``severity`` means two different things in one module, that ``/audit/log``
    stores its detail blob verbatim while ``/audit/log/auditable`` scrubs it and
    the two declare the field identically, and that the database CHECK constraint
    the severity column relies on is documented here but *not* verified, because
    a schemas module claiming to have checked the DDL would be a lie with a
    passing test.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "contracts": audit_schemas.contract_inventory(routes=app),
        "governance": audit_schemas.build_contract_catalog(routes=app),
    }


@app.get("/meta/customer-360")
async def customer_360_catalog():
    """Describe the customer-facing visibility surfaces added in Stage A.

    Four related capabilities, and the interesting part is that each one is
    *composed* from an engine that already existed rather than introducing a new
    source of truth:

    * ``customer_360``      -- the aggregator and the sections it composes.
    * ``explainability``    -- the plain-language bands, phrases and next-step
      templates, plus a validation report over them.
    * ``preferences``       -- the declared key space and the consent purposes,
      including which purposes actually gate outreach.
    * ``self_service``      -- the recovery, points-forecast and policy-posture
      surfaces.

    The validation report is the part worth reading first. It is report-only:
    these phrases are already being served to customers, so correcting one
    changes a string someone is reading. A *missing* phrase is an error because
    the fallback text would be served silently; a band whose direction
    regresses is an error because monotonicity is a property of the table
    rather than of any threshold.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "customer_360": {
            "sections": list(customer_360.CUSTOMER_360_SECTIONS),
            "composition_only": True,
            "note": (
                "every section is composed from an engine that already existed; a "
                "section that cannot be built is reported rather than defaulted healthy"
            ),
        },
        "explainability": {
            **customer_explain.build_explainability_vocabulary_catalog(),
            "validation": customer_explain.validate_explanation_tables(),
        },
        "preferences": {
            **preferences_service.preference_catalog(),
            "consent": preferences_service.consent_report(
                preferences_service.default_consents()
            ),
        },
        "self_service": {
            "points_forecast": {
                "window_days": self_service.POINTS_FORECAST_WINDOW_DAYS,
                "horizon_days": self_service.POINTS_FORECAST_HORIZON_DAYS,
                "min_rows": self_service.POINTS_FORECAST_MIN_ROWS,
                "method": "scaled_repeat_of_observed_movement",
                "is_projection": True,
            },
            "note": (
                "the forecast is a scaled repeat of observed ledger movement, not a "
                "prediction model; a wallet with no exchange rule reports "
                "no_exchange_rule rather than a number the customer cannot spend"
            ),
        },
        "endpoints": {
            "customer_360": "/chat/customer-360",
            "customer_360_admin": "/chat/admin/customer-360",
            "recovery_status": "/chat/me/recovery-status",
            "status": "/chat/me/status",
            "points_forecast": "/chat/me/points-forecast",
            "policy_posture": "/chat/me/policy-posture",
            "preferences": "/chat/me/preferences",
            "consent_history": "/chat/me/consent-history",
            "explanations": "/chat/me/explanations",
            "explanation_vocabulary": "/chat/admin/explanation-vocabulary",
            "preference_catalog": "/chat/admin/preference-catalog",
        },
    }


@app.get("/meta/regional")
async def regional_policy_catalog(region: str | None = None):
    """Expose the regional booking & tax policy surfaces.

    Includes the regional calendars (weekends + holidays), labor-compliance
    guardrails, and per-region tax rules. Pass ``?region=de`` to also resolve
    the working-day verdict for *today* plus a sample tax computation.
    """
    catalog = regional_policy.build_regional_policy_catalog()
    payload = {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "catalog": catalog,
    }
    if region:
        payload["resolved"] = {
            "region": region,
            "working_day": regional_policy.is_working_day(region),
            "labor": regional_policy.get_labor_rule(region),
            "tax": regional_policy.compute_taxed_amount(100.0, region, "consultation"),
        }
    return payload

# --- Phase 1: decision quality & consistency surfaces ------------------------


@app.get("/meta/decisions")
async def decisions_metadata():
    """Expose the decision-intelligence subsystem: model versioning + canary,
    what-if simulation, explainability traces, and optimistic concurrency."""
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_versioning": model_versioning.build_model_versioning_catalog(),
        "simulation": simulation_engine.build_simulation_engine_catalog(),
        "explainability": explainability.build_explainability_catalog(),
        "optimistic_locking": optimistic_locking.build_optimistic_locking_catalog(),
    }


@app.post("/meta/decisions/simulate")
async def simulate_decision(payload: DecisionSimulateRequest):
    """Run a what-if simulation (risk rule changes / retention policy changes).

    Pure simulation — live config, weight state, and partitions are never
    mutated. Returns baseline vs variant with delta and affected rows.
    """
    moment = (
        datetime.fromisoformat(payload.moment)
        if payload.moment is not None
        else None
    )
    try:
        return simulation_engine.run_simulation(
            payload.kind,
            label=payload.label or "what-if",
            context=payload.context,
            weight_overrides=payload.weight_overrides,
            disabled_rules=payload.disabled_rules,
            level_max_overrides=payload.level_max_overrides,
            retention_overrides=payload.retention_overrides,
            active_partitions=payload.active_partitions,
            moment=moment,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@app.post("/meta/decisions/canary/run")
async def run_canary_shadow(
    payload: CanaryRunRequest,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Shadow-mode scoring: serve from the active model, score with the canary.

    Served decisions never come from the candidate; the run records a canary
    trace and updates divergence/confidence for the model.

    That last clause is why this is gated. No customer is answered from the
    candidate, but the run *does* move the model's divergence and confidence
    counters -- which is what `canary_error_rate_below_threshold` reads on the
    release ladder. An unauthenticated caller who could write those counters
    could unblock their own promotion, so "no traffic" is not the same as "no
    effect". See `AUTHZ_RULES["decision_canary_admin"]`.
    """
    try:
        result = model_versioning.get_default_runner().shadow_score(
            payload.model_name, payload.context, entity_ref=payload.entity_ref
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if result.get("canary_version") is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=result.get("reason", "no canary candidate"),
        )
    return result


@app.post("/meta/decisions/canary/promote")
async def promote_canary(
    payload: CanaryPromoteRequest,
    current_user: models.User = Depends(deps.get_current_admin_user),
):
    """Promote the canary candidate to active (optimistic-lock guarded).

    Pass the ``guard_version`` you observed; a stale write returns 409 instead
    of silently clobbering a concurrent promotion.

    The optimistic lock guards against a *concurrent* admin. It does not guard
    against an anonymous caller, which is why the dependency is here and not
    only in the rule table: this endpoint decides which model scores every
    customer, and it previously did so with no credential of any kind. See
    `AUTHZ_RULES["decision_canary_admin"]`.
    """
    try:
        return model_versioning.get_default_registry().promote(
            payload.model_name,
            expected_version=payload.expected_version,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except optimistic_locking.StaleVersionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


@app.get("/meta/decisions/explanations")
async def list_explanations(
    entity_ref: str | None = Query(default=None, max_length=200),
    decision_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """List decision traces, newest first, filterable by entity/type."""
    store = explainability.get_default_trace_store()
    return {
        "generated_at": datetime.now(timezone.utc),
        "total": store.stats()["total"],
        "limit": limit,
        "traces": store.list(
            entity_ref=entity_ref,
            decision_type=decision_type,
            limit=limit,
        ),
    }


@app.get("/meta/decisions/explanations/{decision_id}")
async def explanation_detail(decision_id: str):
    """Full factor-trail explanation for one recorded decision."""
    store = explainability.get_default_trace_store()
    try:
        return store.explain(decision_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

@app.get("/meta/tenants")
async def tenants_metadata():
    """Expose runtime multi-tenant DB routing: mode, registration, pool tuning."""
    return tenant_router.build_tenant_router_catalog()


@app.get("/meta/tenants/policy")
async def tenant_routing_policy_metadata():
    """Expose the tenant routing policy: id pattern, allowlist, lifecycle, plan."""
    return tenant_router.build_tenant_routing_policy()


@app.get("/meta/partitions")
async def partitions_metadata():
    """Expose the dynamic partition lifecycle: policies, retention, active set."""
    return partition_manager.build_partition_manager_catalog()


@app.get("/meta/partitions/policy")
async def partition_lifecycle_policy_metadata():
    """Expose the tiered retention lifecycle behind the partition catalog.

    A sibling of ``/meta/partitions``, not a key inside it: that catalog's key
    set is a pinned contract. This is where the retention tiers, the guard
    bands that outrank them, the batch caps and the last retirement are
    introspected.
    """
    return partition_manager.build_partition_lifecycle_policy()


@app.get("/meta/zero-trust")
async def zero_trust_metadata():
    """Expose the zero-trust surfaces: HSM signing, biometrics, risk, cell access."""
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hsm_signer": hsm_signer.build_hsm_signer_catalog(),
        "biometric_vault": biometric_vault.build_biometric_vault_catalog(),
        "risk_evaluator": risk_evaluator.build_risk_evaluator_catalog(),
        "cell_matrix": cell_matrix.build_cell_matrix_catalog(),
    }


@app.get("/meta/zero-trust/policy")
async def zero_trust_policy_metadata():
    """Expose the governance layer over the zero-trust primitives.

    ``/meta/zero-trust`` reports what exists -- which keys are loaded, which
    templates are enrolled, which cells are guarded. This reports the policy
    that constrains them: key rotation and retirement rules, enrollment quality
    and presentation-attack bounds, risk composition / escalation / suppression,
    and the layer-5 access narrowing (purposes, read budgets, masking profiles).
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hsm_signer": hsm_signer.build_signer_governance(),
        "biometric_vault": biometric_vault.build_biometric_vault_policy(),
        "risk_evaluator": risk_evaluator.build_risk_evaluator_policy(),
        "cell_matrix": cell_matrix.build_cell_matrix_policy(),
        "note": (
            "each section is the sibling policy catalog of the matching entry in "
            "/meta/zero-trust; the observable catalogs there are unchanged, so a "
            "consumer can read state from one and constraints from the other"
        ),
    }


@app.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(text("SELECT 1"))
        database_ok = result.scalar() == 1
        
        return {
            "status": "healthy",
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "environment": APP_ENV,
            },
            "database": {
                "connected": database_ok,
            },
            "cors": {
                "allowed_origins": APP_CORS_ORIGINS,
            },
            "locale": locale_payload(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "environment": APP_ENV,
            },
            "database": {
                "connected": False,
            },
            "locale": locale_payload(),
            "error": str(e)
        }


@app.get("/meta/runtime")
async def runtime_metadata():
    return {
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "environment": APP_ENV,
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
        },
        "observability": {
            "logging": "standard",
            "health_endpoint": "/health",
            "metadata_endpoint": "/meta",
        },
    }

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)