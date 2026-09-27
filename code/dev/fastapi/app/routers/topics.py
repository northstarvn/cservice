from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.schemas import schemas
from app.services.topics import (
    TOPIC_MATCH_MODES,
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

router = APIRouter(prefix="/topics", tags=["topics"])


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