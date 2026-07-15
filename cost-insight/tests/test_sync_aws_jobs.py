from datetime import date

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common.config import AwsBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_aws_billing_summary import (
    JOB_NAME as SUMMARY_JOB_NAME,
    _add_months,
    _month_floor,
    _start_partition_from_state,
    _watermark as summary_watermark,
    run_sync_aws_billing_summary,
)
from cost_insight.jobs.sync_aws_unmatched_resources import (
    JOB_NAME as UNMATCHED_JOB_NAME,
    _watermark as unmatched_watermark,
    run_sync_aws_unmatched_resources,
)


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in (
            """
            CREATE TABLE cost_sources (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              billing_account_id TEXT,
              display_name TEXT,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(vendor, account_id)
            )
            """,
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
            """,
            """
            CREATE TABLE cost_bq_export_summary_daily (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              billing_account_id TEXT,
              export_partition_date TEXT NOT NULL,
              usage_date TEXT NOT NULL,
              service_name TEXT,
              sku_name TEXT,
              org TEXT,
              repo TEXT,
              target_branch TEXT,
              vendor_tags_json TEXT,
              author TEXT,
              list_cost REAL,
              effective_cost REAL,
              credit_amount REAL,
              net_cost REAL,
              source_export_time TEXT,
              source_row_hash TEXT NOT NULL,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(vendor, account_id, export_partition_date, source_row_hash)
            )
            """,
            """
            CREATE TABLE cost_unmatched_resource_daily (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              billing_account_id TEXT,
              export_partition_date TEXT NOT NULL,
              usage_date TEXT NOT NULL,
              service_name TEXT,
              sku_name TEXT,
              namespace TEXT,
              org TEXT,
              repo TEXT,
              target_branch TEXT,
              vendor_tags_json TEXT,
              author TEXT,
              resource_name TEXT NOT NULL,
              usage_seconds REAL,
              list_cost REAL,
              effective_cost REAL,
              credit_amount REAL,
              net_cost REAL,
              source_export_time TEXT,
              source_row_hash TEXT NOT NULL,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(vendor, account_id, export_partition_date, source_row_hash)
            )
            """,
        ):
            connection.execute(text(statement))
    return engine


def _summary_row(day: str = "2026-05-01") -> dict[str, object]:
    return {
        "vendor": "aws",
        "account_id": "946646677266",
        "billing_account_id": "payer-1",
        "export_partition_date": "2026-05-01",
        "usage_date": day,
        "service_name": "Amazon Elastic Compute Cloud",
        "sku_name": "EBS:VolumeUsage.gp3",
        "author": "test-infra",
        "org": "qe",
        "repo": "test-infra",
        "list_cost": "10.00",
        "effective_cost": "8.00",
        "credit_amount": "-1.00",
        "net_cost": "7.00",
        "source_export_time": "2026-05-02T01:02:03Z",
    }


def _resource_row() -> dict[str, object]:
    return {
        "vendor": "aws",
        "account_id": "946646677266",
        "billing_account_id": "payer-1",
        "export_partition_date": "2026-05-01",
        "usage_date": "2026-05-01",
        "service_name": "Amazon Elastic Compute Cloud",
        "sku_name": "BoxUsage:c7g.2xlarge",
        "namespace": None,
        "author": "test-infra",
        "org": "qe",
        "repo": "test-infra",
        "resource_name": "i-0123456789abcdef0",
        "usage_seconds": "3600.00",
        "list_cost": "12.00",
        "effective_cost": "9.00",
        "credit_amount": "-1.00",
        "net_cost": "8.00",
        "source_export_time": "2026-05-02T01:02:03Z",
    }


def test_aws_summary_partition_helpers() -> None:
    assert _month_floor(date(2026, 5, 17)) == date(2026, 5, 1)
    assert _add_months(date(2026, 1, 1), 2) == date(2026, 3, 1)
    assert _start_partition_from_state(
        {"export_partition_end": "2026-05-01"},
        end_date=date(2026, 6, 1),
        overlap_months=1,
        initial_lookback_months=2,
    ) == date(2026, 5, 1)
    assert _start_partition_from_state(
        {},
        end_date=date(2026, 6, 1),
        overlap_months=1,
        initial_lookback_months=2,
    ) == date(2026, 5, 1)
    assert _start_partition_from_state(
        {},
        end_date=date(2026, 6, 1),
        overlap_months=1,
        initial_lookback_months=None,
    ) == date(2026, 6, 1)
    assert summary_watermark(
        account_id="946646677266",
        export_partition_start=date(2026, 5, 1),
        export_partition_end=date(2026, 5, 1),
    ) == {
        "vendor": "aws",
        "account_id": "946646677266",
        "export_partition_start": "2026-05-01",
        "export_partition_end": "2026-05-01",
    }
    assert unmatched_watermark(
        account_id="946646677266",
        usage_start_date=date(2026, 5, 1),
        usage_end_date=date(2026, 5, 2),
        export_partition_start=date(2026, 5, 1),
        export_partition_end=date(2026, 5, 1),
    ) == {
        "vendor": "aws",
        "account_id": "946646677266",
        "usage_start_date": "2026-05-01",
        "usage_end_date": "2026-05-02",
        "export_partition_start": "2026-05-01",
        "export_partition_end": "2026-05-01",
    }


def test_run_sync_aws_billing_summary_writes_rows_and_touched_dates() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=2)

    try:
        summary = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-01"), _summary_row("2026-05-02")],
        )

        assert summary.rows_seen == 2
        assert summary.rows_written == 2
        assert summary.touched_usage_dates == (date(2026, 5, 1), date(2026, 5, 2))
        with engine.begin() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")).scalar_one()
            state = state_store.get_job_state(
                connection,
                source_job_name(SUMMARY_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
            source = connection.execute(
                text(
                    """
                    SELECT billing_account_id
                    FROM cost_sources
                    WHERE vendor = 'aws' AND account_id = '946646677266'
                    """
                )
            ).scalar_one()
        assert count == 2
        assert state is not None
        assert state.last_status == "succeeded"
        assert source == "payer-1"
    finally:
        engine.dispose()


def test_run_sync_aws_billing_summary_can_replace_existing_partitions() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=2)

    try:
        run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-01"), _summary_row("2026-05-02")],
        )

        summary = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            replace_existing_partitions=True,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-03")],
        )

        assert summary.rows_seen == 1
        assert summary.rows_written == 1
        assert summary.touched_usage_dates == (date(2026, 5, 3),)
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT usage_date
                    FROM cost_bq_export_summary_daily
                    WHERE vendor = 'aws'
                      AND account_id = '946646677266'
                      AND export_partition_date = '2026-05-01'
                    ORDER BY usage_date
                    """
                )
            ).scalars().all()
        assert rows == ["2026-05-03"]
    finally:
        engine.dispose()


