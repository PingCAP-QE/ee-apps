from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from ci_dashboard.api.queries.status import _parse_datetime, get_freshness


def test_parse_datetime_supports_naive_aware_and_string_values() -> None:
    aware = datetime(2026, 4, 29, 8, 0, 0, tzinfo=timezone.utc)
    assert _parse_datetime(None) is None
    assert _parse_datetime(aware) == aware
    assert _parse_datetime(datetime(2026, 4, 29, 8, 0, 0)) == aware
    assert _parse_datetime("2026-04-29T08:00:00Z") == aware
    assert _parse_datetime(123) is None


def test_get_freshness_clamps_negative_lag_and_serializes_datetimes(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_job_state (
                  job_name,
                  watermark_json,
                  last_started_at,
                  last_succeeded_at,
                  last_status,
                  last_error
                ) VALUES
                ('job-a', '{}', '2026-04-29T08:00:00Z', '2026-04-29T08:05:00Z', 'succeeded', NULL),
                ('job-b', '{}', '2026-04-29T08:00:00Z', '2026-04-29 08:30:00', 'running', NULL),
                ('job-c', '{}', '2026-04-29T08:00:00Z', NULL, 'never', NULL)
                """
            )
        )

    monkeypatch.setattr(
        "ci_dashboard.api.queries.status.utcnow",
        lambda: datetime(2026, 4, 29, 8, 20, 0, tzinfo=timezone.utc),
    )

    payload = get_freshness(sqlite_engine)

    assert payload["generated_at"] == "2026-04-29T08:20:00Z"
    assert payload["jobs"] == [
        {
            "job_name": "job-a",
            "last_status": "succeeded",
            "last_succeeded_at": "2026-04-29T08:05:00Z",
            "lag_minutes": 15,
        },
        {
            "job_name": "job-b",
            "last_status": "running",
            "last_succeeded_at": "2026-04-29T08:30:00Z",
            "lag_minutes": 0,
        },
        {
            "job_name": "job-c",
            "last_status": "never",
            "last_succeeded_at": None,
            "lag_minutes": None,
        },
    ]
