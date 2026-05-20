import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from cost_insight.jobs import state_store


def _sqlite_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    connection = engine.connect()
    connection.execute(
        text(
            """
            CREATE TABLE cost_job_state (
              job_name TEXT PRIMARY KEY,
              watermark_json TEXT,
              last_started_at TEXT,
              last_succeeded_at TEXT,
              last_status TEXT,
              last_error TEXT,
              updated_at TEXT
            )
            """
        )
    )
    return engine, connection


def test_job_state_lifecycle_on_sqlite() -> None:
    engine, connection = _sqlite_connection()
    try:
        assert state_store.get_job_state(connection, "job") is None

        state_store.mark_job_started(connection, "job", {"end_date": "2026-05-18"})
        started = state_store.get_job_state(connection, "job")
        assert started is not None
        assert started.watermark == {"end_date": "2026-05-18"}
        assert started.last_status == "running"
        assert started.last_started_at is not None

        state_store.mark_job_succeeded(connection, "job", {"end_date": "2026-05-19"})
        succeeded = state_store.get_job_state(connection, "job")
        assert succeeded is not None
        assert succeeded.watermark == {"end_date": "2026-05-19"}
        assert succeeded.last_status == "succeeded"
        assert succeeded.last_succeeded_at is not None

        state_store.mark_job_failed(connection, "job", {"end_date": "2026-05-20"}, "x" * 70000)
        failed = state_store.get_job_state(connection, "job")
        assert failed is not None
        assert failed.last_status == "failed"
        assert len(failed.last_error or "") == 65535
    finally:
        connection.close()
        engine.dispose()


def test_get_job_state_parses_dict_watermark_and_datetime_objects() -> None:
    class Result:
        def mappings(self):
            return self

        def first(self):
            return {
                "job_name": "job",
                "watermark_json": {"end_date": "2026-05-18"},
                "last_started_at": datetime(2026, 5, 18, 1, 2, 3),
                "last_succeeded_at": None,
                "last_status": "running",
                "last_error": None,
            }

    class Connection:
        def execute(self, *_args, **_kwargs):
            return Result()

    state = state_store.get_job_state(Connection(), "job")

    assert state is not None
    assert state.watermark == {"end_date": "2026-05-18"}
    assert state.last_started_at == datetime(2026, 5, 18, 1, 2, 3)


def test_coerce_datetime_returns_naive_utc() -> None:
    assert state_store._coerce_datetime("2026-05-18T12:00:00+08:00") == datetime(
        2026, 5, 18, 4, 0
    )
    assert state_store._coerce_datetime(datetime(2026, 5, 18, 1, 2, 3, tzinfo=timezone.utc)) == (
        datetime(2026, 5, 18, 1, 2, 3)
    )


def test_parse_watermark_rejects_invalid_payload() -> None:
    with pytest.raises(ValueError, match="Unsupported watermark_json payload"):
        state_store._parse_watermark(json.dumps(["not", "a", "dict"]))


def test_coerce_datetime_rejects_invalid_payload() -> None:
    with pytest.raises(ValueError, match="Unsupported datetime value"):
        state_store._coerce_datetime(123)


def test_build_upsert_statement_uses_mysql_for_non_sqlite() -> None:
    class Dialect:
        name = "mysql"

    class Connection:
        dialect = Dialect()

    statement = str(state_store._build_upsert_statement(Connection()))

    assert "ON DUPLICATE KEY UPDATE" in statement
