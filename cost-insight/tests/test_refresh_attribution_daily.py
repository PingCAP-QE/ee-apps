from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from cost_insight.common.config import GcpBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.refresh_attribution_daily import (
    JOB_NAME,
    SUMMARY_JOB_NAME,
    _INSERT_ATTRIBUTION_DAILY,
    _INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY,
    normalized_identity_sql,
    _positive_rowcount,
    _watermark,
    run_refresh_cost_attribution_daily,
    run_refresh_cost_attribution_from_summary,
)


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
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
    return engine


def test_watermark_formats_dates() -> None:
    assert _watermark(
        account_id="pingcap-testing-account",
        start_date=date(2026, 5, 9),
        end_date=date(2026, 5, 17),
    ) == {
        "account_id": "pingcap-testing-account",
        "start_date": "2026-05-09",
        "end_date": "2026-05-17",
    }


def test_positive_rowcount_normalizes_unknown_values() -> None:
    assert _positive_rowcount(None) == 0
    assert _positive_rowcount(-1) == 0
    assert _positive_rowcount(3) == 3


def test_normalized_identity_sql_replaces_label_unsafe_characters() -> None:
    sql = normalized_identity_sql("employee.email")

    assert "LOWER(COALESCE(employee.email, ''))" in sql
    assert "SUBSTRING_INDEX" in sql
    assert "'@'" in sql
    assert "'-'" in sql
    assert "'.'" in sql
    assert "' '" in sql


def test_run_refresh_attribution_dry_run_counts_raw_rows() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_raw_details (
                      usage_date DATE NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_raw_details (usage_date, vendor, account_id)
                    VALUES
                      ('2026-05-09', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'aws', '123456789012')
                    """
                )
            )

        summary = run_refresh_cost_attribution_daily(
            engine,
            settings=GcpBillingSettings(account_id="pingcap-testing-account"),
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
            dry_run=True,
        )

        assert summary.raw_rows == 2
        assert summary.rows_deleted == 0
        assert summary.rows_inserted == 0
        assert summary.dry_run is True
        with engine.begin() as connection:
            assert state_store.get_job_state(connection, JOB_NAME) is None
    finally:
        engine.dispose()


def test_run_refresh_attribution_from_summary_dry_run_counts_summary_rows() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_bq_export_summary_daily (
                      usage_date DATE NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (usage_date, vendor, account_id)
                    VALUES
                      ('2026-05-09', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'aws', '123456789012')
                    """
                )
            )

        summary = run_refresh_cost_attribution_from_summary(
            engine,
            settings=GcpBillingSettings(account_id="pingcap-testing-account"),
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
            dry_run=True,
        )

        assert summary.summary_rows == 2
        assert summary.rows_deleted == 0
        assert summary.rows_inserted == 0
        assert summary.dry_run is True
        with engine.begin() as connection:
            assert state_store.get_job_state(connection, SUMMARY_JOB_NAME) is None
    finally:
        engine.dispose()


