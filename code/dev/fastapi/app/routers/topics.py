from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.schemas import schemas
from app.services.topics import (
    archive_topic_selection,
    build_topic_coverage_report,
    build_topic_catalog_report,
    build_topic_portfolio_report,
    build_topic_recommendation_report,
    build_topic_theme_report,
    build_topic_workspace_report,
    build_topic_selection_history_report,
    build_topic_selection_report,
    build_typed_topic_catalog_report,
    build_typed_topic_recommendation_report,
    build_typed_topic_intelligence_report,
    build_typed_topic_theme_report,
    build_typed_topic_selection_history_report,
    build_typed_topic_selection_report,
    create_topic_selection,
    get_latest_topic_selection,
    get_topic_selection_history,
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