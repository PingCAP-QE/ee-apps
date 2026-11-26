"""Read-only V3 machine observations and bounded, label-based workload statistics."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

VENDOR = "tencent"
ACCOUNT_ID = "100050658403"
STATE_KEY = f"utilization:{VENDOR}:{ACCOUNT_ID}"
BEIJING = ZoneInfo("Asia/Shanghai")
IDENTITY_KEYS = {
    "author", "owner", "email", "user", "username", "employee", "employeeid",
    "poduid", "podname", "buildid", "buildnumber", "runid", "executionid",
    "testexecutionid", "jobuid", "jobrunid",
}


def _label_key(key):
    return (isinstance(key, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,62}", key))
            and re.sub(r"[._-]", "", key.lower()) not in IDENTITY_KEYS)


METRICS = ("cpu", "memory")
MACHINE_FIELDS = ("percent_sum", "sample_minutes", "peak_percent")
WORKLOAD_FIELDS = ("used", "requested", "matched_pod_minutes")
WORKLOAD_STATS = ("expected_pod_minutes",) + tuple(
    f"{metric}_{field}" for metric in METRICS for field in WORKLOAD_FIELDS
)
WORKLOAD_ROW_LIMIT = 10_000


def is_enabled() -> bool:
    return os.environ.get("CI_DASHBOARD_UTILIZATION_ENABLED", "false").strip().lower() == "true"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _machine_window_start(today: date) -> date:
    return today - timedelta(days=364)


def _workload_window_start(today: date) -> date:
    return today - timedelta(days=today.weekday() + 7)


def date_range(
    start: date | None, end: date | None, *, kind: str = "machine", now: datetime | None = None
) -> tuple[date, date]:
    today = (now or utc_now()).astimezone(BEIJING).date()
    if (start is None) != (end is None):
        raise ValueError("start_date and end_date must be supplied together")
    if start is None:
        start, end = _workload_window_start(today), today
    if start < date(1970, 1, 1) or start > end or end > today:
        raise ValueError("dates must be ordered, between 1970-01-01 and today in Asia/Shanghai")
    if kind == "machine" and start < _machine_window_start(today):
        raise ValueError("machine dates must be within the last 365 Beijing days, including today")
    if kind == "workload" and start < _workload_window_start(today):
        raise ValueError("workloads retain only the current and previous Beijing week")
    return start, end


def _base() -> dict:
    return {
        "enabled": is_enabled(), "version": 3, "vendor": VENDOR,
        "account_id": ACCOUNT_ID, "timezone": "Asia/Shanghai",
    }


def unavailable(reason: str) -> dict:
    return {**_base(), "available": False, "configured": None, "reason": reason, "items": []}


def _failure(error: SQLAlchemyError) -> dict:
    missing = any(word in str(error).lower() for word in (
        "no such table", "doesn't exist", "unknown column", "no such column",
    ))
    return unavailable("missing_schema" if missing else "database_unavailable")


def _json(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None


def _iso(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC).isoformat() if parsed.tzinfo is None else parsed.isoformat()


def _state(connection) -> dict:
    value = _json(connection.execute(
        text("SELECT watermark_json FROM cost_job_state WHERE job_name=:key"),
        {"key": STATE_KEY},
    ).scalar_one_or_none())
    return value if isinstance(value, dict) else {}


def _configured(state: dict) -> bool:
    scope = state.get("scope")
    return bool(state.get("version") == 3 and isinstance(state.get("contracts"), dict)
                and state["contracts"] and isinstance(scope, dict)
                and scope.get("account_id") == ACCOUNT_ID)


def _pod_stop_at(state: dict) -> datetime | None:
    value = state.get("pod_stop_at")
    if not isinstance(value, str):
        return None
    try:
        stop = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stop if stop.utcoffset() == timedelta(0) else None


def _workload_history(connection, start: date, end: date) -> bool:
    return connection.execute(text(
        "SELECT 1 FROM workload_utilization_daily WHERE account_id=:account "
        "AND usage_date BETWEEN :start AND :end LIMIT 1"
    ), {"account": ACCOUNT_ID, "start": start.isoformat(), "end": end.isoformat()}).scalar() == 1


def _valid_number(value):
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid metric")
    return value


def _machine_stats(row, metric):
    total = _valid_number(row[f"{metric}_percent_sum"])
    count = _valid_number(row[f"{metric}_sample_minutes"])
    peak = row[f"{metric}_peak_percent"]
    if peak is not None:
        _valid_number(peak)
    if (peak is None) != (count == 0) or (not count and total):
        raise ValueError("invalid machine observation")
    return total, count, peak


def _machine_metric(total, count, peak):
    return {"avg": total / count if count else None, "peak": peak, "sample_minutes": count}


def _workload_metrics(stats):
    expected = stats["expected_pod_minutes"]
    result = {}
    for metric in METRICS:
        used, requested, matched = (stats[f"{metric}_{field}"] for field in WORKLOAD_FIELDS)
        result[metric] = {
            "avg": _valid_number(100 * (used / requested)) if requested else None,
            "coverage": matched / expected if expected else None,
        }
    return result


def _machine_interval_summary(rows, max_days: int) -> dict | None:
    if not rows:
        return None
    keys = ("region", "cluster_id", "machine_id")
    identity = {key: rows[0][key] for key in keys}
    if len(rows) > max_days or any(
        row[key] != identity[key] for row in rows for key in keys
    ):
        return {"reason": "ambiguous_machine"}
    try:
        metrics = {}
        for metric in METRICS:
            totals, counts, peaks = zip(*(_machine_stats(row, metric) for row in rows), strict=True)
            metrics[metric] = _machine_metric(
                math.fsum(totals), sum(counts), max((peak for peak in peaks if peak is not None), default=None),
            )
    except (ValueError, TypeError, OverflowError):
        return {"reason": "invalid_data"}
    return {**identity, "account_id": ACCOUNT_ID, "days_with_facts": len(rows), "metrics": metrics}


def _workload_partial(connection, start: date, end: date, now: datetime, stop: datetime | None) -> bool:
    def utc_day(day):
        return datetime.combine(day, datetime.min.time(), BEIJING).astimezone(UTC)

    range_start = utc_day(start)
    range_end = min(utc_day(end + timedelta(days=1)), now, stop or now).replace(second=0, microsecond=0)
    if range_end <= range_start:
        return False
    one_minute = timedelta(minutes=1)
    # Match collection's ceil(planned_until) / floor(observed_until) interval.
    # A tail must contain at least one whole confirmed minute in the selected range.
    if connection.dialect.name == "sqlite":
        last_confirmed_minute = "datetime(substr(observed_until,1,16),'-1 minute') || '.000000'"
    else:
        last_confirmed_minute = "TIMESTAMPADD(MINUTE,-1,DATE_FORMAT(observed_until,'%Y-%m-%d %H:%i:00'))"
    # Registered tasks are whole-minute, single-day windows; a valid pre-stop
    # task must be wholly in bounds. Unplanned tails can be clipped at the stop.
    task_bounds = ("window_start>=:range_start AND window_end<=:range_end" if stop is not None
                   else "window_start<:range_end AND window_end>:range_start")
    # Account/date-level evidence also applies conservatively to label filters.
    query = text(f"""
        SELECT EXISTS (
            SELECT 1 FROM utilization_task WHERE account_id=:account AND kind='pod'
            AND status IN ('pending','running','retry') AND {task_bounds}
        ) OR EXISTS (
            SELECT 1 FROM pod_utilization_context WHERE account_id=:account
            AND planned_until<=:last_start AND observed_until>=:first_end
            AND planned_until<=({last_confirmed_minute})
        )
    """)
    bounds = {"range_start": range_start, "range_end": range_end,
              "last_start": range_end - one_minute, "first_end": range_start + one_minute}
    query = query.bindparams(*(bindparam(key, type_=DateTime) for key in bounds))
    return bool(connection.execute(query, {"account": ACCOUNT_ID, **{
        key: value.replace(tzinfo=None) for key, value in bounds.items()
    }}).scalar_one())


def get_daily(
    engine: Engine | None, *, start_date: date | None = None, end_date: date | None = None,
    kind: str = "machine", machine_id: str | None = None, region: str | None = None,
    cluster_id: str | None = None, resource_class: str | None = None, label_key: str | None = None,
    label_value: str | None = None, group_by: str | None = None,
    page: int = 1, page_size: int = 50, now: datetime | None = None,
) -> dict:
    if kind not in {"machine", "workload"} or not 1 <= page <= 1000 or not 1 <= page_size <= 100:
        raise ValueError("invalid kind or pagination (page 1..1000, page_size 1..100)")
    now = (now or utc_now()).astimezone(UTC)
    start, end = date_range(start_date, end_date, kind=kind, now=now)
    if label_key is not None and not _label_key(label_key):
        raise ValueError("unsupported business label key")
    if group_by is not None and not _label_key(group_by):
        raise ValueError("unsupported business group_by label")
    if (label_key is None) != (label_value is None):
        raise ValueError("label_key and label_value must be supplied together")
    if kind == "machine" and any(value is not None for value in (label_key, label_value, group_by)):
        raise ValueError("business labels apply only to workloads")
    machine_filters = {"machine_id": machine_id, "region": region, "cluster_id": cluster_id,
                       "resource_class": resource_class}
    if kind == "workload" and any(value is not None for value in machine_filters.values()):
        raise ValueError("machine filters apply only to machines")
    if resource_class is not None and resource_class not in {"cvm", "tke_native"}:
        raise ValueError("unsupported machine resource class")
    if any(value is not None and len(value) > 512 for value in (machine_id, region, cluster_id, label_value)):
        raise ValueError("filter exceeds 512 characters")
    if not is_enabled():
        return unavailable("disabled")
    if engine is None:
        return unavailable("database_unavailable")
    params = {
        "account": ACCOUNT_ID, "start": start.isoformat(), "end": end.isoformat(),
        "limit": page_size + 1 if kind == "machine" else WORKLOAD_ROW_LIMIT + 1,
        "offset": (page - 1) * page_size,
    }
    clauses = ["account_id=:account", "usage_date BETWEEN :start AND :end"]
    for key, value in machine_filters.items():
        if value is not None:
            clauses.append(f"{key}=:{key}")  # Column names are fixed, never user input.
            params[key] = value
    if kind == "workload":
        if label_key is not None:
            extract = "JSON_EXTRACT(labels_json,:label_path)"
            if engine.dialect.name != "sqlite":
                extract = f"JSON_UNQUOTE({extract})"
            clauses.append(f"{extract}=:label_value")
            params.update(label_path=f'$."{label_key}"', label_value=label_value)
        columns = "labels_json," + ",".join(WORKLOAD_STATS)
        table, order = "workload_utilization_daily", "usage_date,group_key"
    else:
        columns = "usage_date,region,cluster_id,machine_id,resource_class," + ",".join(
            f"{metric}_{field}" for metric in METRICS for field in MACHINE_FIELDS
        )
        table, order = "machine_utilization_daily", "usage_date DESC,region,cluster_id,machine_id"
    select = text(
        f"SELECT {columns} FROM {table} WHERE " + " AND ".join(clauses)
        + f" ORDER BY {order} LIMIT :limit"
        + (" OFFSET :offset" if kind == "machine" else "")
    )
    interval_rows, partial = None, False
    max_days = (end - start).days + 1
    try:
        with engine.connect() as connection:
            state = _state(connection)
            if not _configured(state):
                return {**unavailable("unconfigured"), "configured": False}
            if kind == "workload":
                pod_enabled = state.get("pod_collection_enabled") is True
                stop = None if pod_enabled else _pod_stop_at(state)
                if not pod_enabled:
                    if stop is None:
                        return {**unavailable("pod_collection_disabled"), "configured": True}
                    # Check before reading facts so concurrent settlement can
                    # leave a conservative warning, not hide the workload.
                    partial = _workload_partial(connection, start, end, now, stop)
                    if not partial:
                        today = now.astimezone(BEIJING).date()
                        retained_start = _workload_window_start(today)
                        # Other dates can keep workloads available without
                        # marking the selected range partial.
                        draining_elsewhere = (start, end) != (retained_start, today) and _workload_partial(
                            connection, retained_start, today, now, stop,
                        )
                        if not draining_elsewhere and not _workload_history(connection, retained_start, today):
                            return {**unavailable("pod_collection_disabled"), "configured": True}
                approved = state["scope"].get("labels") or []
                if any(key is not None and key not in approved for key in (label_key, group_by)):
                    raise ValueError("business label is not enabled in the collection scope")
                if pod_enabled:
                    partial = _workload_partial(connection, start, end, now, None)
            if kind == "machine" and machine_id:
                # A physical identity has at most one fact per day. Read one extra to detect
                # overflow; reuse the same snapshot for day detail whenever a summary is possible.
                interval_rows = connection.execute(
                    select, {**params, "limit": max_days + 1, "offset": 0},
                ).mappings().all()
            if interval_rows is not None and len(interval_rows) <= max_days:
                offset = params["offset"]
                rows = interval_rows[offset:offset + params["limit"]]
            else:
                rows = connection.execute(select, params).mappings().all()
    except SQLAlchemyError as error:
        return _failure(error)
    if kind == "workload" and len(rows) > WORKLOAD_ROW_LIMIT:
        return {**unavailable("query_too_large"), "row_limit": WORKLOAD_ROW_LIMIT}
    try:
        if kind == "machine":
            items = []
            for row in rows[:page_size]:
                item = {key: row[key] for key in (
                    "region", "cluster_id", "machine_id", "resource_class",
                )}
                item.update(account_id=ACCOUNT_ID, usage_date=str(row["usage_date"])[:10], metrics={
                    metric: _machine_metric(*_machine_stats(row, metric)) for metric in METRICS
                })
                items.append(item)
            has_more = len(rows) > page_size
        else:
            # ponytail: <=10,000 selected label-set rows; fail closed above, no rollup tables.
            groups = {}
            for row in rows:
                labels = _json(row["labels_json"])
                if not isinstance(labels, dict):
                    raise ValueError("invalid stored labels")
                value = labels.get(group_by) if group_by else None
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid stored label value")
                expected = row["expected_pod_minutes"]
                if any(row[f"{metric}_matched_pod_minutes"] > expected for metric in METRICS):
                    raise ValueError("matched Pod minutes exceed expected Pod minutes")
                group = groups.setdefault(value, {})
                for key in WORKLOAD_STATS:
                    group[key] = _valid_number(group.get(key, 0) + _valid_number(row[key]))
            ordered = sorted(groups, key=lambda value: (value is not None, value or ""))
            summaries = [{"group_value": value, "metrics": _workload_metrics(groups[value])} for value in ordered]
            offset = params["offset"]
            items = summaries[offset:offset + page_size]
            has_more = len(summaries) > offset + page_size
    except (ValueError, TypeError, OverflowError):
        return unavailable("invalid_data")
    interval_summary = _machine_interval_summary(interval_rows, max_days)
    return {
        **_base(), "available": True, "configured": True, "kind": kind,
        **({"interval_summary": interval_summary} if interval_summary is not None else {}),
        **({"partial": partial} if kind == "workload" else {}),
        "start_date": start.isoformat(), "end_date": end.isoformat(), "group_by": group_by,
        "page": page, "page_size": page_size, "has_more": has_more, "items": items,
        "cost": {"available": False, "reason": "precise_job_cost_not_supported"},
    }


def get_weekly(_engine: Engine | None = None) -> dict:
    return {**unavailable("not_supported"),
            "message": "Weekly summaries were retired; query machine daily facts for up to 365 days."}


def get_status(engine: Engine | None, *, now: datetime | None = None) -> dict:
    if not is_enabled():
        return unavailable("disabled")
    if engine is None:
        return unavailable("database_unavailable")
    now = (now or utc_now()).astimezone(UTC)
    start, end = date_range(None, None, now=now)
    params = {"account": ACCOUNT_ID, "now": now.replace(tzinfo=None),
              "first": _machine_window_start(end).isoformat(), "end": end.isoformat()}
    try:
        with engine.connect() as connection:
            state = _state(connection)
            latest_machine_usage_date = connection.execute(text(
                "SELECT MAX(usage_date) FROM machine_utilization_daily WHERE account_id=:account "
                "AND usage_date BETWEEN :first AND :end"
            ), params).scalar()
            if not _configured(state):
                return {**_base(), "available": True, "configured": False, "reason": "unconfigured"}
            pod_enabled = state.get("pod_collection_enabled") is True
            # The stop marker outlives the drained Pod contract.
            stop = None if pod_enabled else _pod_stop_at(state)
            workload_draining = stop is not None and _workload_partial(connection, start, end, now, stop)
            workload_history = (pod_enabled or stop is not None) and _workload_history(connection, start, end)
            active_contexts = connection.execute(text(
                "SELECT COUNT(*) FROM pod_utilization_context WHERE account_id=:account "
                "AND closed_at IS NULL AND expires_at>:now"
            ), params).scalar() if pod_enabled else None
            backlog = dict(connection.execute(text(
                "SELECT status,COUNT(*) FROM utilization_task WHERE account_id=:account "
                "AND status IN ('pending','running','retry') AND deadline_at>:now "
                + ("" if pod_enabled or workload_draining else "AND kind='machine' ")
                + "GROUP BY status"
            ), params).all())
    except SQLAlchemyError as error:
        return _failure(error)
    timestamps = {}
    for field in ("maintenance_at", "bytes_observed_at"):
        try:
            timestamps[field] = _iso(state.get(field))
        except ValueError:
            timestamps[field] = None
    health_reasons = []
    for field, stamp in timestamps.items():
        if stamp is None or not -60 <= (now - datetime.fromisoformat(stamp)).total_seconds() <= 14400:
            health_reasons.append(f"{field}_stale")
    rows, size = state.get("retained_rows"), state.get("observed_bytes")
    if state.get("counts_reliable") is not True or not isinstance(rows, int) or rows < 0:
        health_reasons.append("counts_unreliable")
    elif rows >= 2_000_000:
        health_reasons.append("row_budget")
    if not isinstance(size, (int, float)) or not math.isfinite(size) or size < 0:
        health_reasons.append("bytes_unreliable")
    elif size >= 8 * 1024**3:
        health_reasons.append("byte_budget")
    if not state.get("enabled"):
        health_reasons.append("collection_disabled")
    if state.get("paused"):
        health_reasons.append("paused")
    maintenance_stale = "maintenance_at_stale" in health_reasons
    healthy = not health_reasons
    default_window = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    result = {
        **_base(), "available": True, "configured": True,
        "reason": None if healthy else "unhealthy", "healthy": healthy, "health_reasons": health_reasons,
        "collection_enabled": state.get("enabled"),
        "paused": state.get("paused"), "pause_reasons": state.get("pause_reasons", []),
        "maintenance_at": timestamps["maintenance_at"], "maintenance_stale": maintenance_stale,
        "counts_reliable": state.get("counts_reliable") is True,
        "latest_machine_usage_date": str(latest_machine_usage_date)[:10] if latest_machine_usage_date else None,
        "pod_collection_enabled": pod_enabled, "pod_collection_stopped": stop is not None,
        "workload_draining": workload_draining, "workload_history_available": workload_history,
        "task_backlog": {key: backlog.get(key, 0) for key in ("pending", "running", "retry")},
        "machine_window": {"start_date": params["first"], "end_date": end.isoformat()},
        "default_window": default_window,
    }
    if pod_enabled:
        result["active_contexts"] = active_contexts
    if pod_enabled or workload_draining or workload_history:
        approved = state["scope"].get("labels") or []
        result.update(workload_window=default_window, label_keys=[key for key in approved[:32] if _label_key(key)])
    return result
