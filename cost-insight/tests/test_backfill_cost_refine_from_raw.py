from datetime import date

from sqlalchemy import create_engine, text

from cost_insight.common.config import GcpBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.backfill_cost_refine_from_raw import (
    JOB_NAME,
    run_backfill_cost_refine_from_raw,
)
from cost_insight.jobs.sync_gcp_billing_summary import JOB_NAME as SUMMARY_JOB_NAME


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in (
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
            CREATE TABLE cost_raw_details (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              billing_account_id TEXT,
              usage_date TEXT NOT NULL,
              service_name TEXT,
              sku_name TEXT,
              region TEXT,
              namespace TEXT,
              author TEXT,
              org TEXT,
              repo TEXT,
              resource_name TEXT,
              usage_seconds REAL,
              list_cost REAL,
              effective_cost REAL,
              credit_amount REAL,
              net_cost REAL,
              source_export_time TEXT,
              source_row_hash TEXT NOT NULL
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
              org TEXT,
              repo TEXT,
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
        connection.execute(
            text(
                """
                INSERT INTO cost_raw_details (
                  vendor, account_id, billing_account_id, usage_date, service_name,
                  sku_name, region, namespace, author, org, repo, resource_name,
                  usage_seconds, list_cost, effective_cost, credit_amount, net_cost,
                  source_export_time, source_row_hash
                ) VALUES
                  (
                    'gcp', 'pingcap-testing-account', 'billing-1', '2026-05-17',
                    'Compute Engine', 'Core running', 'us-central1', 'kube:unallocated',
                    'alice', 'pingcap', 'tidb', 'runner-1',
                    3600, 10, 8, -1, 7, '2026-05-20 01:02:03', 'raw-1'
                  ),
                  (
                    'gcp', 'pingcap-testing-account', 'billing-1', '2026-05-17',
                    'Compute Engine', 'Core running', 'us-central1', 'kube:unallocated',
                    'alice', 'pingcap', 'tidb', 'runner-1',
                    1800, 5, 4, -0.5, 3.5, '2026-05-20 02:02:03', 'raw-2'
                  ),
                  (
                    'gcp', 'pingcap-testing-account', 'billing-1', '2026-05-18',
                    'Cloud Logging', 'Storage', 'global', NULL,
                    NULL, 'pingcap', 'platform', NULL,
                    NULL, 2, 2, 0, 2, NULL, 'raw-3'
                  )
                """
            )
        )
    return engine


def test_backfill_cost_refine_from_raw_writes_summary_and_unmatched_rows() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=1)

    try:
        summary = run_backfill_cost_refine_from_raw(
            engine,
            settings=settings,
            start_date=date(2026, 5, 17),
            end_date=date(2026, 5, 18),
            mark_summary_watermark=True,
        )

        assert summary.summary_rows_seen == 2
        assert summary.summary_rows_written == 2
        assert summary.unmatched_rows_seen == 1
        assert summary.unmatched_rows_written == 1
        assert summary.export_partition_start == date(2026, 5, 18)
        assert summary.export_partition_end == date(2026, 5, 20)
        assert summary.marked_summary_watermark is True

        with engine.begin() as connection:
            summary_cost = connection.execute(
                text(
                    """
                    SELECT list_cost, effective_cost, credit_amount, net_cost
                    FROM cost_bq_export_summary_daily
                    WHERE usage_date = '2026-05-17'
                    """
                )
            ).mappings().one()
            resource = connection.execute(
                text(
                    """
                    SELECT usage_seconds, list_cost
                    FROM cost_unmatched_resource_daily
                    WHERE resource_name = 'runner-1'
                    """
                )
            ).mappings().one()
            job_state = state_store.get_job_state(connection, JOB_NAME)
            summary_state = state_store.get_job_state(connection, SUMMARY_JOB_NAME)

        assert summary_cost["list_cost"] == 15
        assert summary_cost["effective_cost"] == 12
        assert summary_cost["credit_amount"] == -1.5
        assert summary_cost["net_cost"] == 10.5
        assert resource["usage_seconds"] == 5400
        assert resource["list_cost"] == 15
        assert job_state is not None
        assert job_state.last_status == "succeeded"
        assert summary_state is not None
        assert summary_state.watermark["export_partition_end"] == "2026-05-20"
    finally:
        engine.dispose()


def test_backfill_cost_refine_from_raw_dry_run_writes_nothing() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")

    try:
        summary = run_backfill_cost_refine_from_raw(
            engine,
            settings=settings,
            start_date=date(2026, 5, 17),
            end_date=date(2026, 5, 18),
            dry_run=True,
        )

        assert summary.summary_rows_seen == 2
        assert summary.summary_rows_written == 0
        assert summary.unmatched_rows_seen == 1
        assert summary.unmatched_rows_written == 0
        with engine.begin() as connection:
            summary_count = connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one()
            resource_count = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
            assert state_store.get_job_state(connection, JOB_NAME) is None
        assert summary_count == 0
        assert resource_count == 0
    finally:
        engine.dispose()
