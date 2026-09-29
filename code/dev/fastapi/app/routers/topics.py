"""Topic HTTP surface.

Two things live here, in that order of importance.

**The routes.** Thin adapters. Each one resolves the caller, fetches the
caller's current selection where it needs one, and hands off to a service in
``app.services.topics``. There is no decision logic in this file and there
should not be any: the catalog, the guards, the source policies, the match
modes, the ranking weights, the drift bands and the lifecycle policies all
live in config tables the service reads.

**The request-governance layer** (the lower half). The adapters above pass
caller-supplied numbers and free text straight through, so the questions that
usually get asked about an API surface — how large may this response get, what
happens when a caller asks for a million rows, which routes are worth caching
— had no home. They do now, as tables (``TOPIC_ROUTE_POLICIES``,
``TOPIC_PARAM_BOUNDS``, ``TOPIC_RESPONSE_LIMITS``, ``TOPIC_CACHE_POLICIES``,
``TOPIC_RATE_TIERS``, ``TOPIC_CAPACITY_RULES``, ``TOPIC_ERROR_MAP``) plus an
offline planner and a request recorder.

The layer is **advisory**: it describes the routes and answers "what would
happen", and nothing it does changes the behaviour of a single existing route.
The recorder is the one piece wired into the live path, and it is wired through
a route wrapper that returns the original response object untouched.
"""
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
import hashlib
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.schemas import schemas
from app.services.topics import (
    TOPIC_MATCH_MODES,
    TOPIC_SOURCE_POLICIES,
    archive_topic_selection,
    build_topic_coverage_report,
    build_topic_drift_report,
    build_topic_governance_report,
    build_topic_intelligence_overview,
    build_topic_lifecycle_report,
    build_topic_search_report,
    build_topic_catalog_report,
    build_topic_portfolio_report,
    build_topic_recommendation_report,
    build_topic_selection_history_report,
    build_topic_selection_report,
    build_topic_selection_validation,
    build_topic_suggestion_report,
    build_topic_taxonomy_integrity_report,
    build_topic_taxonomy_report,
    build_topic_theme_report,
    build_topic_workspace_report,
    build_typed_topic_catalog_report,
    build_typed_topic_recommendation_report,
    build_typed_topic_intelligence_report,
    build_typed_topic_theme_report,
    build_typed_topic_selection_history_report,
    build_typed_topic_selection_report,
    classify_topic_match,
    create_topic_selection,
    get_latest_topic_selection,
    get_topic_selection_history,
    rank_topics,
    replace_topic_selection,
)

# The ``source`` a write may declare. Taken from the service's own table so the
# param bound cannot drift from the policy it is meant to enforce: a bound that
# hardcoded its own list would accept a source the service has since retired.
TOPIC_SOURCE_NAMES = tuple(str(row["source"]) for row in TOPIC_SOURCE_POLICIES)


class GovernedAPIRoute(APIRoute):
    """Route wrapper that records what each request was asked for.

    Wrapping ``get_route_handler`` is the only way to see every route in this
    router without editing thirty-one handlers, and returning the original
    response object unchanged is what keeps this strictly additive. The
    recording itself is defined further down, next to the recorder it feeds.
    """

    def get_route_handler(self):  # noqa: D102 - FastAPI hook
        original = super().get_route_handler()

        async def _handler(request: Request):
            response = await original(request)
            _observe(request)
            return response

        return _handler


router = APIRouter(prefix="/topics", tags=["topics"], route_class=GovernedAPIRoute)



@router.get("/catalog", response_model=schemas.TopicCatalogReport)
async def read_topic_catalog():
    return build_typed_topic_catalog_report()


@router.get("/themes", response_model=schemas.TopicThemeReport)
async def read_topic_themes():
    return build_typed_topic_theme_report()


