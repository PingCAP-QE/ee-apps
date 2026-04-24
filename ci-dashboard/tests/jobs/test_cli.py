from __future__ import annotations

from datetime import date, datetime

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.common.models import SyncPodsSummary
from ci_dashboard.jobs.cli import build_parser, main


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
    args = build_parser().parse_args(
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

    assert main() == 0
    assert observed["engine"] == "engine"
    assert observed["start_time_from"] == datetime(2026, 4, 21, 0, 0, 0)
    assert observed["start_time_to"] == datetime(2026, 4, 23, 0, 0, 0)
