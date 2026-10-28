"""Hermetic account-level checks: SQLite plus injected BigQuery, never live ADC."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, event, text

from ci_dashboard.common.config import CostFreshnessSettings
from ci_dashboard.jobs import cost_source_freshness as checks

TODAY = date(2026, 10, 28)
SETTINGS = CostFreshnessSettings(
    enabled=True, project="warehouse-f80fbbe8e8", maximum_bytes_billed=1073741824
)


@pytest.fixture
def engine():
    db = create_engine("sqlite://")
    with db.begin() as conn:
        conn.execute(
            text("""CREATE TABLE cost_sources (
            vendor TEXT, account_id TEXT, display_name TEXT, is_active INTEGER,
            source_schema_version TEXT, source_available_from DATE)""")
        )
        for table in ("cost_bq_export_summary_daily", "cost_attribution_daily"):
            conn.execute(
                text(f"CREATE TABLE {table} (vendor TEXT, account_id TEXT, usage_date DATE)")
            )
    yield db
    db.dispose()


def seed(
    engine,
    vendor="gcp",
    account="a",
    summary="2026-10-24",
    attr="2026-10-24",
    *,
    active=True,
    profile=None,
    local_vendor=None,
):
    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO cost_sources VALUES
            (:vendor, :account, 'Name is not a scope', :active, :profile, '2026-01-01')"""),
            {"vendor": vendor, "account": account, "active": int(active), "profile": profile},
        )
        for table, value in (
            ("cost_bq_export_summary_daily", summary),
            ("cost_attribution_daily", attr),
        ):
            if value:
                conn.execute(
                    text(f"INSERT INTO {table} VALUES (:vendor, :account, :usage)"),
                    {"vendor": local_vendor or vendor, "account": account, "usage": value},
                )


def watermark(account="a", raw="2026-10-27", eligible="2026-10-24"):
    return {
        "account_id": account,
        "source_latest": date.fromisoformat(raw) if raw else None,
        "eligible_latest": date.fromisoformat(eligible) if eligible else None,
    }


class FakeClient:
    def __init__(self, rows=None, *, estimate=100, failures=None):
        self.rows = rows or {}
        self.estimate = estimate
        self.failures = failures or {}
        self.calls = []
        self.result_calls = []

    def query(self, sql, *, job_config, **kwargs):
        vendor = next(
            v
            for v, marker in (
                ("aws", "aws_member_daily_cost"),
                ("gcp", "gcp_billing_export_resource"),
                ("azure", "azure_billing_cost"),
                ("alibaba", "daily_en_"),
            )
            if marker in sql
        )
        self.calls.append((vendor, sql, job_config, kwargs))
        failure = self.failures.get((vendor, "dry" if job_config.dry_run else "query"))
        if failure:
            raise failure
        if job_config.dry_run:
            return SimpleNamespace(total_bytes_processed=self.estimate)

        def result(**result_kwargs):
            self.result_calls.append(result_kwargs)
            if (vendor, "result") in self.failures:
                raise self.failures[vendor, "result"]
            return self.rows.get(vendor, [])

        return SimpleNamespace(result=result)


def run(engine, client=None, **kwargs):
    return checks.run_cost_source_checks(
        engine, SETTINGS, client=client or FakeClient(), today=TODAY, **kwargs
    )


def params(client, vendor):
    config = next(c[2] for c in client.calls if c[0] == vendor)
    return {p.name: p.values if hasattr(p, "values") else p.value for p in config.query_parameters}


