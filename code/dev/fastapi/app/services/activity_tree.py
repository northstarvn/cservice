"""Tree-structured customer activity monitoring.

Hypothesis
----------
Monitoring "who to watch next" is easier when customer activities are organized
as a tree the operator can re-shape at query time:

1. Grouping     - fold every customer into a configurable axis first
                  (lifecycle stage, value tier, customer classification,
                  churn risk, journey family) so the population is navigable.
2. Ranking      - within each group, rank members by a configurable metric
                  (loyalty, monetization readiness, signal strength, churn
                  score, activity count) in ascending or descending order.
3. Smart filter - prune the tree by loyalty range, churn risk, sentiment,
                  free-text search, or "anomalies only" before/after grouping.
4. Anomalies    - every user is checked against absolute anomaly rules, and
                  group-relative deviations (loyalty far below the group mean,
                  churn far above it) are highlighted as well; flagged
                  activities (cancelled bookings, negative-chat keywords) light
                  up the leaf level.

Implementation
--------------
Rules are config tables, not branches:

- `ACTIVITY_TREE_GROUP_AXES`        - available first-level grouping axes.
- `ACTIVITY_TREE_RANK_OPTIONS`      - metrics a group can be ranked by.
- `ACTIVITY_TREE_ANOMALY_RULES`     - absolute anomaly detectors (op/threshold).
- `ACTIVITY_TREE_RELATIVE_ANOMALY_RULES` - group-mean deviation detectors.

Adding a grouping axis, a rank metric, or an anomaly rule is a config change
only. `/meta/scoring-catalog` exposes the live catalogs for future surfaces.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.schemas.chat import (
    ActivityTreeActivityItem,
    ActivityTreeAnomalyItem,
    ActivityTreeReport,
    ActivityTreeNode,
)
from app.services.chat_analytics import (
    analyze_sentiment,
    build_churn_prediction,
    build_summary,
    load_user_interaction_window,
)
from app.services.loyalty_journey import (
    build_journey_context,
    load_history_totals,
    match_loyalty_scenarios,
)

# ---------------------------------------------------------------------------
# Config tables (data-driven rule engines)
# ---------------------------------------------------------------------------

ACTIVITY_TREE_GROUP_AXES: dict[str, dict[str, str]] = {
    "lifecycle_stage": {
        "label": "Lifecycle stage",
        "metric_field": "lifecycle_stage",
    },
    "value_tier": {
        "label": "Value tier",
        "metric_field": "value_tier",
    },
    "customer_classification": {
        "label": "Customer classification",
        "metric_field": "customer_classification",
    },
    "churn_risk": {
        "label": "Churn risk",
        "metric_field": "churn_risk",
    },
    "journey_family": {
        "label": "Journey family",
        "metric_field": "journey_family",
    },
}

ACTIVITY_TREE_RANK_OPTIONS: dict[str, str] = {
    "loyalty_score": "Loyalty score",
    "monetization_readiness": "Monetization readiness",
    "signal_strength": "Signal strength",
    "churn_risk_score": "Churn risk score",
    "activity_count": "Activity count",
}

_RANK_KEYS = {
    "loyalty_score": "loyalty_score",
    "monetization_readiness": "monetization_readiness",
    "signal_strength": "signal_strength",
    "churn_risk_score": "churn_risk_score",
    "activity_count": "activity_count",
}

ACTIVITY_TREE_ANOMALY_RULES: list[dict[str, Any]] = [
    {
        "id": "churn_spike",
        "label": "High churn risk",
        "metric": "churn_risk_score",
        "op": "gte",
        "threshold": 50.0,
        "severity": "high",
        "description": "Churn prediction score crossed a critical level; intervene now.",
    },
    {
        "id": "loyalty_drop",
        "label": "Low loyalty score",
        "metric": "loyalty_score",
        "op": "lte",
        "threshold": 40.0,
        "severity": "high",
        "description": "Loyalty score is unusually low for an active user.",
    },
    {
        "id": "negative_sentiment",
        "label": "Negative sentiment",
        "metric": "sentiment_label",
        "op": "eq",
        "threshold": "negative",
        "severity": "medium",
        "description": "The latest interaction carries negative sentiment.",
    },
    {
        "id": "cancellation_burst",
        "label": "Cancellation burst",
        "metric": "cancelled_bookings",
        "op": "gte",
        "threshold": 2,
        "severity": "medium",
        "description": "Multiple bookings were cancelled inside the window.",
    },
    {
        "id": "signal_spike",
        "label": "Signal spike",
        "metric": "signal_strength",
        "op": "gte",
        "threshold": 10.0,
        "severity": "medium",
        "description": "Interaction pressure is far above the typical level.",
    },
    {
        "id": "dormancy",
        "label": "Dormant customer",
        "metric": "dormant",
        "op": "eq",
        "threshold": True,
        "severity": "low",
        "description": "History exists but the customer went quiet.",
    },
]

ACTIVITY_TREE_RELATIVE_ANOMALY_RULES: list[dict[str, Any]] = [
    {
        "id": "loyalty_gap",
        "label": "Loyalty below group average",
        "metric": "loyalty_score",
        "direction": "below",
        "margin": 15.0,
        "severity": "medium",
        "description": "Loyalty score trails its group average by a wide margin.",
    },
    {
        "id": "churn_deviation",
        "label": "Churn score above group average",
        "metric": "churn_risk_score",
        "direction": "above",
        "margin": 20.0,
        "severity": "medium",
        "description": "Churn score deviates well above the group average.",
    },
]

TREE_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "slow",
    "delay",
    "wait",
    "cancel",
    "refund",
    "angry",
    "frustrated",
    "unclear",
    "confusing",
    "bad",
    "issue",
    "problem",
    "error",
    "bug",
    "disappoint",
    "late",
    "lost",
    "broken",
    "wrong",
    "fail",
)

# ---------------------------------------------------------------------------
# Rule evaluation helpers
# ---------------------------------------------------------------------------


def _eval_metric_op(metric_value: Any, op: str, threshold: Any) -> bool:
    if op == "gte":
        return bool(metric_value >= threshold)
    if op == "lte":
        return bool(metric_value <= threshold)
    if op == "eq":
        return bool(metric_value == threshold)
    return False


def detect_user_anomalies(metrics: dict) -> list[dict]:
    """Evaluate the absolute anomaly rules against a user's metric dict."""
    anomalies: list[dict] = []
    for rule in ACTIVITY_TREE_ANOMALY_RULES:
        metric_value = metrics.get(rule["metric"])
        if metric_value is None:
            continue
        try:
            fired = _eval_metric_op(metric_value, rule["op"], rule["threshold"])
        except (TypeError, ValueError):
            fired = False
        if fired:
            anomalies.append(
                {
                    "anomaly": rule["id"],
                    "label": rule["label"],
                    "severity": rule["severity"],
                    "description": rule["description"],
                    "evidence": f"{rule['metric']}={metric_value!r}",
                    "metric_value": metric_value if isinstance(metric_value, (int, float)) else 0.0,
                    "rule": rule,
                }
            )
    return anomalies


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def apply_user_filters(
    metrics: dict,
    *,
    min_loyalty: Optional[float] = None,
    max_loyalty: Optional[float] = None,
    churn_risk: Optional[str] = None,
    sentiment: Optional[str] = None,
    q: Optional[str] = None,
    anomalies_only: bool = False,
) -> bool:
    """Smart filter — decide whether a user's metrics survive the query."""
    loyalty = float(metrics.get("loyalty_score", 0.0) or 0.0)
    if min_loyalty is not None and loyalty < min_loyalty:
        return False
    if max_loyalty is not None and loyalty > max_loyalty:
        return False
    if churn_risk and str(metrics.get("churn_risk", "")) != churn_risk:
        return False
    if sentiment and str(metrics.get("sentiment_label", "")) != sentiment:
        return False
    if q:
        haystack = " ".join(
            [
                str(metrics.get("username", "") or ""),
                str(metrics.get("top_issue", "") or ""),
                str(metrics.get("journey_family", "") or ""),
            ]
        ).lower()
        if str(q).lower() not in haystack:
            return False
    if anomalies_only and int(metrics.get("_anomaly_count", 0) or 0) <= 0:
        return False
    return True


