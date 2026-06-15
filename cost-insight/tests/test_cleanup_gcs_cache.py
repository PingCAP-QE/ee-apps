from datetime import UTC, datetime

import pytest

from cost_insight.common.bigquery import BigQueryQueryResult
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.storage_batch_operations import (
    StorageBatchOperationsJob,
    StorageBatchOperationsJobStatus,
)
from cost_insight.jobs.cleanup_gcs_cache import (
    build_cleanup_gcs_cache_candidate_table_query,
    build_cleanup_gcs_cache_manifest_export_query,
    build_cleanup_gcs_cache_reconcile_current_table_query,
    build_cleanup_gcs_cache_summary_query,
    run_cleanup_gcs_cache,
)


def test_cleanup_gcs_cache_summary_query_targets_current_table() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_cleanup_gcs_cache_summary_query(settings, execute_kind="all")

    assert "`pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`" in query
    assert "ARRAY_AGG(" in query
    assert "@ac_cutoff_days" in query
    assert "@cas_cutoff_days" in query
    assert "object_kind = 'ac'" in query
    assert "object_kind = 'cas'" in query


def test_cleanup_gcs_cache_candidate_query_limits_mixed_canary_to_500_each() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_cleanup_gcs_cache_candidate_table_query(
        settings,
        execute_kind="mixed-canary",
        candidate_table="`project.dataset.table`",
    )

    assert "LIMIT 500" in query
    assert query.count("LIMIT 500") == 2
    assert "UNION ALL" in query
    assert "object_kind = 'ac'" in query
    assert "object_kind = 'cas'" in query


def test_cleanup_gcs_cache_manifest_export_query_uses_bucket_and_name_columns() -> None:
    query = build_cleanup_gcs_cache_manifest_export_query(
        candidate_table="`project.dataset.table`",
        manifest_uri="gs://manifest-bucket/prefix/manifest-*.csv",
        bucket_name="pingcap-ci-bazel-remote-cache-us-central1",
    )

    assert "EXPORT DATA OPTIONS" in query
    assert "manifest-*.csv" in query
    assert "'pingcap-ci-bazel-remote-cache-us-central1' AS bucket" in query
    assert "object_name AS name" in query


def test_cleanup_gcs_cache_reconcile_query_matches_object_name_and_last_seen_at() -> None:
    query = build_cleanup_gcs_cache_reconcile_current_table_query(
        GcsCacheSettings(project_id="pingcap-testing-account"),
        candidate_table="`project.dataset.candidates`",
    )

    assert "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`" in query
    assert "STRUCT(object_name, last_seen_at)" in query
    assert "FROM `project.dataset.candidates`" in query


def test_run_cleanup_gcs_cache_returns_dry_run_summary_with_samples() -> None:
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
                    "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                    "newest_last_seen_at": datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
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

    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="dry-run",
        execute_kind="all",
        ac_retention_days=14,
        cas_retention_days=21,
        safety_buffer_days=1,
        sample_limit=2,
        execute=fake_execute,
        now=lambda: now,
        run_id_factory=lambda: "run-dry-001",
    )

    assert captured["parameters"] == {
        "ac_cutoff_days": 15,
        "cas_cutoff_days": 22,
        "sample_limit": 2,
    }
    assert summary.run_id == "run-dry-001"
    assert summary.candidate_object_count == 20
    assert summary.selected_object_count == 0
    assert summary.ac_candidate_count == 5
    assert summary.cas_candidate_count == 15
    assert len(summary.sample_candidates) == 2
    assert summary.sample_candidates[0].object_name == "ac/foo"
    assert summary.bytes_processed == 12345
    assert summary.run_started_at == now
    assert summary.run_finished_at == now


def test_run_cleanup_gcs_cache_dry_run_ac_only_passes_only_used_parameters() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured["parameters"] = {param.name: param.value for param in parameters}
        return BigQueryQueryResult(
            rows=(
                {
                    "candidate_object_count": 2,
                    "ac_candidate_count": 2,
                    "cas_candidate_count": 0,
                    "oldest_last_seen_at": None,
                    "newest_last_seen_at": None,
                    "sample_candidates": [],
                },
            ),
            total_bytes_processed=1,
        )

    run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="dry-run",
        execute_kind="ac",
        ac_retention_days=14,
        cas_retention_days=21,
        safety_buffer_days=1,
        sample_limit=3,
        execute=fake_execute,
        now=lambda: datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
    )

    assert captured["parameters"] == {
        "ac_cutoff_days": 15,
        "sample_limit": 3,
    }


