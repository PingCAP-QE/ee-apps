from __future__ import annotations

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.common.models import (
    RefreshBuildDerivedSummary,
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
        jobs=JobSettings(batch_size=10),
        log_level="INFO",
    )


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
