from datetime import date, datetime, timezone

import cost_insight.jobs.sync_gcs_cache_last_seen as sync_gcs_cache_last_seen
from cost_insight.common.bigquery import BigQueryQueryResult
from cost_insight.common.config import GcsCacheSettings
from cost_insight.jobs.sync_gcs_cache_last_seen import (
    build_sync_gcs_cache_last_seen_dry_run_query,
    build_sync_gcs_cache_last_seen_query,
    run_sync_gcs_cache_last_seen,
)


def test_sync_gcs_cache_last_seen_dry_run_uses_expected_query_shape() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_sync_gcs_cache_last_seen_dry_run_query(settings)

    assert "storage.objects.get" in query
    assert "storage.objects.create" in query
    assert "WITH extracted AS (" in query
    assert "COUNT(*) AS distinct_objects" in query
    assert "COALESCE(SUM(event_count_in_day), 0) AS source_rows_seen" in query
    assert "COUNTIF(method_name = \"storage.objects.get\") AS get_count_in_day" in query
    assert "resource.labels.bucket_name = @bucket_name" in query
    assert "callerSuppliedUserAgent" in query
    where_section = query.partition("WHERE DATE(timestamp) = @run_date")[2].partition(")\nSELECT")[0]
    assert "callerSuppliedUserAgent" in where_section
    assert "@excluded_get_user_agent" in where_section
    assert "@excluded_get_principal_email" in where_section
    assert "authenticationInfo.principalEmail" in where_section


def test_sync_gcs_cache_last_seen_query_creates_and_merges_tables() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_sync_gcs_cache_last_seen_query(settings)

    assert (
        "CREATE TABLE IF NOT EXISTS `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_daily`"
        in query
    )
    assert "first_seen_at TIMESTAMP NOT NULL" in query
    assert (
        "CREATE TABLE IF NOT EXISTS `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`"
        in query
    )
    assert (
        "MERGE `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current` AS target"
        in query
    )
    assert "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_daily`" in query
    assert "event_count_in_day" not in query.partition("INSERT INTO")[2].partition("FROM daily_rollup")[0]


def test_run_sync_gcs_cache_last_seen_returns_summary_from_executor() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured["query"] = query
        captured["parameters"] = {param.name: param.value for param in parameters}
        return BigQueryQueryResult(
            rows=({"distinct_objects": 12, "source_rows_seen": 345},),
            total_bytes_processed=987654321,
        )

    summary = run_sync_gcs_cache_last_seen(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        run_date=date(2026, 6, 8),
        dry_run=True,
        execute=fake_execute,
    )

    assert captured["parameters"] == {
        "run_date": date(2026, 6, 8),
        "bucket_name": "pingcap-ci-bazel-remote-cache-us-central1",
        "excluded_get_user_agent": "TransferService",
        "excluded_get_principal_email": "",
    }
    assert summary.account_id == "pingcap-testing-account"
    assert summary.run_date == date(2026, 6, 8)
    assert summary.distinct_objects == 12
    assert summary.source_rows_seen == 345
    assert summary.bytes_processed == 987654321
    assert summary.dry_run is True


def test_run_sync_gcs_cache_last_seen_defaults_to_yesterday_utc(monkeypatch) -> None:
    captured = {}

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 10, 12, 0, 0, tzinfo=tz or timezone.utc)

    def fake_execute(query, parameters):
        captured["parameters"] = {param.name: param.value for param in parameters}
        return BigQueryQueryResult(
            rows=({"distinct_objects": 0, "source_rows_seen": 0},),
            total_bytes_processed=None,
        )

    monkeypatch.setattr(sync_gcs_cache_last_seen, "datetime", FrozenDatetime)

    summary = run_sync_gcs_cache_last_seen(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        dry_run=True,
        execute=fake_execute,
    )

    assert captured["parameters"]["run_date"] == date(2026, 6, 9)
    assert summary.run_date == date(2026, 6, 9)


def test_run_sync_gcs_cache_last_seen_passes_optional_principal_email_filter() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured["parameters"] = {param.name: param.value for param in parameters}
        return BigQueryQueryResult(
            rows=({"distinct_objects": 0, "source_rows_seen": 0},),
            total_bytes_processed=None,
        )

    run_sync_gcs_cache_last_seen(
        settings=GcsCacheSettings(
            project_id="pingcap-testing-account",
            last_seen_excluded_get_principal_email=(
                "ci-dashboard@pingcap-testing-account.iam.gserviceaccount.com"
            ),
        ),
        run_date=date(2026, 6, 16),
        dry_run=True,
        execute=fake_execute,
    )

    assert captured["parameters"]["excluded_get_user_agent"] == "TransferService"
    assert (
        captured["parameters"]["excluded_get_principal_email"]
        == "ci-dashboard@pingcap-testing-account.iam.gserviceaccount.com"
    )


def test_sync_gcs_cache_last_seen_skips_exclusion_clause_for_blank_user_agent() -> None:
    query = build_sync_gcs_cache_last_seen_dry_run_query(
        GcsCacheSettings(
            project_id="pingcap-testing-account",
            last_seen_excluded_get_user_agent="   ",
            last_seen_excluded_get_principal_email=(
                "ci-dashboard@pingcap-testing-account.iam.gserviceaccount.com"
            ),
        )
    )

    where_section = query.partition("WHERE DATE(timestamp) = @run_date")[2].partition(")\nSELECT")[0]
    assert "callerSuppliedUserAgent" not in where_section
    assert "authenticationInfo.principalEmail" not in where_section


def test_run_sync_gcs_cache_last_seen_skips_exclusion_parameters_for_blank_user_agent() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured["parameters"] = {param.name: param.value for param in parameters}
        return BigQueryQueryResult(
            rows=({"distinct_objects": 0, "source_rows_seen": 0},),
            total_bytes_processed=None,
        )

    run_sync_gcs_cache_last_seen(
        settings=GcsCacheSettings(
            project_id="pingcap-testing-account",
            last_seen_excluded_get_user_agent="",
            last_seen_excluded_get_principal_email=(
                "ci-dashboard@pingcap-testing-account.iam.gserviceaccount.com"
            ),
        ),
        run_date=date(2026, 6, 16),
        dry_run=True,
        execute=fake_execute,
    )

    assert captured["parameters"] == {
        "run_date": date(2026, 6, 16),
        "bucket_name": "pingcap-ci-bazel-remote-cache-us-central1",
    }
