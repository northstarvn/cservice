from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models
from app.schemas import schemas
from app.services.topics import archive_topic_selection, build_topic_selection_report, create_topic_selection, get_latest_topic_selection, get_topic_selection_history, replace_topic_selection

router = APIRouter(prefix="/topics", tags=["topics"])


@router.get("/current", response_model=schemas.TopicSelectionReport)
async def read_current_topic_selection(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await get_latest_topic_selection(db, current_user.id)
    if not selection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic selection not found")
    return schemas.TopicSelectionReport(**build_topic_selection_report(selection))


@router.post("/current", response_model=schemas.TopicSelectionReport)
async def create_current_topic_selection(
    topic_request: schemas.TopicSelectionCreate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await create_topic_selection(
        db,
        current_user,
        topic=topic_request.topic,
        source=topic_request.source,
        rationale=topic_request.rationale,
        confidence=topic_request.confidence,
    )
    return schemas.TopicSelectionReport(**build_topic_selection_report(selection))


@router.put("/current", response_model=schemas.TopicSelectionReport)
async def replace_current_topic_selection(
    topic_request: schemas.TopicSelectionCreate,
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await replace_topic_selection(
        db,
        current_user,
        topic=topic_request.topic,
        source=topic_request.source,
        rationale=topic_request.rationale,
        confidence=topic_request.confidence,
    )
    return schemas.TopicSelectionReport(**build_topic_selection_report(selection))


@router.get("/history", response_model=schemas.TopicSelectionHistoryReport)
async def read_topic_selection_history(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selections = await get_topic_selection_history(db, current_user.id)
    return schemas.TopicSelectionHistoryReport(
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        user_id=current_user.id,
        items=[schemas.TopicSelectionOut.model_validate(selection) for selection in selections],
    )


@router.delete("/current", response_model=schemas.TopicSelectionReport)
async def archive_current_topic_selection(
    current_user: models.User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(deps.get_db),
):
    selection = await archive_topic_selection(db, current_user)
    if not selection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic selection not found")
    return schemas.TopicSelectionReport(**build_topic_selection_report(selection))