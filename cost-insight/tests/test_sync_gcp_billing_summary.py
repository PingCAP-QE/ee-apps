from datetime import date

from sqlalchemy import create_engine, text

from cost_insight.common.config import GcpBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.sync_gcp_billing_summary import (
    JOB_NAME,
    _normalize_summary_row,
    _start_partition_from_state,
    build_summary_row_hash,
    run_sync_gcp_billing_summary,
)


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
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
                """
            )
        )
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
        connection.execute(
            text(
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
                """
            )
        )
    return engine


def _summary_row(day: str = "2026-05-18") -> dict[str, object]:
    return {
        "vendor": "gcp",
        "account_id": "pingcap-testing-account",
        "billing_account_id": "billing-1",
        "export_partition_date": day,
        "usage_date": day,
        "author": "hawkingrei",
        "org": "pingcap",
        "repo": "tidb",
        "list_cost": "10.00",
        "effective_cost": "8.00",
        "credit_amount": "-1.00",
        "net_cost": "7.00",
        "source_export_time": "2026-05-19T01:02:03Z",
    }


def test_start_partition_from_state_uses_export_overlap() -> None:
    assert _start_partition_from_state(
        {},
        end_date=date(2026, 5, 20),
        overlap_days=0,
        initial_lookback_days=None,
    ) == date(2026, 5, 20)
    assert _start_partition_from_state(
        {},
        end_date=date(2026, 5, 20),
        overlap_days=0,
        initial_lookback_days=7,
    ) == date(2026, 5, 14)
    assert _start_partition_from_state(
        {"export_partition_end": "2026-05-18"},
        end_date=date(2026, 5, 20),
        overlap_days=1,
        initial_lookback_days=None,
    ) == date(2026, 5, 18)


def test_summary_hash_ignores_amount_changes() -> None:
    row = _normalize_summary_row(_summary_row())
    changed = {**row, "net_cost": "99.00"}

    assert build_summary_row_hash(row) == build_summary_row_hash(changed)


def test_run_sync_gcp_billing_summary_writes_rows_and_touched_dates() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=2)

    try:
        summary = run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 19),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-18"), _summary_row("2026-05-19")],
        )

        assert summary.rows_seen == 2
        assert summary.rows_written == 2
        assert summary.touched_usage_dates == (date(2026, 5, 18), date(2026, 5, 19))
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one()
            state = state_store.get_job_state(connection, JOB_NAME)
        assert count == 2
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_dry_run_skips_state() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")

    try:
        summary = run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=True,
            fetch_rows=lambda **_kwargs: [_summary_row()],
        )

        assert summary.rows_seen == 1
        assert summary.rows_written == 0
        with engine.begin() as connection:
            assert state_store.get_job_state(connection, JOB_NAME) is None
    finally:
        engine.dispose()
