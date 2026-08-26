from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common import db
from cost_insight.common.config import (
    AwsBillingSettings,
    DatabaseSettings,
    GcpBillingSettings,
    Settings,
    TcmsAllocationSettings,
)
from cost_insight.common.logging import configure_logging
from cost_insight.jobs import cli
from cost_insight.jobs.bootstrap_gcs_cache_last_seen import BootstrapGcsCacheLastSeenResult
from cost_insight.jobs.cleanup_gcs_cache import CleanupGcsCacheSummary
from cost_insight.jobs.materialize_cost_allocations import MaterializeCostAllocationsSummary
from cost_insight.jobs.refresh_attribution_daily import (    CostAttributionSource,
    RefreshAttributionSummary,
)
from cost_insight.jobs.sync_aws_billing_summary import (
    AWS_SPLIT_COST_SCHEMA_VERSION,
    AwsBillingSource,
)
from cost_insight.jobs.sync_aws_parent_residual_allocations import (
    SyncAwsParentResidualAllocationsResult,
)
from cost_insight.jobs.sync_aws_kubernetes_workload_allocations import (
    SyncAwsKubernetesWorkloadAllocationsSummary,
)
from cost_insight.jobs.sync_gcs_cache_last_seen import SyncGcsCacheLastSeenResult
from cost_insight.jobs.sync_gcp_billing_summary import SyncGcpBillingSummaryResult
from cost_insight.jobs.sync_gcp_kubernetes_workload_allocations import (
    SyncGcpKubernetesWorkloadAllocationsSummary,
)
from cost_insight.jobs.sync_gcp_unmatched_resources import SyncGcpUnmatchedResourcesSummary
from cost_insight.jobs.sync_gcs_cache_ac_references import SyncGcsCacheAcReferencesSummary


def test_cli_cutover_does_not_accept_limit() -> None:
    with pytest.raises(SystemExit):
        cli.main(
            [
                "cutover-aws-split-cost",
                "--usage-start-date",
                "2026-05-01",
                "--usage-end-date",
                "2026-05-01",
                "--limit",
                "1",
            ]
        )


def test_cli_rejects_cross_month_split_cost_cutover(monkeypatch) -> None:
    settings = SimpleNamespace(log_level="INFO")
    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)

    with pytest.raises(ValueError, match="only accepts one billing month"):
        cli.main(
            [
                "cutover-aws-split-cost",
                "--usage-start-date",
                "2026-08-31",
                "--usage-end-date",
                "2026-09-01",
            ]
        )


