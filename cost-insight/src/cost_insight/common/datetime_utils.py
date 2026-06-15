from __future__ import annotations

from datetime import datetime


def coerce_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return coerce_datetime(value)


def coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Unsupported datetime value: {value!r}")