@pytest.mark.parametrize(
    ("raw", "eligible", "summary", "attr", "status", "passed"),
    [
        ("2026-10-27", "2026-10-24", "2026-10-24", "2026-10-24", "within_import_lag", True),
        ("2026-10-24", "2026-10-24", "2026-10-24", "2026-10-24", "ok", True),
        ("2026-10-27", "2026-10-24", "2026-10-26", "2026-10-26", "within_import_lag", True),
        ("2026-10-27", "2026-10-24", "2026-10-27", "2026-10-27", "ok", True),
        ("2026-10-24", "2026-10-24", "2026-10-25", "2026-10-24", "local_ahead", False),
        ("2026-10-24", "2026-10-24", "2026-10-24", "2026-10-25", "local_ahead", False),
        ("2026-10-27", "2026-10-24", "2026-10-22", "2026-10-22", "ingestion_delay", False),
        ("2026-10-27", "2026-10-24", "2026-10-23", "2026-10-23", "within_import_lag", True),
        ("2026-10-24", "2026-10-24", "2026-10-23", "2026-10-23", "ok", True),
        ("2026-10-27", "2026-10-24", "2026-10-24", "2026-10-23", "attribution_delay", False),
        ("2026-10-27", "2026-10-24", None, None, "missing_summary", False),
        ("2026-10-27", "2026-10-24", "2026-10-24", None, "missing_attribution", False),
        (None, None, "2026-10-24", "2026-10-24", "source_empty", False),
        ("2026-10-27", None, "2026-10-24", "2026-10-24", "waiting_import_lag", None),
        (
            "2026-10-20",
            "2026-10-20",
            "2026-10-20",
            "2026-10-20",
            "source_stale_or_no_recent_usage",
            False,
        ),
        (
            "2026-10-10",
            "2026-10-10",
            "2026-10-09",
            "2026-10-09",
            "source_stale_with_import_gap",
            False,
        ),
        ("2026-10-21", "2026-10-21", "2026-10-21", "2026-10-21", "ok", True),
        ("2026-10-21", "2026-10-21", "2026-10-20", "2026-10-20", "ok", True),
    ],
)
def test_watermark_policy(engine, raw, eligible, summary, attr, status, passed):
    seed(engine, summary=summary, attr=attr)
    (result,) = checks.run_cost_source_checks(
        engine,
        replace(SETTINGS, lookback_days=28),
        client=FakeClient({"gcp": [watermark(raw=raw, eligible=eligible)]}),
        today=TODAY,
    )
    assert result.status == status
    assert result.passed is passed
    assert result.level == "MEDIUM"


def test_accounts_active_and_vendor_case_isolation(engine):
    seed(engine, "GCP", "a", "2026-10-20", "2026-10-20", local_vendor="gcp")
    seed(engine, "gcp", "b", "2026-10-27", "2026-10-27", local_vendor="GCP")
    seed(engine, "gcp", "inactive", active=False)
    seed(engine, "azure", "a", "2026-10-27", "2026-10-27", local_vendor="AZURE")
    client = FakeClient({"gcp": [watermark("a"), watermark("b")], "azure": [watermark("a")]})
    results = run(engine, client)
    by_key = {(r.vendor, r.account_id): r for r in results}
    assert len(results) == 3
    assert by_key["gcp", "a"].status == "ingestion_delay"
    assert by_key["gcp", "b"].status == "ok"
    assert by_key["azure", "a"].status == "ok"
    assert params(client, "gcp")["accounts"] == ["a", "b"]
    assert len(client.calls) == 4  # Exactly one dry run + one execution per vendor.


def test_not_returned_active_accounts_are_explicitly_empty(engine):
    seed(engine, account="a")
    seed(engine, account="b")
    results = run(engine, FakeClient({"gcp": [watermark("a")]}))
    assert [r.status for r in results] == ["within_import_lag", "source_empty"]


@pytest.mark.parametrize("profile", ["aws_split_cost_v1", "aws_tidb_cloud_f04_v1", "unknown"])
def test_aws_uncomparable_profiles_are_diagnostics_not_healthy(engine, profile):
    seed(engine, "aws", profile=profile)
    (result,) = run(engine, FakeClient({"aws": [watermark()]}))
    assert result.status == "scope_unverified"
    assert result.scope == "accountwide_diagnostic_only"
    assert result.source_latest == date(2026, 10, 27)
    assert result.passed is False
    assert result.level == "LOW"
    assert "do not verify" in result.describe()


@pytest.mark.parametrize("profile", [None, "aws_cur_legacy_v1"])
def test_ordinary_aws_profiles_are_account_comparable(engine, profile):
    seed(engine, "aws", profile=profile)
    (result,) = run(engine, FakeClient({"aws": [watermark(raw="2026-10-24")]}))
    assert result.status == "ok"
    assert result.scope == "account"


@pytest.mark.parametrize("vendor", ["gcp", "azure", "alibaba"])
def test_unknown_nonaws_profile_not_guessed(engine, vendor):
    seed(engine, vendor, profile="unknown")
    (result,) = run(engine, FakeClient({vendor: [watermark()]}))
    assert result.status == "scope_unverified"


