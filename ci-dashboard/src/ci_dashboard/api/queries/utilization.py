"""Read-only utilization serving; scopes are stored summaries, never query-time rollups."""

from __future__ import annotations

import json
import os
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

VENDOR = "tencent"
ACCOUNT_ID = "100050658403"
STATE_KEY = f"utilization:{VENDOR}:{ACCOUNT_ID}"
BEIJING = ZoneInfo("Asia/Shanghai")
METRICS = {"cpu", "memory", "cpu_allocation", "memory_allocation"}
SCOPES = {"resource", "job", "repo", "account_class"}


def is_enabled() -> bool:
    return os.environ.get("CI_DASHBOARD_UTILIZATION_ENABLED", "false").strip().lower() == "true"


def utc_now() -> datetime:
    return datetime.now(UTC)


def date_range(
    start: date | None, end: date | None, *, weekly: bool = False, now: datetime | None = None
) -> tuple[date, date]:
    today = (now or utc_now()).astimezone(BEIJING).date()
    monday = today - timedelta(days=today.weekday())
    if (start is None) != (end is None):
        raise ValueError("start_date and end_date must be supplied together")
    if start is None:
        return (monday - timedelta(weeks=51) if weekly else monday - timedelta(days=7), today)
    if start < date(1970, 1, 1) or start > end or end > today:
        raise ValueError("dates must be ordered, between 1970-01-01 and today in Asia/Shanghai")
    if weekly:
        anniversary = date(
            start.year + 1, start.month, min(start.day, monthrange(start.year + 1, start.month)[1])
        )
        if end >= anniversary:
            raise ValueError("weekly range must be shorter than one calendar year")
    elif start < monday - timedelta(days=7):
        raise ValueError(
            "daily details are retained only for the current and previous Beijing week"
        )
    return start, end


def _base() -> dict:
    return {
        "enabled": is_enabled(),
        "vendor": VENDOR,
        "account_id": ACCOUNT_ID,
        "timezone": "Asia/Shanghai",
    }


def unavailable(reason: str) -> dict:
    return {**_base(), "available": False, "configured": None, "reason": reason, "items": []}


def _failure(error: SQLAlchemyError) -> dict:
    message = str(error).lower()
    missing = any(
        word in message
        for word in ("no such table", "doesn't exist", "unknown column", "no such column")
    )
    return unavailable("missing_schema" if missing else "database_unavailable")


def _json(value) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, dict) else {}


def _iso(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC).isoformat() if parsed.tzinfo is None else parsed.isoformat()


def _params(start: date, end: date) -> dict:
    return {
        "vendor": VENDOR,
        "account": ACCOUNT_ID,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_week": (start - timedelta(days=start.weekday())).isoformat(),
        "end_week": (end - timedelta(days=end.weekday())).isoformat(),
    }


