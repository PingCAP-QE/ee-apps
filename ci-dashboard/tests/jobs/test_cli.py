from __future__ import annotations

from datetime import date, datetime

import pytest

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.common.models import (
    AnalyzeErrorsSummary,
    ArchiveErrorLogsSummary,
    BackfillFlakyIssuePrLinksSummary,
    ConsumeJenkinsEventsSummary,
    RepairJobNamesSummary,
    RefreshBuildDerivedSummary,
    ReviewErrorSummary,
    SyncBuildsSummary,
    SyncFlakyIssuesSummary,
    SyncPodsSummary,
    SyncPrEventsSummary,
)
from ci_dashboard.jobs import cli


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(batch_size=100),
        log_level="INFO",
    )


def test_build_parser_parses_reconcile_pod_linkage_range() -> None:
    args = cli.build_parser().parse_args(
        [
            "reconcile-pod-linkage-range",
            "--start-date",
            "2026-04-21",
            "--end-date",
            "2026-04-22",
        ]
    )

    assert args.command == "reconcile-pod-linkage-range"
    assert args.start_date == date(2026, 4, 21)
    assert args.end_date == date(2026, 4, 22)


def test_main_runs_reconcile_pod_linkage_range(monkeypatch) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr("ci_dashboard.jobs.cli.get_settings", _settings)
    monkeypatch.setattr("ci_dashboard.jobs.cli.configure_logging", lambda _level: None)
    monkeypatch.setattr("ci_dashboard.jobs.cli.build_engine", lambda _settings: "engine")

    def fake_run_reconcile(engine, *, start_time_from, start_time_to):
        observed["engine"] = engine
        observed["start_time_from"] = start_time_from
        observed["start_time_to"] = start_time_to
        return SyncPodsSummary(reconciled_rows_updated=3)

    monkeypatch.setattr(
        "ci_dashboard.jobs.cli.run_reconcile_pod_linkage_for_time_window",
        fake_run_reconcile,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci-dashboard",
            "reconcile-pod-linkage-range",
            "--start-date",
            "2026-04-21",
            "--end-date",
            "2026-04-22",
        ],
    )

    assert cli.main() == 0
    assert observed["engine"] == "engine"
    assert observed["start_time_from"] == datetime(2026, 4, 21, 0, 0, 0)
    assert observed["start_time_to"] == datetime(2026, 4, 23, 0, 0, 0)


def test_cli_sync_builds_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}

    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: "engine")
    monkeypatch.setattr(
        cli,
        "run_sync_builds",
        lambda engine, settings: called.setdefault("summary", SyncBuildsSummary(rows_written=2)),
    )
    monkeypatch.setattr(cli, "configure_logging", lambda level: called.setdefault("log_level", level))
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "sync-builds"])

    assert cli.main() == 0
    assert called["log_level"] == "INFO"
    assert isinstance(called["summary"], SyncBuildsSummary)


def test_cli_consume_jenkins_events_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}

    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: called.setdefault("log_level", level))
    monkeypatch.setattr(
        cli,
        "run_consume_jenkins_events",
        lambda engine, settings, max_messages, topic, group_id: called.setdefault(
            "summary",
            ConsumeJenkinsEventsSummary(events_processed=2),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci-dashboard",
            "consume-jenkins-events",
            "--max-messages",
            "10",
            "--topic",
            "jenkins-event",
            "--group-id",
            "ci-dashboard-test",
        ],
    )

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert called["log_level"] == "INFO"
    assert isinstance(called["summary"], ConsumeJenkinsEventsSummary)


def test_cli_sync_pr_events_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_sync_pr_events",
        lambda engine, settings: called.setdefault("summary", SyncPrEventsSummary(events_written=3)),
    )
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "sync-pr-events"])

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], SyncPrEventsSummary)


def test_cli_sync_flaky_issues_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_sync_flaky_issues",
        lambda engine, settings: called.setdefault(
            "summary",
            SyncFlakyIssuesSummary(rows_written=2),
        ),
    )
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "sync-flaky-issues"])

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], SyncFlakyIssuesSummary)


def test_cli_backfill_flaky_issue_pr_links_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_backfill_flaky_issue_pr_links",
        lambda engine, settings: called.setdefault(
            "summary",
            BackfillFlakyIssuePrLinksSummary(issue_rows_touched=2, issue_pr_links_written=3),
        ),
    )
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "backfill-flaky-issue-pr-links"])

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], BackfillFlakyIssuePrLinksSummary)


