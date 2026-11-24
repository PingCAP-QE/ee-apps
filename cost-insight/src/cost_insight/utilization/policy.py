"""Calendar, source contracts and fail-closed admission policy (no I/O)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
VENDOR = "tencent"
ACCOUNT_ID = "100050658403"
GUARD_KEY = f"utilization:{VENDOR}:{ACCOUNT_ID}"
ROW_WARNING = 1_000_000
ROW_LIMIT = 2_000_000
BYTE_WARNING = 4 * 1024**3
BYTE_LIMIT = 8 * 1024**3
METRICS = frozenset({"cpu", "memory", "cpu_allocation", "memory_allocation"})
CONTRACT_KEYS = frozenset(
    {
        "namespace",
        "metric",
        "period",
        "source_stat",
        "unit",
        "normalized_unit",
        "semantic_version",
        "denominator",
    }
)


def utc_naive(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def week_start(value: date | datetime) -> date:
    if isinstance(value, datetime):
        value = utc_naive(value).replace(tzinfo=UTC).astimezone(BEIJING).date()
    return value - timedelta(days=value.weekday())


def day_start(value: date) -> datetime:
    return datetime.combine(value, time(), BEIJING).astimezone(UTC).replace(tzinfo=None)


def expires_at(week: date) -> datetime:
    return day_start(week_start(week) + timedelta(days=14))


def expected_contract(
    contracts: dict, resource_class: str, metric: str, scope: str = "account_class"
) -> dict:
    entry = contracts[resource_class][metric]
    return entry.get("scope_contracts", {}).get(scope, entry["contract"])


def validate_contract(contract: dict, metric: str, scope: str) -> None:
    if metric not in METRICS or not CONTRACT_KEYS <= contract.keys():
        raise ValueError("incomplete metric contract")
    if contract["period"] != 60 or contract["source_stat"] != "avg":
        raise ValueError("only verified 60s avg metrics are supported")
    denominator = (
        "allocatable"
        if metric.endswith("_allocation")
        else "requests"
        if scope in {"job", "repo"}
        else "physical"
    )
    if contract["denominator"] != denominator:
        raise ValueError("denominator contract mismatch")
    unit = "cores" if metric.startswith("cpu") else "bytes"
    if contract["normalized_unit"] != unit:
        raise ValueError("normalized unit mismatch")
    if "source_scale" in contract:
        scale = contract["source_scale"]
        if (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(scale)
            or scale <= 0
        ):
            raise ValueError("source_scale must be finite and positive")


def validate_source_window(entry: dict) -> None:
    horizon = entry.get("verified_horizon_seconds")
    margin = entry.get("safety_margin_seconds", 0)
    if (
        isinstance(horizon, bool)
        or not isinstance(horizon, (int, float))
        or not math.isfinite(horizon)
        or horizon <= 0
        or isinstance(margin, bool)
        or not isinstance(margin, (int, float))
        or not math.isfinite(margin)
        or not 0 <= margin < horizon
    ):
        raise ValueError("invalid verified source horizon/safety margin")
    if entry.get("timestamp_alignment_verified") is not True:
        raise ValueError("source timestamp alignment is not verified")


def replay_allowed(usage_date: date, entry: dict, now: datetime) -> bool:
    """Require the *whole* requested day prefix, never a clipped/coarser replay."""
    try:
        validate_source_window(entry)
    except ValueError:
        return False
    horizon = entry["verified_horizon_seconds"]
    margin = entry.get("safety_margin_seconds", 0)
    now = utc_naive(now)
    return (
        day_start(usage_date) <= now < expires_at(week_start(usage_date))
        and (now - day_start(usage_date)).total_seconds() <= horizon - margin
    )


def writable_week(state: dict, week: date, now: datetime) -> bool:
    closed = state.get("finalized_through_week")
    stop = state.get("collection_stop_week")
    return (
        week in {week_start(now), week_start(now) - timedelta(days=7)}
        and week >= date.fromisoformat(state["collection_start_week"])
        and (not stop or week <= date.fromisoformat(stop))
        and (not closed or week > date.fromisoformat(closed))
        and now < expires_at(week)
    )


def health(state: dict, now: datetime, *, projected_rows: int | None = None) -> dict:
    """Both writers use these exact thresholds; unknown/stale stats fail closed."""
    warnings, reasons = [], []
    now = utc_naive(now)
    if state.get("version") != 1 or not state.get("enabled"):
        reasons.append("disabled_or_unconfigured")
    reconciled = state.get("reconciled_at")
    if not state.get("counts_reliable"):
        reasons.append("counts_unreliable")
    elif not reconciled or not -60 <= (now - utc_naive(reconciled)).total_seconds() <= 26 * 3600:
        reasons.append("counts_stale")
    maintenance = state.get("maintenance_at")
    if not maintenance:
        reasons.append("maintenance_uninitialized")
    else:
        age = (now - utc_naive(maintenance)).total_seconds()
        if age > 3600:
            warnings.append("maintenance_stale")
        if age > 14400 or age < -60:
            reasons.append("maintenance_stale")
    oldest = state.get("oldest_expired_at")
    if oldest:
        age = (now - utc_naive(oldest)).total_seconds()
        if age > 7200:
            warnings.append("expiry_overdue")
        if age > 14400:
            reasons.append("expiry_overdue")
    rows = state.get("retained_rows") if projected_rows is None else projected_rows
    if not isinstance(rows, int) or rows < 0:
        reasons.append("counts_unreliable")
    elif rows >= ROW_WARNING:
        warnings.append("row_budget")
        if rows >= ROW_LIMIT:
            reasons.append("row_budget")
    size = state.get("observed_bytes")
    measured = state.get("bytes_observed_at")
    if not isinstance(size, (int, float)) or size < 0 or not measured:
        reasons.append("bytes_unreliable")
    elif not -60 <= (now - utc_naive(measured)).total_seconds() <= 14400:
        reasons.append("bytes_stale")
    elif size >= BYTE_WARNING:
        warnings.append("byte_budget")
        if size >= BYTE_LIMIT:
            reasons.append("byte_budget")
    if state.get("paused"):
        reasons.append("paused")
    return {"allowed": not reasons, "reasons": reasons, "warnings": warnings}
