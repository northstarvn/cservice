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
from app.schemas.chat import EcosystemStatusReport, EcosystemSubserviceStatus, AuthenticatedProbeReport, AuthenticatedProbeTarget, CapabilitySummaryReport, CapabilitySummaryItem
from app.routers import bookings, chat, users
from app import deps, models

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


@app.get("/meta")
async def app_metadata():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "cors_origins": APP_CORS_ORIGINS,
        "features": [
            "auth",
            "bookings",
            "booking_events",
            "chat",
            "chat_history_summary",
            "retention",
        ],
    }


@app.get("/meta/capabilities")
async def app_capabilities():
    return CapabilitySummaryReport(
        generated_at=datetime.now(timezone.utc),
        title="Study-aligned capability summary",
        items=[
            CapabilitySummaryItem(
                key="youth_conversion_intelligence",
                study_theme="Younger demographics are less likely to make large purchases online.",
                feature="Segment-aware conversion guidance",
                signal_focus=["small cart value", "repeat short visits", "social referral traffic", "promo sensitivity"],
                route="/chat/system-priorities",
            ),
            CapabilitySummaryItem(
                key="market_penetration_adoption",
                study_theme="Higher internet penetration does not always mean higher e-commerce adoption.",
                feature="Regional adoption gap analysis",
                signal_focus=["traffic without conversion", "payment failure patterns", "shipping delays", "regional marketplace usage"],
                route="/chat/retention-dashboard",
            ),
            CapabilitySummaryItem(
                key="device_experience_optimizer",
                study_theme="More developed regions have lower engagement in mobile-only usage.",
                feature="Device mix and UX optimization",
                signal_focus=["device mix", "browser usage", "session length by device", "desktop conversion lift"],
                route="/meta/probe/routes",
            ),
            CapabilitySummaryItem(
                key="ad_cpc_geo_optimizer",
                study_theme="Regions with lower average income can have higher CPC in advertising.",
                feature="Geo-aware acquisition planning",
                signal_focus=["ad CPC", "regional conversion rate", "payment method choice", "channel efficiency"],
                route="/meta/ecosystem",
            ),
            CapabilitySummaryItem(
                key="older_adult_engagement",
                study_theme="Older adults are not only growing in usage but also outperform younger groups in certain metrics.",
                feature="Senior-friendly engagement tracking",
                signal_focus=["older cohort engagement", "content completion", "support success", "repeat usage"],
                route="/chat/insights",
            ),
            CapabilitySummaryItem(
                key="low_penetration_engagement",
                study_theme="Regions with lower internet penetration have users who are more engaged per capita.",
                feature="Per-capita engagement monitoring",
                signal_focus=["engagement per user", "network quality", "message frequency", "local app usage"],
                route="/meta/features",
            ),
        ],
    )


@app.get("/meta/ecosystem", response_model=EcosystemStatusReport)
async def app_ecosystem():
    health_status = "ready"
    health_notes = ["database health is exposed through /health"]
    runtime_notes = ["runtime metadata is exposed through /meta/runtime"]
    subservices = {
        "identity": EcosystemSubserviceStatus(
            name="identity",
            purpose="authentication and account management",
            routes=["/users/me", "/users/password"],
            status=health_status,
            health_endpoint="/health",
            notes=health_notes + ["backed by auth-protected user endpoints"],
        ),
        "booking_core": EcosystemSubserviceStatus(
            name="booking_core",
            purpose="booking lifecycle, change tracking, and analytics",
            routes=["/bookings", "/bookings/analytics/summary", "/bookings/analytics/events"],
            status=health_status,
            health_endpoint="/health",
            notes=health_notes + ["supports lifecycle and analytics workflows"],
        ),
        "chat_intelligence": EcosystemSubserviceStatus(
            name="chat_intelligence",
            purpose="conversation memory, sentiment analysis, and retention scoring",
            routes=[
                "/chat/history",
                "/chat/insights",
                "/chat/system-priorities",
                "/chat/trends",
                "/chat/retention-dashboard",
            ],
            status=health_status,
            health_endpoint="/chat/admin/snapshot-health-score",
            notes=runtime_notes + ["structured summary and retention reports available"],
        ),
        "retention_ops": EcosystemSubserviceStatus(
            name="retention_ops",
            purpose="snapshot health, compliance, launch readiness, and go/no-go checks",
            routes=[
                "/chat/admin/snapshot-health-score",
                "/chat/admin/snapshot-operations-status",
                "/chat/admin/snapshot-operations-compliance",
                "/chat/admin/snapshot-operations-launch-readiness",
                "/chat/admin/snapshot-operations-gonogo",
            ],
            status=health_status,
            health_endpoint="/chat/admin/snapshot-health-score",
            notes=["operational readiness surface is exposed"] + health_notes,
        ),
        "portfolio_intelligence": EcosystemSubserviceStatus(
            name="portfolio_intelligence",
            purpose="cross-segment monetization and capability discovery",
            routes=["/chat/admin/monetization-cohorts", "/meta/capabilities"],
            status="ready" if APP_ENV != "production" else health_status,
            health_endpoint="/meta/capabilities",
            notes=["capability discovery is exposed through metadata"] + runtime_notes,
        ),
    }
    overall_status = "ready" if all(item.status == "ready" for item in subservices.values()) else "degraded"
    return EcosystemStatusReport(
        name=APP_NAME,
        version=APP_VERSION,
        environment=APP_ENV,
        generated_at=datetime.now(timezone.utc),
        overall_status=overall_status,
        subservices=subservices,
    )


@app.get("/meta/probe/routes", response_model=AuthenticatedProbeReport)
async def app_authenticated_probe(current_user: models.User = Depends(deps.get_current_user)):
    targets = [
        AuthenticatedProbeTarget(route="/users/me", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/bookings", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/chat/history", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/chat/retention-dashboard", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/chat/snapshots", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/chat/snapshots/delta", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/chat/snapshots/trends", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/chat/system-priorities", method="GET", requires_auth=True, expected_status=200),
        AuthenticatedProbeTarget(route="/chat/admin/snapshot-health-score", method="GET", requires_auth=True, expected_status=200),
    ]
    return AuthenticatedProbeReport(
        generated_at=datetime.now(timezone.utc),
        scope="authenticated route-level probe",
        actor=current_user.username,
        targets=targets,
        status="ready",
        notes=[
            "Auth is enforced through the shared dependency layer.",
            "Targets are representative route-level checks for protected surfaces.",
        ],
    )


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
            "booking_analytics": "/bookings/analytics/summary",
            "booking_events": "/bookings/analytics/events",
            "booking_export": "/bookings/analytics/export",
            "chat_history": "/chat/history",
            "retention_dashboard": "/retention/dashboard",
            "retention_maintenance": "/retention/maintenance",
        },
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