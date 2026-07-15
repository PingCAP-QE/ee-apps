from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any


def nullable_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def normalize_vendor_tags_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed = json.loads(text)
    elif isinstance(value, dict):
        parsed = value
    else:
        raise ValueError(f"Unsupported vendor_tags_json value: {value!r}")
    if not isinstance(parsed, dict):
        raise ValueError(f"vendor_tags_json must be a JSON object: {value!r}")

    normalized: dict[str, str] = {}
    for key in ("cluster", "shared_pool"):
        normalized_value = nullable_text(parsed.get(key))
        if normalized_value is not None:
            normalized[key] = normalized_value
    for key, raw_value in parsed.items():
        if key in normalized:
            continue
        normalized_value = nullable_text(raw_value)
        if normalized_value is not None:
            normalized[str(key)] = normalized_value

    if not normalized:
        return None
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ValueError(f"Unsupported date value: {value!r}")


def coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_naive_utc(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return to_naive_utc(parsed)
    raise ValueError(f"Unsupported datetime value: {value!r}")


def to_naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def hash_value(value: Any) -> str | int | float | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def bind_decimal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()}
        for row in rows
    ]