def test_skip_excluded_and_unknown_never_create_client(engine):
    seed(engine, "tencent")
    seed(engine, "gcp", "excluded")
    seed(engine, "mystery")
    with patch("google.cloud.bigquery.Client", side_effect=AssertionError("No ADC")):
        results = checks.run_cost_source_checks(
            engine, replace(SETTINGS, excluded_sources=("gcp/excluded",)), today=TODAY
        )
    by_vendor = {r.vendor: r for r in results}
    assert by_vendor["tencent"].status == "non_bigquery"
    assert by_vendor["tencent"].passed is None
    assert by_vendor["gcp"].status == "excluded"
    assert by_vendor["gcp"].passed is None
    assert "Explicitly" in by_vendor["gcp"].detail
    assert by_vendor["mystery"].status == "scope_unverified"
    assert by_vendor["mystery"].passed is False


def test_sql_is_bounded_parameterized_canonical_and_no_raw_cur(engine):
    client = FakeClient()
    for vendor in ("aws", "gcp", "azure", "alibaba"):
        seed(engine, vendor, "value' OR 1=1 --")
    run(engine, client)
    for vendor, sql, config, kwargs in client.calls:
        assert "value'" not in sql
        assert "IN UNNEST(@accounts)" in sql
        assert "BETWEEN @usage_start AND @usage_end" in sql
        assert "warehouse-f80fbbe8e8." in sql
        assert kwargs == {"location": "us-west1", "timeout": 30, "retry": None, "job_retry": None}
        assert config.maximum_bytes_billed == 1073741824
        assert config.use_query_cache is True
        assert "_stage_" not in sql and "INSERT" not in sql
        p = params(client, vendor)
        assert p["usage_start"] == date(2026, 10, 15)
        assert p["usage_end"] == TODAY
        assert p["accounts"] == ["value' OR 1=1 --"]
        if vendor == "aws":
            assert "billing_period BETWEEN @month_start AND @month_end" in sql
            assert "charge_type" not in sql
            assert p["month_start"] == date(2026, 10, 1)
            assert p["month_end"] == date(2026, 10, 1)
        elif vendor == "gcp":
            assert "_PARTITIONDATE BETWEEN @export_start AND @export_end" in sql
            assert "MAX(IF(_PARTITIONDATE <= @eligible_export_end" in sql
            assert p["eligible_export_end"] == date(2026, 10, 24)
        elif vendor == "azure":
            assert set(p) == {
                "accounts",
                "usage_start",
                "usage_end",
                "month_start_suffix",
                "month_end_suffix",
                "eligible_month_suffix",
            }
            assert "_TABLE_SUFFIX BETWEEN @month_start_suffix AND @month_end_suffix" in sql
            assert "r'^[0-9]{8}$'" in sql
            assert "billingPeriodStartDate" not in sql
            assert "MAX(IF(_TABLE_SUFFIX <= @eligible_month_suffix, date, NULL))" in sql
            assert p["eligible_month_suffix"] == "20261001"
        else:
            assert "_TABLE_SUFFIX BETWEEN @export_start_suffix AND @export_end_suffix" in sql
            assert "partition_date BETWEEN @export_start AND @export_end" in sql
            assert "MAX(IF(partition_date <= @eligible_export_end" in sql
            assert "MAX(DATE(usage_start_time))" in sql
            assert "CAST(owner_account_id AS STRING)" in sql
            assert p["export_start"] == date(2026, 9, 1)
            assert p["export_start_suffix"] == "20260901"
            assert p["export_end"] == TODAY
            assert p["export_end_suffix"] == "20261028"
            assert p["eligible_export_end"] == date(2026, 10, 22)
    for index in range(0, len(client.calls), 2):
        dry, real = client.calls[index : index + 2]
        assert dry[2].dry_run is True
        assert not real[2].dry_run
        assert dry[2].query_parameters == real[2].query_parameters
        assert dry[1] == real[1]
    assert client.result_calls == [{"timeout": 30, "retry": None, "job_retry": None}] * 4


@pytest.mark.parametrize(
    ("today", "eligible_month", "eligible_daily"),
    [
        (date(2026, 11, 1), "20261001", date(2026, 10, 28)),
        (date(2027, 1, 2), "20261201", date(2026, 12, 29)),
        (date(2028, 3, 1), "20280201", date(2028, 2, 26)),
    ],
)
def test_export_eligibility_month_year_leap_rollover(engine, today, eligible_month, eligible_daily):
    seed(engine, "azure")
    seed(engine, "gcp")
    seed(engine, "alibaba")
    client = FakeClient()
    checks.run_cost_source_checks(engine, SETTINGS, client=client, today=today)
    assert params(client, "azure")["eligible_month_suffix"] == eligible_month
    assert params(client, "azure")["month_end_suffix"] == today.replace(day=1).strftime("%Y%m%d")
    assert params(client, "gcp")["eligible_export_end"] == eligible_daily
    assert params(client, "alibaba")["eligible_export_end"] == today - timedelta(days=6)
    assert (today - params(client, "alibaba")["export_start"]).days <= 61


