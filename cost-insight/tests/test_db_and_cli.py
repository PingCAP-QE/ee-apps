from datetime import date
from types import SimpleNamespace

from cost_insight.common import db
from cost_insight.common.config import DatabaseSettings, GcpBillingSettings, Settings
from cost_insight.common.logging import configure_logging
from cost_insight.jobs import cli
from cost_insight.jobs.backfill_cost_refine_from_raw import BackfillCostRefineFromRawSummary
from cost_insight.jobs.refresh_attribution_daily import RefreshAttributionSummary
from cost_insight.jobs.sync_gcp_billing_summary import SyncGcpBillingSummaryResult
from cost_insight.jobs.sync_gcp_billing_export import SyncGcpBillingSummary
from cost_insight.jobs.sync_gcp_unmatched_resources import SyncGcpUnmatchedResourcesSummary


def test_build_engine_uses_database_url(monkeypatch) -> None:
    calls = []

    def fake_create_engine(*args, **kwargs):
        calls.append((args, kwargs))
        return "engine"

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    settings = Settings(
        database=DatabaseSettings(
            url="mysql+pymysql://user:pass@host:4000/cost",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca="/tmp/ca.pem",
        )
    )

    assert db.build_engine(settings) == "engine"
    assert calls[0][0][0] == "mysql+pymysql://user:pass@host:4000/cost"
    assert calls[0][1]["pool_pre_ping"] is True
    assert calls[0][1]["connect_args"] == {"ssl": {"ca": "/tmp/ca.pem"}}


def test_build_engine_uses_tidb_parts(monkeypatch) -> None:
    calls = []

    def fake_create_engine(*args, **kwargs):
        calls.append((args, kwargs))
        return "engine"

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    settings = Settings(
        database=DatabaseSettings(
            url=None,
            host="127.0.0.1",
            port=4000,
            user="user",
            password="pass",
            database="cost",
            ssl_ca=None,
        )
    )

    assert db.build_engine(settings) == "engine"
    assert "mysql+pymysql://user:***@127.0.0.1:4000/cost?charset=utf8mb4" in str(calls[0][0][0])
    assert calls[0][1]["connect_args"] == {}


def test_configure_logging_accepts_unknown_level() -> None:
    configure_logging("not-a-level")


