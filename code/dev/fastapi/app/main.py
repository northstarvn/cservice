import os
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
from app.i18n import locale_payload
from app.routers import bookings, chat, topics, users
from app.services import chat_analytics, loyalty_journey, policy_scoring, activity_tree, communication_strategy

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
    
    yield  # App runs here
    
    # Shutdown
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