def test_alibaba_prepaid_export_day_is_not_usage_watermark(engine):
    seed(engine, "alibaba", summary="2026-10-17", attr="2026-10-17")
    client = FakeClient({"alibaba": [watermark(raw="2026-10-17", eligible="2026-10-17")]})
    (result,) = checks.run_cost_source_checks(
        engine, SETTINGS, client=client, today=date(2026, 10, 20)
    )
    p = params(client, "alibaba")
    # $157 exported/paid Oct 14, usage Oct 17: the row is eligible, even though
    # its usage is newer than the export cutoff. Never clamp usage at the lag.
    assert p["eligible_export_end"] == date(2026, 10, 14)
    assert p["usage_end"] == date(2026, 10, 20)
    assert p["export_start"] < p["usage_start"]
    assert result.source_latest == result.eligible_latest == date(2026, 10, 17)
    assert result.status == "ok"


def test_local_queries_are_account_and_date_bounded(engine):
    seed(engine, summary="2026-10-14", attr="2026-10-14")
    with engine.begin() as conn:
        for table in ("cost_bq_export_summary_daily", "cost_attribution_daily"):
            conn.execute(text(f"INSERT INTO {table} VALUES ('gcp','a','2026-10-29')"))
            conn.execute(text(f"INSERT INTO {table} VALUES ('gcp','different','2026-10-27')"))
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: statements.append(
            (statement, parameters)
        ),
    )
    (result,) = run(engine, FakeClient({"gcp": [watermark()]}))
    assert result.summary_latest is None
    assert result.attribution_latest is None
    assert result.status == "missing_summary"
    for sql, bound in statements[1:]:
        assert "account_id IN (?)" in sql
        assert "usage_date BETWEEN ? AND ?" in sql
        assert date(2026, 10, 15) in bound and TODAY in bound
        assert "MAX(usage_date)" in sql


def test_db_connection_returned_before_bigquery(engine):
    seed(engine)
    held = []
    event.listen(engine, "checkout", lambda *args: held.append(True))
    event.listen(engine, "checkin", lambda *args: held.pop())
    client = FakeClient({"gcp": [watermark()]})
    original = client.query

    def query(*args, **kwargs):
        assert not held
        return original(*args, **kwargs)

    client.query = query
    assert run(engine, client)[0].passed is True


@pytest.mark.parametrize("stage", ["dry", "query", "result"])
@pytest.mark.parametrize("error", [PermissionError("403 denied"), TimeoutError("deadline")])
def test_vendor_query_errors_isolated_and_bounded(engine, stage, error):
    seed(engine, "gcp")
    seed(engine, "azure")
    client = FakeClient({"azure": [watermark()]}, failures={("gcp", stage): error})
    results = {r.vendor: r for r in run(engine, client)}
    assert results["gcp"].status == "query_error"
    assert results["gcp"].passed is False
    assert str(error) in results["gcp"].detail
    assert results["azure"].passed is True
    assert len(client.calls) <= 4


@pytest.mark.parametrize("estimate", [1073741825, None])
def test_dry_run_cap_unknown_or_over_budget_never_executes(engine, estimate):
    seed(engine)
    client = FakeClient(estimate=estimate)
    (result,) = run(engine, client)
    assert result.status == "query_error"
    assert "cap" in result.detail
    assert len(client.calls) == 1


def test_explicit_configured_cap_honored_without_automatic_increase(engine):
    seed(engine)
    for cap in (2**31, 1000):
        client = FakeClient()
        checks.run_cost_source_checks(
            engine, replace(SETTINGS, maximum_bytes_billed=cap), client=client, today=TODAY
        )
        assert all(c[2].maximum_bytes_billed == cap for c in client.calls)


def test_explicit_two_gib_budget_allows_measured_gcp_estimate(engine):
    seed(engine)
    client = FakeClient({"gcp": [watermark()]}, estimate=1118126285)
    (result,) = checks.run_cost_source_checks(
        engine, replace(SETTINGS, maximum_bytes_billed=2**31), client=client, today=TODAY
    )
    assert result.passed is True
    assert len(client.calls) == 2
    assert all(call[2].maximum_bytes_billed == 2**31 for call in client.calls)


