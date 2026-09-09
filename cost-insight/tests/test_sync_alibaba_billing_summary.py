from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common.config import AlibabaBillingSettings, load_settings
from cost_insight.jobs import cli
from cost_insight.jobs.sync_alibaba_billing_summary import run_sync_alibaba_billing_summary
from cost_insight.jobs.sync_gcp_billing_summary import (
    SyncGcpBillingSummaryResult,
    _delete_legacy_summary_rows,
    _normalize_summary_row,
    _quote_sql_table,
    _write_summary_rows,
    replace_summary_partition_usage_dates,
    replace_summary_usage_dates,
    run_sync_billing_summary,
    write_summary_rows,
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
                  source_table TEXT,
                  source_schema_version TEXT,
                  source_available_from TEXT,
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
                  usage_type TEXT,
                  cost_driver_key TEXT,
                  region TEXT,
                  org TEXT,
                  repo TEXT,
                  target_branch TEXT,
                  resource_name TEXT,
                  vendor_tags_json TEXT,
                  author TEXT,
                  source_schema_version TEXT,
                  source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                  cluster_name TEXT,
                  cluster_location TEXT,
                  kubernetes_cost_class TEXT,
                  kubernetes_residual_type TEXT,
                  kubernetes_cost_component TEXT,
                  namespace TEXT,
                  workload_name TEXT,
                  workload_type TEXT,
                  owner TEXT,
                  service TEXT,
                  project TEXT,
                  service_exec_id TEXT,
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


def test_alibaba_settings_read_provider_specific_values() -> None:
    settings = load_settings(
        {
            "COST_INSIGHT_ALIBABA_BILLING_TABLE": "project.dataset.daily_en_*",
            "COST_INSIGHT_ALIBABA_ACCOUNT_ID": "account-1",
            "COST_INSIGHT_ALIBABA_EARLIEST_USAGE_DATE": "2026-08-01",
            "COST_INSIGHT_ALIBABA_SYNC_LAG_DAYS": "3",
            "COST_INSIGHT_ALIBABA_EXPORT_OVERLAP_DAYS": "1",
            "COST_INSIGHT_ALIBABA_SYNC_INITIAL_LOOKBACK_DAYS": "4",
            "COST_INSIGHT_ALIBABA_SYNC_PAGE_SIZE": "2",
        },
        require_database=False,
    ).alibaba_billing

    assert settings == AlibabaBillingSettings(
        billing_table="project.dataset.daily_en_*",
        account_id="account-1",
        earliest_usage_date=date(2026, 8, 1),
        sync_lag_days=3,
        export_overlap_days=1,
        sync_initial_lookback_days=4,
        page_size=2,
    )


def test_cli_runs_alibaba_summary_sync(monkeypatch, capsys) -> None:
    captured = {}

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        alibaba_billing=AlibabaBillingSettings(),
        log_level="INFO",
    )

    def fake_sync(_engine, **kwargs):
        captured.update(kwargs)
        return SyncGcpBillingSummaryResult(
            account_id=kwargs["account_id"],
            export_partition_start=kwargs["export_partition_start"],
            export_partition_end=kwargs["export_partition_end"],
            rows_seen=1,
            rows_written=1,
            dry_run=kwargs["dry_run"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_alibaba_billing_summary", fake_sync)

    assert (
        cli.main(
            [
                "sync-alibaba-billing-summary",
                "--export-partition-start",
                "2026-08-01",
                "--export-partition-end",
                "2026-08-31",
                "--dry-run",
            ]
        )
        == 0
    )
    assert captured["account_id"] == "5028760335873601"
    assert captured["display_name"] == "alicloud-testing-infra-dev"
    assert captured["dry_run"] is True
    assert '"rows_written": 1' in capsys.readouterr().out


def test_alibaba_summary_sync_upserts_generic_summary_ledger() -> None:
    engine = _sqlite_engine()
    try:
        result = run_sync_alibaba_billing_summary(
            engine,
            settings=AlibabaBillingSettings(page_size=1),
            account_id="5028760335873601",
            display_name="alicloud-testing-infra-dev",
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 1),
            earliest_usage_date=date(2026, 8, 1),
            fetch_rows=lambda **_kwargs: iter(
                [
                    {
                        "vendor": "alibaba",
                        "account_id": "5028760335873601",
                        "billing_account_id": "5028760335873601",
                        "export_partition_date": date(2026, 8, 1),
                        "usage_date": date(2026, 8, 2),
                        "service_name": "Elastic Compute Service",
                        "sku_name": "ecs_intl",
                        "usage_type": "Pay-As-You-Go",
                        "region": "Singapore",
                        "org": "tenant-1",
                        "resource_name": "instance-1",
                        "vendor_tags_json": {
                            "instance_tag": "key:tenant value:tenant-1",
                            "tenant": "tenant-1",
                        },
                        "list_cost": "1.234567891",
                        "effective_cost": "1.000000000",
                        "credit_amount": "-0.100000000",
                        "net_cost": "0.900000000",
                        "source_export_time": "2026-08-02T00:00:00Z",
                    }
                ]
            ),
        )

        assert result.account_id == "5028760335873601"
        assert result.rows_seen == result.rows_written == 1
        with engine.begin() as connection:
            summary = connection.execute(
                text(
                    """
                    SELECT vendor, account_id, billing_account_id, export_partition_date, usage_date,
                      org, vendor_tags_json, list_cost, effective_cost, credit_amount, net_cost
                    FROM cost_bq_export_summary_daily
                    """
                )
            ).one()
            source = connection.execute(
                text("SELECT vendor, account_id, billing_account_id, display_name FROM cost_sources")
            ).one()
            job = connection.execute(text("SELECT job_name, last_status FROM cost_job_state")).one()
        assert summary[:6] == (
            "alibaba",
            "5028760335873601",
            "5028760335873601",
            "2026-08-01",
            "2026-08-02",
            "tenant-1",
        )
        assert summary[6] == '{"instance_tag":"key:tenant value:tenant-1","tenant":"tenant-1"}'
        assert tuple(Decimal(str(value)) for value in summary[7:]) == (
            Decimal("1.234567891"),
            Decimal("1.0"),
            Decimal("-0.1"),
            Decimal("0.9"),
        )
        assert source == (
            "alibaba",
            "5028760335873601",
            "5028760335873601",
            "alicloud-testing-infra-dev",
        )
        assert job == ("sync_alibaba_billing_summary:alibaba:5028760335873601", "succeeded")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--replace-existing-partitions",), "requires --export-partition-start"),
        (("--replace-usage-start-date", "2026-08-01"), "must be set together"),
        (
            (
                "--replace-usage-start-date",
                "2026-08-01",
                "--replace-usage-end-date",
                "2026-08-01",
            ),
            "requires --replace-existing-partitions",
        ),
    ),
)
def test_cli_rejects_unsafe_alibaba_replacement_requests(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda require_database=True: SimpleNamespace(
            alibaba_billing=AlibabaBillingSettings(),
            log_level="INFO",
        ),
    )
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)

    with pytest.raises(ValueError, match=message):
        cli.main(["sync-alibaba-billing-summary", *arguments])