@router.get("/intelligence", response_model=schemas.TopicIntelligenceReport)
async def read_topic_intelligence(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    return build_typed_topic_intelligence_report(selection)


@router.get("/coverage")
async def read_topic_coverage(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    return build_topic_coverage_report(selection)


@router.get("/portfolio")
async def read_topic_portfolio(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    return build_topic_portfolio_report(selection)


@router.get("/workspace", response_model=schemas.TopicWorkspaceReport)
async def read_topic_workspace(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    selections = await get_topic_selection_history(db, current_user.id)
    return build_topic_workspace_report(current_user.id, selection, selections)


@router.get("/overview", response_model=schemas.TopicIntelligenceOverview)
async def read_topic_overview(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    selections = await get_topic_selection_history(db, current_user.id)
    return build_topic_intelligence_overview(current_user.id, selection, selections)


@router.get("/search", response_model=schemas.TopicSearchReport)
async def read_topic_search(
    query: str,
    limit: int = 5,
    page: int = 1,
    per_page: int = 5,
    theme: str | None = None,
    sector: str | None = None,
):
    return build_topic_search_report(query, limit=limit, page=page, per_page=per_page, theme=theme, sector=sector)


@router.get("/suggestions", response_model=schemas.TopicSuggestionReport)
async def read_topic_suggestions(
    query: str,
    limit: int = 5,
    theme: str | None = None,
    sector: str | None = None,
):
    return build_topic_suggestion_report(query, limit=limit, theme=theme, sector=sector)


@router.get("/recommendations", response_model=schemas.TopicRecommendationReport)
async def read_topic_recommendations(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    return build_typed_topic_recommendation_report(selection)


@router.get("/current", response_model=schemas.TopicSelectionReport)
async def read_current_topic_selection(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    if not selection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic selection not found")
    return build_typed_topic_selection_report(selection)


@router.post("/current", response_model=schemas.TopicSelectionReport)
async def create_current_topic_selection(
    topic_request: schemas.TopicSelectionCreate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        selection = await create_topic_selection(
            db,
            current_user,
            topic=topic_request.topic,
            source=topic_request.source,
            rationale=topic_request.rationale,
            confidence=topic_request.confidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return build_typed_topic_selection_report(selection)


@router.put("/current", response_model=schemas.TopicSelectionReport)
async def replace_current_topic_selection(
    topic_request: schemas.TopicSelectionCreate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        selection = await replace_topic_selection(
            db,
            current_user,
            topic=topic_request.topic,
            source=topic_request.source,
            rationale=topic_request.rationale,
            confidence=topic_request.confidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return build_typed_topic_selection_report(selection)


@router.get("/history", response_model=schemas.TopicSelectionHistoryReport)
async def read_topic_selection_history(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selections = await get_topic_selection_history(db, current_user.id)
    return build_typed_topic_selection_history_report(current_user.id, selections)


@router.delete("/current", response_model=schemas.TopicSelectionReport)
async def archive_current_topic_selection(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    archived = await archive_topic_selection(db, current_user)
    if not archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic selection not found")

    return build_typed_topic_selection_report(archived)


# --- governance surfaces -----------------------------------------------------
#
# Thin adapters only: every decision below is made by the config tables in
# ``app.services.topics`` (sources, guards, sensitive patterns, match modes,
# ranking weights, drift bands, lifecycle policies), so changing behaviour is a
# table edit rather than a code change.


@router.get("/taxonomy", response_model=schemas.TopicTaxonomyReport)
async def read_topic_taxonomy():
    return build_topic_taxonomy_report()


@router.get("/governance")
async def read_topic_governance():
    return build_topic_governance_report()


@router.get("/integrity")
async def read_topic_taxonomy_integrity():
    """Data-quality findings over the hand-maintained taxonomy tables.

    Separate from ``/topics/governance`` because the audience differs: policy is
    configured, but the findings below (unthemed topics, a duplicated catalog
    entry) have to be *fixed* in the tables.
    """
    return build_topic_taxonomy_integrity_report()


@router.get("/match")
async def read_topic_match(topic: str):
    """How a free-text topic maps to the catalog, and which mode matched."""
    match = classify_topic_match(topic)
    return {
        "generated_at": datetime.now(timezone.utc),
        **match,
        "modes": [dict(row) for row in TOPIC_MATCH_MODES],
    }


@router.get("/ranked")
async def read_ranked_topics(
    query: str = "",
    limit: int = 5,
    theme: str | None = None,
    sector: str | None = None,
    strict_filters: bool = False,
):
    return rank_topics(
        query,
        limit=limit,
        theme=theme,
        sector=sector,
        strict_filters=strict_filters,
    )


@router.post("/validate")
async def validate_topic_selection(
    topic_request: schemas.TopicSelectionCreate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Pre-flight a proposed selection against the guard table. Writes nothing.

    The verdict is advisory — ``POST /topics/current`` still behaves exactly as
    before — so this is a "may I, and why" check, not a new gate.
    """
    current = await get_latest_topic_selection(db, current_user.id)
    return build_topic_selection_validation(
        topic_request.topic,
        source=topic_request.source,
        rationale=topic_request.rationale,
        confidence=topic_request.confidence,
        current_topic=current.topic if current else None,
    )


@router.get("/drift")
async def read_topic_drift(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
    window: int | None = None,
):
    selections = await get_topic_selection_history(db, current_user.id)
    report = build_topic_drift_report(selections, window=window)
    report["user_id"] = current_user.id
    return report


@router.get("/lifecycle")
async def read_topic_lifecycle(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
    policy: str | None = None,
):
    selections = await get_topic_selection_history(db, current_user.id)
    report = build_topic_lifecycle_report(selections, policy_id=policy)
    report["user_id"] = current_user.id
    return report


# --- Request governance -------------------------------------------------------
#
# Everything above this line is a thin adapter: it resolves the caller, fetches
# the selection, and hands off to a service. That is the right shape for a
# router, and it is why this module is small.
#
# What a thin adapter does *not* have is any opinion about the inputs. Every
# number and every free-text string on the routes above is caller-supplied and
# arrives at ``app.services.topics`` untouched:
#
#     GET /topics/search?query=...&limit=100000&per_page=100000
#     GET /topics/drift?window=-5
#     GET /topics/match?topic=<4 KB of text>
#
# FastAPI coerces the *type* and nothing else. ``limit=100000`` is a valid
# integer, so it is honoured, and ``build_topic_suggestion_report`` then ranks
# and sorts on a bound nobody chose. ``window=-5`` reaches
# ``sequence[-int(window):]`` as ``sequence[5:]`` — a negative window silently
# becomes "everything except the first few", the opposite of what the caller
# asked for, and it is the kind of bug that only shows up in production.
#
# The fix is not to add a branch to each handler. It is a *table* of what each
# parameter is allowed to be, which route requires what, and what a request is
# worth to serve — plus an offline planner so all of it can be reviewed without
# sending traffic.
#
# The rules that keep this honest:
#
# - **Additive only.** No existing route, signature, or payload changes. The
#   table describes the routes as they are; nothing is enforced on them yet.
#   ``GET /topics/plan`` and the governance reads are new surfaces, and the
#   recorder is wired in through a route wrapper that returns the response
#   untouched.
# - **Planning never costs budget.** ``admit_topic_request`` only consumes a
#   token when asked. Otherwise reviewing a thousand scenarios would rate-limit
#   the reviewer, and the rate limiter would be measuring the wrong thing.
# - **Clamps are reported, not hidden.** A caller who asked for ``per_page=5000``
#   is told it received 100 and why, because a silently shortened page reads as
#   "there is nothing more" rather than "you hit a limit".
# - **The table and the router are cross-checked.** ``build_topic_route_drift_report``
#   walks ``router.routes`` at call time, so a route added without a policy row
#   is reported rather than silently ungoverned.
#
# Config tables: ``TOPIC_ROUTE_POLICIES``, ``TOPIC_PARAM_BOUNDS``,
# ``TOPIC_RESPONSE_LIMITS``, ``TOPIC_CACHE_POLICIES``, ``TOPIC_RATE_TIERS``,
# ``TOPIC_CAPACITY_RULES``, ``TOPIC_ERROR_MAP``, ``TOPIC_RATE_LIMITER``.


def _topic_now() -> datetime:
    return datetime.now(timezone.utc)


# Scope classes. A route's scope says what kind of request it is, which is what
# the rest of the table is keyed on.
TOPIC_SCOPE_CATALOG = "catalog"
TOPIC_SCOPE_USER = "user"
TOPIC_SCOPE_USER_WRITE = "user_write"
TOPIC_SCOPE_ADVISORY = "advisory"
TOPIC_SCOPE_GOVERNANCE = "governance"

TOPIC_SCOPES = (
    TOPIC_SCOPE_CATALOG,
    TOPIC_SCOPE_USER,
    TOPIC_SCOPE_USER_WRITE,
    TOPIC_SCOPE_ADVISORY,
    TOPIC_SCOPE_GOVERNANCE,
)


# One row per route in this router. ``params`` is the set the route actually
# reads; a param the route does not declare is reported as ``unused`` by the
# planner rather than silently dropped.
#
# ``cost_weight`` is relative, not a latency measurement: 1.0 is "reads a
# static table", and 6.0 is "ranks the whole catalog". It exists so a
# capacity review has an ordering, not so it can be summed into an SLO.
TOPIC_ROUTE_POLICIES: list[dict[str, object]] = [
    {
        "route_id": "read_topic_catalog",
        "method": "GET",
        "path": "/topics/catalog",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_stable",
        "rate_tier": "catalog",
        "cost_weight": 1.0,
        "max_rows": 200,
        "mutating": False,
        "params": (),
        "description": "The whole topic catalog, typed.",
    },
    {
        "route_id": "read_topic_themes",
        "method": "GET",
        "path": "/topics/themes",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_stable",
        "rate_tier": "catalog",
        "cost_weight": 1.0,
        "max_rows": 200,
        "mutating": False,
        "params": (),
        "description": "Theme groups and their coverage.",
    },
    {
        "route_id": "read_topic_intelligence",
        "method": "GET",
        "path": "/topics/intelligence",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 3.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Intelligence for the caller's current selection.",
    },
    {
        "route_id": "read_topic_coverage",
        "method": "GET",
        "path": "/topics/coverage",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 3.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Coverage of the caller's topic against the catalog.",
    },
    {
        "route_id": "read_topic_portfolio",
        "method": "GET",
        "path": "/topics/portfolio",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 3.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Theme/sector portfolio for the caller's topic.",
    },
    {
        "route_id": "read_topic_workspace",
        "method": "GET",
        "path": "/topics/workspace",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 4.0,
        "max_rows": 200,
        "mutating": False,
        "params": (),
        "description": "Selection plus its history, as a workspace.",
    },
    {
        "route_id": "read_topic_overview",
        "method": "GET",
        "path": "/topics/overview",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 5.0,
        "max_rows": 200,
        "mutating": False,
        "params": (),
        "description": "The composite overview: workspace + suggestions.",
    },
    {
        "route_id": "read_topic_search",
        "method": "GET",
        "path": "/topics/search",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_short",
        "rate_tier": "search",
        "cost_weight": 6.0,
        "max_rows": 200,
        "mutating": False,
        "params": ("query", "limit", "page", "per_page", "theme", "sector"),
        "description": "Ranked, paginated catalog search. Heaviest route here.",
    },
    {
        "route_id": "read_topic_suggestions",
        "method": "GET",
        "path": "/topics/suggestions",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_short",
        "rate_tier": "search",
        "cost_weight": 4.0,
        "max_rows": 100,
        "mutating": False,
        "params": ("query", "limit", "theme", "sector"),
        "description": "Unpaginated ranked suggestions.",
    },
    {
        "route_id": "read_topic_recommendations",
        "method": "GET",
        "path": "/topics/recommendations",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 4.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Recommendations for the caller's current selection.",
    },
    {
        "route_id": "read_current_topic_selection",
        "method": "GET",
        "path": "/topics/current",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 2.0,
        "max_rows": 20,
        "mutating": False,
        "params": (),
        "description": "The current selection; 404 when there is none.",
    },
    {
        "route_id": "create_current_topic_selection",
        "method": "POST",
        "path": "/topics/current",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER_WRITE,
        "cache": "no_store",
        "rate_tier": "write",
        "cost_weight": 5.0,
        "max_rows": 20,
        "mutating": True,
        "params": ("topic", "source", "rationale", "confidence"),
        "description": "Create a selection. 422 on a ValueError from the service.",
    },
    {
        "route_id": "replace_current_topic_selection",
        "method": "PUT",
        "path": "/topics/current",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER_WRITE,
        "cache": "no_store",
        "rate_tier": "write",
        "cost_weight": 5.0,
        "max_rows": 20,
        "mutating": True,
        "params": ("topic", "source", "rationale", "confidence"),
        "description": "Replace the selection. 422 on a ValueError from the service.",
    },
    {
        "route_id": "read_topic_selection_history",
        "method": "GET",
        "path": "/topics/history",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 3.0,
        "max_rows": 500,
        "mutating": False,
        "params": (),
        "description": "Full selection history for the caller.",
    },
    {
        "route_id": "archive_current_topic_selection",
        "method": "DELETE",
        "path": "/topics/current",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER_WRITE,
        "cache": "no_store",
        "rate_tier": "write",
        "cost_weight": 3.0,
        "max_rows": 20,
        "mutating": True,
        "params": (),
        "description": "Archive the current selection; 404 when there is none.",
    },
    {
        "route_id": "read_topic_taxonomy",
        "method": "GET",
        "path": "/topics/taxonomy",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_stable",
        "rate_tier": "catalog",
        "cost_weight": 1.0,
        "max_rows": 300,
        "mutating": False,
        "params": (),
        "description": "The hand-maintained taxonomy tables.",
    },
    {
        "route_id": "read_topic_governance",
        "method": "GET",
        "path": "/topics/governance",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_stable",
        "rate_tier": "catalog",
        "cost_weight": 1.0,
        "max_rows": 300,
        "mutating": False,
        "params": (),
        "description": "Configured policy: sources, guards, match modes, weights.",
    },
    {
        "route_id": "read_topic_taxonomy_integrity",
        "method": "GET",
        "path": "/topics/integrity",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_stable",
        "rate_tier": "catalog",
        "cost_weight": 2.0,
        "max_rows": 300,
        "mutating": False,
        "params": (),
        "description": "Data-quality findings about the taxonomy tables.",
    },
    {
        "route_id": "read_topic_match",
        "method": "GET",
        "path": "/topics/match",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_stable",
        "rate_tier": "catalog",
        "cost_weight": 1.0,
        "max_rows": 50,
        "mutating": False,
        "params": ("topic",),
        "description": "How one free-text topic maps to the catalog.",
    },
    {
        "route_id": "read_ranked_topics",
        "method": "GET",
        "path": "/topics/ranked",
        "auth": "none",
        "scope": TOPIC_SCOPE_CATALOG,
        "cache": "catalog_short",
        "rate_tier": "search",
        "cost_weight": 5.0,
        "max_rows": 200,
        "mutating": False,
        "params": ("query", "limit", "theme", "sector", "strict_filters"),
        "description": "Raw ranked list, with optional strict filters.",
    },
    {
        "route_id": "validate_topic_selection",
        "method": "POST",
        "path": "/topics/validate",
        "auth": "user",
        "scope": TOPIC_SCOPE_ADVISORY,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 2.0,
        "max_rows": 50,
        "mutating": False,
        "params": ("topic", "source", "rationale", "confidence"),
        "description": "Advisory pre-flight of a proposed selection. Writes nothing.",
    },
    {
        "route_id": "read_topic_drift",
        "method": "GET",
        "path": "/topics/drift",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 3.0,
        "max_rows": 200,
        "mutating": False,
        "params": ("window",),
        "description": "Is the caller's topic settled? ``window`` is a slice count.",
    },
    {
        "route_id": "read_topic_lifecycle",
        "method": "GET",
        "path": "/topics/lifecycle",
        "auth": "user",
        "scope": TOPIC_SCOPE_USER,
        "cache": "no_store",
        "rate_tier": "read",
        "cost_weight": 3.0,
        "max_rows": 200,
        "mutating": False,
        "params": ("policy",),
        "description": "Lifecycle transitions under a named policy.",
    },
    # --- request-governance surfaces ---
    {
        "route_id": "read_topic_route_policies",
        "method": "GET",
        "path": "/topics/governance/routes",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "governance_short",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "The route policy table as the router actually implements it.",
    },
    {
        "route_id": "read_topic_param_bounds",
        "method": "GET",
        "path": "/topics/governance/params",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "governance_short",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 50,
        "mutating": False,
        "params": (),
        "description": "Declared bounds for every request parameter.",
    },
    {
        "route_id": "read_topic_response_limits",
        "method": "GET",
        "path": "/topics/governance/limits",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "governance_short",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Response size caps and cache policies per route.",
    },
    {
        "route_id": "read_topic_capacity",
        "method": "GET",
        "path": "/topics/governance/capacity",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "governance_short",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Cost model and SLO tier per route.",
    },
    {
        "route_id": "read_topic_request_analytics",
        "method": "GET",
        "path": "/topics/governance/requests",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "no_store",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 200,
        "mutating": False,
        "params": ("window",),
        "description": "What the live routes have actually been asked for.",
    },
    {
        "route_id": "read_topic_policy_validation",
        "method": "GET",
        "path": "/topics/governance/validation",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "governance_short",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Errors and warnings from checking these tables.",
    },
    {
        "route_id": "read_topic_route_drift",
        "method": "GET",
        "path": "/topics/governance/drift",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "governance_short",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 100,
        "mutating": False,
        "params": (),
        "description": "Where the route table and the router disagree.",
    },
    {
        "route_id": "plan_topic_request",
        "method": "GET",
        "path": "/topics/plan",
        "auth": "none",
        "scope": TOPIC_SCOPE_GOVERNANCE,
        "cache": "no_store",
        "rate_tier": "governance",
        "cost_weight": 1.0,
        "max_rows": 50,
        "mutating": False,
        "params": ("route_id", "method", "consume", "window", "query", "limit", "page", "per_page", "theme", "sector", "topic", "policy", "strict_filters", "source", "rationale", "confidence"),
        "description": "Plan a request offline: bounds, admission, page math, caps.",
    },
]

TOPIC_ROUTE_POLICY_BY_ID = {str(row["route_id"]): row for row in TOPIC_ROUTE_POLICIES}
TOPIC_ROUTE_IDS = tuple(sorted(TOPIC_ROUTE_POLICY_BY_ID))

# Declared bounds for every parameter any route reads. ``on_violation`` is the
# interesting column: ``clamp`` repairs a value and records the repair,
# ``reject`` refuses it. Numeric parameters clamp (a caller asking for 200
# results means "as many as you have", not "error"), while enumerated and
# required parameters reject (a wrong ``source`` is a bug, not a preference).
TOPIC_PARAM_BOUNDS: list[dict[str, object]] = [
    {
        "param": "query",
        "kind": "str",
        "default": "",
        "max_length": 200,
        "required": False,
        "on_violation": "reject",
        "description": "Free-text search. Long enough for a sentence, short enough to rank.",
    },
    {
        "param": "topic",
        "kind": "str",
        "default": None,
        "max_length": 200,
        "required": True,
        "on_violation": "reject",
        "description": "A proposed topic. Required wherever a topic is the subject.",
    },
    {
        "param": "limit",
        "kind": "int",
        "minimum": 1,
        "maximum": 50,
        "default": 5,
        "required": False,
        "on_violation": "clamp",
        "description": "How many ranked suggestions to consider.",
    },
    {
        "param": "page",
        "kind": "int",
        "minimum": 1,
        "maximum": 1000,
        "default": 1,
        "required": False,
        "on_violation": "clamp",
        "description": "1-based page index. A negative page silently means the last page upstream.",
    },
    {
        "param": "per_page",
        "kind": "int",
        "minimum": 1,
        "maximum": 100,
        "default": 5,
        "required": False,
        "on_violation": "clamp",
        "description": "Rows per page. The one number that decides payload size.",
    },
    {
        "param": "theme",
        "kind": "str",
        "default": None,
        "max_length": 64,
        "required": False,
        "on_violation": "clamp",
        "description": "Restrict to a theme group.",
    },
    {
        "param": "sector",
        "kind": "str",
        "default": None,
        "max_length": 64,
        "required": False,
        "on_violation": "clamp",
        "description": "Restrict to a sector group.",
    },
    {
        "param": "window",
        "kind": "int",
        "minimum": 1,
        "maximum": 365,
        "default": None,
        "required": False,
        "on_violation": "clamp",
        "description": "Slice count, not days. Negative values slice from the wrong end.",
    },
    {
        "param": "policy",
        "kind": "str",
        "default": None,
        "max_length": 64,
        "required": False,
        "on_violation": "clamp",
        "description": "Named lifecycle policy id.",
    },
    {
        "param": "source",
        "kind": "str",
        "default": None,
        "max_length": 40,
        "required": False,
        "on_violation": "reject",
        "allowed": TOPIC_SOURCE_NAMES,
        "description": "Where the topic came from. Must be a configured source policy.",
    },
    {
        "param": "rationale",
        "kind": "str",
        "default": "",
        "max_length": 500,
        "required": False,
        "on_violation": "reject",
        "description": "Why this topic. Long enough for a justification, short enough to store.",
    },
    {
        "param": "confidence",
        "kind": "float",
        "minimum": 0.0,
        "maximum": 1.0,
        "default": 0.0,
        "required": False,
        "on_violation": "clamp",
        "description": "Caller's confidence in the proposed topic.",
    },
    {
        "param": "strict_filters",
        "kind": "bool",
        "default": False,
        "required": False,
        "on_violation": "reject",
        "description": "Treat theme/sector as hard filters rather than preferences.",
    },
    {
        "param": "route_id",
        "kind": "str",
        "default": None,
        "max_length": 64,
        "required": True,
        "on_violation": "reject",
        "description": "Which route to plan.",
    },
    {
        "param": "method",
        "kind": "str",
        "default": None,
        "max_length": 8,
        "required": False,
        "on_violation": "clamp",
        "description": "Override the route's declared method when planning.",
    },
    {
        "param": "consume",
        "kind": "bool",
        "default": False,
        "required": False,
        "on_violation": "reject",
        "description": "Planning a request should not spend the caller's rate budget.",
    },
]

TOPIC_PARAM_BOUND_BY_NAME = {str(row["param"]): row for row in TOPIC_PARAM_BOUNDS}
TOPIC_PARAM_NAMES = tuple(sorted(TOPIC_PARAM_BOUND_BY_NAME))

# Caps on list-valued response fields, per route. ``overflow`` says what happens
# when a list would exceed its cap: ``truncate`` cuts it and says so,
# ``report_only`` means the cap is advisory and the payload is left alone (used
# where the list is a fixed-size hand-maintained table, so the cap is a tripwire
# rather than a rule).
TOPIC_RESPONSE_LIMITS: list[dict[str, object]] = [
    {"route_id": "read_topic_search", "field": "items", "cap": 100, "overflow": "truncate", "description": "Ranked results on the page."},
    {"route_id": "read_topic_search", "field": "topic_focus", "cap": 20, "overflow": "truncate", "description": "Focus topics."},
    {"route_id": "read_topic_suggestions", "field": "suggested_topics", "cap": 100, "overflow": "truncate", "description": "Ranked suggestions."},
    {"route_id": "read_topic_suggestions", "field": "topic_focus", "cap": 20, "overflow": "truncate", "description": "Focus topics."},
    {"route_id": "read_ranked_topics", "field": "items", "cap": 200, "overflow": "truncate", "description": "Raw ranked list."},
    {"route_id": "read_topic_overview", "field": "suggested_topics", "cap": 50, "overflow": "truncate", "description": "Suggestions folded into the overview."},
    {"route_id": "read_topic_workspace", "field": "selections", "cap": 200, "overflow": "truncate", "description": "Selections in the workspace."},
    {"route_id": "read_topic_selection_history", "field": "entries", "cap": 500, "overflow": "truncate", "description": "History rows."},
    {"route_id": "read_topic_drift", "field": "sequence", "cap": 200, "overflow": "truncate", "description": "Distinct-topic sequence."},
    {"route_id": "read_topic_lifecycle", "field": "transitions", "cap": 200, "overflow": "truncate", "description": "Lifecycle transitions."},
    {"route_id": "read_topic_taxonomy_integrity", "field": "findings", "cap": 300, "overflow": "report_only", "description": "Findings over hand-maintained tables."},
    {"route_id": "read_topic_taxonomy", "field": "groups", "cap": 300, "overflow": "report_only", "description": "Taxonomy groups."},
    {"route_id": "read_topic_route_policies", "field": "policies", "cap": 100, "overflow": "report_only", "description": "One row per route."},
    {"route_id": "read_topic_param_bounds", "field": "bounds", "cap": 50, "overflow": "report_only", "description": "One row per parameter."},
    {"route_id": "read_topic_response_limits", "field": "limits", "cap": 100, "overflow": "report_only", "description": "One row per capped field."},
    {"route_id": "read_topic_capacity", "field": "entries", "cap": 100, "overflow": "report_only", "description": "One row per route."},
    {"route_id": "read_topic_request_analytics", "field": "recent", "cap": 200, "overflow": "truncate", "description": "Recent request samples."},
    {"route_id": "plan_topic_request", "field": "caps", "cap": 50, "overflow": "report_only", "description": "Caps that would apply."},
]

TOPIC_RESPONSE_LIMITS_BY_ROUTE: dict[str, list[dict[str, object]]] = {}
for _row in TOPIC_RESPONSE_LIMITS:
    TOPIC_RESPONSE_LIMITS_BY_ROUTE.setdefault(str(_row["route_id"]), []).append(_row)
del _row

# Cache classes. ``private`` is the one that matters: a user-scoped response
# must never land in a shared cache, so anything with a credential in
# ``varies_on`` is ``no_store`` with a private marker.
TOPIC_CACHE_POLICIES: list[dict[str, object]] = [
    {
        "cache_class": "no_store",
        "max_age": 0,
        "stale_while_revalidate": 0,
        "private": True,
        "varies_on": ("Authorization", "X-API-Key", "X-Correlation-ID", "Accept-Language"),
        "description": "Per-caller data. Not cacheable, not shareable.",
    },
    {
        "cache_class": "catalog_stable",
        "max_age": 300,
        "stale_while_revalidate": 300,
        "private": False,
        "varies_on": ("Accept-Language",),
        "description": "Static config tables. Long TTL, safe to share.",
    },
    {
        "cache_class": "catalog_short",
        "max_age": 30,
        "stale_while_revalidate": 30,
        "private": False,
        "varies_on": ("Accept-Language",),
        "description": "Ranked output that depends on the query. Short TTL.",
    },
    {
        "cache_class": "governance_short",
        "max_age": 15,
        "stale_while_revalidate": 15,
        "private": False,
        "varies_on": (),
        "description": "Config introspection. Short TTL so a table edit shows up.",
    },
]

TOPIC_CACHE_POLICY_BY_CLASS = {str(row["cache_class"]): row for row in TOPIC_CACHE_POLICIES}
TOPIC_CACHE_CLASSES = tuple(sorted(TOPIC_CACHE_POLICY_BY_CLASS))

# Rate-limit tiers. A route's tier is named explicitly in the route table
# rather than derived from scope, because the right limit follows the cost of
# the work, not the label on the route.
TOPIC_RATE_TIERS: list[dict[str, object]] = [
    {
        "tier": "catalog",
        "capacity": 600,
        "refill_per_second": 10.0,
        "applies_to": (TOPIC_SCOPE_CATALOG,),
        "description": "Static-table reads.",
    },
    {
        "tier": "search",
        "capacity": 120,
        "refill_per_second": 2.0,
        "applies_to": (TOPIC_SCOPE_CATALOG,),
        "description": "Ranking routes that scan the catalog.",
    },
    {
        "tier": "read",
        "capacity": 240,
        "refill_per_second": 4.0,
        "applies_to": (TOPIC_SCOPE_USER, TOPIC_SCOPE_ADVISORY),
        "description": "Per-caller reads against the database.",
    },
    {
        "tier": "write",
        "capacity": 30,
        "refill_per_second": 0.5,
        "applies_to": (TOPIC_SCOPE_USER_WRITE,),
        "description": "Mutations. Deliberately tight: these commit.",
    },
    {
        "tier": "governance",
        "capacity": 60,
        "refill_per_second": 1.0,
        "applies_to": (TOPIC_SCOPE_GOVERNANCE,),
        "description": "Config introspection and the offline planner.",
    },
]

TOPIC_RATE_TIER_BY_NAME = {str(row["tier"]): row for row in TOPIC_RATE_TIERS}
TOPIC_RATE_TIER_NAMES = tuple(sorted(TOPIC_RATE_TIER_BY_NAME))

# Cost model per route. The ``*`` row is the fallback so a newly added route is
# cheap to govern; ``build_topic_route_drift_report`` reports which routes are
# living on the default.
TOPIC_CAPACITY_RULES: list[dict[str, object]] = [
    {"route_id": "*", "slo_tier": "standard", "latency_budget_ms": 1000, "db_queries": 1, "catalog_scans": 0, "cacheable": True, "note": "Fallback for any route without an explicit cost row."},
    {"route_id": "read_topic_catalog", "slo_tier": "realtime", "latency_budget_ms": 150, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Static table."},
    {"route_id": "read_topic_themes", "slo_tier": "realtime", "latency_budget_ms": 150, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Static table."},
    {"route_id": "read_topic_taxonomy", "slo_tier": "realtime", "latency_budget_ms": 150, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Static table."},
    {"route_id": "read_topic_governance", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Config dump."},
    {"route_id": "read_topic_match", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Classification only."},
    {"route_id": "read_topic_taxonomy_integrity", "slo_tier": "fast", "latency_budget_ms": 400, "db_queries": 0, "catalog_scans": 3, "cacheable": True, "note": "Walks the taxonomy tables."},
    {"route_id": "read_topic_intelligence", "slo_tier": "standard", "latency_budget_ms": 800, "db_queries": 1, "catalog_scans": 2, "cacheable": False, "note": "One selection fetch."},
    {"route_id": "read_topic_coverage", "slo_tier": "standard", "latency_budget_ms": 800, "db_queries": 1, "catalog_scans": 2, "cacheable": False, "note": "One selection fetch."},
    {"route_id": "read_topic_portfolio", "slo_tier": "standard", "latency_budget_ms": 800, "db_queries": 1, "catalog_scans": 2, "cacheable": False, "note": "One selection fetch."},
    {"route_id": "read_topic_recommendations", "slo_tier": "standard", "latency_budget_ms": 900, "db_queries": 1, "catalog_scans": 3, "cacheable": False, "note": "One selection fetch."},
    {"route_id": "read_current_topic_selection", "slo_tier": "realtime", "latency_budget_ms": 250, "db_queries": 1, "catalog_scans": 0, "cacheable": False, "note": "One row by primary key."},
    {"route_id": "read_topic_selection_history", "slo_tier": "fast", "latency_budget_ms": 500, "db_queries": 1, "catalog_scans": 0, "cacheable": False, "note": "Unbounded history for one user."},
    {"route_id": "read_topic_workspace", "slo_tier": "standard", "latency_budget_ms": 900, "db_queries": 2, "catalog_scans": 2, "cacheable": False, "note": "Selection plus history."},
    {"route_id": "read_topic_overview", "slo_tier": "bulk", "latency_budget_ms": 1500, "db_queries": 2, "catalog_scans": 4, "cacheable": False, "note": "Workspace plus a nested search."},
    {"route_id": "read_topic_suggestions", "slo_tier": "fast", "latency_budget_ms": 500, "db_queries": 0, "catalog_scans": 2, "cacheable": True, "note": "Ranks the catalog."},
    {"route_id": "read_ranked_topics", "slo_tier": "fast", "latency_budget_ms": 500, "db_queries": 0, "catalog_scans": 2, "cacheable": True, "note": "Ranks the catalog."},
    {"route_id": "read_topic_search", "slo_tier": "bulk", "latency_budget_ms": 1200, "db_queries": 0, "catalog_scans": 3, "cacheable": True, "note": "Ranks, then paginates. The heaviest route."},
    {"route_id": "read_topic_drift", "slo_tier": "fast", "latency_budget_ms": 500, "db_queries": 1, "catalog_scans": 0, "cacheable": False, "note": "History window."},
    {"route_id": "read_topic_lifecycle", "slo_tier": "fast", "latency_budget_ms": 500, "db_queries": 1, "catalog_scans": 0, "cacheable": False, "note": "History window."},
    {"route_id": "validate_topic_selection", "slo_tier": "fast", "latency_budget_ms": 400, "db_queries": 1, "catalog_scans": 2, "cacheable": False, "note": "Reads the current selection, writes nothing."},
    {"route_id": "create_current_topic_selection", "slo_tier": "standard", "latency_budget_ms": 1000, "db_queries": 2, "catalog_scans": 2, "cacheable": False, "note": "Insert plus guard evaluation."},
    {"route_id": "replace_current_topic_selection", "slo_tier": "standard", "latency_budget_ms": 1000, "db_queries": 2, "catalog_scans": 2, "cacheable": False, "note": "Update plus guard evaluation."},
    {"route_id": "archive_current_topic_selection", "slo_tier": "standard", "latency_budget_ms": 1000, "db_queries": 2, "catalog_scans": 1, "cacheable": False, "note": "Update."},
    {"route_id": "read_topic_route_policies", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Table read."},
    {"route_id": "read_topic_param_bounds", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Table read."},
    {"route_id": "read_topic_response_limits", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Table read."},
    {"route_id": "read_topic_capacity", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Table read."},
    {"route_id": "read_topic_policy_validation", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Table cross-check."},
    {"route_id": "read_topic_route_drift", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 1, "cacheable": True, "note": "Router walk."},
    {"route_id": "read_topic_request_analytics", "slo_tier": "realtime", "latency_budget_ms": 200, "db_queries": 0, "catalog_scans": 0, "cacheable": False, "note": "In-process ring."},
    {"route_id": "plan_topic_request", "slo_tier": "realtime", "latency_budget_ms": 250, "db_queries": 0, "catalog_scans": 1, "cacheable": False, "note": "Pure computation."},
]

TOPIC_CAPACITY_RULE_BY_ROUTE = {str(row["route_id"]): row for row in TOPIC_CAPACITY_RULES}
TOPIC_CAPACITY_DEFAULT_ROW = TOPIC_CAPACITY_RULE_BY_ROUTE["*"]
TOPIC_SLO_TIERS: dict[str, int] = {
    "realtime": 150,
    "fast": 500,
    "standard": 1000,
    "bulk": 3000,
}

# The failures this surface can report, as a table so a client and the router
# agree on one status code per condition instead of each inventing one. The
# first four mirror the status codes the existing routes already return, which
# is why they are here at all: the planner can now predict a 404/422 the route
# would have produced anyway.
TOPIC_ERROR_MAP: list[dict[str, object]] = [
    {"condition": "unknown_route", "status_code": 404, "code": "unknown_topic_route", "detail": "No route policy is declared for this route_id.", "retryable": False},
    {"condition": "param_out_of_range", "status_code": 422, "code": "topic_param_out_of_range", "detail": "A parameter is outside its declared bounds and is configured to reject.", "retryable": False},
    {"condition": "param_too_long", "status_code": 422, "code": "topic_param_too_long", "detail": "A text parameter exceeded its declared maximum length.", "retryable": False},
    {"condition": "wrong_type", "status_code": 422, "code": "topic_param_wrong_type", "detail": "A parameter could not be read as its declared kind.", "retryable": False},
    {"condition": "not_allowed", "status_code": 422, "code": "topic_param_not_allowed", "detail": "A parameter is outside its declared allowed set.", "retryable": False},
    {"condition": "missing_required", "status_code": 422, "code": "topic_param_missing", "detail": "A required parameter was not supplied.", "retryable": False},
    {"condition": "cap_exceeded", "status_code": 422, "code": "topic_response_cap_exceeded", "detail": "A response list exceeded a cap configured to reject.", "retryable": False},
    {"condition": "rate_limited", "status_code": 429, "code": "topic_rate_limited", "detail": "The caller's tier bucket is empty.", "retryable": True},
    {"condition": "missing_selection", "status_code": 404, "code": "topic_selection_not_found", "detail": "The caller has no current selection. Mirrors GET/DELETE /topics/current.", "retryable": False},
    {"condition": "invalid_selection", "status_code": 422, "code": "topic_selection_invalid", "detail": "The service rejected the selection. Mirrors the ValueError path on POST/PUT.", "retryable": False},
    {"condition": "cross_tenant", "status_code": 403, "code": "topic_tenant_mismatch", "detail": "The caller is not bound to the requested tenant.", "retryable": False},
]

TOPIC_ERROR_BY_CONDITION = {str(row["condition"]): row for row in TOPIC_ERROR_MAP}

# Violation reason -> error-map condition. Kept explicit so a new reason cannot
# quietly fall through to a generic 500.
TOPIC_VIOLATION_CONDITION: dict[str, str] = {
    "out_of_range": "param_out_of_range",
    "too_long": "param_too_long",
    "wrong_type": "wrong_type",
    "not_allowed": "not_allowed",
    "empty": "missing_required",
}


def topic_error_for(reason: str) -> dict[str, object]:
    """The status/code a violation reason maps to. Falls back to 422."""
    condition = TOPIC_VIOLATION_CONDITION.get(reason, "param_out_of_range")
    return dict(TOPIC_ERROR_BY_CONDITION[condition])


# --- Parameter resolution ------------------------------------------------------
#
# ``resolve_topic_params`` is the only place a caller-supplied value becomes a
# number or a string. It is pure, it never raises, and it returns both the
# resolved value and a record of what it had to change.


def _coerce_number(spec: dict[str, object], value: object) -> tuple[float | None, str | None]:
    """Read ``value`` as the declared numeric kind.

    ``int(3.7)`` is 3 in Python and raises nothing, so a naive cast would
    silently truncate a fractional number and report no coercion at all. Every
    lossy path here returns ``coerced_type`` so the caller is told.
    """
    kind = str(spec.get("kind"))
    # ``bool`` is an ``int`` subclass; treating True as 1 would let a
    # `?limit=true` through as a legitimate page size.
    if isinstance(value, bool):
        return None, "wrong_type"
    if kind == "float":
        if isinstance(value, (int, float)):
            return float(value), None
        try:
            return float(str(value).strip()), None
        except (TypeError, ValueError):
            return None, "wrong_type"
    # kind == "int"
    if isinstance(value, int):
        return float(value), None
    if isinstance(value, float):
        if value.is_integer():
            return value, None
        return float(int(value)), "coerced_type"
    text = str(value).strip()
    try:
        return float(int(text)), None
    except (TypeError, ValueError):
        pass
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None, "wrong_type"
    if number.is_integer():
        return number, None
    return float(int(number)), "coerced_type"


_TRUTHY = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSY = frozenset({"0", "false", "no", "off", "f", "n", ""})


def coerce_topic_param(
    spec: dict[str, object], value: object
) -> tuple[object, list[dict[str, object]], list[dict[str, object]]]:
    """Resolve one parameter against its declared bound.

    Returns ``(value, coercions, violations)``. A violation means the value
    could not be repaired under the row's ``on_violation`` setting; a coercion
    means it could, and says what changed.
    """
    coercions: list[dict[str, object]] = []
    violations: list[dict[str, object]] = []
    name = str(spec.get("param"))
    kind = str(spec.get("kind") or "str")
    reject = str(spec.get("on_violation") or "clamp") == "reject"
    default = spec.get("default")

    def _violation(reason: str, bound: str, detail: str) -> None:
        violations.append(
            {
                "param": name,
                "supplied": value if isinstance(value, (str, int, float, bool, type(None))) else str(value),
                "reason": reason,
                "bound": bound,
                "detail": detail,
            }
        )

    supplied = value
    absent = value is None or (isinstance(value, str) and not value.strip())
    if absent:
        if spec.get("required"):
            _violation("empty", "required", f"'{name}' is required and was not supplied.")
            return None, coercions, violations
        if default is not None and default != value:
            coercions.append(
                {
                    "param": name,
                    "supplied": value,
                    "resolved": default,
                    "rule": "defaulted",
                    "reason": "no usable value supplied",
                }
            )
        return default, coercions, violations

    if kind in ("int", "float"):
        number, type_problem = _coerce_number(spec, value)

        # Coercion records carry the *converted* value, not the raw bound. A
        # report that says ``per_page 5000 -> 100.0`` for an int parameter is
        # describing a float the caller never receives.
        def _as_kind(number_value: float) -> object:
            return int(number_value) if kind == "int" else number_value

        # Only ``number is None`` is a failure. ``type_problem == "coerced_type"``
        # is a *lossy but usable* read, and treating it as unreadable would fall
        # back to the default — turning ``limit=3.7`` into 5 rather than 3.
        if number is None:
            if reject:
                _violation("wrong_type", f"kind={kind}", f"'{name}' must be a {kind}.")
                return None, coercions, violations
            coercions.append(
                {
                    "param": name,
                    "supplied": value,
                    "resolved": default,
                    "rule": "coerced_type",
                    "reason": f"unreadable as {kind}; fell back to the default",
                }
            )
            return default, coercions, violations
        if type_problem == "coerced_type":
            coercions.append(
                {
                    "param": name,
                    "supplied": value,
                    "resolved": _as_kind(number),
                    "rule": "coerced_type",
                    "reason": "fractional value truncated toward zero",
                }
            )
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and number < float(minimum):
            if reject:
                _violation("out_of_range", "minimum", f"'{name}' is below {minimum}.")
                return None, coercions, violations
            number = float(minimum)
            coercions.append(
                {
                    "param": name,
                    "supplied": supplied,
                    "resolved": _as_kind(number),
                    "rule": "clamped",
                    "reason": f"below the declared minimum of {minimum}",
                }
            )
        if maximum is not None and number > float(maximum):
            if reject:
                _violation("out_of_range", "maximum", f"'{name}' is above {maximum}.")
                return None, coercions, violations
            number = float(maximum)
            coercions.append(
                {
                    "param": name,
                    "supplied": supplied,
                    "resolved": _as_kind(number),
                    "rule": "clamped",
                    "reason": f"above the declared maximum of {maximum}",
                }
            )
        allowed = spec.get("allowed")
        if allowed:
            candidate = int(number) if kind == "int" else number
            if candidate not in allowed:
                if reject:
                    _violation("not_allowed", "allowed", f"'{name}' is not one of {sorted(allowed)}.")
                    return None, coercions, violations
                coercions.append(
                    {
                        "param": name,
                        "supplied": supplied,
                        "resolved": default,
                        "rule": "defaulted",
                        "reason": "outside the allowed set",
                    }
                )
                return default, coercions, violations
        return (int(number) if kind == "int" else number), coercions, violations

    if kind == "bool":
        if isinstance(value, bool):
            return value, coercions, violations
        text = str(value).strip().lower()
        if text in _TRUTHY:
            return True, coercions, violations
        if text in _FALSY:
            return False, coercions, violations
        if reject:
            _violation("wrong_type", "kind=bool", f"'{name}' must be a boolean.")
            return None, coercions, violations
        coercions.append(
            {
                "param": name,
                "supplied": value,
                "resolved": bool(default),
                "rule": "coerced_type",
                "reason": "unreadable as a boolean; fell back to the default",
            }
        )
        return bool(default), coercions, violations

    text = str(value)
    max_length = spec.get("max_length")
    if max_length is not None and len(text) > int(max_length):
        if reject:
            _violation("too_long", f"max_length={max_length}", f"'{name}' is longer than {max_length} characters.")
            return None, coercions, violations
        coercions.append(
            {
                "param": name,
                "supplied": value,
                "resolved": text[: int(max_length)],
                "rule": "truncated",
                "reason": f"longer than the declared max_length of {max_length}",
            }
        )
        text = text[: int(max_length)]
    allowed = spec.get("allowed")
    if allowed and text not in allowed:
        if reject:
            _violation("not_allowed", "allowed", f"'{name}' is not one of {sorted(allowed)}.")
            return None, coercions, violations
        coercions.append(
            {
                "param": name,
                "supplied": value,
                "resolved": default,
                "rule": "defaulted",
                "reason": "outside the allowed set",
            }
        )
        return default, coercions, violations
    return text, coercions, violations


def resolve_topic_params(
    route_id: str, params: dict[str, object] | None = None
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]], list[str]]:
    """Resolve every parameter a route declares.

    Returns ``(resolved, coercions, violations, unused)``. ``unused`` names the
    supplied parameters the route does not declare — reported, never rejected,
    because FastAPI already ignores them and a caller sending a spare param
    should not get a 422 for it.
    """
    supplied = {str(key): value for key, value in (params or {}).items()}
    policy = TOPIC_ROUTE_POLICY_BY_ID.get(route_id)
    if policy is None:
        return {}, [], [], sorted(supplied)
    declared = tuple(str(name) for name in (policy.get("params") or ()))
    resolved: dict[str, object] = {}
    coercions: list[dict[str, object]] = []
    violations: list[dict[str, object]] = []
    for name in declared:
        spec = TOPIC_PARAM_BOUND_BY_NAME.get(name)
        if spec is None:
            violations.append(
                {
                    "param": name,
                    "supplied": supplied.get(name),
                    "reason": "wrong_type",
                    "bound": "undeclared",
                    "detail": f"route '{route_id}' declares parameter '{name}', which has no bounds row.",
                }
            )
            continue
        value, row_coercions, row_violations = coerce_topic_param(spec, supplied.get(name))
        coercions.extend(row_coercions)
        violations.extend(row_violations)
        if not row_violations:
            resolved[name] = value
    unused = sorted(set(supplied) - set(declared))
    return resolved, coercions, violations, unused


def resolve_topic_page(
    page: object = None, per_page: object = None, total: object = None
) -> dict[str, object]:
    """The page arithmetic, stated once instead of inlined per route.

    ``total=None`` means the total is not known yet (the caller has not run the
    query), so the window bounds are reported as ``None`` rather than guessed.
    """
    page_number = max(1, int(page or 1))
    size = max(1, int(per_page or 1))
    offset = (page_number - 1) * size
    if total is None:
        return {
            "page": page_number,
            "per_page": size,
            "total": None,
            "total_pages": None,
            "offset": offset,
            "window_start": None,
            "window_end": None,
            "beyond_end": False,
            "empty_page": False,
        }
    total_rows = max(0, int(total))
    total_pages = max(1, (total_rows + size - 1) // size)
    return {
        "page": page_number,
        "per_page": size,
        "total": total_rows,
        "total_pages": total_pages,
        "offset": offset,
        "window_start": offset + 1 if total_rows else 0,
        "window_end": min(offset + size, total_rows),
        "beyond_end": bool(total_rows) and offset >= total_rows,
        "empty_page": total_rows > 0 and offset >= total_rows,
    }


# --- Admission -----------------------------------------------------------------


class TopicRateLimiter:
    """Token bucket over the configured tiers, keyed by ``(tier, caller)``.

    Bounded on purpose: the keyspace is caller-supplied, so an unbounded map is
    a memory leak that a single client can cause. When the key count reaches
    ``max_keys`` the least-recently-seen bucket is evicted, which trades a
    little accuracy for a bounded footprint — the right trade for a limiter
    whose job is to shed load, not to bill for it.
    """

    def __init__(self, tiers: dict[str, dict[str, object]] | None = None, *, max_keys: int = 4096) -> None:
        self._tiers = tiers if tiers is not None else TOPIC_RATE_TIER_BY_NAME
        self._max_keys = max(int(max_keys), 1)
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self.evictions = 0

    def resolved_tier(self, tier: str) -> str:
        """The tier whose numbers will actually be used.

        Reporting the *requested* tier while applying the fallback's numbers
        would make a misconfigured tier look correctly configured.
        """
        if tier in self._tiers:
            return str(tier)
        return str("read" if "read" in self._tiers else next(iter(self._tiers), ""))

    def bucket(self, tier: str, caller: str) -> tuple[int, float]:
        spec = self._tiers.get(tier) or self._tiers.get(self.resolved_tier(tier)) or {}
        return int(spec.get("capacity", 1)), float(spec.get("refill_per_second", 0.0))

    def check(
        self, tier: str, caller: str, *, now: float | None = None, consume: bool = False
    ) -> dict[str, object]:
        """Report whether a caller may proceed. Never raises."""
        capacity, refill = self.bucket(tier, caller)
        applied = self.resolved_tier(tier)
        if refill <= 0.0:
            return {
                "allowed": False,
                "reason": "rate_limited",
                "tier": applied,
                "requested_tier": str(tier),
                "capacity": capacity,
                "remaining": 0,
                "retry_after_seconds": 0,
                "refill_per_second": refill,
                "consumed": False,
                "note": "tier refills at zero, so it is closed until configured otherwise",
            }
        moment = float(now) if now is not None else time.monotonic()
        key = (str(tier), str(caller))
        tokens, last = self._buckets.get(key, (float(capacity), moment))
        elapsed = max(moment - last, 0.0)
        tokens = min(float(capacity), tokens + elapsed * refill)
        allowed = tokens >= 1.0
        consumed = bool(allowed and consume)
        if consumed:
            tokens -= 1.0
        self._evict_if_needed(key)
        self._buckets[key] = (tokens, moment)
        retry_after = 0 if allowed else int(math.ceil((1.0 - tokens) / refill))
        return {
            "allowed": allowed,
            "reason": "ok" if allowed else "rate_limited",
            "tier": applied,
            "requested_tier": str(tier),
            "capacity": capacity,
            "remaining": max(0, int(tokens)),
            "retry_after_seconds": retry_after,
            "refill_per_second": refill,
            "consumed": consumed,
            "note": "",
        }

    def _evict_if_needed(self, incoming: tuple[str, str]) -> None:
        if len(self._buckets) < self._max_keys or incoming in self._buckets:
            return
        oldest = min(self._buckets.items(), key=lambda item: item[1][1])[0]
        self._buckets.pop(oldest, None)
        self.evictions += 1

    def reset(self, tier: str | None = None, caller: str | None = None) -> None:
        if tier is None and caller is None:
            self._buckets.clear()
            return
        for key in [k for k in self._buckets if (tier is None or k[0] == tier) and (caller is None or k[1] == caller)]:
            self._buckets.pop(key, None)

    def stats(self) -> dict[str, object]:
        by_tier: dict[str, int] = {}
        for tier, _caller in self._buckets:
            by_tier[tier] = by_tier.get(tier, 0) + 1
        return {
            "tracked_keys": len(self._buckets),
            "max_keys": self._max_keys,
            "evictions": self.evictions,
            "by_tier": by_tier,
            "tiers": sorted(self._tiers),
        }


TOPIC_RATE_LIMITER = TopicRateLimiter()
TOPIC_RATE_LIMITER_SPEC: dict[str, object] = {
    "max_keys": 4096,
    "keyed_by": ["rate_tier", "caller"],
    "eviction": "least-recently-seen",
    "consume_on_plan": False,
    "note": "Planning a request never spends budget; only a real pass through "
    "the route would, and those are not counted here.",
}


@dataclass(frozen=True, eq=False)
class TopicRequestContext:
    """The resolved shape of one topic request, before anything runs."""

    route_id: str
    known: bool
    method: str
    path: str
    auth: str
    scope: str
    cache: str
    rate_tier: str
    cost_weight: float
    max_rows: int
    mutating: bool
    declared_params: tuple[str, ...]
    params: MappingProxyType
    coercions: tuple[dict, ...]
    violations: tuple[dict, ...]
    unused: tuple[str, ...]
    page: dict
    capacity: dict
    caps: tuple[dict, ...]
    admission: dict

    def param(self, name: str, default: object = None) -> object:
        return self.params.get(name, default)

    @property
    def admitted(self) -> bool:
        return bool(self.admission.get("allowed"))

    def signature(self) -> tuple:
        """A comparable projection, for invariants and fuzz checks."""
        return (
            self.route_id,
            self.known,
            self.admission.get("reason"),
            self.admission.get("allowed"),
            tuple(sorted(self.params.items(), key=lambda item: item[0])),
            tuple((c["param"], c["rule"]) for c in self.coercions),
            tuple((v["param"], v["reason"]) for v in self.violations),
            self.page.get("page"),
            self.page.get("per_page"),
        )


def resolve_topic_request(
    route_id: str,
    params: dict[str, object] | None = None,
    *,
    caller: str = "anonymous",
    now: float | None = None,
    consume: bool = False,
    limiter: TopicRateLimiter | None = None,
    check_rate: bool = True,
) -> TopicRequestContext:
    """Resolve a request against the tables. Pure apart from the limiter.

    An unknown ``route_id`` is not an error here — it produces a context with
    ``known=False`` and a ``unknown_route`` admission, so the planner can answer
    for a route that does not exist instead of raising.
    """
    supplied = {str(key): value for key, value in (params or {}).items()}
    policy = TOPIC_ROUTE_POLICY_BY_ID.get(str(route_id))
    if policy is None:
        return TopicRequestContext(
            route_id=str(route_id),
            known=False,
            method="",
            path="",
            auth="",
            scope="",
            cache="",
            rate_tier="",
            cost_weight=0.0,
            max_rows=0,
            mutating=False,
            declared_params=(),
            params=MappingProxyType({}),
            coercions=(),
            violations=(),
            unused=tuple(sorted(supplied)),
            page=resolve_topic_page(None, None, None),
            capacity=dict(TOPIC_CAPACITY_DEFAULT_ROW),
            caps=(),
            admission={
                "allowed": False,
                "reason": "unknown_route",
                "rate_tier": "",
                "capacity": 0,
                "remaining": 0,
                "retry_after_seconds": 0,
                "violations": [],
                "detail": f"no route policy is declared for '{route_id}'",
            },
        )

    resolved, coercions, violations, unused = resolve_topic_params(str(route_id), supplied)
    page = resolve_topic_page(
        resolved.get("page"), resolved.get("per_page"), None
    )
    capacity = resolve_topic_capacity(str(route_id))
    caps = tuple(TOPIC_RESPONSE_LIMITS_BY_ROUTE.get(str(route_id), ()))
    context = TopicRequestContext(
        route_id=str(route_id),
        known=True,
        method=str(policy.get("method") or ""),
        path=str(policy.get("path") or ""),
        auth=str(policy.get("auth") or ""),
        scope=str(policy.get("scope") or ""),
        cache=str(policy.get("cache") or ""),
        rate_tier=str(policy.get("rate_tier") or ""),
        cost_weight=float(policy.get("cost_weight") or 0.0),
        max_rows=int(policy.get("max_rows") or 0),
        mutating=bool(policy.get("mutating")),
        declared_params=tuple(str(name) for name in (policy.get("params") or ())),
        params=MappingProxyType(dict(resolved)),
        coercions=tuple(coercions),
        violations=tuple(violations),
        unused=tuple(unused),
        page=page,
        capacity=capacity,
        caps=caps,
        admission={},
    )
    admission = admit_topic_request(
        context, caller=caller, now=now, consume=consume, limiter=limiter, check_rate=check_rate
    )
    return TopicRequestContext(**{**context.__dict__, "admission": admission})


def admit_topic_request(
    context: TopicRequestContext,
    *,
    caller: str = "anonymous",
    now: float | None = None,
    consume: bool = False,
    limiter: TopicRateLimiter | None = None,
    check_rate: bool = True,
) -> dict[str, object]:
    """Decide whether a resolved request may proceed.

    Order matters: an unparseable parameter is refused before the rate limiter
    is consulted, so a caller cannot spend another caller's budget by sending
    garbage, and a refused request reports *why* it was refused rather than
    blaming the rate limit.

    ``check_rate=False`` reports the parameter verdict only and touches no
    bucket. The in-request recorder uses it: a ring that updated a token bucket
    on every read would turn an observer into a load generator.
    """
    if not context.known:
        return dict(context.admission) if context.admission else {
            "allowed": False,
            "reason": "unknown_route",
            "rate_tier": "",
            "capacity": 0,
            "remaining": 0,
            "retry_after_seconds": 0,
            "violations": [],
            "detail": f"no route policy is declared for '{context.route_id}'",
        }
    if context.violations:
        first = context.violations[0]
        error = topic_error_for(str(first.get("reason")))
        return {
            "allowed": False,
            "reason": "param_out_of_range",
            "rate_tier": context.rate_tier,
            "capacity": 0,
            "remaining": 0,
            "retry_after_seconds": 0,
            "violations": list(context.violations),
            "detail": (
                f"{len(context.violations)} parameter(s) refused; first was "
                f"'{first.get('param')}' ({first.get('reason')}, {error['code']})."
            ),
        }
    if not check_rate:
        return {
            "allowed": True,
            "reason": "ok",
            "rate_tier": context.rate_tier,
            "capacity": 0,
            "remaining": 0,
            "retry_after_seconds": 0,
            "violations": [],
            "detail": "rate tier not evaluated",
        }
    engine = limiter if limiter is not None else TOPIC_RATE_LIMITER
    check = engine.check(context.rate_tier, caller, now=now, consume=consume)
    return {
        "allowed": bool(check["allowed"]),
        "reason": str(check["reason"]),
        "rate_tier": str(check["tier"]),
        "capacity": int(check["capacity"]),
        "remaining": int(check["remaining"]),
        "retry_after_seconds": int(check["retry_after_seconds"]),
        "violations": [],
        "detail": str(check["note"] or ""),
    }


def resolve_topic_capacity(route_id: str) -> dict[str, object]:
    """The cost row for a route, merged with its route policy."""
    policy = TOPIC_ROUTE_POLICY_BY_ID.get(route_id) or {}
    rule = TOPIC_CAPACITY_RULE_BY_ROUTE.get(route_id) or TOPIC_CAPACITY_DEFAULT_ROW
    cache_class = str(policy.get("cache") or "")
    cache_row = TOPIC_CACHE_POLICY_BY_CLASS.get(cache_class) or {}
    return {
        "route_id": route_id,
        "slo_tier": str(rule.get("slo_tier") or "standard"),
        "latency_budget_ms": int(rule.get("latency_budget_ms") or 1000),
        "db_queries": int(rule.get("db_queries") or 0),
        "catalog_scans": int(rule.get("catalog_scans") or 0),
        "cacheable": bool(rule.get("cacheable", True)) and int(cache_row.get("max_age", 0)) > 0,
        "rate_tier": str(policy.get("rate_tier") or ""),
        "cost_weight": float(policy.get("cost_weight") or 0.0),
        "cache": cache_class,
        "max_rows": int(policy.get("max_rows") or 0),
        "defaulted": route_id not in TOPIC_CAPACITY_RULE_BY_ROUTE,
        "note": str(rule.get("note") or ""),
    }


# --- Response shaping ----------------------------------------------------------


def cap_topic_response(
    route_id: str, payload: dict[str, object], *, limits: list[dict[str, object]] | None = None
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Apply a route's response caps to a payload. Returns a new dict.

    Only top-level list fields are capped, and only those the route declares.
    A field configured ``report_only`` is measured and left alone — the caller
    decides what to do with the measurement, which is the point of a tripwire.
    The input payload is never mutated.
    """
    rows = limits if limits is not None else TOPIC_RESPONSE_LIMITS_BY_ROUTE.get(str(route_id), [])
    if not rows or not isinstance(payload, dict):
        return (dict(payload) if isinstance(payload, dict) else payload), []
    shaped = dict(payload)
    truncations: list[dict[str, object]] = []
    for row in rows:
        field = str(row.get("field") or "")
        cap = int(row.get("cap") or 0)
        overflow = str(row.get("overflow") or "truncate")
        value = shaped.get(field)
        if not isinstance(value, list) or cap <= 0:
            continue
        original = len(value)
        if original <= cap:
            continue
        truncations.append(
            {
                "field": field,
                "original_length": original,
                "kept_length": cap if overflow == "truncate" else original,
                "dropped": original - cap if overflow == "truncate" else 0,
                "cap": cap,
                "overflow": overflow,
            }
        )
        if overflow == "truncate":
            shaped[field] = list(value[:cap])
    return shaped, truncations


def _cap_schema(row: dict[str, object]) -> "schemas.TopicResponseLimit":
    """One response-limit row as its schema.

    The table keys on ``route_id`` (a route may cap several fields); the schema
    calls the same thing ``report`` because that is what the row describes. The
    rename lives in this one function so the two views cannot disagree.
    """
    route_id = str(row.get("route_id") or "")
    return schemas.TopicResponseLimit(
        report=route_id,
        field=str(row.get("field") or ""),
        cap=int(row.get("cap") or 0),
        overflow=str(row.get("overflow") or "truncate"),
        routes=[route_id] if route_id else [],
        description=str(row.get("description") or ""),
    )


# --- Route / table reconciliation ----------------------------------------------


def observed_topic_routes() -> dict[str, dict[str, object]]:
    """The routes this module actually registers, read at call time.

    Walking ``router.routes`` rather than trusting the table is the whole point:
    a route added above without a policy row has to be *visible* as drift.
    """
    observed: dict[str, dict[str, object]] = {}
    for route in router.routes:
        methods = sorted(
            method
            for method in (getattr(route, "methods", None) or set())
            if method not in ("HEAD", "OPTIONS")
        )
        observed[str(route.name)] = {
            "route_id": str(route.name),
            "method": methods[0] if methods else "",
            "path": str(getattr(route, "path", "")),
            "handler": str(getattr(route, "name", "")),
            # The endpoint's own function name, which is what a *rename* of a
            # handler would change. ``route.name`` follows the function name
            # too, so comparing the two catches a table row kept under the old
            # identity after the handler was renamed.
            "endpoint": str(getattr(getattr(route, "endpoint", None), "__name__", "") or ""),
        }
    return observed


def build_topic_route_drift_report() -> schemas.TopicRouteDriftReport:
    """Where the route table and the router disagree."""
    observed = observed_topic_routes()
    undeclared = sorted(set(observed) - set(TOPIC_ROUTE_POLICY_BY_ID))
    missing = sorted(set(TOPIC_ROUTE_POLICY_BY_ID) - set(observed))
    method_mismatches: list[str] = []
    path_mismatches: list[str] = []
    handler_mismatches: list[str] = []
    for route_id, declared in sorted(TOPIC_ROUTE_POLICY_BY_ID.items()):
        actual = observed.get(route_id)
        if actual is None:
            continue
        if str(declared.get("method") or "") != str(actual["method"]):
            method_mismatches.append(
                f"{route_id}: table says {declared.get('method')}, router says {actual['method']}"
            )
        if str(declared.get("path") or "") != str(actual["path"]):
            path_mismatches.append(
                f"{route_id}: table says {declared.get('path')}, router says {actual['path']}"
            )
        endpoint = str(actual.get("endpoint") or "")
        if endpoint and endpoint != str(actual["handler"]):
            handler_mismatches.append(
                f"{route_id}: registered as {actual['handler']} but the handler is {endpoint}"
            )
    in_sync = not (undeclared or missing or method_mismatches or path_mismatches or handler_mismatches)
    return schemas.TopicRouteDriftReport(
        generated_at=_topic_now(),
        declared=len(TOPIC_ROUTE_POLICY_BY_ID),
        observed=len(observed),
        undeclared=undeclared,
        missing=missing,
        method_mismatches=method_mismatches,
        path_mismatches=path_mismatches,
        handler_mismatches=handler_mismatches,
        in_sync=in_sync,
        note=(
            "The table describes the router; it does not enforce it. Drift here "
            "means a route exists that no policy row describes."
        ),
    )


# --- Recorder ------------------------------------------------------------------


class TopicRequestRecorder:
    """A bounded ring of recent requests, for capacity review.

    Bounded because this is process memory in a request path. Over-capacity
    writes drop the oldest sample and *count* the drop rather than discarding
    it silently — a ring that quietly stops recording looks identical to a
    route nobody calls.
    """

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = max(int(capacity), 1)
        self._samples: deque[dict[str, object]] = deque(maxlen=self.capacity)
        self.recorded = 0
        self.admitted = 0
        self.refused = 0
        self.dropped = 0
        self.record_errors = 0

    def record(self, context: TopicRequestContext, *, observed_at: datetime | None = None) -> None:
        before = len(self._samples)
        self._samples.append(
            {
                "route_id": context.route_id,
                "method": context.method,
                "scope": context.scope,
                "rate_tier": context.rate_tier,
                "cost_weight": context.cost_weight,
                "admitted": context.admitted,
                "reason": str(context.admission.get("reason") or "ok"),
                # Defaults are not repairs: a request that simply omitted a
                # param did not need adjusting. Count only the coercions that
                # changed something the caller actually sent.
                "coercions": sum(
                    1 for item in context.coercions if item["rule"] != "defaulted"
                ),
                "violations": len(context.violations),
                "page": int(context.page.get("page") or 1),
                "per_page": int(context.page.get("per_page") or 1),
                "observed_at": observed_at or _topic_now(),
            }
        )
        if len(self._samples) == self.capacity and before == self.capacity:
            self.dropped += 1
        self.recorded += 1
        if context.admitted:
            self.admitted += 1
        else:
            self.refused += 1

    def samples(self, *, window: int | None = None) -> list[dict[str, object]]:
        rows = list(self._samples)
        if window:
            rows = rows[-max(1, int(window)) :]
        return rows

    def reset(self) -> None:
        self._samples.clear()
        self.recorded = 0
        self.admitted = 0
        self.refused = 0
        self.dropped = 0
        self.record_errors = 0

    def stats(self) -> dict[str, object]:
        by_route: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        for sample in self._samples:
            by_route[str(sample["route_id"])] = by_route.get(str(sample["route_id"]), 0) + 1
            by_reason[str(sample["reason"])] = by_reason.get(str(sample["reason"]), 0) + 1
            by_tier[str(sample["rate_tier"])] = by_tier.get(str(sample["rate_tier"]), 0) + 1
        return {
            "tracked": True,
            "capacity": self.capacity,
            "recorded": self.recorded,
            "admitted": self.admitted,
            "refused": self.refused,
            "dropped": self.dropped,
            "record_errors": self.record_errors,
            "retained": len(self._samples),
            "by_route": dict(sorted(by_route.items(), key=lambda item: (-item[1], item[0]))),
            "by_reason": by_reason,
            "by_tier": by_tier,
        }


TOPIC_REQUEST_RECORDER = TopicRequestRecorder()
TOPIC_REQUEST_RECORDER_SPEC: dict[str, object] = {
    "capacity": 200,
    "storage": "in-process ring",
    "records": "resolved request contexts, not payloads",
    "secrets": "credentials are hashed before they become a bucket key; a raw "
    "token is never stored",
}


def _caller_fingerprint(request: Request) -> str:
    """A stable, non-reversible bucket key for a request's caller.

    The API key and bearer token are hashed, never stored: a rate-limit bucket
    key is long-lived and ends up in dumps, and a dump of live credentials is
    a worse problem than a rate-limit reset.
    """
    material = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization")
        or ""
    ).strip()
    if not material:
        return "anonymous"
    return "fp_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _observe(request: Request) -> None:
    """Record one served request. Never raises, and never changes the response.

    Called from :class:`GovernedAPIRoute` *after* the real handler has produced
    its response, so a failure here cannot turn a 200 into a 500. The failure
    count is reported by the analytics surface instead of being swallowed.
    """
    try:
        params = {str(key): request.query_params.get(str(key)) for key in request.query_params.keys()}
        context = resolve_topic_request(
            str(request.scope.get("route") and request.scope["route"].name or ""),
            params,
            caller=_caller_fingerprint(request),
            consume=False,
            check_rate=False,
        )
        TOPIC_REQUEST_RECORDER.record(context)
    except Exception:  # pragma: no cover - defensive; counted, not hidden
        TOPIC_REQUEST_RECORDER.record_errors += 1


# NOTE: ``GovernedAPIRoute`` is declared once, immediately above ``router`` —
# the router binds that class object as its ``route_class`` at import time, so a
# second definition here would shadow the *name* while every registered route
# stayed an instance of the first. ``isinstance(route, GovernedAPIRoute)`` would
# then be False for all of them, which is exactly the kind of drift this layer
# exists to catch.


# --- Reports -------------------------------------------------------------------


def build_topic_route_policy_report() -> schemas.TopicRoutePolicyReport:
    """The route table, annotated with what the router actually registers."""
    observed = observed_topic_routes()
    policies: list[schemas.TopicRoutePolicy] = []
    undeclared: list[str] = []
    by_scope: dict[str, int] = {}
    by_cache: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for route_id in TOPIC_ROUTE_IDS:
        row = TOPIC_ROUTE_POLICY_BY_ID[route_id]
        actual = observed.get(route_id)
        scope = str(row.get("scope") or "")
        by_scope[scope] = by_scope.get(scope, 0) + 1
        by_cache[str(row.get("cache") or "")] = by_cache.get(str(row.get("cache") or ""), 0) + 1
        by_tier[str(row.get("rate_tier") or "")] = by_tier.get(str(row.get("rate_tier") or ""), 0) + 1
        if actual is None:
            undeclared.append(route_id)
        policies.append(
            schemas.TopicRoutePolicy(
                route_id=route_id,
                method=str(row.get("method") or ""),
                path=str(row.get("path") or ""),
                handler=str(observed.get(route_id, {}).get("handler") or route_id),
                auth=str(row.get("auth") or "none"),
                scope=scope,
                cache=str(row.get("cache") or ""),
                rate_tier=str(row.get("rate_tier") or ""),
                cost_weight=float(row.get("cost_weight") or 0.0),
                max_rows=int(row.get("max_rows") or 0),
                mutating=bool(row.get("mutating")),
                observed=actual is not None,
                params=[str(name) for name in (row.get("params") or ())],
                description=str(row.get("description") or ""),
            )
        )
    for route_id in sorted(observed):
        if route_id not in TOPIC_ROUTE_POLICY_BY_ID:
            undeclared.append(route_id)
    return schemas.TopicRoutePolicyReport(
        generated_at=_topic_now(),
        total=len(policies),
        observed=sum(1 for policy in policies if policy.observed),
        undeclared=sorted(undeclared),
        stale=sorted(route_id for route_id in undeclared if route_id in TOPIC_ROUTE_POLICY_BY_ID),
        policies=policies,
        by_scope=by_scope,
        by_cache=by_cache,
        by_tier=by_tier,
        note=(
            "Scopes: catalog is public static reads, user is per-caller reads, "
            "user_write commits, advisory writes nothing, governance introspects "
            "this layer."
        ),
    )


def build_topic_param_bounds_report() -> schemas.TopicParamBoundsReport:
    """Every parameter contract, with the routes that use it."""
    used_by: dict[str, list[str]] = {name: [] for name in TOPIC_PARAM_NAMES}
    for route_id, row in sorted(TOPIC_ROUTE_POLICY_BY_ID.items()):
        for name in row.get("params") or ():
            used_by.setdefault(str(name), []).append(route_id)
    bounds = [
        schemas.TopicParamBound(
            param=str(row["param"]),
            kind=str(row.get("kind") or "str"),
            minimum=row.get("minimum"),
            maximum=row.get("maximum"),
            default=row.get("default"),
            max_length=row.get("max_length"),
            required=bool(row.get("required")),
            on_violation=str(row.get("on_violation") or "clamp"),
            used_by=used_by.get(str(row["param"]), []),
            description=str(row.get("description") or ""),
        )
        for row in TOPIC_PARAM_BOUNDS
    ]
    return schemas.TopicParamBoundsReport(
        generated_at=_topic_now(),
        count=len(bounds),
        bounds=bounds,
        clamp_params=[str(row["param"]) for row in TOPIC_PARAM_BOUNDS if str(row.get("on_violation")) == "clamp"],
        reject_params=[str(row["param"]) for row in TOPIC_PARAM_BOUNDS if str(row.get("on_violation")) == "reject"],
        note=(
            "clamp repairs and reports; reject refuses. Numeric parameters clamp "
            "because 'as many as you have' is a preference, while enumerated and "
            "required parameters reject because a wrong value is a bug."
        ),
    )


def build_topic_response_limit_report() -> schemas.TopicResponseLimitReport:
    """Response caps and cache policies, with the routes each one covers."""
    routes_by_cache: dict[str, list[str]] = {}
    for route_id, row in sorted(TOPIC_ROUTE_POLICY_BY_ID.items()):
        routes_by_cache.setdefault(str(row.get("cache") or ""), []).append(route_id)
    limits = [_cap_schema(row) for row in TOPIC_RESPONSE_LIMITS]
    cache_policies = [
        schemas.TopicCachePolicy(
            cache_class=str(row["cache_class"]),
            max_age=int(row["max_age"]),
            stale_while_revalidate=int(row["stale_while_revalidate"]),
            private=bool(row["private"]),
            varies_on=[str(name) for name in (row.get("varies_on") or ())],
            routes=routes_by_cache.get(str(row["cache_class"]), []),
            description=str(row.get("description") or ""),
        )
        for row in TOPIC_CACHE_POLICIES
    ]
    return schemas.TopicResponseLimitReport(
        generated_at=_topic_now(),
        total=len(limits),
        limits=limits,
        cache_policies=cache_policies,
        max_cap=max((int(row["cap"]) for row in TOPIC_RESPONSE_LIMITS), default=0),
        total_capped_fields=sum(1 for row in TOPIC_RESPONSE_LIMITS if str(row.get("overflow")) == "truncate"),
        cache_classes=list(TOPIC_CACHE_CLASSES),
        note=(
            "truncate caps are enforced by cap_topic_response when a caller "
            "opts in; report_only caps are tripwires over hand-maintained tables."
        ),
    )


def build_topic_capacity_report() -> schemas.TopicCapacityReport:
    """Cost model per route, ordered by what it costs to serve."""
    entries: list[schemas.TopicCapacityEntry] = []
    by_slo_tier: dict[str, int] = {}
    total_db_queries = 0
    total_catalog_scans = 0
    aggregate_cost_weight = 0.0
    cacheable = 0
    for route_id in TOPIC_ROUTE_IDS:
        row = resolve_topic_capacity(route_id)
        by_slo_tier[str(row["slo_tier"])] = by_slo_tier.get(str(row["slo_tier"]), 0) + 1
        total_db_queries += int(row["db_queries"])
        total_catalog_scans += int(row["catalog_scans"])
        aggregate_cost_weight += float(row["cost_weight"])
        cacheable += 1 if row["cacheable"] else 0
        entries.append(
            schemas.TopicCapacityEntry(
                route_id=route_id,
                slo_tier=str(row["slo_tier"]),
                latency_budget_ms=int(row["latency_budget_ms"]),
                db_queries=int(row["db_queries"]),
                catalog_scans=int(row["catalog_scans"]),
                rate_tier=str(row["rate_tier"]),
                cost_weight=float(row["cost_weight"]),
                cache=str(row["cache"]),
                cacheable=bool(row["cacheable"]),
                max_rows=int(row["max_rows"]),
                note=str(row["note"]),
            )
        )
    entries.sort(key=lambda entry: (-entry.cost_weight, entry.route_id))
    heaviest = [entry.route_id for entry in entries[:5]]
    total = len(entries) or 1
    return schemas.TopicCapacityReport(
        generated_at=_topic_now(),
        total=len(entries),
        by_slo_tier=by_slo_tier,
        entries=entries,
        heaviest=heaviest,
        total_db_queries=total_db_queries,
        total_catalog_scans=total_catalog_scans,
        aggregate_cost_weight=round(aggregate_cost_weight, 2),
        cacheable_share=round(cacheable / total, 4),
        note=(
            "cost_weight is a relative ordering, not a latency measurement. "
            "The database work is the part that scales with traffic; the "
            "catalog scans are CPU and are the part a cache removes."
        ),
    )


def build_topic_request_analytics(*, window: int | None = None) -> schemas.TopicRequestAnalyticsReport:
    """What the live routes have actually been asked for."""
    stats = TOPIC_REQUEST_RECORDER.stats()
    rows = TOPIC_REQUEST_RECORDER.samples(window=window)
    coerced: dict[str, int] = {}
    refused: dict[str, int] = {}
    for row in rows:
        route_id = str(row["route_id"])
        if int(row["coercions"]):
            coerced[route_id] = coerced.get(route_id, 0) + int(row["coercions"])
        if int(row["violations"]):
            refused[route_id] = refused.get(route_id, 0) + int(row["violations"])
    return schemas.TopicRequestAnalyticsReport(
        generated_at=_topic_now(),
        tracked=True,
        capacity=int(stats["capacity"]),
        recorded=int(stats["recorded"]),
        admitted=int(stats["admitted"]),
        refused=int(stats["refused"]),
        dropped=int(stats["dropped"]),
        record_errors=int(TOPIC_REQUEST_RECORDER.record_errors),
        by_route=dict(stats["by_route"]),  # type: ignore[arg-type]
        by_reason=dict(stats["by_reason"]),  # type: ignore[arg-type]
        by_tier=dict(stats["by_tier"]),  # type: ignore[arg-type]
        coerced_params=dict(sorted(coerced.items())),
        refused_params=dict(sorted(refused.items())),
        heaviest_routes=[str(row["route_id"]) for row in rows[-5:]][::-1],
        recent=[schemas.TopicRequestSample(**row) for row in rows],  # type: ignore[misc]
        note=(
            "Counts are a bounded ring, not lifetime totals: 'recorded' and "
            "'retained' differ once the ring has wrapped, and 'dropped' says "
            "how many samples that cost."
        ),
    )


def build_topic_request_plan(
    route_id: str,
    params: dict[str, object] | None = None,
    *,
    caller: str = "planner",
    method: str | None = None,
) -> schemas.TopicRequestPlanReport:
    """What a request *would* do. Writes nothing and spends no rate budget.

    ``method`` is a deliberate mismatch check: planning ``POST`` against a
    route the table declares as ``GET`` is how a client discovers it called the
    wrong verb, and it is cheaper to learn that here than in a 405.
    """
    context = resolve_topic_request(
        route_id, params, caller=caller, consume=False, now=0.0
    )
    capacity = context.capacity or dict(TOPIC_CAPACITY_DEFAULT_ROW)
    resolved = dict(context.params)
    supplied = {str(key): value for key, value in (params or {}).items()}
    defaults_applied = [
        str(item["param"]) for item in context.coercions if item["rule"] == "defaulted"
    ]
    if not context.known:
        return schemas.TopicRequestPlanReport(
            generated_at=_topic_now(),
            route_id=route_id,
            known=False,
            supplied=supplied,
            unused=list(context.unused),
            admission=schemas.TopicAdmissionDecision(**context.admission),
            summary=(
                f"No route policy is declared for '{route_id}'. "
                f"{len(context.unused)} supplied parameter(s) could not be planned."
            ),
        )
    error = topic_error_for(str(context.violations[0]["reason"])) if context.violations else None
    parts = [
        f"Route {context.method} {context.path} ({context.route_id}) is {context.scope} / tier {context.rate_tier}.",
        f"{len(resolved)} parameter(s) resolved, {len(context.coercions)} coerced, {len(context.violations)} refused.",
    ]
    if context.coercions:
        parts.append(
            "Coerced: "
            + ", ".join(f"{item['param']} {item['supplied']}->{item['resolved']} ({item['rule']})" for item in context.coercions)
            + "."
        )
    if context.violations:
        parts.append(
            "Refused: "
            + ", ".join(f"{item['param']} ({item['reason']})" for item in context.violations)
            + (f" -> would be {error['status_code']} {error['code']}." if error else ".")
        )
    if method and context.method and str(method).strip().upper() != context.method:
        parts.append(
            f"Planned as {str(method).strip().upper()} but this route is {context.method}."
        )
    if context.unused:
        parts.append(f"Ignored {len(context.unused)} parameter(s) the route does not declare.")
    parts.append(
        f"Would serve {capacity['catalog_scans']} catalog scan(s) and {capacity['db_queries']} database query "
        f"against a {capacity['latency_budget_ms']}ms {capacity['slo_tier']} budget, "
        f"applying {len(context.caps)} response cap(s)."
    )
    parts.append(
        "Admitted."
        if context.admitted
        else f"Refused at admission ({context.admission.get('reason')})."
    )
    parts.append("Planning does not consume rate budget.")
    return schemas.TopicRequestPlanReport(
        generated_at=_topic_now(),
        route_id=context.route_id,
        known=True,
        method=context.method,
        path=context.path,
        auth=context.auth,
        scope=context.scope,
        cache=context.cache,
        rate_tier=context.rate_tier,
        cost_weight=context.cost_weight,
        max_rows=context.max_rows,
        supplied=supplied,
        resolved=resolved,
        unused=list(context.unused),
        defaults_applied=defaults_applied,
        coercions=[schemas.TopicParamCoercion(**item) for item in context.coercions],
        violations=[schemas.TopicParamViolation(**item) for item in context.violations],
        page=dict(context.page),
        admission=schemas.TopicAdmissionDecision(**context.admission),
        caps=[_cap_schema(row) for row in context.caps],
        db_queries=int(capacity.get("db_queries") or 0),
        latency_budget_ms=int(capacity.get("latency_budget_ms") or 0),
        slo_tier=str(capacity.get("slo_tier") or ""),
        summary=" ".join(parts),
    )


def validate_topic_request_policies() -> schemas.TopicPolicyValidationReport:
    """Check the governance tables against each other and against the router.

    Errors mean the tables cannot be trusted: a route exists with no policy, a
    policy names a tier that does not exist, a cap is non-positive. Warnings
    mean something is governable but ungoverned — living on the default cost
    row, or clamping a parameter on a route that commits.
    """
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for row in TOPIC_ROUTE_POLICIES:
        route_id = str(row["route_id"])
        if route_id in seen:
            errors.append(f"duplicate route_id in TOPIC_ROUTE_POLICIES: {route_id}")
        seen.add(route_id)
        if str(row.get("scope")) not in TOPIC_SCOPES:
            errors.append(f"{route_id}: unknown scope '{row.get('scope')}'")
        if str(row.get("rate_tier")) not in TOPIC_RATE_TIER_BY_NAME:
            errors.append(f"{route_id}: unknown rate tier '{row.get('rate_tier')}'")
        if str(row.get("cache")) not in TOPIC_CACHE_POLICY_BY_CLASS:
            errors.append(f"{route_id}: unknown cache class '{row.get('cache')}'")
        if int(row.get("max_rows") or 0) <= 0:
            warnings.append(f"{route_id}: max_rows is {row.get('max_rows')}; a row budget of 0 disables it")
        for name in row.get("params") or ():
            if str(name) not in TOPIC_PARAM_BOUND_BY_NAME:
                errors.append(f"{route_id}: parameter '{name}' has no row in TOPIC_PARAM_BOUNDS")
        tier = TOPIC_RATE_TIER_BY_NAME.get(str(row.get("rate_tier")))
        if tier and str(row.get("scope")) not in tuple(tier.get("applies_to") or ()):
            warnings.append(
                f"{route_id}: scope '{row.get('scope')}' is not in tier '{row.get('rate_tier')}' applies_to"
            )
        if row.get("mutating"):
            for name in row.get("params") or ():
                spec = TOPIC_PARAM_BOUND_BY_NAME.get(str(name))
                if spec and str(spec.get("on_violation")) == "clamp":
                    warnings.append(
                        f"{route_id}: mutating route clamps '{name}'; a silently "
                        f"adjusted write input is a data-quality bug"
                    )
    for row in TOPIC_RESPONSE_LIMITS:
        route_id = str(row.get("route_id"))
        if route_id not in TOPIC_ROUTE_POLICY_BY_ID:
            errors.append(f"response limit targets unknown route '{route_id}'")
        if int(row.get("cap") or 0) <= 0:
            errors.append(f"response limit {route_id}.{row.get('field')} has a non-positive cap")
        policy = TOPIC_ROUTE_POLICY_BY_ID.get(route_id) or {}
        if int(row.get("cap") or 0) > int(policy.get("max_rows") or 0) > 0:
            warnings.append(
                f"{route_id}.{row.get('field')}: cap {row.get('cap')} exceeds the route's max_rows {policy.get('max_rows')}"
            )
    for tier in TOPIC_RATE_TIERS:
        if int(tier.get("capacity") or 0) <= 0:
            errors.append(f"rate tier '{tier.get('tier')}' has a non-positive capacity")
        if float(tier.get("refill_per_second") or 0.0) < 0.0:
            errors.append(f"rate tier '{tier.get('tier')}' has a negative refill rate")
    for row in TOPIC_CAPACITY_RULES:
        if str(row.get("route_id")) != "*" and str(row.get("route_id")) not in TOPIC_ROUTE_POLICY_BY_ID:
            errors.append(f"capacity rule targets unknown route '{row.get('route_id')}'")
        if str(row.get("slo_tier")) not in TOPIC_SLO_TIERS:
            errors.append(f"capacity rule '{row.get('route_id')}' has unknown slo_tier '{row.get('slo_tier')}'")
    param_names = {str(row["param"]) for row in TOPIC_PARAM_BOUNDS}
    declared_params = {
        str(name) for row in TOPIC_ROUTE_POLICIES for name in (row.get("params") or ())
    }
    for orphan in sorted(param_names - declared_params):
        warnings.append(f"parameter '{orphan}' has bounds but no route declares it")
    for row in TOPIC_ROUTE_POLICIES:
        if str(row["route_id"]) not in TOPIC_CAPACITY_RULE_BY_ROUTE:
            warnings.append(f"{row['route_id']}: living on the default capacity row")
    drift = build_topic_route_drift_report()
    if drift.undeclared:
        errors.append(f"routes with no policy row: {', '.join(drift.undeclared)}")
    if drift.missing:
        errors.append(f"policy rows with no route: {', '.join(drift.missing)}")
    for mismatch in drift.method_mismatches + drift.path_mismatches + drift.handler_mismatches:
        errors.append(mismatch)
    return schemas.TopicPolicyValidationReport(
        generated_at=_topic_now(),
        errors=len(errors),
        warnings=len(warnings),
        error_list=errors,
        warning_list=warnings,
        routes=len(TOPIC_ROUTE_POLICIES),
        params=len(TOPIC_PARAM_BOUNDS),
        limits=len(TOPIC_RESPONSE_LIMITS),
        note=(
            "errors mean the tables disagree with each other or with the "
            "router; warnings mean something is governable but not yet governed."
        ),
    )


def simulate_topic_admission(
    scenarios: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """Compare each scenario's declared parameters against a variant.

    The *baseline* is resolved from the scenario's own ``params`` and the
    *candidate* from ``params`` merged with ``overrides`` — building the
    baseline from the overrides instead would make every scenario report
    ``changed: False``, which is the failure mode this function exists to avoid.
    """
    rows: list[dict[str, object]] = []
    for scenario in scenarios or []:
        route_id = str(scenario.get("route_id") or "")
        params = dict(scenario.get("params") or {})
        overrides = dict(scenario.get("overrides") or {})
        caller = str(scenario.get("caller") or "simulator")
        baseline = resolve_topic_request(route_id, params, caller=caller, now=0.0, consume=False)
        candidate_params = {**params, **overrides}
        candidate = resolve_topic_request(
            route_id, candidate_params, caller=caller, now=0.0, consume=False
        )
        # "Refused after change" has to mean the override *introduced* the
        # refusal. A scenario that was already refused at the baseline and
        # stayed refused is a pre-existing problem, not something the reviewer
        # just caused, and counting it would blame the override for it.
        became_refused = bool(
            baseline.admitted and not candidate.admitted
        )
        rows.append(
            {
                "route_id": route_id,
                "known": baseline.known,
                "changed": baseline.signature() != candidate.signature(),
                "baseline_admitted": baseline.admitted,
                "candidate_admitted": candidate.admitted,
                "became_refused": became_refused,
                "baseline_params": dict(baseline.params),
                "candidate_params": dict(candidate.params),
                "new_coercions": [
                    dict(item) for item in candidate.coercions
                    if item not in baseline.coercions
                ],
                "resolved_violations": [
                    dict(item) for item in candidate.violations
                    if item not in baseline.violations
                ],
                "baseline_reason": str(baseline.admission.get("reason") or ""),
                "candidate_reason": str(candidate.admission.get("reason") or ""),
            }
        )
    return {
        "generated_at": _topic_now(),
        "scenarios": len(rows),
        "changed": sum(1 for row in rows if row["changed"]),
        "refused_after_change": sum(1 for row in rows if row["became_refused"]),
        "results": rows,
        "note": (
            "Simulation never consumes rate budget, so a reviewer can run a "
            "thousand scenarios without locking themselves out."
        ),
    }


def build_topic_request_governance_catalog() -> dict[str, object]:
    """Introspectable contract for the request-governance layer."""
    validation = validate_topic_request_policies()
    return {
        "scopes": list(TOPIC_SCOPES),
        "route_count": len(TOPIC_ROUTE_POLICIES),
        "param_count": len(TOPIC_PARAM_BOUNDS),
        "limit_count": len(TOPIC_RESPONSE_LIMITS),
        "cache_classes": list(TOPIC_CACHE_CLASSES),
        "rate_tiers": [dict(row) for row in TOPIC_RATE_TIERS],
        "slo_tiers": dict(TOPIC_SLO_TIERS),
        "error_map": [dict(row) for row in TOPIC_ERROR_MAP],
        "violation_conditions": dict(TOPIC_VIOLATION_CONDITION),
        "rate_limiter": {**TOPIC_RATE_LIMITER.stats(), **TOPIC_RATE_LIMITER_SPEC},
        "recorder": {**TOPIC_REQUEST_RECORDER.stats(), **TOPIC_REQUEST_RECORDER_SPEC},
        "validation": {
            "errors": validation.errors,
            "warnings": validation.warnings,
        },
        "advisory": (
            "Nothing here gates a live route. The table describes the router, "
            "the planner answers 'what would happen', and the recorder reports "
            "what did; enforcement would be a separate, opt-in decision."
        ),
    }


# --- Governance routes ---------------------------------------------------------
#
# Deliberately unauthenticated, like `/topics/governance` and
# `/topics/integrity` above: these expose configuration, not caller data. The
# request analytics surface is the exception worth thinking about — it reports
# *traffic shape* (which routes are called, how often, with which coercion
# rates), not any caller's content, and it is aggregated over a process-local
# ring, so it identifies nobody.


@router.get("/governance/routes", response_model=schemas.TopicRoutePolicyReport)
async def read_topic_route_policies():
    return build_topic_route_policy_report()


@router.get("/governance/params", response_model=schemas.TopicParamBoundsReport)
async def read_topic_param_bounds():
    return build_topic_param_bounds_report()


@router.get("/governance/limits", response_model=schemas.TopicResponseLimitReport)
async def read_topic_response_limits():
    return build_topic_response_limit_report()


@router.get("/governance/capacity", response_model=schemas.TopicCapacityReport)
async def read_topic_capacity():
    return build_topic_capacity_report()


@router.get("/governance/requests", response_model=schemas.TopicRequestAnalyticsReport)
async def read_topic_request_analytics(window: int | None = None):
    return build_topic_request_analytics(window=window)


@router.get("/plan", response_model=schemas.TopicRequestPlanReport)
async def plan_topic_request(
    route_id: str,
    method: str | None = None,
    consume: bool = False,
    window: int | None = None,
    query: str | None = None,
    limit: int | None = None,
    page: int | None = None,
    per_page: int | None = None,
    theme: str | None = None,
    sector: str | None = None,
    topic: str | None = None,
    policy: str | None = None,
    strict_filters: bool | None = None,
    source: str | None = None,
    rationale: str | None = None,
    confidence: float | None = None,
):
    """Plan a request against the tables without sending it. Writes nothing.

    ``consume=False`` is the default and the reason this route is safe to call
    in a loop: planning does not spend the caller's rate budget, so reviewing a
    thousand scenarios cannot rate-limit the reviewer.
    """
    # Only forward what the caller actually sent. A parameter left out is not
    # the same as one supplied as null, and reporting absent parameters as
    # "ignored by the target route" would bury the ones that really are.
    supplied = {
        name: value
        for name, value in {
            "query": query,
            "limit": limit,
            "page": page,
            "per_page": per_page,
            "theme": theme,
            "sector": sector,
            "topic": topic,
            "policy": policy,
            "window": window,
            "strict_filters": strict_filters,
            "source": source,
            "rationale": rationale,
            "confidence": confidence,
        }.items()
        if value is not None
    }
    return build_topic_request_plan(route_id, supplied, method=method)


@router.get("/governance/validation", response_model=schemas.TopicPolicyValidationReport)
async def read_topic_policy_validation():
    return validate_topic_request_policies()


@router.get("/governance/drift", response_model=schemas.TopicRouteDriftReport)
async def read_topic_route_drift():
    return build_topic_route_drift_report()
