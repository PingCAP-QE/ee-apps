import hashlib
import json
from datetime import date

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.row_utils import hash_value
from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_billing_summary import (
    JOB_NAME,
    _normalize_summary_row,
    _select_billing_account_id,
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
                  service_name TEXT,
                  sku_name TEXT,
                  region TEXT,
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
        "service_name": "Compute Engine",
        "sku_name": "C4 Instance Core running in Americas",
        "region": "us-central1",
        "author": "hawkingrei",
        "org": "pingcap",
        "repo": "tidb",
        "target_branch": "master",
        "vendor_tags_json": None,
        "list_cost": "10.00",
        "effective_cost": "8.00",
        "credit_amount": "-1.00",
        "net_cost": "7.00",
        "source_export_time": "2026-05-19T01:02:03Z",
    }


def _sqlite_summary_row(row: dict[str, object]) -> dict[str, object]:
    return {
        **row,
        "list_cost": float(row["list_cost"]),
        "effective_cost": float(row["effective_cost"]),
        "credit_amount": float(row["credit_amount"]),
        "net_cost": float(row["net_cost"]),
    }


def _insert_summary_row(connection, row: dict[str, object]) -> None:
    connection.execute(
        text(
            """
            INSERT INTO cost_bq_export_summary_daily (
              vendor,
              account_id,
              billing_account_id,
              export_partition_date,
              usage_date,
              service_name,
              sku_name,
              region,
              org,
              repo,
              target_branch,
              vendor_tags_json,
              author,
              list_cost,
              effective_cost,
              credit_amount,
              net_cost,
              source_export_time,
              source_row_hash
            ) VALUES (
              :vendor,
              :account_id,
              :billing_account_id,
              :export_partition_date,
              :usage_date,
              :service_name,
              :sku_name,
              :region,
              :org,
              :repo,
              :target_branch,
              :vendor_tags_json,
              :author,
              :list_cost,
              :effective_cost,
              :credit_amount,
              :net_cost,
              :source_export_time,
              :source_row_hash
            )
            """
        ),
        _sqlite_summary_row(row),
    )


def _legacy_summary_row_hash(row: dict[str, object]) -> str:
    legacy_fields = (
        "vendor",
        "account_id",
        "billing_account_id",
        "export_partition_date",
        "usage_date",
        "service_name",
        "sku_name",
        "region",
        "author",
        "org",
        "repo",
        "target_branch",
    )
    payload = {field: hash_value(row.get(field)) for field in legacy_fields}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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

    assert build_summary_row_hash(row) == _legacy_summary_row_hash(row)
    assert build_summary_row_hash(row) == build_summary_row_hash(changed)
    assert build_summary_row_hash(row) != build_summary_row_hash(
        {**row, "service_name": "Cloud Storage"}
    )
    assert build_summary_row_hash(row) != build_summary_row_hash(
        {**row, "target_branch": "release-8.5"}
    )
    assert build_summary_row_hash(row) != build_summary_row_hash({**row, "region": "europe-west1"})
    assert build_summary_row_hash(row) != build_summary_row_hash(
        {
            **row,
            "vendor_tags_json": '{"cluster":"10149878793099322221","shared_pool":"2076551309477019648"}',
        }
    )