def test_alibaba_summary_sync_replaces_an_export_partition() -> None:
    engine = _sqlite_engine()
    try:
        def source_row(list_cost: str):
            return iter(
                [
                    {
                        "vendor": "alibaba",
                        "account_id": "5028760335873601",
                        "billing_account_id": "5028760335873601",
                        "export_partition_date": date(2026, 8, 1),
                        "usage_date": date(2026, 8, 1),
                        "service_name": "Elastic Compute Service",
                        "sku_name": "ecs_intl",
                        "list_cost": list_cost,
                    }
                ]
            )

        kwargs = {
            "settings": AlibabaBillingSettings(page_size=1),
            "account_id": "5028760335873601",
            "display_name": "alicloud-testing-infra-dev",
            "export_partition_start": date(2026, 8, 1),
            "export_partition_end": date(2026, 8, 1),
            "earliest_usage_date": date(2026, 8, 1),
        }
        run_sync_alibaba_billing_summary(
            engine,
            **kwargs,
            fetch_rows=lambda **_kwargs: source_row("1"),
        )
        result = run_sync_alibaba_billing_summary(
            engine,
            **kwargs,
            replace_existing_partitions=True,
            fetch_rows=lambda **_kwargs: source_row("2"),
        )

        with engine.begin() as connection:
            costs = connection.execute(
                text("SELECT list_cost FROM cost_bq_export_summary_daily")
            ).scalars().all()
        assert result.rows_seen == result.rows_written == 1
        assert costs == [2.0]
    finally:
        engine.dispose()


def test_summary_helpers_ignore_empty_rows_and_reject_untrusted_target_tables() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            _delete_legacy_summary_rows(connection, [])
            _write_summary_rows(connection, [])
        with pytest.raises(ValueError, match="Invalid SQL table identifier"):
            _quote_sql_table("cost_summary; DROP TABLE cost_sources")
    finally:
        engine.dispose()