def test_run_refresh_attribution_marks_success(monkeypatch) -> None:
    engine = _sqlite_engine()
    executed = []

    def fake_execute(self, statement, params=None, *args, **kwargs):
        sql = str(statement)
        if "DELETE FROM cost_attribution_daily" in sql:
            executed.append(("delete", params))

            class Result:
                rowcount = 4

            return Result()
        if "INSERT INTO cost_attribution_daily" in sql:
            executed.append(("insert", params))

            class Result:
                rowcount = 7

            return Result()
        return original_execute(self, statement, params, *args, **kwargs)

    original_execute = Connection.execute
    monkeypatch.setattr("sqlalchemy.engine.base.Connection.execute", fake_execute)

    try:
        summary = run_refresh_cost_attribution_daily(
            engine,
            settings=GcpBillingSettings(account_id="pingcap-testing-account"),
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
        )

        assert summary.rows_deleted == 4
        assert summary.rows_inserted == 7
        assert [kind for kind, _params in executed] == ["delete", "insert"]
        assert executed[0][1]["account_id"] == "pingcap-testing-account"
        with engine.begin() as connection:
            state = state_store.get_job_state(connection, JOB_NAME)
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_refresh_attribution_from_summary_marks_success(monkeypatch) -> None:
    engine = _sqlite_engine()
    executed = []

    def fake_execute(self, statement, params=None, *args, **kwargs):
        sql = str(statement)
        if "DELETE FROM cost_attribution_daily" in sql:
            executed.append(("delete", params))

            class Result:
                rowcount = 2

            return Result()
        if "FROM cost_bq_export_summary_daily summary" in sql:
            executed.append(("insert-summary", params))

            class Result:
                rowcount = 5

            return Result()
        return original_execute(self, statement, params, *args, **kwargs)

    original_execute = Connection.execute
    monkeypatch.setattr("sqlalchemy.engine.base.Connection.execute", fake_execute)

    try:
        summary = run_refresh_cost_attribution_from_summary(
            engine,
            settings=GcpBillingSettings(account_id="pingcap-testing-account"),
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
        )

        assert summary.rows_deleted == 2
        assert summary.rows_inserted == 5
        assert [kind for kind, _params in executed] == ["delete", "insert-summary"]
        with engine.begin() as connection:
            state = state_store.get_job_state(connection, SUMMARY_JOB_NAME)
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_refresh_attribution_marks_failure(monkeypatch) -> None:
    engine = _sqlite_engine()

    def fake_execute(self, statement, params=None, *args, **kwargs):
        if "DELETE FROM cost_attribution_daily" in str(statement):
            raise RuntimeError("delete failed")
        return original_execute(self, statement, params, *args, **kwargs)

    original_execute = Connection.execute
    monkeypatch.setattr("sqlalchemy.engine.base.Connection.execute", fake_execute)

    try:
        with pytest.raises(RuntimeError, match="delete failed"):
            run_refresh_cost_attribution_daily(
                engine,
                settings=GcpBillingSettings(account_id="pingcap-testing-account"),
                start_date=date(2026, 5, 9),
                end_date=date(2026, 5, 10),
            )

        with engine.begin() as connection:
            state = state_store.get_job_state(connection, JOB_NAME)
        assert state is not None
        assert state.last_status == "failed"
        assert "RuntimeError" in (state.last_error or "")
    finally:
        engine.dispose()


def test_run_refresh_attribution_rejects_invalid_range() -> None:
    engine = _sqlite_engine()
    try:
        with pytest.raises(ValueError, match="start_date"):
            run_refresh_cost_attribution_daily(
                engine,
                settings=GcpBillingSettings(account_id="pingcap-testing-account"),
                start_date=date(2026, 5, 10),
                end_date=date(2026, 5, 9),
            )
    finally:
        engine.dispose()


def test_insert_sql_contains_roster_matching_and_daily_dimensions() -> None:
    sql = str(_INSERT_ATTRIBUTION_DAILY)

    assert "LEFT JOIN roster_employees github_employee" in sql
    assert "LEFT JOIN roster_employees normalized_employee" in sql
    assert "LOWER(github_employee.github_id) = LOWER(raw.author)" in sql
    assert "SUBSTRING_INDEX(email_employee.email, '@', 1)" in sql
    assert "LEFT JOIN roster_groups matched_group" in sql
    assert "author_github" in sql
    assert "author_email" in sql
    assert "author_normalized" in sql
    assert "missing_author" in sql
    assert "resource_name" in sql
    assert "SHA2(" in sql
    assert "{normalized_" not in sql


def test_summary_insert_sql_uses_summary_source_and_nullable_resource_columns() -> None:
    sql = str(_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY)

    assert "FROM cost_bq_export_summary_daily summary" in sql
    assert "NULL AS service_name" in sql
    assert "NULL AS sku_name" in sql
    assert "NULL AS resource_name" in sql
    assert "NULL AS usage_seconds" in sql
    assert "LEFT JOIN roster_employees github_employee" in sql
    assert "LOWER(github_employee.github_id) = LOWER(summary.author)" in sql
    assert "author_normalized" in sql
    assert "SHA2(" in sql
    assert "{normalized_" not in sql