def test_run_sync_aws_billing_summary_dry_run_and_failure() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266")

    def raise_fetch(**_kwargs):
        raise RuntimeError("boom")
        yield

    try:
        summary = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            dry_run=True,
            fetch_rows=lambda **_kwargs: [_summary_row()],
        )
        assert summary.rows_seen == 1
        assert summary.rows_written == 0

        with pytest.raises(RuntimeError, match="boom"):
            run_sync_aws_billing_summary(
                engine,
                settings=settings,
                account_id="946646677266",
                export_partition_start=date(2026, 5, 1),
                export_partition_end=date(2026, 5, 1),
                fetch_rows=raise_fetch,
            )

        with engine.begin() as connection:
            state = state_store.get_job_state(
                connection,
                source_job_name(SUMMARY_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
        assert state is not None
        assert state.last_status == "failed"
    finally:
        engine.dispose()


def test_run_sync_aws_unmatched_resources_writes_rows() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=1)

    try:
        summary = run_sync_aws_unmatched_resources(
            engine,
            settings=settings,
            account_id="946646677266",
            usage_start_date=date(2026, 5, 1),
            usage_end_date=date(2026, 5, 2),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )

        assert summary.rows_seen == 1
        assert summary.rows_written == 1
        assert summary.export_partition_start == date(2026, 5, 1)
        assert summary.export_partition_end == date(2026, 5, 1)
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
            state = state_store.get_job_state(
                connection,
                source_job_name(UNMATCHED_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
        assert count == 1
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_sync_aws_unmatched_resources_rejects_invalid_range_and_marks_failure() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266")

    def raise_fetch(**_kwargs):
        raise RuntimeError("fetch failed")
        yield

    try:
        with pytest.raises(ValueError, match="usage_start_date"):
            run_sync_aws_unmatched_resources(
                engine,
                settings=settings,
                account_id="946646677266",
                usage_start_date=date(2026, 5, 2),
                usage_end_date=date(2026, 5, 1),
                fetch_rows=lambda **_kwargs: [],
            )

        with pytest.raises(RuntimeError, match="fetch failed"):
            run_sync_aws_unmatched_resources(
                engine,
                settings=settings,
                account_id="946646677266",
                usage_start_date=date(2026, 5, 1),
                usage_end_date=date(2026, 5, 1),
                fetch_rows=raise_fetch,
            )

        with engine.begin() as connection:
            state = state_store.get_job_state(
                connection,
                source_job_name(UNMATCHED_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
        assert state is not None
        assert state.last_status == "failed"
    finally:
        engine.dispose()