def test_summary_partition_replacement_validates_its_scope() -> None:
    engine = _sqlite_engine()
    try:
        assert write_summary_rows(engine, [], dry_run=False) == 0
        with pytest.raises(ValueError, match="usage_start_date"):
            replace_summary_partition_usage_dates(
                engine,
                [],
                vendor="alibaba",
                account_id="5028760335873601",
                export_partition_date=date(2026, 8, 1),
                usage_start_date=date(2026, 8, 2),
                usage_end_date=date(2026, 8, 1),
                dry_run=False,
                batch_size=1,
            )
        with pytest.raises(ValueError, match="batch_size"):
            replace_summary_partition_usage_dates(
                engine,
                [],
                vendor="alibaba",
                account_id="5028760335873601",
                export_partition_date=date(2026, 8, 1),
                usage_start_date=date(2026, 8, 1),
                usage_end_date=date(2026, 8, 1),
                dry_run=False,
                batch_size=0,
            )
        assert (
            replace_summary_partition_usage_dates(
                engine,
                [],
                vendor="alibaba",
                account_id="5028760335873601",
                export_partition_date=date(2026, 8, 1),
                usage_start_date=date(2026, 8, 1),
                usage_end_date=date(2026, 8, 1),
                dry_run=True,
                batch_size=1,
            )
            == 0
        )
        with pytest.raises(ValueError, match="usage_start_date"):
            replace_summary_usage_dates(
                engine,
                [],
                row_count=0,
                vendor="alibaba",
                account_id="5028760335873601",
                usage_start_date=date(2026, 8, 2),
                usage_end_date=date(2026, 8, 1),
                dry_run=False,
                batch_size=1,
            )
        outside_scope = _normalize_summary_row(
            {
                "vendor": "alibaba",
                "account_id": "5028760335873601",
                "export_partition_date": date(2026, 8, 1),
                "usage_date": date(2026, 8, 2),
            }
        )
        with pytest.raises(ValueError, match="outside the partition usage-date replacement scope"):
            replace_summary_partition_usage_dates(
                engine,
                [outside_scope],
                vendor="alibaba",
                account_id="5028760335873601",
                export_partition_date=date(2026, 8, 1),
                usage_start_date=date(2026, 8, 1),
                usage_end_date=date(2026, 8, 1),
                dry_run=False,
                batch_size=1,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("vendor", "job_name", "message"),
    (("", "sync_alibaba_billing_summary", "vendor must not be empty"), ("alibaba", "", "job_name must not be empty")),
)
def test_generic_summary_sync_requires_vendor_and_job_name(vendor, job_name, message) -> None:
    with pytest.raises(ValueError, match=message):
        run_sync_billing_summary(
            object(),
            settings=AlibabaBillingSettings(),
            vendor=vendor,
            job_name=job_name,
        )
    with pytest.raises(ValueError, match="must be set together"):
        run_sync_billing_summary(
            object(),
            settings=AlibabaBillingSettings(),
            vendor="alibaba",
            job_name="sync_alibaba_billing_summary",
            replacement_usage_start_date=date(2026, 8, 1),
        )
    with pytest.raises(ValueError, match="start date must be before"):
        run_sync_billing_summary(
            object(),
            settings=AlibabaBillingSettings(),
            vendor="alibaba",
            job_name="sync_alibaba_billing_summary",
            replacement_usage_start_date=date(2026, 8, 2),
            replacement_usage_end_date=date(2026, 8, 1),
        )


@pytest.mark.parametrize("replace_existing_partitions", (False, True))
def test_alibaba_summary_sync_rejects_rows_from_another_vendor(
    replace_existing_partitions,
) -> None:
    engine = _sqlite_engine()
    try:
        with pytest.raises(ValueError, match="does not match 'alibaba'"):
            run_sync_alibaba_billing_summary(
                engine,
                settings=AlibabaBillingSettings(),
                account_id="5028760335873601",
                display_name="alicloud-testing-infra-dev",
                export_partition_start=date(2026, 8, 1),
                export_partition_end=date(2026, 8, 1),
                earliest_usage_date=date(2026, 8, 1),
                replace_existing_partitions=replace_existing_partitions,
                fetch_rows=lambda **_kwargs: iter(
                    [
                        {
                            "vendor": "gcp",
                            "account_id": "5028760335873601",
                            "export_partition_date": date(2026, 8, 1),
                            "usage_date": date(2026, 8, 1),
                        }
                    ]
                ),
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("row", "message"),
    (
        ({"export_partition_date": date(2026, 8, 1), "usage_date": date(2026, 8, 1)}, "account_id"),
        ({"account_id": "account-1", "usage_date": date(2026, 8, 1)}, "export_partition_date"),
        ({"account_id": "account-1", "export_partition_date": date(2026, 8, 1)}, "usage_date"),
    ),
)
def test_summary_normalization_requires_ledger_identity(row, message) -> None:
    with pytest.raises(ValueError, match=message):
        _normalize_summary_row(row)
