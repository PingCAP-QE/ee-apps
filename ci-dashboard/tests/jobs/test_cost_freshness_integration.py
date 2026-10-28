"""The optional source comparison shares the existing daily report and notifier."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from ci_dashboard.common.config import load_settings
from ci_dashboard.jobs import check_data_freshness as freshness
from ci_dashboard.jobs.cost_source_freshness import CostSourceResult, _classify


@dataclass
class SourceResult:
    vendor: str = "gcp"
    account_id: str = "project-a"
    passed: bool | None = True
    level: str = "MEDIUM"
    detail: str = "ok"

    def describe(self):
        return f"{self.vendor}/{self.account_id}: BQ=2026-10-27 summary=2026-10-24 diff=+3d; {self.detail}"


@pytest.fixture
def setup(monkeypatch):
    engine = Mock()
    monkeypatch.setattr(freshness, "build_engine", Mock(return_value=engine))
    monkeypatch.setattr(freshness, "run_all_checks", Mock(return_value=freshness.Report(
        timestamp=datetime(2026, 10, 28, 1, 10, tzinfo=timezone.utc))))
    checker = Mock(return_value=[SourceResult()])
    sender = Mock(return_value=True)
    monkeypatch.setattr(freshness, "run_cost_source_checks", checker)
    monkeypatch.setattr(freshness, "_send_lark_dm", sender)
    monkeypatch.setenv("FRESHNESS_DRY_RUN", "false")
    return engine, checker, sender


def settings(enabled=True):
    return load_settings({"CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///:memory:",
        "FRESHNESS_COST_BQ_ENABLED": str(enabled).lower(),
        "FRESHNESS_COST_BQ_PROJECT": "warehouse-f80fbbe8e8"})


def test_disabled_preserves_failure_only_legacy_behavior(setup):
    engine, checker, sender = setup
    report = freshness.run_check_data_freshness(settings(False))
    checker.assert_not_called()
    sender.assert_not_called()
    engine.dispose.assert_called_once()
    assert report.cost_sources == [] and report.passed_all


def test_enabled_sends_daily_source_diff_even_when_healthy(setup):
    engine, checker, sender = setup
    report = freshness.run_check_data_freshness(settings())
    checker.assert_called_once_with(engine, settings().cost_freshness)
    sender.assert_called_once()
    message = sender.call_args.args[0]
    assert "BigQuery → imported summary → attribution" in message
    assert "diff=+3d" in message and "0 failed, 1 passed" in message
    assert "Watermarks only" in message
    assert len(report.results) == 1 and report.passed_all
    engine.dispose.assert_called_once()


def test_source_error_is_part_of_existing_failure_report(setup):
    _, checker, sender = setup
    checker.return_value = [SourceResult(passed=False, detail="query_error: 403")]
    report = freshness.run_check_data_freshness(settings())
    assert not report.passed_all and len(report.failed) == 1
    assert report.failed[0].check.db == "cost" and report.failed[0].value["detail"] == "query_error: 403"
    assert "query_error: 403" in sender.call_args.args[0]


@pytest.mark.parametrize("legacy_passed", [True, False])
def test_unexpected_cost_runner_error_preserves_legacy_report(setup, legacy_passed):
    engine, checker, sender = setup
    legacy = freshness.CheckResult(
        check=freshness.CHECKS[0],
        passed=legacy_passed,
        value=None,
        lag_description="legacy CI result",
    )
    freshness.run_all_checks.return_value.results.append(legacy)
    checker.side_effect = RuntimeError("unexpected cost runner failure")

    report = freshness.run_check_data_freshness(settings())

    assert report.results[0] is legacy
    assert len(report.results) == 2
    failure = report.results[1]
    assert failure.check.name == "cost_source_diff:runner"
    assert failure.check.db == "cost" and failure.check.level == "MEDIUM"
    assert not failure.passed and not report.passed_all
    assert failure.error == "unexpected cost runner failure"
    sender.assert_called_once()
    message = sender.call_args.args[0]
    assert "cost_source_diff:runner" in message
    assert "unexpected cost runner failure" in message
    assert f"{1 if legacy_passed else 2} failed" in message
    if not legacy_passed:
        assert legacy.check.name in message and "legacy CI result" in message
    engine.dispose.assert_called_once()


def test_exclusions_are_visible_but_not_counted_as_healthy(setup):
    _, checker, sender = setup
    checker.return_value = [SourceResult(vendor="tencent", passed=None, detail="non_bigquery")]
    report = freshness.run_check_data_freshness(settings())
    assert not report.results and report.cost_sources
    assert "0 failed, 0 passed" in sender.call_args.args[0]
    assert "non_bigquery" in sender.call_args.args[0]


def test_dry_run_prints_source_diff_without_sending(setup, monkeypatch, capsys):
    _, _, sender = setup
    monkeypatch.setenv("FRESHNESS_DRY_RUN", "true")
    report = freshness.run_check_data_freshness(settings())
    sender.assert_not_called()
    assert "diff=+3d" in capsys.readouterr().out
    assert report.passed_all


def test_attribution_ahead_is_not_healthy_even_if_below_bigquery_date():
    result = CostSourceResult("gcp", "project-a", source_latest=date(2026, 10, 27),
        eligible_latest=date(2026, 10, 24), summary_latest=date(2026, 10, 24),
        attribution_latest=date(2026, 10, 25))
    _classify(result, date(2026, 10, 28))
    assert result.status == "attribution_ahead" and result.passed is False


def test_brand_new_unimportable_source_is_pending_not_failed_or_healthy():
    result = CostSourceResult("gcp", "project-a", source_latest=date(2026, 10, 27))
    _classify(result, date(2026, 10, 28))
    assert result.status == "waiting_import_lag" and result.passed is None


@pytest.mark.parametrize("attr,status", [
    (None, "missing_attribution"), (date(2026, 10, 24), "attribution_delay"),
    (date(2026, 10, 26), "attribution_ahead"),
])
def test_unknown_upstream_scope_does_not_hide_a_known_local_lineage_failure(attr, status):
    result = CostSourceResult("aws", "split-account", scope="accountwide_diagnostic_only",
        source_latest=date(2026, 10, 27), summary_latest=date(2026, 10, 25),
        attribution_latest=attr)
    _classify(result, date(2026, 10, 28))
    assert result.status == status and result.level == "MEDIUM" and result.passed is False