def test_run_cleanup_gcs_cache_delete_ac_creates_manifest_and_batch_job() -> None:
    captured_queries = []
    created_jobs = []
    waited_jobs = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {param.name: param.value for param in parameters}))
        if "ARRAY_AGG(" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_object_count": 7,
                        "ac_candidate_count": 7,
                        "cas_candidate_count": 0,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 8, 0, 0, tzinfo=UTC),
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=100,
            )
        if "CREATE OR REPLACE TABLE" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=200)
        if "selected_object_count" in query:
            return BigQueryQueryResult(
                rows=({"selected_object_count": 7},),
                total_bytes_processed=25,
            )
        if "EXPORT DATA OPTIONS" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=50)
        if query.startswith("DELETE FROM "):
            return BigQueryQueryResult(rows=(), total_bytes_processed=30)
        raise AssertionError(f"Unexpected query: {query}")

    def fake_create_batch_job(**kwargs):
        created_jobs.append(kwargs)
        return StorageBatchOperationsJob(
            job_name="projects/pingcap-testing-account/locations/global/jobs/job-1",
            operation_name="operations/op-1",
        )

    def fake_wait_for_batch_job(**kwargs):
        waited_jobs.append(kwargs)
        return StorageBatchOperationsJobStatus(
            job_name="projects/pingcap-testing-account/locations/global/jobs/job-1",
            state="SUCCEEDED",
            total_object_count=7,
            succeeded_object_count=7,
            failed_object_count=0,
            total_bytes_transformed=123,
            complete_time=datetime(2026, 6, 15, 10, 31, tzinfo=UTC),
        )

    run_started_at = datetime(2026, 6, 15, 10, 30, tzinfo=UTC)
    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="ac",
        ac_retention_days=14,
        cas_retention_days=21,
        safety_buffer_days=1,
        max_delete_objects=10000000,
        execute=fake_execute,
        create_batch_job=fake_create_batch_job,
        wait_for_batch_job=fake_wait_for_batch_job,
        now=lambda: run_started_at,
        run_id_factory=lambda: "steady-run-001",
    )

    assert summary.candidate_object_count == 7
    assert summary.selected_object_count == 7
    assert summary.sample_candidates == ()
    assert summary.manifest_uri == (
        "gs://pingcap-ci-console-logs-us-central1/"
        "gcs-cache-steady-state-delete/2026-06-15/ac/steady-run-001/manifest-*.csv"
    )
    assert summary.batch_job_name == "projects/pingcap-testing-account/locations/global/jobs/job-1"
    assert summary.bytes_processed == 405
    assert len(captured_queries) == 5
    assert captured_queries[0][1] == {
        "ac_cutoff_days": 15,
        "sample_limit": 10,
    }
    assert captured_queries[1][1] == {
        "run_id": "steady-run-001",
        "ttl_days": 7,
        "ac_cutoff_days": 15,
        "limit": 7,
    }
    assert captured_queries[4][0].startswith(
        "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`"
    )
    assert created_jobs[0]["manifest_uri"] == summary.manifest_uri
    assert created_jobs[0]["dry_run"] is False
    assert created_jobs[0]["bucket_name"] == "pingcap-ci-bazel-remote-cache-us-central1"
    assert waited_jobs == [{"job_name": "projects/pingcap-testing-account/locations/global/jobs/job-1"}]


def test_run_cleanup_gcs_cache_delete_mixed_canary_uses_fixed_500_per_kind() -> None:
    captured_queries = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {param.name: param.value for param in parameters}))
        if "ARRAY_AGG(" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_object_count": 5000,
                        "ac_candidate_count": 1200,
                        "cas_candidate_count": 3800,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 20, 0, 0, tzinfo=UTC),
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=10,
            )
        if "CREATE OR REPLACE TABLE" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=20)
        if "selected_object_count" in query:
            return BigQueryQueryResult(rows=({"selected_object_count": 1000},), total_bytes_processed=5)
        if "EXPORT DATA OPTIONS" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=3)
        if query.startswith("DELETE FROM "):
            return BigQueryQueryResult(rows=(), total_bytes_processed=2)
        raise AssertionError(f"Unexpected query: {query}")

    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="mixed-canary",
        execute=fake_execute,
        create_batch_job=lambda **_: StorageBatchOperationsJob(
            job_name="projects/pingcap-testing-account/locations/global/jobs/job-canary",
            operation_name="operations/op-canary",
        ),
        wait_for_batch_job=lambda **_: StorageBatchOperationsJobStatus(
            job_name="projects/pingcap-testing-account/locations/global/jobs/job-canary",
            state="SUCCEEDED",
            total_object_count=1000,
            succeeded_object_count=1000,
            failed_object_count=0,
            total_bytes_transformed=321,
            complete_time=datetime(2026, 6, 15, 8, 1, tzinfo=UTC),
        ),
        now=lambda: datetime(2026, 6, 15, 8, 0, tzinfo=UTC),
        run_id_factory=lambda: "steady-canary-001",
    )

    create_query, create_params = captured_queries[1]
    assert create_query.count("LIMIT 500") == 2
    assert captured_queries[0][1] == {
        "ac_cutoff_days": 15,
        "cas_cutoff_days": 22,
        "sample_limit": 10,
    }
    assert create_params == {
        "run_id": "steady-canary-001",
        "ttl_days": 7,
        "ac_cutoff_days": 15,
        "cas_cutoff_days": 22,
    }
    assert captured_queries[4][0].startswith(
        "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`"
    )
    assert summary.selected_object_count == 1000


