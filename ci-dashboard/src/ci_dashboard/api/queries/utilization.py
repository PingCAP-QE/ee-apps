"""Read-only V2 machine facts and bounded, label-based workload statistics."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

VENDOR = "tencent"
ACCOUNT_ID = "100050658403"
STATE_KEY = f"utilization:{VENDOR}:{ACCOUNT_ID}"
BEIJING = ZoneInfo("Asia/Shanghai")
PERSONAL_KEYS = {"author", "owner", "email", "user", "username", "employee", "employee_id"}


def _label_key(key):
    return (isinstance(key, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,62}", key))
            and key.lower() not in PERSONAL_KEYS)
STAT_FIELDS = (
    "used", "capacity", "total_used", "expected_capacity", "expected_seconds",
    "observed_seconds", "usage_seconds", "unknown_seconds", "zero_seconds",
)
WORKLOAD_ROW_LIMIT = 10_000


def is_enabled() -> bool:
    return os.environ.get("CI_DASHBOARD_UTILIZATION_ENABLED", "false").strip().lower() == "true"


def utc_now() -> datetime:
    return datetime.now(UTC)


def date_range(
    start: date | None, end: date | None, *, kind: str = "machine", now: datetime | None = None
) -> tuple[date, date]:
    today = (now or utc_now()).astimezone(BEIJING).date()
    monday = today - timedelta(days=today.weekday())
    if (start is None) != (end is None):
        raise ValueError("start_date and end_date must be supplied together")
    if start is None:
        start, end = monday - timedelta(days=7), today
    if start < date(1970, 1, 1) or start > end or end > today:
        raise ValueError("dates must be ordered, between 1970-01-01 and today in Asia/Shanghai")
    if kind == "machine" and (end - start).days >= 365:
        raise ValueError("machine range must be at most 365 days inclusive")
    if kind == "workload" and start < monday - timedelta(days=7):
        raise ValueError("workloads retain only the current and previous Beijing week")
    return start, end


def _base() -> dict:
    return {
        "enabled": is_enabled(), "version": 2, "vendor": VENDOR,
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
    return bool(state.get("version") == 2 and isinstance(state.get("contracts"), dict)
                and state["contracts"] and isinstance(scope, dict)
                and scope.get("account_id") == ACCOUNT_ID)


def _metrics(row, *, peaks: bool = False) -> dict:
    result = {}
    for prefix in ("cpu", "memory"):
        metric = {field: row[f"{prefix}_{field}"] for field in STAT_FIELDS}
        for value in metric.values():
            if value is not None and (not math.isfinite(float(value)) or value < 0):
                raise ValueError("invalid stored metric")
        used, capacity = metric["used"], metric["capacity"]
        metric["avg"] = 100 * used / capacity if used is not None and capacity else None
        expected, observed = metric["expected_seconds"], metric["observed_seconds"]
        expected_capacity = metric["expected_capacity"]
        metric["coverage"] = (
            min(capacity / expected_capacity if expected_capacity > 0 else 1, observed / expected)
            if expected and observed is not None and capacity is not None
            and expected_capacity is not None else None
        )
        if peaks:
            peak = row[f"{prefix}_peak"]
            if peak is not None and (not math.isfinite(float(peak)) or peak < 0):
                raise ValueError("invalid stored peak")
            metric.update(peak=peak, peak_at=_iso(row[f"{prefix}_peak_at"]))
        # Overflow is not a valid JSON number, even if the individual inputs were finite.
        if any(value is not None and not math.isfinite(value)
               for value in (metric["avg"], metric["coverage"])):
            raise ValueError("invalid derived metric")
        result[prefix] = metric
    return result


STAT_COLUMNS = ",".join(f"{metric}_{field}" for metric in ("cpu", "memory") for field in STAT_FIELDS)


def get_daily(
    engine: Engine | None, *, start_date: date | None = None, end_date: date | None = None,
    kind: str = "machine", machine_id: str | None = None, region: str | None = None,
    resource_class: str | None = None, label_key: str | None = None,
    label_value: str | None = None, group_by: str | None = None,
    page: int = 1, page_size: int = 50, now: datetime | None = None,
) -> dict:
    if kind not in {"machine", "workload"} or not 1 <= page <= 1000 or not 1 <= page_size <= 100:
        raise ValueError("invalid kind or pagination (page 1..1000, page_size 1..100)")
    start, end = date_range(start_date, end_date, kind=kind, now=now)
    if label_key is not None and not _label_key(label_key):
        raise ValueError("unsupported business label key")
    if group_by is not None and not _label_key(group_by):
        raise ValueError("unsupported business group_by label")
    if (label_key is None) != (label_value is None):
        raise ValueError("label_key and label_value must be supplied together")
    if kind == "machine" and any(value is not None for value in (label_key, label_value, group_by)):
        raise ValueError("business labels apply only to workloads")
    if kind == "workload" and any(value is not None for value in (machine_id, region, resource_class)):
        raise ValueError("machine filters apply only to machines")
    if resource_class is not None and resource_class not in {"cvm", "tke_native"}:
        raise ValueError("unsupported machine resource class")
    if any(value is not None and len(value) > 512 for value in (machine_id, region, label_value)):
        raise ValueError("filter exceeds 512 characters")
    if not is_enabled():
        return unavailable("disabled")
    if engine is None:
        return unavailable("database_unavailable")
    params = {
        "account": ACCOUNT_ID, "start": start.isoformat(), "end": end.isoformat(),
        "now": (now or utc_now()).astimezone(UTC).replace(tzinfo=None),
        "limit": page_size + 1 if kind == "machine" else WORKLOAD_ROW_LIMIT + 1,
        "offset": (page - 1) * page_size,
    }
    clauses = ["account_id=:account", "usage_date BETWEEN :start AND :end"]
    for key, value in {"machine_id": machine_id, "region": region,
                       "resource_class": resource_class}.items():
        if value is not None:
            clauses.append(f"{key}=:{key}")  # Column names are fixed, never user input.
            params[key] = value
    if kind == "workload":
        clauses.append("expires_at>:now")
        if label_key is not None:
            extract = "JSON_EXTRACT(labels_json,:label_path)"
            if engine.dialect.name != "sqlite":
                extract = f"JSON_UNQUOTE({extract})"
            clauses.append(f"{extract}=:label_value")
            params.update(label_path=f'$."{label_key}"', label_value=label_value)
        columns = f"usage_date,labels_json,cutoff,updated_at,{STAT_COLUMNS}"
        table, order = "workload_utilization_daily", "usage_date,group_key"
    else:
        columns = (
            "usage_date,region,cluster_id,machine_id,resource_class,instance_type,cpu_cores,"
            f"memory_bytes,cutoff,updated_at,{STAT_COLUMNS},cpu_peak,cpu_peak_at,"
            "memory_peak,memory_peak_at"
        )
        table, order = "machine_utilization_daily", "usage_date DESC,region,cluster_id,machine_id"
    try:
        with engine.connect() as connection:
            state = _state(connection)
            if not _configured(state):
                return {**unavailable("unconfigured"), "configured": False}
            approved = state["scope"].get("labels") or []
            if any(key is not None and key not in approved for key in (label_key, group_by)):
                raise ValueError("business label is not enabled in the collection scope")
            rows = connection.execute(text(
                f"SELECT {columns} FROM {table} WHERE " + " AND ".join(clauses)
                + f" ORDER BY {order} LIMIT :limit"
                + (" OFFSET :offset" if kind == "machine" else "")
            ), params).mappings().all()
    except SQLAlchemyError as error:
        return _failure(error)
    if kind == "workload" and len(rows) > WORKLOAD_ROW_LIMIT:
        return {**unavailable("query_too_large"), "row_limit": WORKLOAD_ROW_LIMIT}
    try:
        if kind == "machine":
            items = []
            for row in rows[:page_size]:
                item = {key: row[key] for key in (
                    "region", "cluster_id", "machine_id", "resource_class", "instance_type",
                    "cpu_cores", "memory_bytes",
                )}
                item.update(account_id=ACCOUNT_ID, usage_date=str(row["usage_date"])[:10],
                            cutoff=_iso(row["cutoff"]),
                            updated_at=_iso(row["updated_at"]), metrics=_metrics(row, peaks=True))
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
                _metrics(row)  # Reject nonfinite/negative source statistics before summing.
                if value not in groups:
                    groups[value] = {
                        **{f"{metric}_{field}": row[f"{metric}_{field}"]
                           for metric in ("cpu", "memory") for field in STAT_FIELDS},
                        "group_value": value, "cutoff": _iso(row["cutoff"]),
                        "updated_at": _iso(row["updated_at"]),
                    }
                    continue
                group = groups[value]
                for metric in ("cpu", "memory"):
                    for field in STAT_FIELDS:
                        key = f"{metric}_{field}"
                        group[key] = (group[key] + row[key]
                                      if group[key] is not None and row[key] is not None else None)
                # An aggregate is only as fresh as its oldest contributing cutoff.
                for field in ("cutoff", "updated_at"):
                    stamp = _iso(row[field])
                    group[field] = min(group[field], stamp) if group[field] and stamp else None
            ordered = sorted(groups.values(), key=lambda group: (group["group_value"] is not None,
                                                                 group["group_value"] or ""))
            offset = params["offset"]
            items = [{"group_value": group["group_value"], "cutoff": group["cutoff"],
                      "updated_at": group["updated_at"], "metrics": _metrics(group)}
                     for group in ordered[offset:offset + page_size]]
            has_more = len(ordered) > offset + page_size
    except (ValueError, TypeError, OverflowError):
        return unavailable("invalid_data")
    return {
        **_base(), "available": True, "configured": True, "kind": kind,
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
              "first": (end - timedelta(days=364)).isoformat(), "end": end.isoformat()}
    try:
        with engine.connect() as connection:
            state = _state(connection)
            machine_cutoff = connection.execute(text(
                "SELECT MAX(cutoff) FROM machine_utilization_daily WHERE account_id=:account "
                "AND usage_date BETWEEN :first AND :end"
            ), params).scalar()
            workload_cutoff = connection.execute(text(
                "SELECT MAX(cutoff) FROM workload_utilization_daily "
                "WHERE account_id=:account AND expires_at>:now"
            ), params).scalar()
            active_contexts = connection.execute(text(
                "SELECT COUNT(*) FROM pod_utilization_context WHERE account_id=:account "
                "AND closed_at IS NULL AND expires_at>:now"
            ), params).scalar()
            backlog = dict(connection.execute(text(
                "SELECT status,COUNT(*) FROM utilization_task WHERE account_id=:account "
                "AND status IN ('pending','running','retry') AND deadline_at>:now GROUP BY status"
            ), params).all())
    except SQLAlchemyError as error:
        return _failure(error)
    configured = _configured(state)
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
    healthy = configured and not health_reasons
    cutoff = _iso(machine_cutoff)
    # Daily 06:30 Beijing planning scans D-4/D-3. Allow one hour for dispatch/collection,
    # then expect the end of D-3, not same-day data from the retired V1 refresh.
    local_now = now.astimezone(BEIJING)
    planner_due = local_now.replace(hour=7, minute=30, second=0, microsecond=0)
    planner_day = local_now.date() - timedelta(days=local_now < planner_due)
    expected_cutoff = datetime.combine(
        planner_day - timedelta(days=2), datetime.min.time(), tzinfo=BEIJING
    )
    approved = (state["scope"].get("labels") or []) if configured else []
    return {
        **_base(), "available": True, "configured": configured,
        "reason": "unconfigured" if not configured else None if healthy else "unhealthy",
        "healthy": healthy, "health_reasons": health_reasons,
        "collection_enabled": state.get("enabled") if configured else False,
        "paused": state.get("paused") if configured else None,
        "pause_reasons": state.get("pause_reasons", []) if configured else [],
        "maintenance_at": timestamps["maintenance_at"], "maintenance_stale": maintenance_stale,
        "counts_reliable": state.get("counts_reliable") is True,
        "latest_cutoff": cutoff, "workload_cutoff": _iso(workload_cutoff),
        "stale": cutoff is None or datetime.fromisoformat(cutoff) < expected_cutoff,
        "active_contexts": active_contexts,
        "task_backlog": {key: backlog.get(key, 0) for key in ("pending", "running", "retry")},
        "label_keys": [key for key in approved[:32] if _label_key(key)],
        "machine_window": {"start_date": params["first"], "end_date": end.isoformat()},
        "default_window": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "workload_window": {"start_date": start.isoformat(), "end_date": end.isoformat()},
    }