def test_cli_sync_pods_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_sync_pods",
        lambda engine, settings: called.setdefault("summary", SyncPodsSummary(event_rows_written=5)),
    )
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "sync-pods"])

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], SyncPodsSummary)


def test_cli_repair_job_names_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_repair_job_names",
        lambda engine: called.setdefault("summary", RepairJobNamesSummary(build_rows_updated=3, pod_rows_updated=5)),
    )
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "repair-job-names"])

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], RepairJobNamesSummary)


def test_cli_refresh_build_derived_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_refresh_build_derived",
        lambda engine, settings: called.setdefault(
            "summary",
            RefreshBuildDerivedSummary(impacted_builds=4),
        ),
    )
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "refresh-build-derived"])

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], RefreshBuildDerivedSummary)


def test_cli_archive_error_logs_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_archive_error_logs",
        lambda engine, settings, limit, build_id, force: called.setdefault(
            "summary",
            ArchiveErrorLogsSummary(builds_archived=1),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["ci-dashboard", "archive-error-logs", "--limit", "10", "--build-id", "101", "--force"],
    )

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], ArchiveErrorLogsSummary)


def test_cli_analyze_errors_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_analyze_errors",
        lambda engine, settings, limit, build_id, force: called.setdefault(
            "summary",
            AnalyzeErrorsSummary(builds_classified=2),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["ci-dashboard", "analyze-errors", "--limit", "10", "--build-id", "101", "--force"],
    )

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], AnalyzeErrorsSummary)


def test_cli_review_error_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "review_error_classification",
        lambda engine, build_id, l1_category, l2_subcategory: called.setdefault(
            "summary",
            ReviewErrorSummary(rows_updated=1),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["ci-dashboard", "review-error", "--build-id", "101", "--l1", "INFRA", "--l2", "NETWORK"],
    )

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], ReviewErrorSummary)


def test_cli_review_error_reports_validation_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: "engine")
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)

    def _raise_value_error(engine, build_id, l1_category, l2_subcategory):
        del engine, build_id, l1_category, l2_subcategory
        raise ValueError("review category values must be non-empty")

    monkeypatch.setattr(cli, "review_error_classification", _raise_value_error)
    monkeypatch.setattr(
        "sys.argv",
        ["ci-dashboard", "review-error", "--build-id", "101", "--l1", " ", "--l2", "NETWORK"],
    )

    with pytest.raises(SystemExit, match="2"):
        cli.main()

    captured = capsys.readouterr()
    assert "review category values must be non-empty" in captured.err


def test_cli_refresh_build_derived_range_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_refresh_build_derived_for_time_window",
        lambda engine, settings, start_time_from, start_time_to: called.setdefault(
            "summary",
            RefreshBuildDerivedSummary(impacted_builds=4),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci-dashboard",
            "refresh-build-derived-range",
            "--start-date",
            "2026-04-13",
            "--end-date",
            "2026-04-14",
        ],
    )

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], RefreshBuildDerivedSummary)


def test_cli_refresh_flaky_signals_range_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_refresh_flaky_signals_for_time_window",
        lambda engine, settings, start_time_from, start_time_to: called.setdefault(
            "summary",
            RefreshBuildDerivedSummary(impacted_builds=4),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci-dashboard",
            "refresh-flaky-signals-range",
            "--start-date",
            "2026-04-13",
            "--end-date",
            "2026-04-14",
        ],
    )

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["summary"], RefreshBuildDerivedSummary)


def test_cli_backfill_range_dispatch(monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "build_engine", lambda settings: called.setdefault("engine", "engine"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "run_sync_builds_for_time_window",
        lambda engine, settings, start_time_from, start_time_to: called.setdefault(
            "builds_summary",
            SyncBuildsSummary(rows_written=2),
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_sync_pr_events_for_time_window",
        lambda engine, settings, start_time_from, start_time_to: called.setdefault(
            "pr_summary",
            SyncPrEventsSummary(events_written=3),
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_refresh_build_derived_for_time_window",
        lambda engine, settings, start_time_from, start_time_to: called.setdefault(
            "refresh_summary",
            RefreshBuildDerivedSummary(impacted_builds=4),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["ci-dashboard", "backfill-range", "--start-date", "2026-04-13", "--end-date", "2026-04-14"],
    )

    assert cli.main() == 0
    assert called["engine"] == "engine"
    assert isinstance(called["builds_summary"], SyncBuildsSummary)
    assert isinstance(called["pr_summary"], SyncPrEventsSummary)
    assert isinstance(called["refresh_summary"], RefreshBuildDerivedSummary)