def test_cli_cutover_reuses_summary_guardrail_for_unmatched_and_residual(monkeypatch, capsys) -> None:
    captured: dict[str, dict] = {}

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        aws_billing=AwsBillingSettings(account_id="946646677266"),
        tcms_allocation=TcmsAllocationSettings(),
        log_level="INFO",
    )
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 8, 2),
    )

    def fake_summary(_engine, **kwargs):
        captured["summary"] = kwargs
        return SyncGcpBillingSummaryResult(
            account_id=source.account_id,
            export_partition_start=kwargs["export_partition_start"],
            export_partition_end=kwargs["export_partition_end"],
            rows_seen=1,
            rows_written=1,
            dry_run=kwargs["dry_run"],
            touched_usage_dates=(date(2026, 8, 2),),
        )

    def fake_unmatched(_engine, **kwargs):
        captured["unmatched"] = kwargs
        return SyncGcpUnmatchedResourcesSummary(
            account_id=source.account_id,
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            export_partition_start=kwargs["export_partition_start"],
            export_partition_end=kwargs["export_partition_end"],
            rows_seen=1,
            rows_written=1,
            dry_run=kwargs["dry_run"],
        )

    def fake_residual(_engine, **kwargs):
        captured["residual"] = kwargs
        return SyncAwsParentResidualAllocationsResult(
            account_id=source.account_id,
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            rows_seen=1,
            rows_written=1,
            parent_days=1,
            dry_run=kwargs["dry_run"],
        )

    def fake_attribution(_engine, **kwargs):
        captured["attribution"] = kwargs
        return RefreshAttributionSummary(
            vendor="aws",
            account_id=source.account_id,
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            rows_deleted=1,
            rows_inserted=1,
            dry_run=kwargs["dry_run"],
        )

    def fake_kubernetes_allocations(_engine, **kwargs):
        captured["kubernetes"] = kwargs
        return SyncAwsKubernetesWorkloadAllocationsSummary(
            account_id=source.account_id,
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            summary_rows_seen=1,
            residual_allocation_rows_seen=1,
            rows_written=1,
            dry_run=kwargs["dry_run"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "_resolve_aws_split_cutover_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(cli, "run_sync_aws_billing_summary", fake_summary)
    monkeypatch.setattr(cli, "run_sync_aws_unmatched_resources", fake_unmatched)
    monkeypatch.setattr(cli, "run_sync_aws_parent_residual_allocations", fake_residual)
    monkeypatch.setattr(cli, "run_sync_aws_kubernetes_workload_allocations", fake_kubernetes_allocations)
    monkeypatch.setattr(cli, "run_refresh_cost_attribution_from_summary", fake_attribution)

    assert (
        cli.main(
            [
                "cutover-aws-split-cost",
                "--usage-start-date",
                "2026-08-02",
                "--usage-end-date",
                "2026-08-15",
                "--dry-run",
            ]
        )
        == 0
    )
    assert captured["summary"]["validate_guardrail"] is True
    assert captured["unmatched"]["validate_guardrail"] is False
    assert captured["residual"]["validate_guardrail"] is False
    assert captured["kubernetes"]["batch_size"] == settings.aws_billing.page_size
    assert '"rows_written": 1' in capsys.readouterr().out


def test_cli_cutover_can_skip_unmatched_resources(monkeypatch, capsys) -> None:
    calls: list[str] = []

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        aws_billing=AwsBillingSettings(account_id="946646677266"),
        tcms_allocation=TcmsAllocationSettings(),
        log_level="INFO",
    )
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 8, 2),
    )

    def fake_result(name: str):
        def run(*_args, **_kwargs):
            calls.append(name)
            return SimpleNamespace(step=name)

        return run

    def fail_unmatched(*_args, **_kwargs):
        pytest.fail("unmatched sync should be skipped")

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "_resolve_aws_split_cutover_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(cli, "run_sync_aws_billing_summary", fake_result("summary"))
    monkeypatch.setattr(cli, "run_sync_aws_unmatched_resources", fail_unmatched)
    monkeypatch.setattr(cli, "run_sync_aws_parent_residual_allocations", fake_result("residual"))
    monkeypatch.setattr(
        cli, "run_sync_aws_kubernetes_workload_allocations", fake_result("kubernetes")
    )
    monkeypatch.setattr(
        cli, "run_refresh_cost_attribution_from_summary", fake_result("attribution")
    )

    assert (
        cli.main(
            [
                "cutover-aws-split-cost",
                "--usage-start-date",
                "2026-08-02",
                "--usage-end-date",
                "2026-08-15",
                "--skip-unmatched-resources",
                "--dry-run",
            ]
        )
        == 0
    )

    assert calls == ["summary", "residual", "kubernetes", "attribution"]
    assert '"step": "unmatched"' not in capsys.readouterr().out


