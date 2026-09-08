from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


def build_topic_selection_report(selection: models.TopicSelection) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc),
        "user_id": selection.user_id,
        "topic": selection.topic,
        "source": selection.source,
        "rationale": selection.rationale,
        "confidence": selection.confidence,
        "is_current": bool(getattr(selection, "is_current", True)),
    }


async def create_topic_selection(
    db: AsyncSession,
    current_user: models.User,
    topic: str,
    source: str = "chat",
    rationale: str = "",
    confidence: float = 0.0,
) -> models.TopicSelection:
    selection = models.TopicSelection(
        user_id=current_user.id,
        topic=topic,
        source=source,
        rationale=rationale,
        confidence=confidence,
        is_current=True,
    )
    db.add(selection)
    await db.commit()
    await db.refresh(selection)
    return selection


async def replace_topic_selection(
    db: AsyncSession,
    current_user: models.User,
    topic: str,
    source: str = "chat",
    rationale: str = "",
    confidence: float = 0.0,
) -> models.TopicSelection:
    prior_selection = await get_latest_topic_selection(db, current_user.id)
    if prior_selection:
        prior_selection.is_current = False
        db.add(prior_selection)

    return await create_topic_selection(
        db,
        current_user,
        topic=topic,
        source=source,
        rationale=rationale,
        confidence=confidence,
    )


async def get_latest_topic_selection(
    db: AsyncSession,
    user_id: int,
) -> Optional[models.TopicSelection]:
    result = await db.execute(
        select(models.TopicSelection)
        .where(models.TopicSelection.user_id == user_id)
        .where(models.TopicSelection.is_current.is_(True))
        .order_by(models.TopicSelection.created_at.desc(), models.TopicSelection.id.desc())
    )
    selection = result.scalars().first()
    if selection:
        return selection

    fallback = await db.execute(
        select(models.TopicSelection)
        .where(models.TopicSelection.user_id == user_id)
        .order_by(models.TopicSelection.created_at.desc(), models.TopicSelection.id.desc())
    )
    return fallback.scalars().first()


async def get_topic_selection_history(
    db: AsyncSession,
    user_id: int,
) -> list[models.TopicSelection]:
    result = await db.execute(
        select(models.TopicSelection)
        .where(models.TopicSelection.user_id == user_id)
        .order_by(models.TopicSelection.created_at.desc(), models.TopicSelection.id.desc())
    )
    return result.scalars().all()


async def archive_topic_selection(
    db: AsyncSession,
    current_user: models.User,
) -> Optional[models.TopicSelection]:
    selection = await get_latest_topic_selection(db, current_user.id)
    if not selection:
        return None

    selection.is_current = False
    db.add(selection)
    await db.commit()
    await db.refresh(selection)
    return selection