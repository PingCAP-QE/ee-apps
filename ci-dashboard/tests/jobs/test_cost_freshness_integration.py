"""The optional source comparison shares the existing daily report and notifier."""
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from ci_dashboard.common.config import load_settings
from ci_dashboard.jobs import check_data_freshness as freshness
from ci_dashboard.jobs.cost_source_freshness import CostSourceResult, _classify


@pytest.fixture
def setup(monkeypatch):
    engine = Mock()
    monkeypatch.setattr(freshness, "build_engine", Mock(return_value=engine))
    monkeypatch.setattr(freshness, "run_all_checks", Mock(return_value=freshness.Report(
        timestamp=datetime(2026, 10, 28, 1, 10, tzinfo=timezone.utc))))
    checker = Mock(return_value=[CostSourceResult(
        "gcp", "project-a", source_latest=date(2026, 10, 27),
        eligible_latest=date(2026, 10, 24), summary_latest=date(2026, 10, 24),
        attribution_latest=date(2026, 10, 24), status="within_import_lag",
    )])
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


def test_enabled_stays_silent_when_healthy(setup):
    engine, checker, sender = setup
    report = freshness.run_check_data_freshness(settings())
    checker.assert_called_once_with(engine, settings().cost_freshness)
    sender.assert_not_called()
    assert len(report.results) == 1 and report.passed_all
    engine.dispose.assert_called_once()


def test_source_error_is_part_of_existing_failure_report(setup):
    _, checker, sender = setup
    checker.return_value = [CostSourceResult("gcp", "project-a", detail="query_error: 403")]
    report = freshness.run_check_data_freshness(settings())
    assert not report.passed_all and len(report.failed) == 1
    assert report.failed[0].check.db == "cost" and report.failed[0].value["detail"] == "query_error: 403"
    message = sender.call_args.args[0]
    assert "gcp/project-a" in message and "检查失败" in message
    assert "403" not in message  # Full error details stay in logs / structured results.


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
    assert "runner" in message
    assert "检查执行失败" in message
    assert "unexpected cost runner failure" not in message
    assert f"{1 if legacy_passed else 2} 项异常" in message
    if not legacy_passed:
        assert legacy.check.name in message and "legacy CI result" in message
    engine.dispose.assert_called_once()


def test_exclusions_are_visible_but_not_counted_as_healthy(setup):
    _, checker, sender = setup
    checker.return_value = [CostSourceResult("tencent", "account", status="non_bigquery")]
    report = freshness.run_check_data_freshness(settings())
    assert not report.results and report.cost_sources
    sender.assert_not_called()


def test_dry_run_prints_concise_all_clear_without_sending(setup, monkeypatch, capsys):
    _, _, sender = setup
    monkeypatch.setenv("FRESHNESS_DRY_RUN", "true")
    report = freshness.run_check_data_freshness(settings())
    sender.assert_not_called()
    output = capsys.readouterr().out
    assert "数据新鲜度正常" in output
    assert "project-a" not in output
    assert report.passed_all


@pytest.mark.parametrize("attr,suffix", [
    (date(2026, 10, 25), ""),
    (date(2026, 10, 24), "；归因落后 1 天（允许 0 天）"),
])
def test_only_failures_sent_once_with_gap_and_excess(setup, attr, suffix):
    _, checker, sender = setup
    healthy = checker.return_value[0]
    delayed = CostSourceResult("aws", "delayed-account", source_latest=date(2026, 10, 28),
        eligible_latest=date(2026, 10, 28), summary_latest=date(2026, 10, 25),
        attribution_latest=attr)
    _classify(delayed, date(2026, 10, 28))
    assert delayed.status == "ingestion_delay"
    checker.return_value = [healthy, delayed]
    report = freshness.run_check_data_freshness(settings())
    message = sender.call_args.args[0]
    assert len(report.failed) == 1
    assert message.count("aws/delayed-account") == 1
    assert f"导入落后 3 天（允许 1 天，超出 2 天）{suffix}" in message
    assert "project-a" not in message and "MAX watermarks" not in message
    assert "BQ raw=" not in message and "profile=" not in message
    assert len(message.splitlines()) == 2


def test_scope_diagnostics_not_failures_or_healthy(setup):
    _, checker, sender = setup
    checker.return_value = [CostSourceResult("aws", "f04", status="scope_unverified",
                                           scope="accountwide_diagnostic_only")]
    report = freshness.run_check_data_freshness(settings())
    assert report.cost_sources and not report.results
    assert report.cost_sources[0].passed is None
    sender.assert_not_called()


def test_unknown_vendor_alerts_and_cli_exits_nonzero(setup, monkeypatch):
    from ci_dashboard.jobs import cli

    _, checker, sender = setup
    checker.return_value = [CostSourceResult(
        "alicloud", "account", status="scope_unverified", scope="unknown_vendor",
    )]
    report = freshness.run_check_data_freshness(settings())
    assert not report.passed_all and len(report.failed) == 1
    assert report.failed[0].check.level == "LOW"
    sender.assert_called_once()
    message = sender.call_args.args[0]
    assert "⚪ alicloud/account: 成本数据源厂商不受支持，需检查配置" in message
    assert len(message.splitlines()) == 2

    monkeypatch.setattr(cli, "get_settings", settings)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(cli, "run_check_data_freshness", Mock(return_value=report))
    monkeypatch.setattr("sys.argv", ["ci-dashboard", "check-data-freshness"])
    assert cli.main() == 1


def test_empty_active_source_still_warns_without_claiming_import_delay(setup):
    _, checker, sender = setup
    checker.return_value = [CostSourceResult("azure", "empty", status="source_empty")]
    report = freshness.run_check_data_freshness(settings())
    assert len(report.failed) == 1
    message = sender.call_args.args[0]
    assert "无源数据" in message and "差值未知" in message
    assert "导入落后" not in message


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