def test_client_creation_failure_is_explicit(engine):
    seed(engine)
    with patch("google.cloud.bigquery.Client", side_effect=RuntimeError("ADC unavailable")):
        (result,) = checks.run_cost_source_checks(engine, SETTINGS, today=TODAY)
    assert result.status == "client_error"
    assert result.passed is False
    assert "ADC unavailable" in result.detail


def test_direct_adc_client_uses_explicit_project_location(engine):
    seed(engine)
    client = FakeClient({"gcp": [watermark()]})
    with patch("google.cloud.bigquery.Client", return_value=client) as factory:
        (result,) = checks.run_cost_source_checks(engine, SETTINGS, today=TODAY)
    factory.assert_called_once_with(project=SETTINGS.project, location="us-west1")
    assert result.passed is True


@pytest.mark.parametrize(
    "changes",
    [
        {"project": None},
        {"project": "p`;DROP TABLE t"},
        {"project": "Uppercase"},
        {"location": ""},
        {"location": " "},
        {"lookback_days": 0},
        {"timeout_seconds": 0},
        {"maximum_bytes_billed": 0},
    ],
)
def test_invalid_config_no_network(engine, changes):
    seed(engine)
    client = FakeClient()
    (result,) = checks.run_cost_source_checks(
        engine, replace(SETTINGS, **changes), client=client, today=TODAY
    )
    assert result.status == "client_error"
    assert not client.calls


def test_registry_failure_never_raises(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE cost_sources"))
    (result,) = run(engine)
    assert result.status == "registry_error"
    assert result.passed is False


def test_local_failure_never_raises_and_retains_all_sources(engine):
    seed(engine, "gcp")
    seed(engine, "azure")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE cost_attribution_daily"))
    client = FakeClient()
    results = run(engine, client)
    assert len(results) == 2
    assert all(r.status == "local_query_error" and r.passed is False for r in results)
    assert not client.calls


def test_local_vendor_failure_does_not_hide_other_vendors(engine):
    seed(engine, "gcp")
    seed(engine, "azure")

    def fail_gcp(conn, cursor, statement, parameters, context, many):
        if "MAX(usage_date)" in statement and parameters[0] == "gcp":
            raise RuntimeError("gcp local SQL failure")

    event.listen(engine, "before_cursor_execute", fail_gcp)
    client = FakeClient({"azure": [watermark()]})
    results = {r.vendor: r for r in run(engine, client)}
    assert results["gcp"].status == "local_query_error"
    assert results["azure"].passed is True
    assert [call[0] for call in client.calls] == ["azure", "azure"]


def test_empty_registry_is_not_healthy(engine):
    (result,) = run(engine)
    assert result.status == "registry_empty"
    assert result.passed is False


def test_disabled_never_touches_engine():
    engine = Mock()
    assert checks.run_cost_source_checks(engine, CostFreshnessSettings()) == []
    engine.connect.assert_not_called()


def test_default_today_is_utc_and_date_conversion(engine):
    seed(engine)
    now = datetime(2026, 10, 28, 1, 10, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return now

    with patch.object(checks, "datetime", Clock):
        client = FakeClient()
        checks.run_cost_source_checks(engine, SETTINGS, client=client)
    assert params(client, "gcp")["usage_end"] == TODAY
    assert checks._date(now) == TODAY
    assert checks._date("2026-10-28") == TODAY
    assert checks._date(TODAY) == TODAY
    assert checks._date(None) is None


def test_describe_contains_signed_diffs_and_missing_values():
    result = checks.CostSourceResult(
        "gcp",
        "a",
        source_latest=date(2026, 10, 27),
        eligible_latest=date(2026, 10, 24),
        summary_latest=date(2026, 10, 26),
        attribution_latest=date(2026, 10, 25),
        status="ok",
    )
    message = result.describe()
    assert "BQ raw=2026-10-27, eligible=2026-10-24" in message
    assert "summary=2026-10-26, attr=2026-10-25" in message
    assert "raw-summary=+1d" in message
    assert "eligible-summary=-2d" in message
    assert "summary-attr=+1d" in message
    assert "n/a" in checks.CostSourceResult("gcp", "missing").describe()


def test_no_cross_package_or_circular_imports():
    source = Path(checks.__file__).read_text()
    tree = ast.parse(source)
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(
        name and (name.startswith("cost_insight") or "check_data_freshness" in name)
        for name in imports
    )
    assert "impersonat" not in source


def test_unknown_sql_vendor_fails_closed():
    with pytest.raises(ValueError, match="Unknown BigQuery vendor"):
        checks._source_sql("unknown", SETTINGS.project)
