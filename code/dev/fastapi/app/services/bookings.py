from datetime import datetime, timezone
import json
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas import schemas
from app.services.policy_scoring import can_access_functionality
from app.services.topics import (
    TOPIC_CATALOG,
    TOPIC_SECTORS,
    TOPIC_THEME_GROUPS,
    build_topic_coverage_report,
    build_topic_intelligence_report,
    build_topic_portfolio_report,
    build_topic_theme_coverage,
)


def normalize_assignment_state(value: Optional[object]) -> str:
    if value is None:
        return "suggested"
    return getattr(value, "value", value)


def normalize_status_value(value: Optional[object]) -> str:
    if value is None:
        return "unknown"
    return str(normalize_assignment_state(value))


def status_value(value: Optional[object]) -> str:
    if value is None:
        return "unknown"
    return str(getattr(value, "value", value))


def count_booking_statuses(status_rows) -> dict[str, int]:
    counts: dict[str, int] = {
        status.value: 0 for status in models.BookingStatus
    }
    for status_value, count in status_rows:
        normalized_status = normalize_status_value(status_value)
        counts[normalized_status] = int(count or 0)
    return counts


def count_status_values(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        normalized_status = normalize_status_value(value)
        counts[normalized_status] = counts.get(normalized_status, 0) + 1
    return counts


def _booking_topic_context(booking: models.Booking, assignments: List[models.BookingAssignment], events: List[models.BookingEvent]) -> str:
    status_parts = [
        str(getattr(booking, "status", "")),
        " ".join(count_status_values([booking.status]).keys()),
        " ".join(normalize_status_value(assignment.state) for assignment in assignments[:3]),
        " ".join(str(getattr(event, "event_type", "")) for event in events[:3]),
    ]
    return " ".join(part for part in status_parts if part)


def _booking_topic_details(topic_text: str) -> dict[str, object]:
    selection = type("BookingTopicSelection", (), {"topic": topic_text})()
    intelligence = build_topic_intelligence_report(selection)
    portfolio = build_topic_portfolio_report(selection)
    coverage = build_topic_coverage_report(selection)
    theme_coverage = build_topic_theme_coverage(selection)
    matched_themes = [item["theme"] for item in theme_coverage if item["matched_count"]]
    matched_sectors = [sector["sector"] for sector in TOPIC_SECTORS if any(topic in topic_text.lower() for topic in sector["topics"])]
    topic_focus = list(dict.fromkeys(coverage["matched_topics"] + [item.topic for item in intelligence.suggested_topics[:3]]))
    return {
        "intelligence": intelligence,
        "portfolio": portfolio,
        "coverage": coverage,
        "theme_coverage": theme_coverage,
        "matched_themes": matched_themes,
        "matched_sectors": matched_sectors,
        "topic_focus": topic_focus,
        "catalog_total": len(TOPIC_CATALOG),
        "theme_total": len(TOPIC_THEME_GROUPS),
    }


def build_deterministic_booking_match(booking: models.Booking, current_user: models.User) -> dict:
    room_id = booking.id
    match_reason = f"booking-{status_value(booking.status)}-and-user-{current_user.id}"
    return {
        "room_id": room_id,
        "match_reason": match_reason,
        "source": "booking-service",
        "explanation": "Deterministic booking match derived from booking state and user context.",
        "state": "suggested",
    }


def build_booking_assignment_decisions(
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> List[dict]:
    created_at = datetime.now(timezone.utc)
    requested_assignment = requested_assignment or {}
    fallback_assignment = build_deterministic_booking_match(booking, current_user)
    room_id = requested_assignment.get("room_id", fallback_assignment["room_id"])
    match_reason = requested_assignment.get("match_reason", fallback_assignment["match_reason"])
    source = requested_assignment.get("source", fallback_assignment["source"])
    explanation = requested_assignment.get("explanation", fallback_assignment["explanation"])
    state = requested_assignment.get("state", fallback_assignment["state"])
    return [
        {
            "booking_id": booking.id,
            "user_id": current_user.id,
            "room_id": room_id,
            "match_reason": match_reason,
            "state": state,
            "source": source,
            "explanation": explanation,
            "created_at": created_at,
        }
    ]


def build_booking_assignment_report(
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> dict:
    decisions = build_booking_assignment_decisions(booking, current_user, requested_assignment=requested_assignment)
    current_state = decisions[-1]["state"] if decisions else "suggested"
    return {
        "generated_at": datetime.now(timezone.utc),
        "booking_id": booking.id,
        "user_id": current_user.id,
        "current_state": current_state,
        "decisions": decisions,
    }


def build_booking_assignment_report_payload(
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> dict:
    return build_booking_assignment_report(booking, current_user, requested_assignment=requested_assignment)


def build_booking_assignment_report_from_record(
    assignment: models.BookingAssignment,
) -> dict:
    return {
        "generated_at": assignment.created_at,
        "booking_id": assignment.booking_id,
        "user_id": assignment.user_id,
        "current_state": normalize_assignment_state(assignment.state),
        "current_assignment_is_current": bool(getattr(assignment, "is_current", True)),
        "decisions": [
            {
                "booking_id": assignment.booking_id,
                "user_id": assignment.user_id,
                "room_id": assignment.room_id,
                "match_reason": assignment.match_reason,
                "state": normalize_assignment_state(assignment.state),
                "source": assignment.source,
                "explanation": assignment.explanation,
                "created_at": assignment.created_at,
                "is_current": bool(getattr(assignment, "is_current", True)),
            }
        ],
    }


def build_booking_assignment_reports_from_records(
    assignments: List[models.BookingAssignment],
) -> List[dict]:
    return [build_booking_assignment_report_from_record(assignment) for assignment in assignments]


def build_typed_booking_assignment_reports_from_records(
    assignments: List[models.BookingAssignment],
    current_user: models.User,
    control_posture: str,
) -> List[schemas.BookingAssignmentReport]:
    return [
        build_typed_booking_assignment_report_from_record(assignment, current_user, control_posture)
        for assignment in assignments
    ]


def build_booking_assignment_report_page(assignments: List[models.BookingAssignment]) -> schemas.BookingAssignmentReportPage:
    state_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    current_assignments = 0
    historical_assignments = 0

    for assignment in assignments:
        state = normalize_assignment_state(assignment.state)
        source = str(getattr(assignment, "source", "unknown") or "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        if bool(getattr(assignment, "is_current", True)):
            current_assignments += 1
        else:
            historical_assignments += 1

    return schemas.BookingAssignmentReportPage(
        booking_id=0,
        user_id=0,
        total_assignments=len(assignments),
        current_assignments=current_assignments,
        historical_assignments=historical_assignments,
        state_counts=state_counts,
        source_counts=source_counts,
        created_after=None,
        created_before=None,
        page=1,
        per_page=max(len(assignments), 1),
        pages=1 if assignments else 0,
        items=[],
    )


def build_booking_summary(booking: models.Booking, assignments: List[models.BookingAssignment]) -> dict:
    status_counts = {normalize_status_value(booking.status): 1}
    assignment_count = len(assignments)
    return {
        "booking_id": booking.id,
        "user_id": getattr(booking, "user_id", None),
        "current_status": booking.status,
        "status_counts": status_counts,
        "assignment_count": assignment_count,
    }


def build_booking_service_summary(
    booking: models.Booking,
    assignments: List[models.BookingAssignment],
    events: List[models.BookingEvent],
) -> dict:
    assignment_summary = build_booking_assignment_summary(assignments)
    history_summary = build_booking_assignment_history_summary(events)
    status_counts = {normalize_status_value(booking.status): 1}
    status_counts.update(assignment_summary["state_counts"])
    status_counts.update(history_summary["event_type_counts"])
    booking_topic_text = _booking_topic_context(booking, assignments, events)
    topic_details = _booking_topic_details(booking_topic_text)
    topic_intelligence = topic_details["intelligence"]
    topic_portfolio = topic_details["portfolio"]
    topic_signal_depth = (
        f"themes={len(topic_details['matched_themes'])}, "
        f"sectors={len(topic_details['matched_sectors'])}, "
        f"coverage={topic_intelligence.coverage_ratio:.2f}, "
        f"focus={len(topic_details['topic_focus'])}"
    )
    return {
        "booking_id": booking.id,
        "user_id": getattr(booking, "user_id", None),
        "current_status": booking.status,
        "assignment_count": assignment_summary["total_assignments"],
        "event_count": history_summary["event_count"],
        "assignment_events": history_summary["assignment_events"],
        "status_counts": status_counts,
        "state_counts": assignment_summary["state_counts"],
        "source_counts": assignment_summary["source_counts"],
        "latest_event_at": history_summary["latest_event_at"],
        "latest_event_type": history_summary["latest_event_type"],
        "has_mutations": history_summary["has_mutations"],
        "topic_context": f"{topic_intelligence.summary}; themes={len(topic_details['matched_themes'])}; sectors={len(topic_details['matched_sectors'])}",
        "topic_coverage_ratio": topic_intelligence.coverage_ratio,
        "topic_matches": [item.topic for item in topic_intelligence.suggested_topics[:3]],
        "topic_portfolio_coverage": topic_portfolio["coverage_ratio"],
        "topic_theme_coverage": topic_portfolio["theme_coverage"],
        "topic_signal_depth": topic_signal_depth,
        "topic_focus": topic_details["topic_focus"],
        "topic_catalog_total": topic_details["catalog_total"],
        "topic_theme_total": topic_details["theme_total"],
        "topic_matched_themes": topic_details["matched_themes"],
        "topic_matched_sectors": topic_details["matched_sectors"],
    }


def build_booking_timeline_summary(
    booking: models.Booking,
    assignments: List[models.BookingAssignment],
    events: List[models.BookingEvent],
) -> dict:
    assignment_summary = build_booking_assignment_summary(assignments)
    history_summary = build_booking_assignment_history_summary(events)
    latest_assignment = assignments[0] if assignments else None
    latest_event = events[0] if events else None
    topic_text = _booking_topic_context(booking, assignments, events)
    topic_details = _booking_topic_details(topic_text)
    topic_coverage = topic_details["coverage"]
    status_counts = {
        normalize_status_value(booking.status): 1,
        **{state: count for state, count in assignment_summary["state_counts"].items()},
    }
    return {
        "booking_id": booking.id,
        "user_id": getattr(booking, "user_id", None),
        "current_status": booking.status,
        "assignment_count": assignment_summary["total_assignments"],
        "event_count": history_summary["event_count"],
        "assignment_events": history_summary["assignment_events"],
        "latest_assignment_id": getattr(latest_assignment, "id", None),
        "latest_event_id": getattr(latest_event, "id", None),
        "latest_event_type": history_summary["latest_event_type"],
        "status_counts": status_counts,
        "state_counts": assignment_summary["state_counts"],
        "topic_coverage_ratio": topic_coverage["coverage_ratio"],
        "topic_recommendations": topic_coverage["top_recommendations"],
        "topic_theme_coverage": topic_details["theme_coverage"],
        "topic_matched_themes": topic_details["matched_themes"],
        "topic_focus": topic_details["topic_focus"],
    }


def build_booking_operation_report(
    booking: models.Booking,
    assignments: List[models.BookingAssignment],
    events: List[models.BookingEvent],
    control_posture: str,
) -> dict:
    summary = build_booking_service_summary(booking, assignments, events)
    timeline = build_booking_timeline_summary(booking, assignments, events)
    user_ref = type("UserRef", (), {"id": getattr(booking, "user_id", None)})()
    booking_summary = build_typed_booking_summary(booking, user_ref, assignments, control_posture)
    booking_assignment_summary = build_typed_booking_assignment_summary(booking, user_ref, assignments, control_posture)
    return {
        "generated_at": datetime.now(timezone.utc),
        "booking_id": booking.id,
        "user_id": getattr(booking, "user_id", None),
        "control_posture": control_posture,
        "summary": summary,
        "timeline": timeline,
        "typed_summary": booking_summary.model_dump(),
        "typed_assignment_summary": booking_assignment_summary.model_dump(),
        "topic_context": summary["topic_context"],
        "topic_coverage_ratio": summary["topic_coverage_ratio"],
        "topic_portfolio_coverage": summary["topic_portfolio_coverage"],
        "topic_catalog_total": summary["topic_catalog_total"],
        "topic_theme_total": summary["topic_theme_total"],
        "topic_matched_themes": summary["topic_matched_themes"],
        "topic_matched_sectors": summary["topic_matched_sectors"],
        "topic_focus": summary["topic_focus"],
        "recommendations": [
            "Review assignment history for repeated state changes.",
            "Surface the latest event type when booking details are shown.",
            "Use controlled posture notes when exposing booking details to users.",
            "Carry topic coverage and topic matches into booking decision prompts.",
        ],
    }


def build_typed_booking_operation_report(
    booking: models.Booking,
    assignments: List[models.BookingAssignment],
    events: List[models.BookingEvent],
    control_posture: str,
) -> dict:
    return build_booking_operation_report(booking, assignments, events, control_posture)


def build_typed_booking_timeline_summary(
    booking: models.Booking,
    current_user: models.User,
    assignments: List[models.BookingAssignment],
    events: List[models.BookingEvent],
    control_posture: str,
) -> dict:
    summary = build_booking_timeline_summary(booking, assignments, events)
    summary["user_id"] = current_user.id
    summary["control_posture"] = control_posture
    return summary


def build_typed_booking_summary(
    booking: models.Booking,
    current_user: models.User,
    assignments: List[models.BookingAssignment],
    control_posture: str,
) -> schemas.BookingSummary:
    summary = build_booking_summary(booking, assignments)
    return schemas.BookingSummary(
        booking_id=summary["booking_id"],
        user_id=current_user.id,
        current_status=summary["current_status"],
        status_counts=summary["status_counts"],
        assignment_count=summary["assignment_count"],
        control_posture=control_posture,
    )


def build_typed_booking_summary_report(
    booking: models.Booking,
    current_user: models.User,
    assignments: List[models.BookingAssignment],
    control_posture: str,
) -> schemas.BookingSummaryReport:
    summary = build_booking_summary(booking, assignments)
    topic_context = _booking_topic_context(booking, assignments, [])
    topic_details = _booking_topic_details(topic_context)
    return schemas.BookingSummaryReport(
        generated_at=datetime.now(timezone.utc),
        booking_id=summary["booking_id"],
        user_id=current_user.id,
        current_status=summary["current_status"],
        status_counts=summary["status_counts"],
        assignment_count=summary["assignment_count"],
        control_posture=control_posture,
        summary=(
            f"{summary['current_status']} booking with {summary['assignment_count']} assignments."
            f" Topic coverage ratio {topic_details['intelligence'].coverage_ratio:.2f}"
            f" across {len(topic_details['matched_themes'])} themes"
            f" and {len(topic_details['matched_sectors'])} sectors."
        ),
    )


def build_typed_booking_assignment_report(
    booking: models.Booking,
    current_user: models.User,
    report: dict,
    control_posture: str,
) -> schemas.BookingAssignmentReport:
    decisions = [schemas.BookingAssignmentDecision(**decision) for decision in report["decisions"]]
    if control_posture in {"constrained", "observed"}:
        for decision in decisions:
            decision.explanation = decision.explanation or "Controlled posture note: assignment recommendation is monitored."

    return schemas.BookingAssignmentReport(
        generated_at=report["generated_at"],
        booking_id=booking.id,
        user_id=current_user.id,
        current_state=report["current_state"],
        current_assignment_is_current=report.get("current_assignment_is_current", True),
        decisions=decisions,
        control_posture=control_posture,
    )


def build_typed_booking_assignment_report_from_payload(
    booking: models.Booking,
    current_user: models.User,
    report: dict,
    control_posture: str,
) -> schemas.BookingAssignmentReport:
    return build_typed_booking_assignment_report(
        booking,
        current_user,
        report,
        control_posture,
    )


def build_typed_booking_assignment_report_from_record(
    assignment: models.BookingAssignment,
    current_user: models.User,
    control_posture: str,
) -> schemas.BookingAssignmentReport:
    booking = getattr(assignment, "booking", None)
    if booking is None:
        booking = type("BookingRef", (), {"id": assignment.booking_id})()
    return build_typed_booking_assignment_report(
        booking,
        current_user,
        build_booking_assignment_report_from_record(assignment),
        control_posture,
    )


def build_booking_assignment_report_from_record_compat(
    assignment: models.BookingAssignment,
) -> dict:
    return build_booking_assignment_report_from_record(assignment)


def build_typed_booking_assignment_report_from_existing_report(
    booking: models.Booking,
    current_user: models.User,
    report: dict,
    control_posture: str,
) -> schemas.BookingAssignmentReport:
    return build_typed_booking_assignment_report(
        booking,
        current_user,
        report,
        control_posture,
    )


def build_booking_assignment_summary(assignments: List[models.BookingAssignment]) -> dict:
    state_counts: dict[str, int] = {}
    current_count = 0
    historical_count = 0
    source_counts: dict[str, int] = {}

    for assignment in assignments:
        state = normalize_assignment_state(assignment.state)
        source = str(getattr(assignment, "source", "unknown") or "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        if bool(getattr(assignment, "is_current", True)):
            current_count += 1
        else:
            historical_count += 1

    return {
        "total_assignments": len(assignments),
        "current_assignments": current_count,
        "historical_assignments": historical_count,
        "state_counts": state_counts,
        "source_counts": source_counts,
    }


def build_typed_booking_assignment_summary(
    booking: models.Booking,
    current_user: models.User,
    assignments: List[models.BookingAssignment],
    control_posture: str,
) -> schemas.BookingAssignmentSummary:
    summary = build_booking_assignment_summary(assignments)
    topic_context = _booking_topic_context(booking, assignments, [])
    topic_details = _booking_topic_details(topic_context)
    return schemas.BookingAssignmentSummary(
        booking_id=booking.id,
        user_id=current_user.id,
        total_assignments=summary["total_assignments"],
        current_assignments=summary["current_assignments"],
        historical_assignments=summary["historical_assignments"],
        state_counts=summary["state_counts"],
        control_posture=control_posture,
        summary=(
            f"{summary['total_assignments']} assignments across {summary['current_assignments']} current and {summary['historical_assignments']} historical records."
            f" Topic coverage ratio {topic_details['intelligence'].coverage_ratio:.2f}"
            f" across {len(topic_details['matched_themes'])} themes and {len(topic_details['matched_sectors'])} sectors."
        ),
    )


def build_booking_assignment_history_summary(events: List[models.BookingEvent]) -> dict:
    latest_event = events[0] if events else None
    event_type_counts: dict[str, int] = {}
    assignment_events = 0

    for event in events:
        event_type = str(getattr(event, "event_type", "") or "unknown").lower()
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        if event_type.startswith("assignment_"):
            assignment_events += 1

    return {
        "event_count": len(events),
        "assignment_events": assignment_events,
        "latest_event_at": getattr(latest_event, "created_at", None),
        "latest_event_type": getattr(latest_event, "event_type", None),
        "event_type_counts": event_type_counts,
        "has_mutations": any(event_type in {"updated", "transitioned", "assignment_created", "assignment_updated"} for event_type in event_type_counts.keys()),
    }


def build_typed_booking_assignment_history_summary(
    booking: models.Booking,
    current_user: models.User,
    events: List[models.BookingEvent],
    control_posture: str,
) -> schemas.BookingAssignmentHistorySummary:
    summary = build_booking_assignment_history_summary(events)
    return schemas.BookingAssignmentHistorySummary(
        booking_id=booking.id,
        user_id=current_user.id,
        event_count=summary["event_count"],
        assignment_events=summary["assignment_events"],
        latest_event_at=summary["latest_event_at"],
        latest_event_type=summary["latest_event_type"],
        event_type_counts=summary["event_type_counts"],
        control_posture=control_posture,
    )


def can_user_access_booking_functionality(policy_score: models.CustomerPolicyScore, functionality: str) -> bool:
    if functionality in {"create", "update", "delete", "history"}:
        return can_access_functionality(policy_score, required_tier="standard")
    if functionality in {"assign", "recommend", "match"}:
        return can_access_functionality(policy_score, required_tier="customer-premium")
    return can_access_functionality(policy_score, required_tier="restricted")


async def create_booking_event(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    event_type: str,
    note: str = "",
    from_status=None,
    to_status=None,
):
    note_value = note
    if isinstance(note, dict):
        note_value = json.dumps(note, default=str)
    event = models.BookingEvent(
        booking_id=booking.id,
        user_id=current_user.id,
        event_type=event_type,
        from_status=status_value(from_status),
        to_status=status_value(to_status),
        note=note_value,
    )
    db.add(event)
    return event


async def create_booking_assignment_event(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    assignment: models.BookingAssignment,
    event_type: str,
):
    note = {
        "assignment_id": assignment.id,
        "room_id": assignment.room_id,
        "state": normalize_assignment_state(assignment.state),
        "source": assignment.source,
        "explanation": assignment.explanation,
        "is_current": assignment.is_current,
    }
    return await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note=note,
        from_status=booking.status,
        to_status=booking.status,
    )


async def create_booking_assignment(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> models.BookingAssignment:
    requested_assignment = requested_assignment or {}
    assignment = models.BookingAssignment(
        booking_id=booking.id,
        user_id=current_user.id,
        room_id=requested_assignment.get("room_id", booking.id),
        match_reason=requested_assignment.get("match_reason", f"booking-{booking.id}-assignment"),
        state=normalize_assignment_state(requested_assignment.get("state")),
        source=requested_assignment.get("source", "booking-service"),
        explanation=requested_assignment.get("explanation", "Generated booking assignment."),
        is_current=True,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    await create_booking_assignment_event(
        db,
        booking,
        current_user,
        assignment,
        event_type="assignment_created",
    )
    await db.commit()
    return assignment


async def update_booking_assignment(
    db: AsyncSession,
    assignment: models.BookingAssignment,
    current_user: models.User,
    requested_assignment: Optional[dict] = None,
) -> models.BookingAssignment:
    requested_assignment = requested_assignment or {}
    if "room_id" in requested_assignment and requested_assignment["room_id"] is not None:
        assignment.room_id = requested_assignment["room_id"]
    if "match_reason" in requested_assignment and requested_assignment["match_reason"] is not None:
        assignment.match_reason = requested_assignment["match_reason"]
    if "state" in requested_assignment and requested_assignment["state"] is not None:
        assignment.state = normalize_assignment_state(requested_assignment["state"])
    if "source" in requested_assignment and requested_assignment["source"] is not None:
        assignment.source = requested_assignment["source"]
    if "explanation" in requested_assignment and requested_assignment["explanation"] is not None:
        assignment.explanation = requested_assignment["explanation"]
    assignment.is_current = True
    assignment.updated_at = datetime.now(timezone.utc)
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    await create_booking_assignment_event(
        db,
        assignment.booking,
        current_user,
        assignment,
        event_type="assignment_updated",
    )
    await db.commit()
    return assignment


async def get_latest_booking_assignment(
    db: AsyncSession,
    booking_id: int,
    user_id: int,
) -> Optional[models.BookingAssignment]:
    result = await db.execute(
        select(models.BookingAssignment)
        .where(models.BookingAssignment.booking_id == booking_id)
        .where(models.BookingAssignment.user_id == user_id)
        .where(models.BookingAssignment.is_current.is_(True))
        .order_by(models.BookingAssignment.created_at.desc(), models.BookingAssignment.id.desc())
    )
    assignment = result.scalars().first()
    if assignment:
        return assignment

    fallback_result = await db.execute(
        select(models.BookingAssignment)
        .where(models.BookingAssignment.booking_id == booking_id)
        .where(models.BookingAssignment.user_id == user_id)
        .order_by(models.BookingAssignment.created_at.desc(), models.BookingAssignment.id.desc())
    )
    return fallback_result.scalars().first()


async def get_booking_assignment_by_id(
    db: AsyncSession,
    booking_id: int,
    assignment_id: int,
    user_id: int,
) -> Optional[models.BookingAssignment]:
    result = await db.execute(
        select(models.BookingAssignment)
        .where(models.BookingAssignment.id == assignment_id)
        .where(models.BookingAssignment.booking_id == booking_id)
        .where(models.BookingAssignment.user_id == user_id)
    )
    return result.scalars().first()


async def list_booking_assignments(
    db: AsyncSession,
    booking_id: int,
    user_id: int,
) -> List[models.BookingAssignment]:
    result = await db.execute(
        select(models.BookingAssignment)
        .where(models.BookingAssignment.booking_id == booking_id)
        .where(models.BookingAssignment.user_id == user_id)
        .order_by(models.BookingAssignment.created_at.desc(), models.BookingAssignment.id.desc())
    )
    return list(result.scalars().all())


def booking_to_event_out(event: models.BookingEvent, *, is_assignment_event: Optional[bool] = None) -> schemas.BookingEventOut:
    return schemas.BookingEventOut(
        id=event.id,
        booking_id=event.booking_id,
        user_id=event.user_id,
        event_type=event.event_type,
        from_status=status_value(event.from_status),
        to_status=status_value(event.to_status),
        note=event.note,
        created_at=event.created_at,
        is_assignment_event=is_assignment_event if is_assignment_event is not None else event.event_type.startswith("assignment_"),
    )


def touch_booking(booking: models.Booking) -> None:
    booking.updated_at = datetime.now(timezone.utc)


def build_booking_diff(before: models.Booking, update_data: dict) -> dict[str, dict[str, object]]:
    diff: dict[str, dict[str, object]] = {}
    for field, new_value in update_data.items():
        if not hasattr(before, field):
            continue
        old_value = getattr(before, field)
        old_normalized = status_value(old_value)
        new_normalized = status_value(new_value)
        if old_normalized == new_normalized:
            continue
        diff[field] = {
            "from": old_normalized,
            "to": new_normalized,
        }
    return diff


def apply_booking_updates(booking: models.Booking, update_data: dict) -> None:
    for field, value in update_data.items():
        if not hasattr(booking, field):
            continue

        if field in ["service_type", "status"] and hasattr(value, "value"):
            setattr(booking, field, value.value)
        else:
            setattr(booking, field, value)


async def get_owned_booking(
    db: AsyncSession,
    booking_id: int,
    current_user: models.User,
) -> models.Booking:
    result = await db.execute(
        select(models.Booking).where(
            and_(models.Booking.id == booking_id, models.Booking.user_id == current_user.id)
        )
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return booking


async def ensure_booking_transition_allowed(booking: models.Booking, target_status) -> None:
    if status_value(booking.status) == status_value(target_status):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking already in target status")
    if status_value(booking.status) == "cancelled" and status_value(target_status) == "confirmed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cancelled bookings cannot be confirmed")


async def transition_booking(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    target_status,
    event_type: str,
    note: str = "",
) -> dict:
    await ensure_booking_transition_allowed(booking, target_status)
    control_posture = getattr(getattr(current_user, "policy_score", None), "control_posture", "observed")
    if control_posture in {"high_trust", "customer_trusted"}:
        note = note or "Booking transition completed under elevated posture"
    elif control_posture in {"constrained", "observed"}:
        note = note or "Booking transition completed under controlled posture"
    elif not note:
        note = "Booking transition completed"
    booking.status = target_status
    touch_booking(booking)
    event = await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note=note,
        from_status=target_status,
        to_status=target_status,
    )
    db.add(booking)
    await db.commit()
    await db.refresh(booking)
    await db.refresh(event)
    return {"booking": booking, "event": event}


async def record_booking_mutation_event(
    db: AsyncSession,
    booking: models.Booking,
    current_user: models.User,
    event_type: str,
    previous_booking: Optional[models.Booking] = None,
    update_data: Optional[dict] = None,
    note: str = "",
) -> models.BookingEvent:
    note_payload = {
        "message": note,
        "booking_id": booking.id,
        "user_id": current_user.id,
        "update_fields": sorted((update_data or {}).keys()),
    }
    if previous_booking is not None:
        note_payload["previous_booking_id"] = previous_booking.id
        note_payload["previous_status"] = status_value(previous_booking.status)
    return await create_booking_event(
        db,
        booking,
        current_user,
        event_type=event_type,
        note=note_payload,
        from_status=booking.status,
        to_status=booking.status,
    )
