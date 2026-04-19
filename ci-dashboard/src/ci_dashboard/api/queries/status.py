from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.api.queries.base import isoformat_utc, utcnow


def get_freshness(engine: Engine) -> dict[str, object]:
    generated_at = utcnow()
    jobs: list[dict[str, object]] = []

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT job_name, last_status, last_succeeded_at
                FROM ci_job_state
                ORDER BY job_name
                """
            )
        ).mappings()

        for row in rows:
            last_succeeded_at = _parse_datetime(row["last_succeeded_at"])
            lag_minutes = None
            if last_succeeded_at is not None:
                lag_seconds = max((generated_at - last_succeeded_at).total_seconds(), 0)
                lag_minutes = int(lag_seconds // 60)
            jobs.append(
                {
                    "job_name": row["job_name"],
                    "last_status": row["last_status"],
                    "last_succeeded_at": isoformat_utc(last_succeeded_at),
                    "lag_minutes": lag_minutes,
                }
            )

    return {
        "jobs": jobs,
        "generated_at": isoformat_utc(generated_at),
    }


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None