def test_run_cleanup_gcs_cache_delete_cas_passes_only_used_parameters() -> None:
    captured_queries = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {param.name: param.value for param in parameters}))
        if "ARRAY_AGG(" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_object_count": 9,
                        "ac_candidate_count": 0,
                        "cas_candidate_count": 9,
                        "oldest_last_seen_at": None,
                        "newest_last_seen_at": None,
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=10,
            )
        if "CREATE OR REPLACE TABLE" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=20)
        if "selected_object_count" in query:
            return BigQueryQueryResult(rows=({"selected_object_count": 9},), total_bytes_processed=5)
        if "EXPORT DATA OPTIONS" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=3)
        if query.startswith("DELETE FROM "):
            return BigQueryQueryResult(rows=(), total_bytes_processed=2)
        raise AssertionError(f"Unexpected query: {query}")

    run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="cas",
        execute=fake_execute,
        create_batch_job=lambda **_: StorageBatchOperationsJob(
            job_name="projects/pingcap-testing-account/locations/global/jobs/job-cas",
            operation_name="operations/op-cas",
        ),
        wait_for_batch_job=lambda **_: StorageBatchOperationsJobStatus(
            job_name="projects/pingcap-testing-account/locations/global/jobs/job-cas",
            state="SUCCEEDED",
            total_object_count=9,
            succeeded_object_count=9,
            failed_object_count=0,
            total_bytes_transformed=123,
            complete_time=datetime(2026, 6, 15, 8, 1, tzinfo=UTC),
        ),
        now=lambda: datetime(2026, 6, 15, 8, 0, tzinfo=UTC),
        run_id_factory=lambda: "steady-cas-001",
    )

    assert captured_queries[0][1] == {
        "cas_cutoff_days": 22,
        "sample_limit": 10,
    }
    assert captured_queries[1][1] == {
        "run_id": "steady-cas-001",
        "ttl_days": 7,
        "cas_cutoff_days": 22,
        "limit": 9,
    }


def test_run_cleanup_gcs_cache_delete_does_not_reconcile_when_batch_job_has_failures() -> None:
    captured_queries = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {param.name: param.value for param in parameters}))
        if "ARRAY_AGG(" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_object_count": 7,
                        "ac_candidate_count": 7,
                        "cas_candidate_count": 0,
                        "oldest_last_seen_at": None,
                        "newest_last_seen_at": None,
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=10,
            )
        if "CREATE OR REPLACE TABLE" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=20)
        if "selected_object_count" in query:
            return BigQueryQueryResult(rows=({"selected_object_count": 7},), total_bytes_processed=5)
        if "EXPORT DATA OPTIONS" in query:
            return BigQueryQueryResult(rows=(), total_bytes_processed=3)
        if query.startswith("DELETE FROM "):
            raise AssertionError("Reconcile query should not run when batch delete has failures")
        raise AssertionError(f"Unexpected query: {query}")

    with pytest.raises(RuntimeError, match="reported failed objects"):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            mode="delete",
            execute_kind="ac",
            execute=fake_execute,
            create_batch_job=lambda **_: StorageBatchOperationsJob(
                job_name="projects/pingcap-testing-account/locations/global/jobs/job-failed",
                operation_name="operations/op-failed",
            ),
            wait_for_batch_job=lambda **_: StorageBatchOperationsJobStatus(
                job_name="projects/pingcap-testing-account/locations/global/jobs/job-failed",
                state="SUCCEEDED",
                total_object_count=7,
                succeeded_object_count=6,
                failed_object_count=1,
                total_bytes_transformed=100,
                complete_time=datetime(2026, 6, 15, 8, 1, tzinfo=UTC),
            ),
            now=lambda: datetime(2026, 6, 15, 8, 0, tzinfo=UTC),
            run_id_factory=lambda: "steady-failed-001",
        )


def test_run_cleanup_gcs_cache_rejects_delete_without_specific_execute_kind() -> None:
    with pytest.raises(
        ValueError,
        match="requires --execute-kind ac, cas, or mixed-canary",
    ):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            mode="delete",
            execute_kind="all",
        )


def test_run_cleanup_gcs_cache_propagates_query_failures() -> None:
    def fake_execute(query, parameters):
        raise RuntimeError("Query execution failed")

    with pytest.raises(RuntimeError, match="Query execution failed"):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            execute=fake_execute,
        )