def get_daily(
    engine: Engine | None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    scope: str = "job",
    repo: str | None = None,
    job_name: str | None = None,
    resource_class: str | None = None,
    resource_spec: str | None = None,
    resource_key: str | None = None,
    page: int = 1,
    page_size: int = 50,
    now: datetime | None = None,
) -> dict:
    start, end = date_range(start_date, end_date, now=now)
    if scope not in SCOPES or not 1 <= page <= 100 or not 1 <= page_size <= 100:
        raise ValueError("invalid scope or pagination (page and page_size must be 1..100)")
    if (repo and scope not in {"repo", "job"}) or (job_name and scope != "job"):
        raise ValueError("repo/Job filters apply only to their stored consumption scope")
    if (resource_key and scope != "resource") or (
        resource_spec and scope not in {"resource", "job"}
    ):
        raise ValueError("resource filters require the resource or Job detail scope")
    if not is_enabled():
        return unavailable("disabled")
    if engine is None:
        return unavailable("database_unavailable")
    params = {
        **_params(start, end),
        "scope": scope,
        "now": (now or utc_now()).astimezone(UTC).replace(tzinfo=None),
        "limit": page_size + 1,
        "offset": (page - 1) * page_size,
    }
    clauses = [
        "vendor=:vendor",
        "account_id=:account",
        "week_start BETWEEN :start_week AND :end_week",
        "usage_date BETWEEN :start AND :end",
        "scope=:scope",
        "expires_at > :now",
    ]
    for key, value in {
        "repo": repo,
        "job_name": job_name,
        "resource_class": resource_class,
        "resource_spec": resource_spec,
        "resource_key": resource_key,
    }.items():
        if value:
            if len(value) > 512:
                raise ValueError("filter exceeds 512 characters")
            clauses.append(f"{key}=:{key}")
            params[key] = value
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT usage_date,scope,resource_key,resource_class,resource_spec,repo,job_name,"
                        "namespace,region,metrics_json,cutoff,evidence_cutoff,status,updated_at "
                        "FROM cost_utilization_daily WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY usage_date DESC,repo,job_name,resource_class,resource_spec,resource_key "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as error:
        return _failure(error)
    items = []
    for row in rows[:page_size]:
        item = dict(row)
        item["metrics"] = {
            key: value for key, value in _json(item.pop("metrics_json")).items() if key in METRICS
        }
        for field in ("usage_date", "cutoff", "evidence_cutoff", "updated_at"):
            item[field] = str(item[field])[:10] if field == "usage_date" else _iso(item[field])
        items.append(item)
    return {
        **_base(),
        "available": True,
        "scope": scope,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "page": page,
        "page_size": page_size,
        "has_more": len(rows) > page_size,
        "items": items,
        "cost": {"available": False, "reason": "precise_job_cost_not_supported"},
    }


def get_weekly(
    engine: Engine | None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    resource_class: str | None = None,
    metric: str | None = None,
    page: int = 1,
    page_size: int = 100,
    now: datetime | None = None,
) -> dict:
    start, end = date_range(start_date, end_date, weekly=True, now=now)
    if metric is not None and metric not in METRICS:
        raise ValueError("unsupported metric")
    if not 1 <= page <= 100 or not 1 <= page_size <= 100:
        raise ValueError("page and page_size must be 1..100")
    if resource_class and len(resource_class) > 512:
        raise ValueError("filter exceeds 512 characters")
    if not is_enabled():
        return unavailable("disabled")
    if engine is None:
        return unavailable("database_unavailable")
    params = {**_params(start, end), "limit": page_size + 1, "offset": (page - 1) * page_size}
    clauses = [
        "vendor=:vendor",
        "account_id=:account",
        "week_start BETWEEN :start AND :end",
        "metric IN ('cpu','memory','cpu_allocation','memory_allocation')",
    ]
    for key, value in {"resource_class": resource_class, "metric": metric}.items():
        if value:
            clauses.append(f"{key}=:{key}")
            params[key] = value
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT week_start,resource_class,metric,contract_json,values_json,status,"
                        "quality_checkpoint_at,finalized_at,updated_at FROM cost_utilization_weekly WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY week_start DESC,resource_class,metric "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as error:
        return _failure(error)
    items = []
    for row in rows[:page_size]:
        item = dict(row)
        item["contract"] = _json(item.pop("contract_json"))
        item["values"] = _json(item.pop("values_json"))
        item["week_start"] = str(item["week_start"])[:10]
        for field in ("quality_checkpoint_at", "finalized_at", "updated_at"):
            item[field] = _iso(item[field])
        items.append(item)
    return {
        **_base(),
        "available": True,
        "scope": "account_class",
        "items": items,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "page": page,
        "page_size": page_size,
        "has_more": len(rows) > page_size,
    }


