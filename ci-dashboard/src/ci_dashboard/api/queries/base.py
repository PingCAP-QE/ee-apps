from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.engine import Connection


FAILURE_LIKE_STATES = ("failure", "error", "timeout", "timed_out", "aborted")
SUCCESS_STATES = ("success", "pass")
SUPPORTED_GRANULARITIES = {"day", "week"}
MAX_RANKING_LIMIT = 50


@dataclass(frozen=True)
class CommonFilters:
    repo: str | None = None
    branch: str | None = None
    job_name: str | None = None
    cloud_phase: str | None = None
    issue_status: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    granularity: str = "day"

    def meta(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "branch": self.branch,
            "job_name": self.job_name,
            "cloud_phase": self.cloud_phase,
            "issue_status": self.issue_status,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "granularity": self.granularity,
        }

    def without_issue_status(self) -> "CommonFilters":
        return CommonFilters(
            repo=self.repo,
            branch=self.branch,
            job_name=self.job_name,
            cloud_phase=self.cloud_phase,
            issue_status=None,
            start_date=self.start_date,
            end_date=self.end_date,
            granularity=self.granularity,
        )

    def without_cloud_phase(self) -> "CommonFilters":
        return CommonFilters(
            repo=self.repo,
            branch=self.branch,
            job_name=self.job_name,
            cloud_phase=None,
            issue_status=self.issue_status,
            start_date=self.start_date,
            end_date=self.end_date,
            granularity=self.granularity,
        )

    def without_repo(self) -> "CommonFilters":
        return CommonFilters(
            repo=None,
            branch=self.branch,
            job_name=self.job_name,
            cloud_phase=self.cloud_phase,
            issue_status=self.issue_status,
            start_date=self.start_date,
            end_date=self.end_date,
            granularity=self.granularity,
        )


def build_common_where(
    filters: CommonFilters,
    *,
    table_alias: str = "",
) -> tuple[str, dict[str, Any]]:
    prefix = f"{table_alias}." if table_alias else ""
    conditions = ["1=1"]
    params: dict[str, Any] = {}

    if filters.repo:
        conditions.append(f"{prefix}repo_full_name = :repo")
        params["repo"] = filters.repo

    if filters.branch:
        conditions.append(f"{branch_expr(table_alias)} = :branch")
        params["branch"] = filters.branch

    if filters.job_name:
        conditions.append(f"{prefix}job_name = :job_name")
        params["job_name"] = filters.job_name

    if filters.cloud_phase:
        conditions.append(f"{prefix}cloud_phase = :cloud_phase")
        params["cloud_phase"] = filters.cloud_phase

    if filters.start_date:
        conditions.append(f"{prefix}start_time >= :start_time_from")
        params["start_time_from"] = datetime.combine(filters.start_date, time.min)

    if filters.end_date:
        conditions.append(f"{prefix}start_time < :start_time_to")
        params["start_time_to"] = datetime.combine(filters.end_date + timedelta(days=1), time.min)

    return " AND ".join(conditions), params


def branch_expr(table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    return f"COALESCE(NULLIF({prefix}target_branch, ''), {prefix}base_ref)"


def bucket_expr(connection: Connection, column_name: str, granularity: str) -> str:
    if granularity == "day":
        return f"DATE({column_name})"

    if connection.dialect.name == "sqlite":
        return (
            "DATE("
            f"{column_name}, "
            "'-' || ((CAST(strftime('%w', "
            f"{column_name}"
            ") AS integer) + 6) % 7) || ' days'"
            ")"
        )

    return f"DATE_SUB(DATE({column_name}), INTERVAL WEEKDAY({column_name}) DAY)"


def filter_complete_week_rows(
    rows: list[dict[str, Any]],
    *,
    start_date: date | None,
    end_date: date | None,
    bucket_key: str = "bucket_start",
) -> list[dict[str, Any]]:
    if start_date is None or end_date is None:
        return rows

    first_complete_week_start, last_complete_week_start = complete_week_bounds(
        start_date,
        end_date,
    )
    if first_complete_week_start is None or last_complete_week_start is None:
        return []

    filtered_rows: list[dict[str, Any]] = []
    for row in rows:
        bucket_start = _coerce_bucket_date(row.get(bucket_key))
        if bucket_start is None:
            continue
        if first_complete_week_start <= bucket_start <= last_complete_week_start:
            filtered_rows.append(row)
    return filtered_rows


def complete_week_bounds(
    start_date: date,
    end_date: date,
) -> tuple[date | None, date | None]:
    first_complete_week_start = start_date + timedelta(
        days=(7 - start_date.weekday()) % 7
    )
    last_complete_week_start = end_date - timedelta(days=end_date.weekday())
    if end_date.weekday() != 6:
        last_complete_week_start -= timedelta(days=7)

    if first_complete_week_start > last_complete_week_start:
        return None, None
    return first_complete_week_start, last_complete_week_start


def _coerce_bucket_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if len(raw) >= 10:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                return None
    return None


def failure_like_expr(table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    states = ", ".join(f"'{state}'" for state in FAILURE_LIKE_STATES)
    return f"LOWER({prefix}state) IN ({states})"


def success_expr(table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    states = ", ".join(f"'{state}'" for state in SUCCESS_STATES)
    return f"LOWER({prefix}state) IN ({states})"


def to_number(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        if value == value.to_integral():
            return int(value)
        return float(value)
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return round(value, 2)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None


def rate_pct(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) * 100.0 / float(denominator), 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
