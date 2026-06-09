from datetime import datetime, timezone

import pytest

from cost_insight.common.bigquery import BigQueryQueryResult
from cost_insight.common.config import GcsCacheSettings
from cost_insight.jobs.cleanup_gcs_cache import (
    build_cleanup_gcs_cache_dry_run_query,
    run_cleanup_gcs_cache,
)


def test_cleanup_gcs_cache_dry_run_query_targets_current_table() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_cleanup_gcs_cache_dry_run_query(settings)

    assert "`pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`" in query
    assert "ARRAY_AGG(" in query
    assert "object_kind = 'ac'" in query
    assert "object_kind = 'cas'" in query


def test_run_cleanup_gcs_cache_returns_summary_with_samples() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured["query"] = query
        captured["parameters"] = {param.name: param.value for param in parameters}
        return BigQueryQueryResult(
            rows=(
                {
                    "candidate_object_count": 20,
                    "ac_candidate_count": 5,
                    "cas_candidate_count": 15,
                    "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
                    "newest_last_seen_at": datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
                    "sample_candidates": [
                        {
                            "object_name": "ac/foo",
                            "object_kind": "ac",
                            "last_seen_at": "2026-05-01T00:00:00+00:00",
                            "idle_days": 39,
                        },
                        {
                            "object_name": "cas/bar",
                            "object_kind": "cas",
                            "last_seen_at": "2026-05-02T00:00:00+00:00",
                            "idle_days": 38,
                        },
                    ],
                },
            ),
            total_bytes_processed=12345,
        )

    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="dry-run",
        ac_retention_days=21,
        cas_retention_days=35,
        sample_limit=2,
        execute=fake_execute,
    )

    assert captured["parameters"] == {
        "ac_retention_days": 21,
        "cas_retention_days": 35,
        "sample_limit": 2,
    }
    assert summary.candidate_object_count == 20
    assert summary.ac_candidate_count == 5
    assert summary.cas_candidate_count == 15
    assert len(summary.sample_candidates) == 2
    assert summary.sample_candidates[0].object_name == "ac/foo"
    assert summary.bytes_processed == 12345


def test_run_cleanup_gcs_cache_rejects_non_dry_run_mode() -> None:
    with pytest.raises(ValueError, match="only supports --mode dry-run"):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            mode="delete",
        )


def test_run_cleanup_gcs_cache_propagates_query_failures() -> None:
    def fake_execute(query, parameters):
        raise RuntimeError("Query execution failed")

    with pytest.raises(RuntimeError, match="Query execution failed"):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            execute=fake_execute,
        )