def get_status(engine: Engine | None, *, now: datetime | None = None) -> dict:
    if not is_enabled():
        return unavailable("disabled")
    if engine is None:
        return unavailable("database_unavailable")
    now = (now or utc_now()).astimezone(UTC)
    start, end = date_range(None, None, now=now)
    params = {
        **_params(start, end),
        "key": STATE_KEY,
        "now": now.replace(tzinfo=None),
        "weekly_start": (end - timedelta(days=364)).isoformat(),
    }
    try:
        with engine.connect() as connection:
            state_row = connection.execute(
                text("SELECT watermark_json FROM cost_job_state WHERE job_name=:key"), params
            ).scalar_one_or_none()
            # Probe both migrations, including weekly, even if there are no daily rows yet.
            weekly = (
                connection.execute(
                    text(
                        "SELECT week_start,resource_class,metric,status FROM cost_utilization_weekly "
                        "WHERE vendor=:vendor AND account_id=:account "
                        "AND week_start BETWEEN :weekly_start AND :end "
                        "AND metric IN ('cpu','memory','cpu_allocation','memory_allocation') "
                        "AND status IN ('final_missing','final_incomplete') "
                        "ORDER BY week_start DESC,resource_class,metric LIMIT 100"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            daily = (
                connection.execute(
                    text(
                        "SELECT usage_date,scope,status,MAX(cutoff) AS cutoff,COUNT(*) AS row_count,"
                        "SUM(CASE WHEN repo='unknown_repo' THEN 1 ELSE 0 END) AS unknown_repo_rows,"
                        "SUM(CASE WHEN job_name='unknown_job' THEN 1 ELSE 0 END) AS unknown_job_rows "
                        "FROM cost_utilization_daily WHERE vendor=:vendor AND account_id=:account "
                        "AND week_start BETWEEN :start_week AND :end_week "
                        "AND usage_date BETWEEN :start AND :end AND expires_at>:now "
                        "GROUP BY usage_date,scope,status ORDER BY usage_date,scope,status LIMIT 200"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as error:
        return _failure(error)
    state = _json(state_row)
    cutoff = max((_iso(row["cutoff"]) for row in daily if row["cutoff"]), default=None)
    maintenance = _iso(state.get("maintenance_at"))
    configured = bool(state.get("collection_start_week") and state.get("contracts"))
    quality = [
        {**dict(row), "usage_date": str(row["usage_date"])[:10], "cutoff": _iso(row["cutoff"])}
        for row in daily
    ]
    return {
        **_base(),
        "available": True,
        "configured": configured,
        "reason": None if configured else "unconfigured",
        "collection_enabled": state.get("enabled"),
        "collection_start_week": state.get("collection_start_week"),
        "collection_stop_week": state.get("collection_stop_week"),
        "paused": state.get("paused"),
        "pause_reasons": state.get("pause_reasons", []),
        "maintenance_at": maintenance,
        "maintenance_stale": maintenance is None
        or now - datetime.fromisoformat(maintenance) > timedelta(hours=4),
        "latest_cutoff": cutoff,
        "stale": cutoff is None or now - datetime.fromisoformat(cutoff) > timedelta(hours=8),
        "quality": quality,
        "collection_gaps": [dict(row) for row in weekly],
        "collection_gaps_limit": 100,
        "missing_daily_dates": _missing_dates(state, daily, start, end),
        "daily_window": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "quality_note": "Row counts describe stored scopes, not usage-weighted attribution coverage.",
    }


def _missing_dates(state: dict, daily: list, start: date, end: date) -> list[str] | None:
    if not state.get("collection_start_week"):
        return None
    first = max(start, date.fromisoformat(state["collection_start_week"]))
    last = end - timedelta(days=1)  # Today's in-progress collection is not a completed-day gap.
    if state.get("collection_stop_week"):
        last = min(last, date.fromisoformat(state["collection_stop_week"]) + timedelta(days=6))
    present = {str(row["usage_date"])[:10] for row in daily if row["scope"] == "account_class"}
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
        if (first + timedelta(days=offset)).isoformat() not in present
    ]
