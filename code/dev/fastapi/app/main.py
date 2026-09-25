import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import platform

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, engine, get_db
from app import i18n
from app.i18n import locale_payload
from app.routers import audit, bookings, chat, topics, users
from app.services import audit_log, chat_analytics, loyalty_journey, policy_scoring, activity_tree, communication_strategy, arrears_payments, points_exchange
from app.services.efficiency_audit import build_efficiency_audit_catalog
from app import security
from app import (
    biometric_vault,
    cell_matrix,
    high_throughput_pipeline,
    hsm_signer,
    partition_manager,
    protobuf_transaction_spec,
    risk_evaluator,
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

    # Optional background workers (off by default so single-tenant behavior and
    # the test suite stay untouched; enable per deployment via env).
    partition_worker = None
    try:
        if partition_manager.PARTITION_WORKER_ENABLED:
            partition_worker = asyncio.create_task(
                partition_manager.run_partition_cycle_forever(),
                name="partition-lifecycle",
            )
            logger.info("Partition lifecycle worker started")
        if high_throughput_pipeline.PIPELINE_AUTOSTART:
            await high_throughput_pipeline.get_default_pipeline().start()
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
app.include_router(bookings.router, prefix="/bookings", tags=["bookings"])
app.include_router(chat.router, tags=["chat"])
app.include_router(topics.router, tags=["topics"])
app.include_router(audit.router, prefix="/audit", tags=["audit"])


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
                "routes": ["/users/me", "/users/password"],
                "purpose": "authentication and account management",
                "status": "ready",
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
                ],
                "purpose": "topic catalog search, ranked suggestions, workspace portfolio, and cross-service topic intelligence",
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
                ],
                "purpose": "immutable operator/system action trail for explainability, with action/severity catalog and rollups",
                "status": "ready",
            },
            "localization": {
                "routes": [
                    "/meta/i18n",
                    "/meta/scoring-catalog",
                ],
                "purpose": "locale resolution and message translation across en/es/fr with fallback to English",
                "status": "ready",
            },
            "tenant_routing": {
                "routes": [
                    "/meta/tenants",
                ],
                "purpose": "runtime multi-tenant database connection routing: per-tenant pooled engines, registration/deregistration, header-driven tenant scope",
                "status": "ready",
                "mode": tenant_router.build_tenant_router_catalog()["mode"],
            },
            "partition_management": {
                "routes": [
                    "/meta/partitions",
                ],
                "purpose": "dynamic data partition lifecycle: config-driven partition creation, archival, and retention-based expiry for append-only volumes",
                "status": "ready",
                "worker_enabled": partition_manager.PARTITION_WORKER_ENABLED,
            },
            "zero_trust": {
                "routes": [
                    "/meta/zero-trust",
                ],
                "purpose": "cryptographic security & zero-trust access: HSM signing abstractions, biometric validation vaults, contextual risk evaluation, and data-cell access matrices",
                "status": "ready",
                "signer_algorithm": hsm_signer.build_hsm_signer_catalog()["algorithm"],
            },
            "high_velocity_audit": {
                "routes": [
                    "/audit/pipeline/event",
                    "/audit/pipeline/stats",
                    "/audit/transactions",
                    "/audit/transactions/spec",
                ],
                "purpose": "async high-throughput audit pipeline encoding immutable, append-only protobuf transactions for security signals",
                "status": "ready",
                "autostart": high_throughput_pipeline.PIPELINE_AUTOSTART,
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
            "booking_analytics": "/bookings/analytics/summary",
            "booking_events": "/bookings/analytics/events",
            "booking_export": "/bookings/analytics/export",
            "chat_history": "/chat/history",
            "retention_dashboard": "/chat/retention-dashboard",
            "retention_maintenance": "/retention/maintenance",
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
            "audit_trail_catalog": "/audit/trail-catalog",
            "i18n_catalog": "/meta/i18n",
            "tenant_routing": "/meta/tenants",
            "partition_management": "/meta/partitions",
            "zero_trust": "/meta/zero-trust",
            "pipeline_event": "/audit/pipeline/event",
            "pipeline_stats": "/audit/pipeline/stats",
            "transactions": "/audit/transactions",
            "transactions_spec": "/audit/transactions/spec",
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
        "i18n": i18n.build_i18n_catalog(),
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
    }


@app.get("/meta/i18n")
async def i18n_catalog(locale: str | None = None):
    """Expose the localization surface: supported locales and message catalog.

    The payload is locale-aware: pass ``?locale=es`` to see how the catalog
    will resolve for that audience, otherwise English is used as the baseline.
    """
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "catalog": i18n.build_i18n_catalog(),
        "resolution": i18n.locale_payload(locale),
    }

@app.get("/meta/tenants")
async def tenants_metadata():
    """Expose runtime multi-tenant DB routing: mode, registration, pool tuning."""
    return tenant_router.build_tenant_router_catalog()


@app.get("/meta/partitions")
async def partitions_metadata():
    """Expose the dynamic partition lifecycle: policies, retention, active set."""
    return partition_manager.build_partition_manager_catalog()


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