"""V3 clock, source-window and fail-closed admission rules."""

import math
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
ACCOUNT_ID = "100050658403"
GUARD_KEY = f"utilization:tencent:{ACCOUNT_ID}"
GUARD_VERSION = 3
RETRY_INTERVAL = timedelta(minutes=5)
ROW_WARNING, ROW_LIMIT = 1_000_000, 2_000_000
BYTE_WARNING, BYTE_LIMIT = 4 * 1024**3, 8 * 1024**3
METRICS = ("cpu", "memory")
# This denylist is mirrored in the dashboard writer and reader. Operators must
# still choose stable business labels, not encode run identities in their values.
IDENTITY_KEYS = frozenset({
    "author", "owner", "email", "user", "username", "employee", "employee_id",
    "pod_uid", "pod_name", "build_id", "build_number", "run_id", "execution_id",
    "test_execution_id", "job_uid", "job_run_id",
})


def label_keys(keys):
    if (
        not isinstance(keys, list)
        or not 1 <= len(keys) <= 32
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,62}", key)
            or key.lower().replace("-", "_").replace(".", "_") in IDENTITY_KEYS
            for key in keys
        )
        or len(set(keys)) != len(keys)
    ):
        raise ValueError("explicit bounded non-personal label keys without instance or execution identifiers required")
    return keys


def utc_naive(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def day_start(value: date):
    return datetime.combine(value, time(), BEIJING).astimezone(UTC).replace(tzinfo=None)


def usage_date(value):
    return utc_naive(value).replace(tzinfo=UTC).astimezone(BEIJING).date()


def expires_at(day):
    day = usage_date(day) if isinstance(day, datetime) else day
    return day_start(day - timedelta(days=day.weekday()) + timedelta(days=14))


def workload_start_date(now):
    """First retained Beijing usage day (Monday of the previous week)."""
    day = usage_date(now)
    return day - timedelta(days=day.weekday() + 7)


def finite(value, *, nullable=False):
    if nullable and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("expected a finite nonnegative resource quantity")
    return float(value)


def validate_source_window(entry):
    horizon = finite(entry.get("verified_horizon_seconds"))
    margin = finite(entry.get("safety_margin_seconds", 0))
    if not 0 <= margin < horizon or entry.get("timestamp_alignment_verified") is not True:
        raise ValueError("unverified source horizon, margin or timestamp alignment")


def health(state, now, *, projected_rows=None):
    now = utc_naive(now)
    reasons, warnings = [], []
    if state.get("version") != GUARD_VERSION or not state.get("enabled"):
        reasons.append("disabled_or_unconfigured")
    if state.get("paused"):
        reasons.append("paused")
    for field in ("maintenance_at", "bytes_observed_at"):
        value = state.get(field)
        if value is None or not -60 <= (now - utc_naive(value)).total_seconds() <= 14400:
            reasons.append(field + "_stale")
    rows = state.get("retained_rows") if projected_rows is None else projected_rows
    if not state.get("counts_reliable") or not isinstance(rows, int) or rows < 0:
        reasons.append("counts_unreliable")
    elif rows >= ROW_LIMIT:
        reasons.append("row_budget")
    elif rows >= ROW_WARNING:
        warnings.append("row_budget")
    size = state.get("observed_bytes")
    if not isinstance(size, (int, float)) or not math.isfinite(size) or size < 0:
        reasons.append("bytes_unreliable")
    elif size >= BYTE_LIMIT:
        reasons.append("byte_budget")
    elif size >= BYTE_WARNING:
        warnings.append("byte_budget")
    return {"allowed": not reasons, "reasons": reasons, "warnings": warnings}
