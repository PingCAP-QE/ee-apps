from datetime import date

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common.config import GcpBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.sync_gcp_unmatched_resources import (
    JOB_NAME,
    _normalize_resource_row,
    build_unmatched_resource_row_hash,
    run_sync_gcp_unmatched_resources,
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
                """
            )
        )
    return engine


def _resource_row() -> dict[str, object]:
    return {
        "vendor": "gcp",
        "account_id": "pingcap-testing-account",
        "billing_account_id": "billing-1",
        "export_partition_date": "2026-05-20",
        "usage_date": "2026-05-18",
        "service_name": "Compute Engine",
        "sku_name": "Core running",
        "namespace": "kube:unallocated",
        "author": "hawkingrei",
        "org": "pingcap",
        "repo": "tidb",
        "resource_name": "tidb-test-pod-1",
        "usage_seconds": "3600.00",
        "list_cost": "10.00",
        "effective_cost": "8.00",
        "credit_amount": "-1.00",
        "net_cost": "7.00",
        "source_export_time": "2026-05-20T01:02:03Z",
    }


def test_unmatched_resource_hash_ignores_amount_changes() -> None:
    row = _normalize_resource_row(_resource_row())
    changed = {**row, "net_cost": "99.00"}

    assert build_unmatched_resource_row_hash(row) == build_unmatched_resource_row_hash(changed)


def test_normalize_resource_row_rejects_missing_resource_name() -> None:
    row = {**_resource_row(), "resource_name": ""}

    with pytest.raises(ValueError, match="Missing resource_name"):
        _normalize_resource_row(row)


def test_run_sync_gcp_unmatched_resources_writes_rows() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=1)

    try:
        summary = run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )

        assert summary.export_partition_start == date(2026, 5, 18)
        assert summary.export_partition_end == date(2026, 5, 23)
        assert summary.rows_seen == 1
        assert summary.rows_written == 1
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
            state = state_store.get_job_state(connection, JOB_NAME)
        assert count == 1
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_sync_gcp_unmatched_resources_rejects_invalid_range() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")

    try:
        with pytest.raises(ValueError, match="usage_start_date"):
            run_sync_gcp_unmatched_resources(
                engine,
                settings=settings,
                usage_start_date=date(2026, 5, 19),
                usage_end_date=date(2026, 5, 18),
                fetch_rows=lambda **_kwargs: [],
            )
    finally:
        engine.dispose()