def _rank_value(metrics: dict, rank_by: str) -> float:
    key = _RANK_KEYS.get(rank_by)
    if key is None:
        return 0.0
    try:
        return float(metrics.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def rank_and_slice(members: list[dict], rank_by: str, order: str, limit: int) -> list[dict]:
    """Rank members by a metric in asc/desc order and cap the visible set."""
    reverse = order != "asc"
    ranked = sorted(members, key=lambda m: _rank_value(m, rank_by), reverse=reverse)
    n = max(0, int(limit or 0))
    return ranked[:n] if n else ranked


def group_members_by_axis(members: list[dict], axis: str) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {}
    for member in members:
        value = str(member.get(axis, "unknown") or "unknown")
        buckets.setdefault(value, []).append(member)
    return buckets


def attach_group_relative_anomalies(members: list[dict]) -> None:
    """Add group-mean deviation anomalies (loyalty_gap / churn_deviation)."""
    if not members:
        return
    n = len(members)
    avg_loyalty = sum(float(m.get("loyalty_score", 0.0) or 0.0) for m in members) / n
    avg_churn = sum(float(m.get("churn_risk_score", 0.0) or 0.0) for m in members) / n
    for member in members:
        anomalies = member.setdefault("_anomalies", [])
        existing = {a["anomaly"] for a in anomalies}
        for rule in ACTIVITY_TREE_RELATIVE_ANOMALY_RULES:
            if rule["id"] in existing:
                continue
            metric_value = float(member.get(rule["metric"], 0.0) or 0.0)
            margin = float(rule["margin"])
            if rule["direction"] == "below" and avg_loyalty - metric_value >= margin:
                anomalies.append(
                    {
                        "anomaly": rule["id"],
                        "label": rule["label"],
                        "severity": rule["severity"],
                        "description": rule["description"],
                        "evidence": (
                            f"{rule['metric']}={metric_value:.1f} vs group avg "
                            f"{avg_loyalty:.1f}"
                        ),
                        "metric_value": round(metric_value, 2),
                        "rule": rule,
                    }
                )
            elif rule["direction"] == "above" and metric_value - avg_churn >= margin:
                anomalies.append(
                    {
                        "anomaly": rule["id"],
                        "label": rule["label"],
                        "severity": rule["severity"],
                        "description": rule["description"],
                        "evidence": (
                            f"{rule['metric']}={metric_value:.1f} vs group avg "
                            f"{avg_churn:.1f}"
                        ),
                        "metric_value": round(metric_value, 2),
                        "rule": rule,
                    }
                )
        member["_anomaly_count"] = len(anomalies)


def build_group_metrics(all_members: list[dict], ranked_members: list[dict]) -> dict[str, Any]:
    n = len(all_members)

    def _avg(field: str) -> float:
        values = [float(m.get(field, 0.0) or 0.0) for m in all_members]
        return round(sum(values) / n, 2) if n else 0.0

    return {
        "member_count": n,
        "shown_members": len(ranked_members),
        "avg_loyalty": _avg("loyalty_score"),
        "avg_churn_score": _avg("churn_risk_score"),
        "avg_monetization_readiness": _avg("monetization_readiness"),
        "anomaly_count": int(sum(int(m.get("_anomaly_count", 0) or 0) for m in all_members)),
    }

# ---------------------------------------------------------------------------
# Activity items (leaf-level data shared by admin and self trees)
# ---------------------------------------------------------------------------


def _flag_chat_item(text: str) -> tuple[bool, str]:
    lowered = text.lower()
    for keyword in TREE_NEGATIVE_KEYWORDS:
        if keyword in lowered:
            return True, f"Negative keyphrase: '{keyword}'"
    return False, ""


def _flag_booking_item(status: str) -> tuple[bool, str]:
    if status == "cancelled":
        return True, "Booking cancelled"
    return False, ""


def _booking_item_value(status: str) -> float:
    return {
        "completed": 3.0,
        "confirmed": 2.0,
        "pending": 1.5,
        "cancelled": 0.5,
    }.get(status, 1.0)


def _item_timestamp(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None and isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc)
    return value


def build_activity_items(
    chat_rows: list[models.ChatHistory],
    bookings: list[models.Booking],
    *,
    kind: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Flatten chat rows and bookings into sortable, flaggable leaf items."""
    items: list[dict] = []
    if not kind or kind == "chat":
        for row in chat_rows:
            text = str(getattr(row, "message", "") or "")
            flagged, reason = _flag_chat_item(text)
            items.append(
                {
                    "kind": "chat",
                    "label": text[:80],
                    "detail": f"{len(text)} chars",
                    "value": round(1.0 + min(len(text) / 200.0, 1.0), 2),
                    "timestamp": _item_timestamp(getattr(row, "timestamp", None)),
                    "reference": getattr(row, "id", None),
                    "flagged": flagged,
                    "flag_reason": reason,
                }
            )
    if not kind or kind == "booking":
        for booking in bookings:
            status = str(
                getattr(getattr(booking, "status", None), "value", getattr(booking, "status", None))
                or ""
            )
            service = str(
                getattr(
                    getattr(booking, "service_type", None),
                    "value",
                    getattr(booking, "service_type", None),
                )
                or "service"
            )
            flagged, reason = _flag_booking_item(status)
            items.append(
                {
                    "kind": "booking",
                    "label": f"{service} · {status}",
                    "detail": str(getattr(booking, "scheduled_date", None) or ""),
                    "value": _booking_item_value(status),
                    "timestamp": _item_timestamp(getattr(booking, "created_at", None)),
                    "reference": getattr(booking, "id", None),
                    "flagged": flagged,
                    "flag_reason": reason,
                }
            )
    items.sort(
        key=lambda item: item["timestamp"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    n = max(0, int(limit or 0))
    return items[:n] if n else items


def rank_items(items: list[dict], rank_by: str, order: str) -> list[dict]:
    """Rank leaf items by recency (default) or by their influence score."""
    if rank_by == "recency":
        key = lambda item: item.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc)
    else:
        key = lambda item: float(item.get("value", 0.0) or 0.0)
    return sorted(items, key=key, reverse=(order != "asc"))


def _clean_metrics(metrics: dict) -> dict[str, Any]:
    return {k: _json_safe(v) for k, v in metrics.items() if not str(k).startswith("_")}

# ---------------------------------------------------------------------------
# Node / report assembly
# ---------------------------------------------------------------------------


def _anomaly_model(anomaly: dict) -> ActivityTreeAnomalyItem:
    return ActivityTreeAnomalyItem(
        anomaly=str(anomaly["anomaly"]),
        label=str(anomaly["label"]),
        severity=str(anomaly["severity"]),
        description=str(anomaly.get("description", "")),
        evidence=str(anomaly.get("evidence", "")),
        metric_value=float(anomaly.get("metric_value", 0.0) or 0.0),
        rule=_json_safe(anomaly.get("rule", {})),
    )


def _activity_item_model(item: dict) -> ActivityTreeActivityItem:
    return ActivityTreeActivityItem(
        kind=str(item["kind"]),
        label=str(item.get("label", "")),
        detail=str(item.get("detail", "")),
        value=float(item.get("value", 0.0) or 0.0),
        timestamp=item.get("timestamp"),
        reference=item.get("reference"),
        flagged=bool(item.get("flagged", False)),
        flag_reason=str(item.get("flag_reason", "")),
    )


def _user_node_dict(metrics: dict, anomalies: list[dict], activity_items: list[dict]) -> dict[str, Any]:
    flagged_items = sum(1 for item in activity_items if item.get("flagged"))
    return {
        "node_id": f"user:{metrics['user_id']}",
        "label": str(metrics.get("username", f"user {metrics['user_id']}")),
        "kind": "user",
        "metrics": _clean_metrics(metrics),
        "child_count": len(activity_items),
        "anomaly_count": int(metrics.get("_anomaly_count", 0) or 0) + flagged_items,
        "anomalies": [_anomaly_model(a) for a in anomalies],
        "children": [],
        "activity_items": [_activity_item_model(item) for item in activity_items],
    }


def _collect_highlights(
    group_nodes: list[dict[str, Any]], *, cap: int = 10
) -> list[dict[str, Any]]:
    highlights: list[dict[str, Any]] = []
    for group in group_nodes:
        for child in group.get("children", []):
            username = str(child["metrics"].get("username", ""))
            for anomaly in child.get("anomalies", []):
                highlights.append(
                    {
                        "severity": anomaly.severity,
                        "label": anomaly.label,
                        "user": username,
                        "evidence": anomaly.evidence,
                    }
                )
            for item in child.get("activity_items", []):
                if item.flagged:
                    highlights.append(
                        {
                            "severity": "medium",
                            "label": item.flag_reason,
                            "user": username,
                            "evidence": item.label,
                        }
                    )
        for item in group.get("activity_items", []):
            if item.flagged:
                highlights.append(
                    {
                        "severity": "medium",
                        "label": item.flag_reason,
                        "user": "self",
                        "evidence": item.label,
                    }
                )
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    highlights.sort(key=lambda h: severity_rank.get(str(h["severity"]), 3))
    return highlights[:cap]


def _default_summary_text(
    scope: str,
    group_count: int,
    total_users: int,
    total_anomalies: int,
    group_by: str,
) -> str:
    if scope == "self":
        return (
            f"self activity tree grouped by activity kind with {total_anomalies} flagged "
            f"anomalies across {group_count} activity groups"
        )
    return (
        f"{total_users} customers grouped by {group_by} across {group_count} groups; "
        f"{total_anomalies} anomalies highlighted for follow-up"
    )


def _assemble_report(
    *,
    scope: str,
    window_days: int,
    group_by: str,
    rank_by: str,
    order: str,
    total_users: int,
    group_nodes: list[dict[str, Any]],
    filtered: dict[str, Any],
    root_metrics: dict[str, Any],
) -> ActivityTreeReport:
    total_nodes = 1 + sum(1 + len(g.get("children", [])) for g in group_nodes)
    total_anomalies = sum(int(g.get("anomaly_count", 0) or 0) for g in group_nodes)
    top_group = (
        max(
            group_nodes,
            key=lambda g: (int(g.get("anomaly_count", 0) or 0), int(g["metrics"].get("member_count", 0) or 0)),
        )
        if group_nodes
        else None
    )
    root_metrics["anomaly_count"] = total_anomalies
    root_metrics["group_count"] = len(group_nodes)
    root = ActivityTreeNode(
        node_id="root",
        label="All customer activities" if scope == "all_users" else "My activities",
        kind="root",
        metrics=root_metrics,
        child_count=len(group_nodes),
        anomaly_count=total_anomalies,
        anomalies=[],
        children=[ActivityTreeNode(**g) for g in group_nodes],
        activity_items=[],
    )
    return ActivityTreeReport(
        generated_at=datetime.now(timezone.utc),
        window_days=window_days,
        scope=scope,
        group_by=group_by,
        rank_by=rank_by,
        order=order,
        total_users=total_users,
        total_nodes=total_nodes,
        group_count=len(group_nodes),
        anomaly_count=total_anomalies,
        top_group=top_group["label"] if top_group else None,
        filtered=filtered,
        root=root,
        summary=_default_summary_text(scope, len(group_nodes), total_users, total_anomalies, group_by),
        highlights=_collect_highlights(group_nodes),
    )

# ---------------------------------------------------------------------------
# User metric loading
# ---------------------------------------------------------------------------


async def _load_user_tree_metrics(
    db: AsyncSession, user_id: int, window_days: int
) -> tuple[dict, list[models.ChatHistory], list[models.Booking]]:
    chat_rows, bookings = await load_user_interaction_window(db, user_id, window_days)
    latest_sentiment = analyze_sentiment(chat_rows[0].message) if chat_rows else None
    summary = build_summary(user_id, chat_rows, bookings, latest_sentiment)
    churn_prediction = await build_churn_prediction(db, user_id, window_days)
    totals = await load_history_totals(db, user_id)
    context = build_journey_context(
        summary,
        churn_prediction,
        chat_rows,
        bookings,
        latest_sentiment,
        total_chat_count=int(totals["total_chat_count"]),
        total_booking_count=int(totals["total_booking_count"]),
        latest_chat_at=totals["latest_chat_at"],
        latest_booking_at=totals["latest_booking_at"],
    )
    matched = match_loyalty_scenarios(context)
    family = str(matched[0]["family"]) if matched else "none"
    metadata = getattr(summary, "metadata", None) or {}
    try:
        signal_strength = float(metadata.get("signal_strength", 0.0) or 0.0)
    except (TypeError, ValueError):
        signal_strength = 0.0
    latest_activity_at = totals.get("latest_booking_at") or totals.get("latest_chat_at")
    metrics: dict[str, Any] = {
        "user_id": user_id,
        "lifecycle_stage": context.lifecycle_stage,
        "churn_risk": str(context.churn_risk),
        "risk_level": str(context.risk_level),
        "value_tier": str(context.value_tier),
        "customer_classification": str(context.customer_classification),
        "loyalty_score": float(context.loyalty_score),
        "monetization_readiness": float(context.monetization_readiness),
        "signal_strength": round(signal_strength, 2),
        "churn_risk_score": float(context.risk_score),
        "sentiment_label": str(context.sentiment_label),
        "chat_count": int(context.chat_count),
        "booking_count": int(context.booking_count),
        "completed_bookings": int(context.completed_bookings),
        "pending_bookings": int(context.pending_bookings),
        "cancelled_bookings": int(context.cancelled_bookings),
        "dormant": bool(context.dormant),
        "days_since_last_activity": context.days_since_last_activity,
        "journey_family": family,
        "top_issue": str(context.top_issues[0]) if context.top_issues else "",
        "activity_count": int(context.chat_count) + int(context.booking_count),
        "latest_activity_at": (
            latest_activity_at.isoformat() if latest_activity_at is not None else None
        ),
    }
    return metrics, chat_rows, bookings

# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


async def build_activity_tree(
    db: AsyncSession,
    window_days: int = 30,
    *,
    group_by: str = "lifecycle_stage",
    rank_by: str = "loyalty_score",
    order: str = "desc",
    limit: int = 5,
    min_loyalty: Optional[float] = None,
    max_loyalty: Optional[float] = None,
    churn_risk: Optional[str] = None,
    sentiment: Optional[str] = None,
    q: Optional[str] = None,
    anomalies_only: bool = False,
    with_activities: bool = False,
    activity_kind: Optional[str] = None,
    activity_limit: int = 6,
    user_ids: Optional[list[int]] = None,
) -> ActivityTreeReport:
    """Build the cross-user monitoring tree (admin surface)."""
    if group_by not in ACTIVITY_TREE_GROUP_AXES:
        group_by = "lifecycle_stage"
    if rank_by not in ACTIVITY_TREE_RANK_OPTIONS:
        rank_by = "loyalty_score"

    user_rows = await db.execute(select(models.User.id, models.User.username))
    users = [(int(row[0]), str(row[1])) for row in user_rows.all()]
    if user_ids is not None:
        wanted = {int(uid) for uid in user_ids}
        users = [(uid, uname) for uid, uname in users if uid in wanted]

    members: list[dict[str, Any]] = []
    for user_id, username in users:
        metrics, chat_sample, booking_sample = await _load_user_tree_metrics(
            db, user_id, window_days
        )
        metrics["username"] = username
        absolute = detect_user_anomalies(metrics)
        metrics["_anomalies"] = list(absolute)
        metrics["_anomaly_count"] = len(absolute)
        metrics["_chat_sample"] = chat_sample
        metrics["_booking_sample"] = booking_sample
        if apply_user_filters(
            metrics,
            min_loyalty=min_loyalty,
            max_loyalty=max_loyalty,
            churn_risk=churn_risk,
            sentiment=sentiment,
            q=q,
            anomalies_only=anomalies_only,
        ):
            members.append(metrics)

    axis_field = ACTIVITY_TREE_GROUP_AXES[group_by]["metric_field"]
    buckets = group_members_by_axis(members, axis_field)

    group_nodes: list[dict[str, Any]] = []
    for label in sorted(buckets):
        bucket = buckets[label]
        attach_group_relative_anomalies(bucket)
        ranked = rank_and_slice(bucket, rank_by, order, limit)
        user_nodes: list[dict[str, Any]] = []
        for member in ranked:
            anomalies = list(member.get("_anomalies", []))
            items: list[dict] = []
            if with_activities:
                chat_sample = member.get("_chat_sample", [])
                booking_sample = member.get("_booking_sample", [])
                items = build_activity_items(
                    chat_sample, booking_sample,
                    kind=activity_kind,
                    limit=activity_limit,
                )
            user_nodes.append(_user_node_dict(member, anomalies, items))
        group_metrics = build_group_metrics(bucket, ranked)
        group_nodes.append(
            {
                "node_id": f"{group_by}:{label}",
                "label": f"{ACTIVITY_TREE_GROUP_AXES[group_by]['label']} · {label}",
                "kind": "group",
                "metrics": group_metrics,
                "child_count": len(user_nodes),
                "anomaly_count": group_metrics["anomaly_count"],
                "anomalies": [],
                "children": user_nodes,
                "activity_items": [],
            }
        )

    if anomalies_only:
        group_nodes = [g for g in group_nodes if int(g.get("anomaly_count", 0) or 0) > 0]

    filtered: dict[str, Any] = {
        "group_by": group_by,
        "rank_by": rank_by,
        "order": order,
        "limit": limit,
        "min_loyalty": min_loyalty,
        "max_loyalty": max_loyalty,
        "churn_risk": churn_risk,
        "sentiment": sentiment,
        "q": q,
        "anomalies_only": anomalies_only,
        "with_activities": with_activities,
        "activity_kind": activity_kind,
    }
    root_metrics: dict[str, Any] = {
        "total_users": len(users),
        "shown_users": len(members),
    }
    return _assemble_report(
        scope="all_users",
        window_days=window_days,
        group_by=group_by,
        rank_by=rank_by,
        order=order,
        total_users=len(users),
        group_nodes=group_nodes,
        filtered=filtered,
        root_metrics=root_metrics,
    )


async def build_self_activity_tree(
    db: AsyncSession,
    user_id: int,
    window_days: int = 30,
    *,
    rank_by: str = "recency",
    order: str = "desc",
    limit: int = 10,
    kind: Optional[str] = None,
) -> ActivityTreeReport:
    """Build the current user's own activities as a tree (self surface)."""
    chat_rows, bookings = await load_user_interaction_window(
        db, user_id, window_days, chat_limit=200, booking_limit=100
    )
    items = build_activity_items(chat_rows, bookings, kind=kind, limit=None)
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(str(item["kind"]), []).append(item)

    group_nodes: list[dict[str, Any]] = []
    for kind_label in sorted(buckets):
        bucket = buckets[kind_label]
        ranked = rank_items(bucket, rank_by, order)
        shown = ranked[:limit] if limit else ranked
        flagged_count = sum(1 for item in shown if item.get("flagged"))
        group_nodes.append(
            {
                "node_id": f"activity_kind:{kind_label}",
                "label": f"Activity kind · {kind_label}",
                "kind": "group",
                "metrics": {
                    "member_count": len(bucket),
                    "shown_items": len(shown),
                    "anomaly_count": flagged_count,
                },
                "child_count": len(shown),
                "anomaly_count": flagged_count,
                "anomalies": [],
                "children": [],
                "activity_items": [_activity_item_model(item) for item in shown],
            }
        )

    filtered: dict[str, Any] = {
        "rank_by": rank_by,
        "order": order,
        "limit": limit,
        "kind": kind,
    }
    return _assemble_report(
        scope="self",
        window_days=window_days,
        group_by="activity_kind",
        rank_by=rank_by,
        order=order,
        total_users=1,
        group_nodes=group_nodes,
        filtered=filtered,
        root_metrics={"total_users": 1, "shown_items": len(items)},
    )


def build_activity_tree_catalog() -> dict[str, Any]:
    """Introspection payload for `/meta/scoring-catalog`."""
    return {
        "catalog_version": "activity_tree_v1",
        "group_axes": [
            {"axis": axis, "label": info["label"], "metric_field": info["metric_field"]}
            for axis, info in ACTIVITY_TREE_GROUP_AXES.items()
        ],
        "rank_by_options": [
            {"rank_by": key, "label": label} for key, label in ACTIVITY_TREE_RANK_OPTIONS.items()
        ],
        "item_rank_options": ["recency", "score"],
        "filters": {
            "min_loyalty": "numeric 0-100 - only keep members at/above the loyalty floor",
            "max_loyalty": "numeric 0-100 - only keep members at/below the loyalty ceiling",
            "churn_risk": "low|medium|high - only keep members in this churn bucket",
            "sentiment": "positive|negative|neutral|none - match the latest sentiment label",
            "q": "free-text match on username / top issue / journey family",
            "anomalies_only": "bool - prune members and groups without anomalies",
            "with_activities": "bool - expand members with recent activity items",
            "activity_kind": "chat|booking - restrict leaf activities to one kind",
        },
        "anomaly_rules_absolute": [_json_safe(rule) for rule in ACTIVITY_TREE_ANOMALY_RULES],
        "anomaly_rules_relative": [
            _json_safe(rule) for rule in ACTIVITY_TREE_RELATIVE_ANOMALY_RULES
        ],
        "negative_keywords": list(TREE_NEGATIVE_KEYWORDS),
    }