def test_cli_cutover_uses_one_transaction_and_rolls_back_on_failure(monkeypatch) -> None:
    captured: list[object] = []

    class Engine:
        def __init__(self) -> None:
            self.transactions: list[str] = []

        @contextmanager
        def begin(self):
            self.transactions.append("started")
            try:
                yield object()
            except Exception:
                self.transactions.append("rolled_back")
                raise
            else:
                self.transactions.append("committed")

        def dispose(self):
            pass

    engine = Engine()
    settings = SimpleNamespace(
        aws_billing=AwsBillingSettings(account_id="946646677266"),
        tcms_allocation=TcmsAllocationSettings(),
        log_level="INFO",
    )
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 8, 2),
    )

    def fake_summary(cutover_engine, **_kwargs):
        captured.append(cutover_engine)
        return object()

    def fail_unmatched(cutover_engine, **_kwargs):
        captured.append(cutover_engine)
        raise RuntimeError("unmatched failed")

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: engine)
    monkeypatch.setattr(cli, "_resolve_aws_split_cutover_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(cli, "run_sync_aws_billing_summary", fake_summary)
    monkeypatch.setattr(cli, "run_sync_aws_unmatched_resources", fail_unmatched)

    with pytest.raises(RuntimeError, match="unmatched failed"):
        cli.main(
            [
                "cutover-aws-split-cost",
                "--usage-start-date",
                "2026-08-02",
                "--usage-end-date",
                "2026-08-15",
            ]
        )

    assert engine.transactions == ["started", "rolled_back"]
    assert len(captured) == 2
    assert captured[0] is captured[1]


def _sqlite_source_engine():
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
                INSERT INTO cost_sources (vendor, account_id, display_name, is_active)
                VALUES
                  ('gcp', 'pingcap-testing-account', 'PingCAP Testing', 1),
                  ('gcp', 'qa-infra-dev', 'QA Infra Dev', 1),
                  ('aws', '946646677266', 'QA Infra Dev AWS', 1)
                """
            )
        )
    return engine


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


def test_cli_runs_sync_gcs_cache_last_seen_without_database(monkeypatch, capsys) -> None:
    calls = []
    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        gcs_cache=SimpleNamespace(),
        log_level="INFO",
    )

    def fake_get_settings(require_database=True):
        calls.append(require_database)
        return settings

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        cli,
        "run_sync_gcs_cache_last_seen",
        lambda **kwargs: SyncGcsCacheLastSeenResult(
            account_id="pingcap-testing-account",
            bucket_name="pingcap-ci-bazel-remote-cache-us-central1",
            run_date=date(2026, 6, 8),
            source_rows_seen=123,
            distinct_objects=45,
            dry_run=True,
            bytes_processed=678,
        ),
    )

    exit_code = cli.main(["sync-gcs-cache-last-seen", "--run-date", "2026-06-08", "--dry-run"])

    assert exit_code == 0
    assert calls == [False]
    assert '"distinct_objects": 45' in capsys.readouterr().out


def test_cli_runs_bootstrap_gcs_cache_last_seen_without_database(monkeypatch, capsys) -> None:
    calls = []
    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        gcs_cache=SimpleNamespace(),
        log_level="INFO",
    )

    def fake_get_settings(require_database=True):
        calls.append(require_database)
        return settings

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        cli,
        "run_bootstrap_gcs_cache_last_seen",
        lambda **kwargs: BootstrapGcsCacheLastSeenResult(
            account_id="pingcap-testing-account",
            bucket_name="pingcap-ci-bazel-remote-cache-us-central1",
            start_date=date(2026, 5, 25),
            end_date=date(2026, 6, 9),
            source_rows_seen=456,
            distinct_objects=78,
            dry_run=True,
            bytes_processed=910,
        ),
    )

    exit_code = cli.main(
        [
            "bootstrap-gcs-cache-last-seen",
            "--start-date",
            "2026-05-25",
            "--end-date",
            "2026-06-09",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert calls == [False]
    assert '"distinct_objects": 78' in capsys.readouterr().out


def test_cli_runs_cleanup_gcs_cache_without_database(monkeypatch, capsys) -> None:
    calls = []
    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        gcs_cache=SimpleNamespace(),
        log_level="INFO",
    )

    def fake_get_settings(require_database=True):
        calls.append(require_database)
        return settings

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        cli,
        "run_cleanup_gcs_cache",
        lambda **kwargs: CleanupGcsCacheSummary(
            account_id="pingcap-testing-account",
            bucket_name="pingcap-ci-bazel-remote-cache-us-central1",
            run_id="run-001",
            mode="dry-run",
            execute_kind="cas",
            dry_run=True,
            ac_retention_days=10,
            cas_retention_days=15,
            safety_buffer_days=1,
            candidate_cas_object_count=99,
            candidate_ac_object_count=10,
            candidate_cas_delete_object_count=89,
            ac_parse_error_count=0,
            selected_ac_object_count=0,
            selected_cas_object_count=0,
            oldest_last_seen_at=None,
            newest_last_seen_at=None,
            sample_candidates=(),
            sample_ac_parse_errors=(),
            bytes_processed=456,
            run_started_at=datetime(2026, 6, 15, 0, 0, tzinfo=UTC),
            run_finished_at=datetime(2026, 6, 15, 0, 0, tzinfo=UTC),
        ),
    )

    exit_code = cli.main(["cleanup-gcs-cache", "--mode", "dry-run"])

    assert exit_code == 0
    assert calls == [False]
    assert '"candidate_cas_object_count": 99' in capsys.readouterr().out


def test_cli_surfaces_cleanup_gcs_cache_delete_requires_specific_execute_kind(monkeypatch) -> None:
    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        gcs_cache=SimpleNamespace(),
        log_level="INFO",
    )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    with pytest.raises(ValueError, match="requires --execute-kind cas"):
        cli.main(["cleanup-gcs-cache", "--mode", "delete"])


def test_cli_rejects_non_positive_cleanup_sample_limit(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit, match="2"):
        parser.parse_args(["cleanup-gcs-cache", "--sample-limit", "-1"])

    assert "expected a positive integer" in capsys.readouterr().err


def test_cli_rejects_non_positive_cleanup_safety_buffer_days(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit, match="2"):
        parser.parse_args(["cleanup-gcs-cache", "--safety-buffer-days", "0"])

    assert "expected a positive integer" in capsys.readouterr().err


def test_cli_runs_sync_gcs_cache_ac_references_without_database(monkeypatch, capsys) -> None:
    calls = []
    run_kwargs = []
    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        gcs_cache=SimpleNamespace(),
        log_level="INFO",
    )

    def fake_get_settings(require_database=True):
        calls.append(require_database)
        return settings

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)

    def fake_run_sync_gcs_cache_ac_references(**kwargs):
        run_kwargs.append(kwargs)
        return SyncGcsCacheAcReferencesSummary(
            account_id="pingcap-testing-account",
            bucket_name="pingcap-ci-bazel-remote-cache-us-central1",
            mode="bootstrap",
            shard_start=0,
            shard_end=15,
            source_object_count=11,
            missing_object_count=1,
            parse_error_count=0,
            replaced_ac_object_count=11,
            reference_row_count=22,
            sample_parse_errors=(),
            dry_run=True,
            indexed_through=datetime(2026, 6, 22, 0, 0, tzinfo=UTC),
            bytes_processed=33,
            run_started_at=datetime(2026, 6, 22, 0, 0, tzinfo=UTC),
            run_finished_at=datetime(2026, 6, 22, 0, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(
        cli,
        "run_sync_gcs_cache_ac_references",
        fake_run_sync_gcs_cache_ac_references,
    )

    exit_code = cli.main(
        [
            "sync-gcs-cache-ac-references",
            "--mode",
            "bootstrap",
            "--shard-start",
            "0",
            "--shard-end",
            "15",
            "--dry-run",
            "--skip-ensure-tables",
        ]
    )

    assert exit_code == 0
    assert calls == [False]
    assert run_kwargs[0]["ensure_tables"] is False
    assert '"reference_row_count": 22' in capsys.readouterr().out


def test_cli_rejects_negative_cleanup_shard_start(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit, match="2"):
        parser.parse_args(
            ["sync-gcs-cache-ac-references", "--mode", "bootstrap", "--shard-start", "-1"]
        )

    assert "expected a non-negative integer" in capsys.readouterr().err


def test_cli_runs_sync_billing_summary_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
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
    monkeypatch.setattr(
        cli,
        "_list_sources",
        lambda _engine, *, vendor: [SimpleNamespace(account_id="pingcap-testing-account")],
    )
    monkeypatch.setattr(cli, "run_sync_gcp_billing_summary", fake_run)

    exit_code = cli.main(
        [
            "sync-gcp-billing-summary",
            "--export-partition-start",
            "2026-05-17",
            "--export-partition-end",
            "2026-05-17",
            "--earliest-usage-date",
            "2026-01-01",
            "--account-id",
            "pingcap-testing-account",
            "--replace-usage-start-date",
            "2026-05-01",
            "--replace-usage-end-date",
            "2026-05-17",
            "--dry-run",
            "--replace-existing-partitions",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["export_partition_start"] == date(2026, 5, 17)
    assert captured["export_partition_end"] == date(2026, 5, 17)
    assert captured["earliest_usage_date"] == date(2026, 1, 1)
    assert captured["limit"] is None
    assert captured["replace_existing_partitions"] is True
    assert captured["replacement_usage_start_date"] == date(2026, 5, 1)
    assert captured["replacement_usage_end_date"] == date(2026, 5, 17)
    assert '"touched_usage_dates": [' in output


def test_resolve_gcp_sources_rejects_unregistered_scoped_account(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_list_sources", lambda _engine, *, vendor: [])

    with pytest.raises(ValueError, match="is not active"):
        cli._resolve_gcp_sources(
            object(),
            settings=GcpBillingSettings(account_id="pingcap-testing-account"),
            account_id="typo-account",
        )


def test_cli_rejects_scoped_gcp_replacement_without_account_id(monkeypatch) -> None:
    settings = SimpleNamespace(log_level="INFO")
    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)

    with pytest.raises(ValueError, match="requires --account-id"):
        cli.main(
            [
                "sync-gcp-billing-summary",
                "--export-partition-start",
                "2026-05-17",
                "--export-partition-end",
                "2026-05-17",
                "--replace-existing-partitions",
                "--replace-usage-start-date",
                "2026-05-01",
                "--replace-usage-end-date",
                "2026-05-17",
            ]
        )


def test_cli_runs_sync_unmatched_resources_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
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


def test_cli_runs_materialize_cost_allocations(monkeypatch, capsys) -> None:
    captured = {}

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(page_size=123),
        log_level="INFO",
    )

    def fake_run(_engine, **kwargs):
        captured.update(kwargs)
        return MaterializeCostAllocationsSummary(
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            allocation_version="v1",
            windows_seen=1,
            rows_written=3,
            dry_run=kwargs["dry_run"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_materialize_cost_allocations", fake_run)
    monkeypatch.setenv("COST_ALLOCATION_EARLIEST_DATE", "2026-08-10")

    assert cli.main(
        [
            "materialize-cost-allocations",
            "--start-date", "2026-08-10",
            "--end-date", "2026-08-10",
            "--eq-root-lark-group-id", "eq",
            "--allocation-version", "v1",
            "--processing-start-date", "2026-08-10",
            "--processing-end-date", "2026-08-10",
            "--no-publish",
        ]
    ) == 0
    assert captured["eq_root_lark_group_id"] == "eq"
    assert captured["earliest_date"] == date(2026, 8, 10)
    assert captured["batch_size"] == 123
    assert captured["allocation_version"] == "v1"
    assert captured["processing_start_date"] == date(2026, 8, 10)
    assert captured["processing_end_date"] == date(2026, 8, 10)
    assert captured["publish"] is False
    assert '"allocation_version": "v1"' in capsys.readouterr().out


def test_cli_publishes_a_staged_materialization_version(monkeypatch, capsys) -> None:
    captured = {}

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(page_size=123),
        log_level="INFO",
    )
    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(
        cli,
        "publish_materialized_cost_allocations",
        lambda _engine, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setenv("COST_ALLOCATION_EARLIEST_DATE", "2026-08-10")

    assert cli.main(
        [
            "materialize-cost-allocations",
            "--start-date", "2026-08-10",
            "--end-date", "2026-08-11",
            "--eq-root-lark-group-id", "eq",
            "--allocation-version", "v1",
            "--publish-only",
        ]
    ) == 0
    assert captured == {
        "start_date": date(2026, 8, 10),
        "end_date": date(2026, 8, 11),
        "earliest_date": date(2026, 8, 10),
        "allocation_version": "v1",
    }
    assert '"published": true' in capsys.readouterr().out


def test_cli_rejects_publish_only_processing_dates(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda require_database=True: SimpleNamespace(log_level="INFO"),
    )
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setenv("COST_ALLOCATION_EARLIEST_DATE", "2026-08-10")

    with pytest.raises(ValueError, match="publish-only cannot be combined with processing dates"):
        cli.main(
            [
                "materialize-cost-allocations",
                "--start-date", "2026-08-10",
                "--end-date", "2026-08-11",
                "--eq-root-lark-group-id", "eq",
                "--allocation-version", "v1",
                "--publish-only",
                "--processing-start-date", "2026-08-10",
                "--processing-end-date", "2026-08-10",
            ]
        )


def test_cli_runs_sync_gcp_kubernetes_workload_allocations_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        log_level="INFO",
    )

    def fake_run(engine, **kwargs):
        captured.update(kwargs)
        return SyncGcpKubernetesWorkloadAllocationsSummary(
            account_id=kwargs["settings"].account_id,
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            export_partition_start=kwargs["export_partition_start"],
            export_partition_end=kwargs["export_partition_end"],
            billing_rows_seen=4,
            direct_rows_seen=6,
            rows_written=8,
            dry_run=kwargs["dry_run"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_gcp_kubernetes_workload_allocations", fake_run)

    exit_code = cli.main(
        [
            "sync-gcp-kubernetes-workload-allocations",
            "--usage-start-date",
            "2026-05-17",
            "--usage-end-date",
            "2026-05-18",
            "--export-partition-start",
            "2026-05-20",
            "--export-partition-end",
            "2026-05-24",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["usage_start_date"] == date(2026, 5, 17)
    assert captured["usage_end_date"] == date(2026, 5, 18)
    assert captured["export_partition_start"] == date(2026, 5, 20)
    assert captured["export_partition_end"] == date(2026, 5, 24)
    assert captured["dry_run"] is True
    assert '"billing_rows_seen": 4' in output


def test_cli_runs_refresh_attribution_from_summary_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        tcms_allocation=TcmsAllocationSettings(),
        log_level="INFO",
    )

    def fake_refresh(
        engine,
        *,
        source,
        start_date,
        end_date,
        dry_run,
        tcms_allocation_table=None,
    ):
        captured["engine"] = engine
        captured["source"] = source
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        captured["dry_run"] = dry_run
        captured["tcms_allocation_table"] = tcms_allocation_table
        return RefreshAttributionSummary(
            vendor=source.vendor,
            account_id=source.account_id,
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
    assert captured["source"] == CostAttributionSource(
        vendor="gcp",
        account_id="pingcap-testing-account",
    )
    assert captured["tcms_allocation_table"] == "tcms_cost.resource_allocation"
    assert '"summary_rows": 10' in output


def test_date_range_rejects_invalid_range() -> None:
    try:
        list(cli._date_range(date(2026, 5, 12), date(2026, 5, 10)))
    except ValueError as exc:
        assert "--start-date" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_cli_runs_sync_aws_billing_summary_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(account_id="946646677266"),
        log_level="INFO",
    )

    def fake_run(engine, **kwargs):
        captured.update(kwargs)
        return SyncGcpBillingSummaryResult(
            account_id=kwargs["account_id"],
            export_partition_start=kwargs["export_partition_start"],
            export_partition_end=kwargs["export_partition_end"],
            rows_seen=4,
            rows_written=4,
            dry_run=kwargs["dry_run"],
            touched_usage_dates=(date(2026, 5, 17),),
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_aws_billing_summary", fake_run)

    exit_code = cli.main(
        [
            "sync-aws-billing-summary",
            "--export-partition-start",
            "2026-05-01",
            "--export-partition-end",
            "2026-05-01",
            "--replace-existing-partitions",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert disposed == [True]
    assert captured["account_id"] == "946646677266"
    assert captured["replace_existing_partitions"] is True
    assert '"account_id": "946646677266"' in output
    assert '"rows_written": 4' in output


def test_cli_sync_aws_summary_refreshes_ledger_for_split_source(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        aws_billing=AwsBillingSettings(account_id="946646677266", page_size=123),
        log_level="INFO",
    )
    legacy_source = AwsBillingSource(
        account_id="111111111111",
        billing_table="project.dataset.legacy",
    )
    split_source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 8, 2),
    )

    def fake_summary(_engine, **kwargs):
        if kwargs["account_id"] == legacy_source.account_id:
            touched_usage_dates = (date(2026, 8, 1),)
        else:
            touched_usage_dates = (date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3))
        return SyncGcpBillingSummaryResult(
            account_id=kwargs["account_id"],
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 1),
            rows_seen=1,
            rows_written=1,
            dry_run=False,
            touched_usage_dates=touched_usage_dates,
        )

    def fake_residual(_engine, **kwargs):
        captured.update(kwargs)
        return SyncAwsParentResidualAllocationsResult(
            account_id=kwargs["source"].account_id,
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            rows_seen=1,
            rows_written=1,
            parent_days=1,
            dry_run=kwargs["dry_run"],
        )

    def fake_kubernetes_allocations(_engine, **kwargs):
        captured["kubernetes"] = kwargs
        return SyncAwsKubernetesWorkloadAllocationsSummary(
            account_id=kwargs["source"].account_id,
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            summary_rows_seen=1,
            residual_allocation_rows_seen=1,
            rows_written=1,
            dry_run=kwargs["dry_run"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(
        cli, "_resolve_aws_sources", lambda *_args, **_kwargs: (legacy_source, split_source)
    )
    monkeypatch.setattr(cli, "run_sync_aws_billing_summary", fake_summary)
    monkeypatch.setattr(cli, "run_sync_aws_parent_residual_allocations", fake_residual)
    monkeypatch.setattr(cli, "run_sync_aws_kubernetes_workload_allocations", fake_kubernetes_allocations)

    assert cli.main(["sync-aws-billing-summary"]) == 0

    assert captured["source"] == split_source
    assert captured["usage_start_date"] == date(2026, 8, 2)
    assert captured["usage_end_date"] == date(2026, 8, 3)
    assert captured["page_size"] == 123
    assert captured["validate_guardrail"] is False
    assert captured["kubernetes"]["batch_size"] == 123
    assert '"parent_days": 1' in capsys.readouterr().out


def test_cli_runs_sync_aws_unmatched_resources_command(monkeypatch, capsys) -> None:
    disposed = []
    captured = {}

    class Engine:
        def dispose(self):
            disposed.append(True)

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(account_id="946646677266"),
        log_level="INFO",
    )

    def fake_run(engine, **kwargs):
        captured.update(kwargs)
        return SyncGcpUnmatchedResourcesSummary(
            account_id=kwargs["account_id"],
            usage_start_date=kwargs["usage_start_date"],
            usage_end_date=kwargs["usage_end_date"],
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            rows_seen=5,
            rows_written=5,
            dry_run=kwargs["dry_run"],
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_aws_unmatched_resources", fake_run)

    exit_code = cli.main(
        [
            "sync-aws-unmatched-resources",
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
    assert captured["account_id"] == "946646677266"
    assert captured["usage_start_date"] == date(2026, 5, 17)
    assert '"rows_seen": 5' in output


def test_cli_refresh_attribution_from_summary_split_by_day_runs_each_date(
    monkeypatch, capsys
) -> None:
    calls = []

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        gcp_billing=GcpBillingSettings(account_id="pingcap-testing-account"),
        aws_billing=AwsBillingSettings(),
        tcms_allocation=TcmsAllocationSettings(),
        log_level="INFO",
    )

    def fake_refresh(
        _engine,
        *,
        source,
        start_date,
        end_date,
        dry_run,
        tcms_allocation_table=None,
    ):
        calls.append((source.account_id, start_date, end_date, dry_run, tcms_allocation_table))
        return RefreshAttributionSummary(
            vendor=source.vendor,
            account_id=source.account_id,
            start_date=start_date,
            end_date=end_date,
            rows_deleted=0,
            rows_inserted=1,
            dry_run=dry_run,
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_refresh_cost_attribution_from_summary", fake_refresh)

    assert (
        cli.main(
            [
                "refresh-cost-attribution-from-summary",
                "--start-date",
                "2026-05-09",
                "--end-date",
                "2026-05-10",
                "--split-by-day",
            ]
        )
        == 0
    )

    assert calls == [
        (
            "pingcap-testing-account",
            date(2026, 5, 9),
            date(2026, 5, 9),
            False,
            "tcms_cost.resource_allocation",
        ),
        (
            "pingcap-testing-account",
            date(2026, 5, 10),
            date(2026, 5, 10),
            False,
            "tcms_cost.resource_allocation",
        ),
    ]
    assert '"start_date": "2026-05-09"' in capsys.readouterr().out


def test_cli_source_resolution_prefers_active_registry() -> None:
    engine = _sqlite_source_engine()
    try:
        gcp_sources = cli._resolve_gcp_sources(
            engine,
            settings=GcpBillingSettings(account_id="fallback-project"),
        )
        aws_sources = cli._resolve_aws_sources(
            engine,
            settings=AwsBillingSettings(account_id="000000000000"),
        )
        attribution_sources = cli._resolve_attribution_sources(
            engine,
            gcp_settings=GcpBillingSettings(account_id="fallback-project"),
            aws_settings=AwsBillingSettings(account_id="000000000000"),
        )

        assert [settings.account_id for settings in gcp_sources] == [
            "pingcap-testing-account",
            "qa-infra-dev",
        ]
        assert [source.account_id for source in aws_sources] == ["946646677266"]
        assert [(source.vendor, source.account_id) for source in attribution_sources] == [
            ("aws", "946646677266"),
            ("gcp", "pingcap-testing-account"),
            ("gcp", "qa-infra-dev"),
        ]
    finally:
        engine.dispose()


def test_cli_aws_source_resolution_rejects_split_schema_without_source_table() -> None:
    engine = _sqlite_source_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE cost_sources
                    SET source_schema_version = :schema_version
                    WHERE vendor = 'aws' AND account_id = '946646677266'
                    """
                ),
                {"schema_version": AWS_SPLIT_COST_SCHEMA_VERSION},
            )

        with pytest.raises(ValueError, match="aws_split_cost_v1.*no source_table"):
            cli._resolve_aws_sources(engine, settings=AwsBillingSettings())
    finally:
        engine.dispose()


def test_cli_source_resolution_falls_back_when_registry_missing() -> None:
    gcp_sources = cli._resolve_gcp_sources(
        object(),
        settings=GcpBillingSettings(account_id="fallback-project"),
    )
    aws_sources = cli._resolve_aws_sources(
        object(),
        settings=AwsBillingSettings(account_id="946646677266"),
    )
    attribution_sources = cli._resolve_attribution_sources(
        object(),
        gcp_settings=GcpBillingSettings(account_id="fallback-project"),
        aws_settings=AwsBillingSettings(account_id="946646677266"),
    )

    assert [settings.account_id for settings in gcp_sources] == ["fallback-project"]
    assert [source.account_id for source in aws_sources] == ["946646677266"]
    assert [(source.vendor, source.account_id) for source in attribution_sources] == [
        ("gcp", "fallback-project"),
        ("aws", "946646677266"),
    ]


def test_cli_aws_source_resolution_returns_empty_without_registry_or_fallback(caplog) -> None:
    with caplog.at_level("WARNING"):
        assert cli._resolve_aws_sources(object(), settings=AwsBillingSettings()) == ()
    assert "No active AWS cost sources found" in caplog.text
