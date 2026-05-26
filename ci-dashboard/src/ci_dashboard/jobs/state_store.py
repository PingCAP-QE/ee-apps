from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ci_dashboard.common.models import JobState


_SELECT_JOB_STATE = text(
    """
    SELECT
      job_name,
      watermark_json,
      last_started_at,
      last_succeeded_at,
      last_status,
      last_error
    FROM ci_job_state
    WHERE job_name = :job_name
    """
)

def _parse_watermark(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return {}
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"Unsupported watermark_json payload: {value!r}")


def get_job_state(connection: Connection, job_name: str) -> JobState | None:
    row = connection.execute(_SELECT_JOB_STATE, {"job_name": job_name}).mappings().first()
    if row is None:
        return None
    return JobState(
        job_name=row["job_name"],
        watermark=_parse_watermark(row["watermark_json"]),
        last_started_at=_coerce_datetime(row["last_started_at"]),
        last_succeeded_at=_coerce_datetime(row["last_succeeded_at"]),
        last_status=str(row["last_status"]),
        last_error=row["last_error"],
    )


def mark_job_started(connection: Connection, job_name: str, watermark: dict[str, Any]) -> None:
    _upsert_job_state(
        connection,
        job_name=job_name,
        watermark=watermark,
        status="running",
        set_started=True,
        error=None,
    )


def save_job_progress(connection: Connection, job_name: str, watermark: dict[str, Any]) -> None:
    _upsert_job_state(
        connection,
        job_name=job_name,
        watermark=watermark,
        status="running",
        error=None,
    )


def mark_job_succeeded(connection: Connection, job_name: str, watermark: dict[str, Any]) -> None:
    _upsert_job_state(
        connection,
        job_name=job_name,
        watermark=watermark,
        status="succeeded",
        set_succeeded=True,
        error=None,
    )


def mark_job_failed(
    connection: Connection,
    job_name: str,
    watermark: dict[str, Any],
    error: str,
) -> None:
    _upsert_job_state(
        connection,
        job_name=job_name,
        watermark=watermark,
        status="failed",
        error=error[:65535],
    )


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Unsupported datetime value: {value!r}")


def _upsert_job_state(
    connection: Connection,
    *,
    job_name: str,
    watermark: dict[str, Any],
    status: str,
    error: str | None,
    set_started: bool = False,
    set_succeeded: bool = False,
) -> None:
    statement = _build_upsert_statement(connection)
    connection.execute(
        statement,
        {
            "job_name": job_name,
            "watermark_json": json.dumps(watermark, sort_keys=True),
            "last_status": status,
            "last_error": error,
            "set_started": 1 if set_started else 0,
            "set_succeeded": 1 if set_succeeded else 0,
        },
    )


def _build_upsert_statement(connection: Connection):
    dialect = connection.dialect.name
    if dialect == "sqlite":
        return text(
            """
            INSERT INTO ci_job_state (
              job_name,
              watermark_json,
              last_started_at,
              last_succeeded_at,
              last_status,
              last_error,
              updated_at
            ) VALUES (
              :job_name,
              :watermark_json,
              CASE WHEN :set_started = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
              CASE WHEN :set_succeeded = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
              :last_status,
              :last_error,
              CURRENT_TIMESTAMP
            )
            ON CONFLICT(job_name) DO UPDATE SET
              watermark_json = excluded.watermark_json,
              last_started_at = CASE
                WHEN :set_started = 1 THEN CURRENT_TIMESTAMP
                ELSE ci_job_state.last_started_at
              END,
              last_succeeded_at = CASE
                WHEN :set_succeeded = 1 THEN CURRENT_TIMESTAMP
                ELSE ci_job_state.last_succeeded_at
              END,
              last_status = excluded.last_status,
              last_error = excluded.last_error,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO ci_job_state (
          job_name,
          watermark_json,
          last_started_at,
          last_succeeded_at,
          last_status,
          last_error
        ) VALUES (
          :job_name,
          CAST(:watermark_json AS JSON),
          CASE WHEN :set_started = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
          CASE WHEN :set_succeeded = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
          :last_status,
          :last_error
        )
        ON DUPLICATE KEY UPDATE
          watermark_json = CAST(:watermark_json AS JSON),
          last_started_at = CASE
            WHEN :set_started = 1 THEN CURRENT_TIMESTAMP
            ELSE last_started_at
          END,
          last_succeeded_at = CASE
            WHEN :set_succeeded = 1 THEN CURRENT_TIMESTAMP
            ELSE last_succeeded_at
          END,
          last_status = VALUES(last_status),
          last_error = VALUES(last_error),
          updated_at = CURRENT_TIMESTAMP
        """
    )
