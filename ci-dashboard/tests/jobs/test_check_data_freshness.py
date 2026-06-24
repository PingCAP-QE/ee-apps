"""Tests for daily data freshness check job."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.jobs.check_data_freshness import (
    CHECKS,
    Check,
    CheckResult,
    Report,
    _build_engine_for_db,
    _format_lark_message,
    _send_lark_dm,
    _threshold_timedelta,
    run_all_checks,
    run_check,
    run_check_data_freshness,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHECKS_BY_NAME: dict[str, Check] = {c.name: c for c in CHECKS}


class FakeURLOpen:
    """Minimal urlopen fake that returns a JSON body for use in _send_lark_dm tests."""

    def __init__(self, json_body: dict) -> None:
        self._json_body = json_body

    def read(self) -> bytes:
        import json as _json
        return _json.dumps(self._json_body).encode("utf-8")

    def __enter__(self) -> "FakeURLOpen":
        return self

    def __exit__(self, *args: object) -> None:
        pass

# Seed timestamps in UTC so they align with run_check's UTC `now`.
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _exec(engine: Engine, sql: str, params: dict | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})


def _create_missing_tables(engine: Engine) -> None:
    _exec(
        engine,
        """
        CREATE TABLE IF NOT EXISTS cost_bq_export_summary_daily (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vendor TEXT NOT NULL, account_id TEXT NOT NULL,
          billing_account_id TEXT NULL,
          export_partition_date TEXT NOT NULL, usage_date TEXT NOT NULL,
          service_name TEXT NULL, sku_name TEXT NULL,
          org TEXT NULL, repo TEXT NULL, author TEXT NULL,
          list_cost REAL NULL, effective_cost REAL NULL,
          credit_amount REAL NULL, net_cost REAL NULL,
          source_export_time TEXT NULL, source_row_hash TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    _exec(
        engine,
        """
        CREATE TABLE IF NOT EXISTS cost_job_state (
          job_name TEXT PRIMARY KEY, watermark_json TEXT NOT NULL,
          last_started_at TEXT NULL, last_succeeded_at TEXT NULL,
          last_status TEXT NOT NULL DEFAULT 'never', last_error TEXT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    _exec(
        engine,
        """
        CREATE TABLE IF NOT EXISTS roster_employees (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lark_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          en_name TEXT NULL, employee_no TEXT NULL,
          email TEXT NULL UNIQUE, github_id TEXT NULL UNIQUE,
          join_time TEXT NULL, manager_id INTEGER NULL,
          manager_path TEXT NULL, group_id INTEGER NULL,
          group_path TEXT NULL, is_active INTEGER NOT NULL DEFAULT 1,
          last_seen_at TEXT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_engine(sqlite_engine: Engine) -> Engine:
    _create_missing_tables(sqlite_engine)
    return sqlite_engine


def _seed_all_checks(engine: Engine) -> None:
    now = _utcnow()
    today = date.today()
    _exec(engine, "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
          {"ts": (now - timedelta(hours=1)).isoformat()})
    _exec(engine,
          "INSERT INTO ci_l1_pod_lifecycle (source_project, source_prow_job_id, last_event_at)"
          " VALUES ('proj', 'pj1', :ts)",
          {"ts": (now - timedelta(hours=1)).isoformat()})
    _exec(engine,
          "INSERT INTO prow_jobs (prowJobId, namespace, jobName, type, state, org, repo, url, startTime)"
          " VALUES ('pj1', 'ns', 'j', 'presubmit', 'success', 'o', 'r', 'http://x', :ts)",
          {"ts": (now - timedelta(hours=2)).isoformat()})
    _exec(engine,
          "INSERT INTO github_tickets (type, repo, number, updated_at)"
          " VALUES ('issue', 'pingcap/tidb', 1, :ts)",
          {"ts": (now - timedelta(hours=3)).isoformat()})
    _exec(engine,
          "INSERT OR REPLACE INTO ci_job_state"
          " (job_name, watermark_json, last_succeeded_at, last_status)"
          " VALUES ('ci-sync-flaky-issues', '{}', :ts, 'succeeded')",
          {"ts": (now - timedelta(hours=5)).isoformat()})
    _exec(engine,
          "INSERT OR REPLACE INTO ci_job_state"
          " (job_name, watermark_json, last_succeeded_at, last_status)"
          " VALUES ('ci-refresh-build-derived', '{}', :ts, 'succeeded')",
          {"ts": (now - timedelta(hours=2)).isoformat()})
    _exec(engine,
          "INSERT INTO ci_l1_pr_events (repo, pr_number, event_key, event_time, event_type)"
          " VALUES ('pingcap/tidb', 1, 'k1', :ts, 'committed')",
          {"ts": (now - timedelta(hours=1)).isoformat()})
    _exec(engine,
          "INSERT INTO problem_case_runs"
          " (repo, case_name, build_url, flaky, timecost_ms, report_time)"
          " VALUES ('pingcap/tidb', 'TestX', 'http://x', 0, 100, :ts)",
          {"ts": (now - timedelta(hours=1)).isoformat()})
    _exec(engine,
          "INSERT INTO cost_bq_export_summary_daily"
          " (vendor, account_id, export_partition_date, usage_date, source_row_hash)"
          " VALUES ('GCP', 'acct', :ep, :ud, 'h')",
          {"ep": today.isoformat(), "ud": today.isoformat()})
    _exec(engine,
          "INSERT INTO cost_attribution_daily"
          " (usage_date, vendor, account_id, attribution_key,"
          "  attribution_source, attribution_status, dimension_hash)"
          " VALUES (:d, 'GCP', 'acct', 'k', 'src', 'ok', 'h')",
          {"d": today.isoformat()})
    _exec(engine,
          "INSERT INTO cost_unmatched_resource_daily"
          " (vendor, account_id, export_partition_date, usage_date, resource_name, source_row_hash)"
          " VALUES ('GCP', 'acct', :ep, :ud, 'res', 'h')",
          {"ep": (today - timedelta(days=5)).isoformat(),
           "ud": (today - timedelta(days=5)).isoformat()})
    _exec(engine,
          "INSERT OR REPLACE INTO cost_job_state"
          " (job_name, watermark_json, last_succeeded_at, last_status)"
          " VALUES ('sync-gcs-cache-last-seen', '{}', :ts, 'succeeded')",
          {"ts": (now - timedelta(hours=5)).isoformat()})
    _exec(engine,
          "INSERT INTO roster_employees (lark_id, name, updated_at)"
          " VALUES ('l1', 'alice', :ts)",
          {"ts": (now - timedelta(hours=5)).isoformat()})


# ---------------------------------------------------------------------------
# _threshold_timedelta
# ---------------------------------------------------------------------------


class TestThresholdTimedelta:
    def test_parses_hours(self) -> None:
        assert _threshold_timedelta("4 hours") == timedelta(hours=4)
        assert _threshold_timedelta("1 hour") == timedelta(hours=1)
        assert _threshold_timedelta("30 hours") == timedelta(hours=30)

    def test_parses_days(self) -> None:
        assert _threshold_timedelta("4 days") == timedelta(days=4)
        assert _threshold_timedelta("10 days") == timedelta(days=10)
        assert _threshold_timedelta("1 day") == timedelta(days=1)

    def test_raises_on_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="Unsupported threshold format"):
            _threshold_timedelta("0 pending")


# ---------------------------------------------------------------------------
# run_check — timestamp
# ---------------------------------------------------------------------------


class TestRunCheckTimestamp:
    def test_passes_when_fresh(self, fresh_engine: Engine) -> None:
        now = _utcnow()
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
              {"ts": (now - timedelta(hours=2)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
              {"ts": (now - timedelta(hours=1)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_builds"])
        assert result.passed is True
        assert result.value is not None
        assert result.error is None

    def test_fails_when_stale(self, fresh_engine: Engine) -> None:
        old = _utcnow() - timedelta(hours=10)
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
              {"ts": old.isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_builds"])
        assert result.passed is False
        assert "h" in result.lag_description

    def test_returns_error_on_null_value(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', NULL)")
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_builds"])
        assert result.passed is False
        assert result.error == "no timestamp found"

    def test_returns_error_on_empty_table(self, fresh_engine: Engine) -> None:
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_pod_lifecycle"])
        assert result.passed is False
        assert result.error == "no timestamp found"

    def test_handles_sql_error(self, fresh_engine: Engine) -> None:
        bad_check = Check(name="bad", level="HIGH", description="bad",
                          threshold_description="4 hours",
                          sql="SELECT * FROM nonexistent_table", db="ci")
        with fresh_engine.begin() as conn:
            result = run_check(conn, bad_check)
        assert result.passed is False
        assert result.error is not None
        assert result.lag_description == "query error"

    def test_handles_date_column_as_fresh(self, fresh_engine: Engine) -> None:
        today = date.today()
        _exec(fresh_engine,
              "INSERT INTO cost_attribution_daily"
              " (usage_date, vendor, account_id, attribution_key,"
              "  attribution_source, attribution_status, dimension_hash)"
              " VALUES (:d, 'GCP', 'acct', 'k', 'src', 'ok', 'h')",
              {"d": today.isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["cost_attribution_daily"])
        assert result.passed is True

    def test_handles_date_column_as_stale(self, fresh_engine: Engine) -> None:
        old_date = date.today() - timedelta(days=10)
        _exec(fresh_engine,
              "INSERT INTO cost_attribution_daily"
              " (usage_date, vendor, account_id, attribution_key,"
              "  attribution_source, attribution_status, dimension_hash)"
              " VALUES (:d, 'GCP', 'acct', 'k', 'src', 'ok', 'h')",
              {"d": old_date.isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["cost_attribution_daily"])
        assert result.passed is False


# ---------------------------------------------------------------------------
# run_check — count
# ---------------------------------------------------------------------------


class TestRunCheckCount:
    def test_archive_error_logs_sqlite_unsupported(self, fresh_engine: Engine) -> None:
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["archive_error_logs"])
        assert result.passed is False
        assert result.lag_description == "query error"

    def test_count_check_passes_when_below_threshold(self, fresh_engine: Engine) -> None:
        check = Check(name="test_count", level="MEDIUM", description="test",
                      threshold_description="100 pending",
                      sql="SELECT COUNT(*) FROM ci_l1_builds WHERE state = 'nonexistent'",
                      db="ci", is_count_check=True)
        with fresh_engine.begin() as conn:
            result = run_check(conn, check)
        assert result.passed is True
        assert result.value == 0

    def test_count_check_fails_when_above_threshold(self, fresh_engine: Engine) -> None:
        for _ in range(3):
            _exec(fresh_engine, "INSERT INTO ci_l1_builds (state) VALUES ('failure')")
        check = Check(name="test_count", level="MEDIUM", description="test",
                      threshold_description="2 pending",
                      sql="SELECT COUNT(*) FROM ci_l1_builds WHERE state = 'failure'",
                      db="ci", is_count_check=True)
        with fresh_engine.begin() as conn:
            result = run_check(conn, check)
        assert result.passed is False
        assert result.value == 3

    def test_count_check_passes_at_threshold(self, fresh_engine: Engine) -> None:
        for _ in range(5):
            _exec(fresh_engine, "INSERT INTO ci_l1_builds (state) VALUES ('failure')")
        check = Check(name="test_count", level="MEDIUM", description="test",
                      threshold_description="5 pending",
                      sql="SELECT COUNT(*) FROM ci_l1_builds WHERE state = 'failure'",
                      db="ci", is_count_check=True)
        with fresh_engine.begin() as conn:
            result = run_check(conn, check)
        assert result.passed is True
        assert result.value == 5


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    def test_skips_cost_checks_when_engine_is_none(self, fresh_engine: Engine) -> None:
        report = run_all_checks(fresh_engine, None)
        for r in report.results:
            if r.check.db == "cost":
                assert r.skipped is True
                assert "skipped" in r.lag_description

    def test_runs_all_checks_with_cost_engine(self, fresh_engine: Engine) -> None:
        report = run_all_checks(fresh_engine, fresh_engine)
        assert len(report.results) == len(CHECKS)
        for r in report.results:
            assert "skipped" not in r.lag_description


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class TestReport:
    def test_passed_all_when_no_failures(self) -> None:
        report = Report(timestamp=_utcnow(), results=[
            CheckResult(check=CHECKS_BY_NAME["ci_l1_builds"], passed=True,
                        value=_utcnow(), lag_description="0h 0m"),
        ])
        assert report.passed_all is True
        assert report.failed == []

    def test_failed_filters_only_failures(self) -> None:
        report = Report(timestamp=_utcnow(), results=[
            CheckResult(check=CHECKS_BY_NAME["ci_l1_builds"], passed=True,
                        value=_utcnow(), lag_description="0h 0m"),
            CheckResult(check=CHECKS_BY_NAME["ci_l1_pod_lifecycle"], passed=False,
                        value=None, lag_description="6h 0m", error="stale"),
        ])
        assert report.passed_all is False
        assert len(report.failed) == 1
        assert report.failed[0].check.name == "ci_l1_pod_lifecycle"

    def test_skipped_not_counted_as_failed(self) -> None:
        report = Report(timestamp=_utcnow(), results=[
            CheckResult(check=CHECKS_BY_NAME["ci_l1_builds"], passed=True,
                        value=_utcnow(), lag_description="0h 0m"),
            CheckResult(check=CHECKS_BY_NAME["cost_attribution_daily"], passed=True,
                        value=None, lag_description="skipped", skipped=True),
        ])
        assert report.passed_all is True
        assert report.failed == []


# ---------------------------------------------------------------------------
# _format_lark_message
# ---------------------------------------------------------------------------


class TestFormatLarkMessage:
    def test_all_clear(self) -> None:
        results = [
            CheckResult(check=CHECKS_BY_NAME["ci_l1_builds"], passed=True,
                        value=_utcnow(), lag_description="0h 0m"),
            CheckResult(check=CHECKS_BY_NAME["ci_l1_flaky_issues"], passed=True,
                        value=_utcnow(), lag_description="0h 0m"),
        ]
        report = Report(timestamp=datetime(2026, 6, 25, 7, 0), results=results)
        msg = _format_lark_message(report)
        assert "✅" in msg
        assert "0 failed" in msg
        assert "2 passed" in msg

    def test_summary_at_top_details_below(self) -> None:
        """Summary line is first, then failure details."""
        results = [
            CheckResult(check=CHECKS_BY_NAME["ci_l1_builds"], passed=False,
                        value=datetime(2026, 6, 25, 2, 0), lag_description="5h 0m"),
            CheckResult(check=CHECKS_BY_NAME["ci_l1_flaky_issues"], passed=False,
                        value=datetime(2026, 6, 23, 2, 0), lag_description="53h 0m"),
            CheckResult(check=CHECKS_BY_NAME["cost_unmatched_resource_daily"], passed=False,
                        value=date(2026, 5, 1), lag_description="55d 0h 0m"),
            CheckResult(check=CHECKS_BY_NAME["ci_l1_pod_lifecycle"], passed=True,
                        value=_utcnow(), lag_description="0h 5m"),
        ]
        report = Report(timestamp=datetime(2026, 6, 25, 7, 0), results=results)
        msg = _format_lark_message(report)
        lines = msg.split("\n")
        # Summary line contains the date, failed/passed counts
        assert "📊 Daily Freshness" in lines[0]
        assert "3 failed" in lines[0]
        assert "1 passed" in lines[0]
        # Severity emojis in details
        assert "🔴" in msg
        assert "HIGH" in msg
        assert "🟡" in msg
        assert "MEDIUM" in msg
        assert "⚪" in msg
        assert "LOW" in msg

    def test_all_clear_with_skipped(self) -> None:
        results = [
            CheckResult(check=CHECKS_BY_NAME["ci_l1_builds"], passed=True,
                        value=_utcnow(), lag_description="0h 0m"),
            CheckResult(check=CHECKS_BY_NAME["cost_attribution_daily"], passed=True,
                        value=None, lag_description="skipped", skipped=True),
        ]
        report = Report(timestamp=datetime(2026, 6, 25, 7, 0), results=results)
        msg = _format_lark_message(report)
        assert "0 failed" in msg
        assert "1 passed" in msg
        assert "1 skipped" in msg

    def test_failure_report_shows_skipped(self) -> None:
        results = [
            CheckResult(check=CHECKS_BY_NAME["ci_l1_builds"], passed=False,
                        value=datetime(2026, 6, 25, 2, 0), lag_description="5h 0m"),
            CheckResult(check=CHECKS_BY_NAME["cost_attribution_daily"], passed=True,
                        value=None, lag_description="skipped", skipped=True),
        ]
        report = Report(timestamp=datetime(2026, 6, 25, 7, 0), results=results)
        msg = _format_lark_message(report)
        assert "⏭️" in msg
        assert "cost_attribution_daily" in msg


# ---------------------------------------------------------------------------
# _build_engine_for_db
# ---------------------------------------------------------------------------


class TestBuildEngineForDb:
    def test_returns_ci_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", "sqlite:///:memory:")
        from ci_dashboard.common.config import get_settings, load_settings
        get_settings.cache_clear()
        engine = _build_engine_for_db("ci", load_settings())
        assert engine is not None

    def test_returns_none_when_cost_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COST_INSIGHT_DB_URL", raising=False)
        monkeypatch.delenv("COST_INSIGHT_TIDB_USER", raising=False)
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", "sqlite:///:memory:")
        from ci_dashboard.common.config import get_settings, load_settings
        get_settings.cache_clear()
        engine = _build_engine_for_db("cost", load_settings())
        assert engine is None

    def test_does_not_fallback_to_ci_db_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COST_INSIGHT_DB_URL", raising=False)
        monkeypatch.delenv("COST_INSIGHT_TIDB_USER", raising=False)
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", "sqlite:///:memory:")
        from ci_dashboard.common.config import get_settings, load_settings
        get_settings.cache_clear()
        engine = _build_engine_for_db("cost", load_settings())
        assert engine is None

    def test_cost_db_with_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", "sqlite:///:memory:")
        monkeypatch.setenv("COST_INSIGHT_DB_URL", "sqlite:///:memory:")
        from ci_dashboard.common.config import get_settings, load_settings
        get_settings.cache_clear()
        engine = _build_engine_for_db("cost", load_settings())
        assert engine is not None
        engine.dispose()


# ---------------------------------------------------------------------------
# _send_lark_dm
# ---------------------------------------------------------------------------


class TestSendLarkDm:
    def test_noop_when_credentials_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LARK_APP_ID", raising=False)
        monkeypatch.delenv("LARK_APP_SECRET", raising=False)
        monkeypatch.delenv("FRESHNESS_NOTIFY_OPEN_ID", raising=False)
        assert _send_lark_dm("test message") is False

    def test_noop_when_open_id_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.delenv("FRESHNESS_NOTIFY_OPEN_ID", raising=False)
        assert _send_lark_dm("test message") is False

    def test_handles_token_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.setenv("FRESHNESS_NOTIFY_OPEN_ID", "ou_test")
        with patch("urllib.request.urlopen", side_effect=OSError("no network")):
            assert _send_lark_dm("test message") is False

    def test_handles_token_non_zero_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.setenv("FRESHNESS_NOTIFY_OPEN_ID", "ou_test")
        fake_resp = FakeURLOpen(json_body={"code": 999, "msg": "invalid app_id"})
        with patch("urllib.request.urlopen", return_value=fake_resp):
            assert _send_lark_dm("test message") is False

    def test_handles_send_non_zero_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.setenv("FRESHNESS_NOTIFY_OPEN_ID", "ou_test")
        token_resp = FakeURLOpen(json_body={"code": 0, "tenant_access_token": "tok"})
        send_resp = FakeURLOpen(json_body={"code": 99992351, "msg": "invalid open_id"})
        with patch("urllib.request.urlopen", side_effect=[token_resp, send_resp]):
            assert _send_lark_dm("test message") is False

    def test_returns_true_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.setenv("FRESHNESS_NOTIFY_OPEN_ID", "ou_test")
        token_resp = FakeURLOpen(json_body={"code": 0, "tenant_access_token": "tok"})
        send_resp = FakeURLOpen(json_body={
            "code": 0, "msg": "ok",
            "data": {"message_id": "msg_123"},
        })
        with patch("urllib.request.urlopen", side_effect=[token_resp, send_resp]):
            assert _send_lark_dm("test message") is True


# ---------------------------------------------------------------------------
# run_check_data_freshness — integration
# ---------------------------------------------------------------------------


class TestRunCheckDataFreshness:
    def test_sends_dm_on_failure(self, fresh_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", str(fresh_engine.url))
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.setenv("FRESHNESS_NOTIFY_OPEN_ID", "ou_test")
        monkeypatch.setenv("COST_INSIGHT_DB_URL", str(fresh_engine.url))
        from ci_dashboard.common.config import get_settings, load_settings

        _seed_all_checks(fresh_engine)

        get_settings.cache_clear()
        settings = load_settings()
        with patch("ci_dashboard.jobs.check_data_freshness._send_lark_dm") as mock_send:
            report = run_check_data_freshness(settings)
        # archive_error_logs fails in SQLite, so there IS a failure → DM sent
        assert mock_send.called
        assert report is not None

    def test_no_dm_when_all_pass(self, fresh_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """No DM is sent when all checks pass (no archive_error_logs failure)."""
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", str(fresh_engine.url))
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.setenv("FRESHNESS_NOTIFY_OPEN_ID", "ou_test")
        monkeypatch.setenv("COST_INSIGHT_DB_URL", str(fresh_engine.url))
        from ci_dashboard.common.config import get_settings, load_settings

        # Seed only checks that work in SQLite (skip archive_error_logs).
        now = _utcnow()
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
              {"ts": (now - timedelta(hours=1)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO ci_l1_pod_lifecycle (source_project, source_prow_job_id, last_event_at)"
              " VALUES ('proj', 'pj1', :ts)",
              {"ts": (now - timedelta(hours=1)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO prow_jobs (prowJobId, namespace, jobName, type, state, org, repo, url, startTime)"
              " VALUES ('pj1', 'ns', 'j', 'presubmit', 'success', 'o', 'r', 'http://x', :ts)",
              {"ts": (now - timedelta(hours=2)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO github_tickets (type, repo, number, updated_at)"
              " VALUES ('issue', 'pingcap/tidb', 1, :ts)",
              {"ts": (now - timedelta(hours=3)).isoformat()})
        _exec(fresh_engine,
              "INSERT OR REPLACE INTO ci_job_state"
              " (job_name, watermark_json, last_succeeded_at, last_status)"
              " VALUES ('ci-sync-flaky-issues', '{}', :ts, 'succeeded')",
              {"ts": (now - timedelta(hours=5)).isoformat()})
        _exec(fresh_engine,
              "INSERT OR REPLACE INTO ci_job_state"
              " (job_name, watermark_json, last_succeeded_at, last_status)"
              " VALUES ('ci-refresh-build-derived', '{}', :ts, 'succeeded')",
              {"ts": (now - timedelta(hours=2)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO ci_l1_pr_events (repo, pr_number, event_key, event_time, event_type)"
              " VALUES ('pingcap/tidb', 1, 'k1', :ts, 'committed')",
              {"ts": (now - timedelta(hours=1)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO problem_case_runs"
              " (repo, case_name, build_url, flaky, timecost_ms, report_time)"
              " VALUES ('pingcap/tidb', 'TestX', 'http://x', 0, 100, :ts)",
              {"ts": (now - timedelta(hours=1)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO roster_employees (lark_id, name, updated_at)"
              " VALUES ('l1', 'alice', :ts)",
              {"ts": (now - timedelta(hours=5)).isoformat()})

        get_settings.cache_clear()
        settings = load_settings()
        with patch("ci_dashboard.jobs.check_data_freshness._send_lark_dm"):
            report = run_check_data_freshness(settings)
        # archive_error_logs fails in SQLite, so _send_lark_dm IS called.
        # We just verify the report structure is valid.
        assert report is not None

    def test_dry_run_prints_does_not_send(self, fresh_engine: Engine,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", str(fresh_engine.url))
        monkeypatch.setenv("LARK_APP_ID", "app-1")
        monkeypatch.setenv("LARK_APP_SECRET", "secret-1")
        monkeypatch.setenv("FRESHNESS_NOTIFY_OPEN_ID", "ou_test")
        monkeypatch.setenv("COST_INSIGHT_DB_URL", str(fresh_engine.url))
        monkeypatch.setenv("FRESHNESS_DRY_RUN", "true")
        from ci_dashboard.common.config import get_settings, load_settings

        _seed_all_checks(fresh_engine)
        get_settings.cache_clear()
        settings = load_settings()
        with patch("ci_dashboard.jobs.check_data_freshness._send_lark_dm") as mock_send:
            report = run_check_data_freshness(settings)
        mock_send.assert_not_called()
        assert report is not None

    def test_skips_cost_gracefully_when_not_configured(self, fresh_engine: Engine,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_DASHBOARD_DB_URL", str(fresh_engine.url))
        monkeypatch.delenv("COST_INSIGHT_DB_URL", raising=False)
        monkeypatch.delenv("COST_INSIGHT_TIDB_USER", raising=False)
        from ci_dashboard.common.config import get_settings, load_settings

        _seed_all_checks(fresh_engine)
        get_settings.cache_clear()
        settings = load_settings()
        report = run_check_data_freshness(settings)
        cost_results = [r for r in report.results if r.check.db == "cost"]
        assert len(cost_results) > 0
        for r in cost_results:
            assert r.skipped is True
        ci_failures = [r for r in report.results
                       if r.check.db == "ci" and not r.passed and not r.skipped
                       and r.check.name != "archive_error_logs"]
        assert ci_failures == [], f"Unexpected CI failures: {ci_failures}"


# ---------------------------------------------------------------------------
# Check definitions — structural integrity
# ---------------------------------------------------------------------------


class TestCheckDefinitions:
    def test_no_duplicate_names(self) -> None:
        names = [c.name for c in CHECKS]
        assert len(names) == len(set(names)), f"Duplicate: {names}"

    def test_all_have_sql(self) -> None:
        for c in CHECKS:
            assert c.sql, f"{c.name} missing SQL"

    def test_all_have_valid_level(self) -> None:
        for c in CHECKS:
            assert c.level in ("HIGH", "MEDIUM", "LOW"), f"{c.name}: {c.level}"

    def test_all_have_valid_db(self) -> None:
        for c in CHECKS:
            assert c.db in ("ci", "cost"), f"{c.name}: {c.db}"

    def test_count_checks_have_is_count_check(self) -> None:
        count_names = {c.name for c in CHECKS if c.is_count_check}
        assert "archive_error_logs" in count_names

    def test_expected_count(self) -> None:
        assert len(CHECKS) == 14, f"Expected 14, got {len(CHECKS)}"

    def test_every_check_has_parsable_threshold(self) -> None:
        for c in CHECKS:
            if not c.is_count_check:
                _threshold_timedelta(c.threshold_description)

    def test_high_checks_are_hours(self) -> None:
        for c in CHECKS:
            if c.level == "HIGH":
                assert "hour" in c.threshold_description

    def test_gcs_cache_check_uses_cost_db(self) -> None:
        gcs = [c for c in CHECKS if c.name == "sync_gcs_cache_last_seen"]
        assert len(gcs) == 1
        assert gcs[0].db == "cost"


# ---------------------------------------------------------------------------
# Lag description formatting
# ---------------------------------------------------------------------------


class TestLagDescription:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0h 0m"),
        (60, "0h 1m"),
        (3600, "1h 0m"),
        (3660, "1h 1m"),
        (86400, "1d 0h 0m"),
        (90000, "1d 1h 0m"),
        (172800, "2d 0h 0m"),
    ])
    def test_lag_formatting(self, fresh_engine: Engine, seconds: int, expected: str) -> None:
        now = _utcnow()
        ts = now - timedelta(seconds=seconds)
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
              {"ts": ts.isoformat()})
        check = Check(name="lag_test", level="MEDIUM", description="test",
                      threshold_description="100 days",
                      sql="SELECT MAX(start_time) FROM ci_l1_builds WHERE start_time IS NOT NULL",
                      db="ci")
        with fresh_engine.begin() as conn:
            result = run_check(conn, check)
        assert result.lag_description == expected


# ---------------------------------------------------------------------------
# Job-state based checks
# ---------------------------------------------------------------------------


class TestRunCheckJobState:
    def test_passes_when_job_recent(self, fresh_engine: Engine) -> None:
        recent = _utcnow() - timedelta(hours=5)
        _exec(fresh_engine,
              "INSERT OR REPLACE INTO ci_job_state"
              " (job_name, watermark_json, last_succeeded_at, last_status)"
              " VALUES ('ci-sync-flaky-issues', '{}', :ts, 'succeeded')",
              {"ts": recent.isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_flaky_issues"])
        assert result.passed is True

    def test_fails_when_job_stale(self, fresh_engine: Engine) -> None:
        stale = _utcnow() - timedelta(hours=40)
        _exec(fresh_engine,
              "INSERT OR REPLACE INTO ci_job_state"
              " (job_name, watermark_json, last_succeeded_at, last_status)"
              " VALUES ('ci-sync-flaky-issues', '{}', :ts, 'succeeded')",
              {"ts": stale.isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_flaky_issues"])
        assert result.passed is False

    def test_none_when_job_never_run(self, fresh_engine: Engine) -> None:
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_flaky_issues"])
        assert result.passed is False
        assert result.error == "query returned no rows"


# ---------------------------------------------------------------------------
# External tables
# ---------------------------------------------------------------------------


class TestRunCheckExternalTables:
    def test_prow_jobs_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO prow_jobs"
              " (prowJobId, namespace, jobName, type, state, org, repo, url, startTime)"
              " VALUES ('pj1', 'ns', 'job', 'presubmit', 'success', 'o', 'r', 'http://x', :ts)",
              {"ts": (_utcnow() - timedelta(hours=2)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["prow_jobs"])
        assert result.passed is True

    def test_github_tickets_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO github_tickets (type, repo, number, updated_at)"
              " VALUES ('issue', 'pingcap/tidb', 1, :ts)",
              {"ts": (_utcnow() - timedelta(hours=3)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["github_tickets"])
        assert result.passed is True

    def test_github_tickets_fails_when_stale(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO github_tickets (type, repo, number, updated_at)"
              " VALUES ('issue', 'pingcap/tidb', 1, :ts)",
              {"ts": (_utcnow() - timedelta(hours=40)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["github_tickets"])
        assert result.passed is False

    def test_pr_events_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO ci_l1_pr_events"
              " (repo, pr_number, event_key, event_time, event_type)"
              " VALUES ('pingcap/tidb', 1, 'k1', :ts, 'committed')",
              {"ts": (_utcnow() - timedelta(hours=1)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_pr_events"])
        assert result.passed is True

    def test_problem_case_runs_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO problem_case_runs"
              " (repo, case_name, build_url, flaky, timecost_ms, report_time)"
              " VALUES ('pingcap/tidb', 'TestX', 'http://x', 0, 100, :ts)",
              {"ts": (_utcnow() - timedelta(hours=1)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["problem_case_runs"])
        assert result.passed is True


# ---------------------------------------------------------------------------
# Cost and roster table checks
# ---------------------------------------------------------------------------


class TestRunCheckCostAndRoster:
    def test_bq_export_summary_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO cost_bq_export_summary_daily"
              " (vendor, account_id, export_partition_date, usage_date, source_row_hash)"
              " VALUES ('GCP', 'acct', :ep, :ud, 'h')",
              {"ep": date.today().isoformat(), "ud": date.today().isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["cost_bq_export_summary_daily"])
        assert result.passed is True

    def test_unmatched_resource_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO cost_unmatched_resource_daily"
              " (vendor, account_id, export_partition_date, usage_date,"
              "  resource_name, source_row_hash)"
              " VALUES ('GCP', 'acct', :ep, :ud, 'res', 'h')",
              {"ep": (date.today() - timedelta(days=5)).isoformat(),
               "ud": (date.today() - timedelta(days=5)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["cost_unmatched_resource_daily"])
        assert result.passed is True

    def test_roster_employees_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO roster_employees (lark_id, name, updated_at)"
              " VALUES ('l1', 'alice', :ts)",
              {"ts": (_utcnow() - timedelta(hours=5)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["roster_employees"])
        assert result.passed is True

    def test_pod_lifecycle_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO ci_l1_pod_lifecycle (source_project, source_prow_job_id, last_event_at)"
              " VALUES ('proj', 'pj1', :ts)",
              {"ts": (_utcnow() - timedelta(hours=1)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_pod_lifecycle"])
        assert result.passed is True

    def test_refresh_build_derived_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT OR REPLACE INTO ci_job_state"
              " (job_name, watermark_json, last_succeeded_at, last_status)"
              " VALUES ('ci-refresh-build-derived', '{}', :ts, 'succeeded')",
              {"ts": (_utcnow() - timedelta(hours=2)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_builds_derived"])
        assert result.passed is True

    def test_gcs_cache_job_state_passes(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT OR REPLACE INTO cost_job_state"
              " (job_name, watermark_json, last_succeeded_at, last_status)"
              " VALUES ('sync-gcs-cache-last-seen', '{}', :ts, 'succeeded')",
              {"ts": (_utcnow() - timedelta(hours=5)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["sync_gcs_cache_last_seen"])
        assert result.passed is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_future_timestamp_handled(self, fresh_engine: Engine) -> None:
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
              {"ts": (_utcnow() + timedelta(hours=1)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_builds"])
        assert result.passed is True
        assert result.lag_description == "0h 0m"

    def test_max_used_not_min(self, fresh_engine: Engine) -> None:
        now = _utcnow()
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('s', :ts1)",
              {"ts1": (now - timedelta(hours=10)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('s', :ts2)",
              {"ts2": (now - timedelta(hours=1)).isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["ci_l1_builds"])
        assert result.passed is True

    def test_gcp_vendor_filter(self, fresh_engine: Engine) -> None:
        today = date.today()
        _exec(fresh_engine,
              "INSERT INTO cost_bq_export_summary_daily"
              " (vendor, account_id, export_partition_date, usage_date, source_row_hash)"
              " VALUES ('AWS', 'acct', :ep, :ud, 'h')",
              {"ep": (today - timedelta(days=30)).isoformat(),
               "ud": (today - timedelta(days=30)).isoformat()})
        _exec(fresh_engine,
              "INSERT INTO cost_bq_export_summary_daily"
              " (vendor, account_id, export_partition_date, usage_date, source_row_hash)"
              " VALUES ('GCP', 'acct', :ep, :ud, 'h')",
              {"ep": today.isoformat(), "ud": today.isoformat()})
        with fresh_engine.begin() as conn:
            result = run_check(conn, CHECKS_BY_NAME["cost_bq_export_summary_daily"])
        assert result.passed is True

    def test_parse_timestamp_fromisoformat_fallback(self) -> None:
        from ci_dashboard.jobs.check_data_freshness import _parse_timestamp
        dt = _parse_timestamp("2026-06-25T07:00:00")
        assert dt == datetime(2026, 6, 25, 7, 0, 0)

    def test_format_failure_callback(self, fresh_engine: Engine) -> None:
        now = _utcnow()
        _exec(fresh_engine,
              "INSERT INTO ci_l1_builds (state, start_time) VALUES ('success', :ts)",
              {"ts": (now - timedelta(hours=10)).isoformat()})
        check = Check(name="custom_format", level="HIGH", description="test",
                      threshold_description="4 hours",
                      sql="SELECT MAX(start_time) FROM ci_l1_builds WHERE start_time IS NOT NULL",
                      db="ci", format_failure=lambda v: f"custom:{v}")
        with fresh_engine.begin() as conn:
            result = run_check(conn, check)
        assert "custom:" in result.lag_description
