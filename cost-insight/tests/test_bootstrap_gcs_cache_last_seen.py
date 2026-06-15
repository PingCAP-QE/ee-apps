from datetime import date, datetime, timezone

import pytest

import cost_insight.jobs.bootstrap_gcs_cache_last_seen as bootstrap_gcs_cache_last_seen
from cost_insight.common.bigquery import BigQueryQueryResult
from cost_insight.common.config import GcsCacheSettings
from cost_insight.jobs.bootstrap_gcs_cache_last_seen import (
    build_bootstrap_gcs_cache_last_seen_dry_run_query,
    build_bootstrap_gcs_cache_last_seen_query,
    run_bootstrap_gcs_cache_last_seen,
)


def test_bootstrap_gcs_cache_last_seen_dry_run_uses_expected_query_shape() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_bootstrap_gcs_cache_last_seen_dry_run_query(settings)

    assert "storage.objects.get" in query
    assert "storage.objects.create" in query
    assert "DATE(timestamp) BETWEEN @start_date AND @end_date" in query
    assert "WITH extracted AS (" in query
    assert "COUNT(*) AS distinct_objects" in query
    assert "COALESCE(SUM(source_event_count), 0) AS source_rows_seen" in query
    assert "COUNTIF(method_name = \"storage.objects.get\") AS total_get_count" in query
    assert "resource.labels.bucket_name = @bucket_name" in query


def test_bootstrap_gcs_cache_last_seen_query_replaces_current_table() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_bootstrap_gcs_cache_last_seen_query(settings)
    replace_select = query.partition("CREATE OR REPLACE TABLE")[2].partition("FROM bootstrap_rollup")[0]

    assert "CREATE TEMP TABLE bootstrap_rollup AS" in query
    assert (
        "CREATE OR REPLACE TABLE `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`"
        in query
    )
    assert "CLUSTER BY object_kind, last_seen_date AS" in query
    assert "DATE(last_seen_at) AS last_seen_date" in query
    assert "source_event_count" not in replace_select


def test_run_bootstrap_gcs_cache_last_seen_returns_summary_from_executor() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured["query"] = query
        captured["parameters"] = {param.name: param.value for param in parameters}
        return BigQueryQueryResult(
            rows=({"distinct_objects": 12, "source_rows_seen": 345},),
            total_bytes_processed=987654321,
        )

    summary = run_bootstrap_gcs_cache_last_seen(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        start_date=date(2026, 5, 25),
        end_date=date(2026, 6, 9),
        dry_run=True,
        execute=fake_execute,
    )

    assert captured["parameters"] == {
        "start_date": date(2026, 5, 25),
        "end_date": date(2026, 6, 9),
        "bucket_name": "pingcap-ci-bazel-remote-cache-us-central1",
    }
    assert summary.account_id == "pingcap-testing-account"
    assert summary.start_date == date(2026, 5, 25)
    assert summary.end_date == date(2026, 6, 9)
    assert summary.distinct_objects == 12
    assert summary.source_rows_seen == 345
    assert summary.bytes_processed == 987654321
    assert summary.dry_run is True


def test_run_bootstrap_gcs_cache_last_seen_defaults_end_date_to_yesterday_utc(monkeypatch) -> None:
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

    monkeypatch.setattr(bootstrap_gcs_cache_last_seen, "datetime", FrozenDatetime)

    summary = run_bootstrap_gcs_cache_last_seen(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        start_date=date(2026, 5, 25),
        dry_run=True,
        execute=fake_execute,
    )

    assert captured["parameters"]["end_date"] == date(2026, 6, 9)
    assert summary.end_date == date(2026, 6, 9)


def test_run_bootstrap_gcs_cache_last_seen_rejects_invalid_date_range() -> None:
    with pytest.raises(ValueError, match="--start-date must be before or equal to --end-date"):
        run_bootstrap_gcs_cache_last_seen(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 9),
            dry_run=True,
            execute=lambda *_args, **_kwargs: BigQueryQueryResult(rows=(), total_bytes_processed=None),
        )