def test_cli_runs_sync_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_run(engine, *, settings, start_date, end_date, dry_run, limit):
        captured["engine"] = engine
        captured["settings"] = settings
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        captured["dry_run"] = dry_run
        captured["limit"] = limit
        return SyncGcpBillingSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_seen=1,
            rows_written=0,
            dry_run=dry_run,
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_gcp_billing_export", fake_run)

    exit_code = cli.main(
        [
            "sync-gcp-billing-export",
            "--start-date",
            "2026-05-17",
            "--end-date",
            "2026-05-18",
            "--limit",
            "10",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["start_date"] == date(2026, 5, 17)
    assert captured["end_date"] == date(2026, 5, 18)
    assert captured["dry_run"] is True
    assert captured["limit"] == 10
    assert '"rows_seen": 1' in output


def test_cli_split_by_day_runs_each_date(monkeypatch, capsys) -> None:
    calls = []

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_run(_engine, *, settings, start_date, end_date, dry_run, limit):
        calls.append((start_date, end_date, dry_run, limit))
        return SyncGcpBillingSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_seen=1,
            rows_written=1,
            dry_run=dry_run,
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_gcp_billing_export", fake_run)

    assert (
        cli.main(
            [
                "sync-gcp-billing-export",
                "--start-date",
                "2026-05-10",
                "--end-date",
                "2026-05-12",
                "--split-by-day",
            ]
        )
        == 0
    )

    assert calls == [
        (date(2026, 5, 10), date(2026, 5, 10), False, None),
        (date(2026, 5, 11), date(2026, 5, 11), False, None),
        (date(2026, 5, 12), date(2026, 5, 12), False, None),
    ]
    assert '"start_date": "2026-05-10"' in capsys.readouterr().out


def test_cli_runs_refresh_attribution_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_refresh(engine, *, settings, start_date, end_date, dry_run):
        captured["engine"] = engine
        captured["settings"] = settings
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        captured["dry_run"] = dry_run
        return RefreshAttributionSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=2,
            rows_inserted=3,
            dry_run=dry_run,
            raw_rows=10 if dry_run else None,
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_refresh_cost_attribution_daily", fake_refresh)

    exit_code = cli.main(
        [
            "refresh-cost-attribution-daily",
            "--start-date",
            "2026-05-09",
            "--end-date",
            "2026-05-17",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["start_date"] == date(2026, 5, 9)
    assert captured["end_date"] == date(2026, 5, 17)
    assert captured["dry_run"] is True
    assert '"rows_inserted": 3' in output
    assert '"raw_rows": 10' in output


def test_cli_runs_sync_billing_summary_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_run(engine, **kwargs):
        captured.update(kwargs)
        return SyncGcpBillingSummaryResult(
            account_id=kwargs["settings"].account_id,
            export_partition_start=kwargs["export_partition_start"],
            export_partition_end=kwargs["export_partition_end"],
            rows_seen=2,
            rows_written=2,
            dry_run=kwargs["dry_run"],
            touched_usage_dates=(date(2026, 5, 17),),
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_gcp_billing_summary", fake_run)

    exit_code = cli.main(
        [
            "sync-gcp-billing-summary",
            "--export-partition-start",
            "2026-05-17",
            "--export-partition-end",
            "2026-05-18",
            "--earliest-usage-date",
            "2026-01-01",
            "--limit",
            "10",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["export_partition_start"] == date(2026, 5, 17)
    assert captured["export_partition_end"] == date(2026, 5, 18)
    assert captured["earliest_usage_date"] == date(2026, 1, 1)
    assert captured["limit"] == 10
    assert '"touched_usage_dates": [' in output


def test_cli_runs_sync_unmatched_resources_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_run(engine, **kwargs):
        captured.update(kwargs)
        return SyncGcpUnmatchedResourcesSummary(
            account_id=kwargs["settings"].account_id,
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            export_partition_start=date(2026, 5, 17),
            export_partition_end=date(2026, 5, 24),
            rows_seen=3,
            rows_written=3,
            dry_run=kwargs["dry_run"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_gcp_unmatched_resources", fake_run)

    exit_code = cli.main(
        [
            "sync-gcp-unmatched-resources",
            "--usage-start-date",
            "2026-05-17",
            "--usage-end-date",
            "2026-05-18",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["usage_start_date"] == date(2026, 5, 17)
    assert captured["usage_end_date"] == date(2026, 5, 18)
    assert '"rows_seen": 3' in output


def test_cli_runs_backfill_cost_refine_from_raw_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_run(engine, **kwargs):
        captured.update(kwargs)
        return BackfillCostRefineFromRawSummary(
            account_id=kwargs["settings"].account_id,
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            summary_rows_seen=2,
            summary_rows_written=2,
            unmatched_rows_seen=1,
            unmatched_rows_written=1,
            export_partition_start=date(2026, 5, 17),
            export_partition_end=date(2026, 5, 20),
            dry_run=kwargs["dry_run"],
            marked_summary_watermark=kwargs["mark_summary_watermark"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_backfill_cost_refine_from_raw", fake_run)

    exit_code = cli.main(
        [
            "backfill-gcp-cost-refine-from-raw",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-05-20",
            "--mark-summary-watermark",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["start_date"] == date(2026, 1, 1)
    assert captured["end_date"] == date(2026, 5, 20)
    assert captured["include_unmatched_resources"] is True
    assert captured["mark_summary_watermark"] is True
    assert captured["dry_run"] is True
    assert '"summary_rows_seen": 2' in output
    assert '"marked_summary_watermark": true' in output


def test_cli_runs_refresh_attribution_from_summary_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_refresh(engine, *, settings, start_date, end_date, dry_run):
        captured["engine"] = engine
        captured["settings"] = settings
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        captured["dry_run"] = dry_run
        return RefreshAttributionSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=2,
            rows_inserted=3,
            dry_run=dry_run,
            summary_rows=10 if dry_run else None,
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_refresh_cost_attribution_from_summary", fake_refresh)

    exit_code = cli.main(
        [
            "refresh-cost-attribution-from-summary",
            "--start-date",
            "2026-05-09",
            "--end-date",
            "2026-05-17",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["start_date"] == date(2026, 5, 9)
    assert captured["end_date"] == date(2026, 5, 17)
    assert '"summary_rows": 10' in output


def test_cli_refresh_attribution_split_by_day_runs_each_date(monkeypatch, capsys) -> None:
    calls = []

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        log_level="INFO",
    )

    def fake_refresh(_engine, *, settings, start_date, end_date, dry_run):
        calls.append((start_date, end_date, dry_run))
        return RefreshAttributionSummary(
            account_id=settings.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=0,
            rows_inserted=1,
            dry_run=dry_run,
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_refresh_cost_attribution_daily", fake_refresh)

    assert (
        cli.main(
            [
                "refresh-cost-attribution-daily",
                "--start-date",
                "2026-05-09",
                "--end-date",
                "2026-05-11",
                "--split-by-day",
            ]
        )
        == 0
    )

    assert calls == [
        (date(2026, 5, 9), date(2026, 5, 9), False),
        (date(2026, 5, 10), date(2026, 5, 10), False),
        (date(2026, 5, 11), date(2026, 5, 11), False),
    ]
    assert '"start_date": "2026-05-09"' in capsys.readouterr().out


def test_date_range_rejects_invalid_range() -> None:
    try:
        list(cli._date_range(date(2026, 5, 12), date(2026, 5, 10)))
    except ValueError as exc:
        assert "--start-date" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