def test_select_billing_account_id_handles_empty_and_multiple_values() -> None:
    assert _select_billing_account_id(set()) is None
    assert _select_billing_account_id({"billing-2", "billing-1"}) == "billing-1"


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
            service_names = connection.execute(
                text("SELECT DISTINCT service_name FROM cost_bq_export_summary_daily")
            ).scalars().all()
            state = state_store.get_job_state(
                connection,
                source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id),
            )
        assert count == 2
        assert service_names == ["Compute Engine"]
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_removes_superseded_unlabeled_row() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    labeled_row = {
        **_summary_row("2026-05-18"),
        "vendor_tags_json": {
            "shared_pool": "2076551309477019648",
            "cluster": "10149878793099322221",
        },
    }

    try:
        run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-18")],
        )
        run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [labeled_row],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT vendor_tags_json, ROUND(SUM(net_cost), 2) AS net_cost
                    FROM cost_bq_export_summary_daily
                    GROUP BY vendor_tags_json
                    """
                )
            ).all()
        assert rows == [
            ('{"cluster":"10149878793099322221","shared_pool":"2076551309477019648"}', 7.0)
        ]
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_can_replace_existing_partitions() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    old_row = _normalize_summary_row({**_summary_row(), "target_branch": None})
    old_row = {
        **old_row,
        "list_cost": float(old_row["list_cost"]),
        "effective_cost": float(old_row["effective_cost"]),
        "credit_amount": float(old_row["credit_amount"]),
        "net_cost": float(old_row["net_cost"]),
    }
    new_row = {**_summary_row(), "target_branch": "master"}

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      vendor,
                      account_id,
                      billing_account_id,
                      export_partition_date,
                      usage_date,
                      service_name,
                      sku_name,
                      region,
                      org,
                      repo,
                      target_branch,
                      author,
                      list_cost,
                      effective_cost,
                      credit_amount,
                      net_cost,
                      source_export_time,
                      source_row_hash
                    ) VALUES (
                      :vendor,
                      :account_id,
                      :billing_account_id,
                      :export_partition_date,
                      :usage_date,
                      :service_name,
                      :sku_name,
                      :region,
                      :org,
                      :repo,
                      :target_branch,
                      :author,
                      :list_cost,
                      :effective_cost,
                      :credit_amount,
                      :net_cost,
                      :source_export_time,
                      :source_row_hash
                    )
                    """
                ),
                old_row,
            )

        run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=False,
            replace_existing_partitions=True,
            fetch_rows=lambda **_kwargs: [new_row],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT target_branch, ROUND(SUM(list_cost), 2) AS list_cost
                    FROM cost_bq_export_summary_daily
                    GROUP BY target_branch
                    """
                )
            ).all()
        assert rows == [("master", 10.0)]
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_replace_keeps_old_rows_when_fetch_fails() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    old_row = _normalize_summary_row({**_summary_row(), "target_branch": None})
    old_row = {
        **old_row,
        "list_cost": float(old_row["list_cost"]),
        "effective_cost": float(old_row["effective_cost"]),
        "credit_amount": float(old_row["credit_amount"]),
        "net_cost": float(old_row["net_cost"]),
    }

    def raise_fetch(**_kwargs):
        raise RuntimeError("bigquery failed")
        yield

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      vendor,
                      account_id,
                      billing_account_id,
                      export_partition_date,
                      usage_date,
                      service_name,
                      sku_name,
                      region,
                      org,
                      repo,
                      target_branch,
                      author,
                      list_cost,
                      effective_cost,
                      credit_amount,
                      net_cost,
                      source_export_time,
                      source_row_hash
                    ) VALUES (
                      :vendor,
                      :account_id,
                      :billing_account_id,
                      :export_partition_date,
                      :usage_date,
                      :service_name,
                      :sku_name,
                      :region,
                      :org,
                      :repo,
                      :target_branch,
                      :author,
                      :list_cost,
                      :effective_cost,
                      :credit_amount,
                      :net_cost,
                      :source_export_time,
                      :source_row_hash
                    )
                    """
                ),
                old_row,
            )

        with pytest.raises(RuntimeError, match="bigquery failed"):
            run_sync_gcp_billing_summary(
                engine,
                settings=settings,
                export_partition_start=date(2026, 5, 18),
                export_partition_end=date(2026, 5, 18),
                dry_run=False,
                replace_existing_partitions=True,
                fetch_rows=raise_fetch,
            )

        with engine.begin() as connection:
            rows = connection.execute(
                text("SELECT target_branch, ROUND(SUM(list_cost), 2) FROM cost_bq_export_summary_daily")
            ).all()
        assert rows == [(None, 10.0)]
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_deletes_superseded_owner_override_rows() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    old_row = _normalize_summary_row(
        {
            **_summary_row(),
            "service_name": "Cloud Logging",
            "sku_name": "Log Storage cost",
            "author": None,
            "target_branch": "master",
        }
    )
    old_row = {
        **old_row,
        "list_cost": float(old_row["list_cost"]),
        "effective_cost": float(old_row["effective_cost"]),
        "credit_amount": float(old_row["credit_amount"]),
        "net_cost": float(old_row["net_cost"]),
    }
    new_row = {
        **_summary_row(),
        "service_name": "Cloud Logging",
        "sku_name": "Log Storage cost",
        "author": "wei_zheng",
    }

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      vendor,
                      account_id,
                      billing_account_id,
                      export_partition_date,
                      usage_date,
                      service_name,
                      sku_name,
                      region,
                      org,
                      repo,
                      target_branch,
                      author,
                      list_cost,
                      effective_cost,
                      credit_amount,
                      net_cost,
                      source_export_time,
                      source_row_hash
                    ) VALUES (
                      :vendor,
                      :account_id,
                      :billing_account_id,
                      :export_partition_date,
                      :usage_date,
                      :service_name,
                      :sku_name,
                      :region,
                      :org,
                      :repo,
                      :target_branch,
                      :author,
                      :list_cost,
                      :effective_cost,
                      :credit_amount,
                      :net_cost,
                      :source_export_time,
                      :source_row_hash
                    )
                    """
                ),
                old_row,
            )

        run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [new_row],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT author, ROUND(SUM(list_cost), 2) AS list_cost
                    FROM cost_bq_export_summary_daily
                    GROUP BY author
                    """
                )
            ).all()
        assert rows == [("wei_zheng", 10.0)]
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_keeps_other_branch_owner_override_rows() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    old_row = _normalize_summary_row(
        {
            **_summary_row(),
            "service_name": "Cloud Logging",
            "sku_name": "Log Storage cost",
            "author": None,
            "target_branch": "release-8.5",
        }
    )
    new_row = {
        **_summary_row(),
        "service_name": "Cloud Logging",
        "sku_name": "Log Storage cost",
        "author": "wei_zheng",
        "target_branch": "master",
    }

    try:
        with engine.begin() as connection:
            _insert_summary_row(connection, old_row)

        run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [new_row],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT target_branch, author, ROUND(SUM(list_cost), 2) AS list_cost
                    FROM cost_bq_export_summary_daily
                    GROUP BY target_branch, author
                    ORDER BY target_branch, author
                    """
                )
            ).all()
        assert rows == [
            ("master", "wei_zheng", 10.0),
            ("release-8.5", None, 10.0),
        ]
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_keeps_legacy_null_branch_owner_override_rows() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    old_row = _normalize_summary_row(
        {
            **_summary_row(),
            "service_name": "Cloud Logging",
            "sku_name": "Log Storage cost",
            "author": None,
            "target_branch": None,
        }
    )
    new_row = {
        **_summary_row(),
        "service_name": "Cloud Logging",
        "sku_name": "Log Storage cost",
        "author": "wei_zheng",
        "target_branch": "master",
    }

    try:
        with engine.begin() as connection:
            _insert_summary_row(connection, old_row)

        run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [new_row],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT COALESCE(target_branch, '(null)') AS branch,
                           author,
                           ROUND(SUM(list_cost), 2) AS list_cost
                    FROM cost_bq_export_summary_daily
                    GROUP BY branch, author
                    ORDER BY branch, author
                    """
                )
            ).all()
        assert rows == [
            ("(null)", None, 10.0),
            ("master", "wei_zheng", 10.0),
        ]
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_summary_deletes_one_year_cud_superseded_rows() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    old_row = _normalize_summary_row(
        {
            **_summary_row(),
            "service_name": "Compute Engine",
            "sku_name": "Compute Flexible Committed Use Discounts - 1 Year",
            "author": None,
            "target_branch": "master",
        }
    )
    old_row = {
        **old_row,
        "list_cost": float(old_row["list_cost"]),
        "effective_cost": float(old_row["effective_cost"]),
        "credit_amount": float(old_row["credit_amount"]),
        "net_cost": float(old_row["net_cost"]),
    }
    new_row = {
        **_summary_row(),
        "service_name": "Compute Engine",
        "sku_name": "Compute Flexible Committed Use Discounts - 1 Year",
        "author": "wei_zheng",
    }

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      vendor,
                      account_id,
                      billing_account_id,
                      export_partition_date,
                      usage_date,
                      service_name,
                      sku_name,
                      region,
                      org,
                      repo,
                      target_branch,
                      author,
                      list_cost,
                      effective_cost,
                      credit_amount,
                      net_cost,
                      source_export_time,
                      source_row_hash
                    ) VALUES (
                      :vendor,
                      :account_id,
                      :billing_account_id,
                      :export_partition_date,
                      :usage_date,
                      :service_name,
                      :sku_name,
                      :region,
                      :org,
                      :repo,
                      :target_branch,
                      :author,
                      :list_cost,
                      :effective_cost,
                      :credit_amount,
                      :net_cost,
                      :source_export_time,
                      :source_row_hash
                    )
                    """
                ),
                old_row,
            )

        run_sync_gcp_billing_summary(
            engine,
            settings=settings,
            export_partition_start=date(2026, 5, 18),
            export_partition_end=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [new_row],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT author, ROUND(SUM(list_cost), 2) AS list_cost
                    FROM cost_bq_export_summary_daily
                    GROUP BY author
                    """
                )
            ).all()
        assert rows == [("wei_zheng", 10.0)]
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
            assert (
                state_store.get_job_state(
                    connection,
                    source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id),
                )
                is None
            )
    finally:
        engine.dispose()
