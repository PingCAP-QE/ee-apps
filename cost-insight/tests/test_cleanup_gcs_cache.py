from datetime import UTC, datetime

import pytest

from cost_insight.common.bigquery import BigQueryQueryResult
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.gcs_objects import GcsObjectMetadata
from cost_insight.common.storage_batch_operations import (
    StorageBatchOperationsJob,
    StorageBatchOperationsJobStatus,
)
from cost_insight.jobs.cleanup_gcs_cache import (
    build_cleanup_gcs_cache_index_ready_query,
    build_cleanup_gcs_cache_manifest_export_query,
    build_cleanup_gcs_cache_reconcile_deleted_cas_query,
    build_cleanup_gcs_cache_summary_query,
    run_cleanup_gcs_cache,
)


def test_cleanup_gcs_cache_summary_query_targets_current_and_reference_tables() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_cleanup_gcs_cache_summary_query(settings)

    assert "`pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`" in query
    assert "`pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_ac_cas_references`" in query
    assert "candidate_cas_object_count" in query
    assert "candidate_ac_object_count" in query
    assert "@cas_cutoff_days" in query


def test_cleanup_gcs_cache_index_ready_query_targets_state_table() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_cleanup_gcs_cache_index_ready_query(settings)

    assert "`pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_ac_reference_index_state`" in query
    assert "COUNTIF(indexed_through IS NOT NULL)" in query


def test_cleanup_gcs_cache_manifest_export_query_uses_generation() -> None:
    query = build_cleanup_gcs_cache_manifest_export_query(
        candidate_table="`project.dataset.table`",
        manifest_uri="gs://manifest-bucket/prefix/manifest-*.csv",
        bucket_name="pingcap-ci-bazel-remote-cache-us-central1",
    )

    assert "'pingcap-ci-bazel-remote-cache-us-central1' AS bucket" in query
    assert "object_name AS name" in query
    assert "generation" in query


def test_cleanup_gcs_cache_reconcile_deleted_cas_query_matches_object_name_and_last_seen_at() -> None:
    query = build_cleanup_gcs_cache_reconcile_deleted_cas_query(
        GcsCacheSettings(project_id="pingcap-testing-account"),
        candidate_table="`project.dataset.candidates`",
    )

    assert "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`" in query
    assert "STRUCT(object_name, last_seen_at)" in query
    assert "FROM `project.dataset.candidates`" in query


def test_run_cleanup_gcs_cache_returns_dry_run_summary_with_samples() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured.setdefault("queries", []).append((query, {param.name: param.value for param in parameters}))
        if "COUNT(*) AS total_shards" in query:
            return BigQueryQueryResult(
                rows=({"total_shards": 256, "ready_shards": 256},),
                total_bytes_processed=5,
            )
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 20,
                        "candidate_ac_object_count": 5,
                        "candidate_cas_delete_object_count": 15,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
                        "sample_candidates": [
                            {
                                "object_name": "cas/foo",
                                "last_seen_at": "2026-05-01T00:00:00+00:00",
                                "idle_days": 39,
                            }
                        ],
                    },
                ),
                total_bytes_processed=10,
            )
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="dry-run",
        execute_kind="all",
        cas_retention_days=14,
        safety_buffer_days=1,
        sample_limit=1,
        execute=fake_execute,
        now=lambda: now,
        run_id_factory=lambda: "run-dry-001",
    )

    assert summary.run_id == "run-dry-001"
    assert summary.execute_kind == "cas"
    assert summary.candidate_cas_object_count == 20
    assert summary.candidate_ac_object_count == 5
    assert summary.candidate_cas_delete_object_count == 15
    assert summary.selected_ac_object_count == 0
    assert summary.selected_cas_object_count == 0
    assert len(summary.sample_candidates) == 1
    assert summary.sample_candidates[0].object_name == "cas/foo"
    assert summary.bytes_processed == 16
    assert summary.run_started_at == now
    assert summary.run_finished_at == now


def test_run_cleanup_gcs_cache_delete_cascades_ac_then_cas() -> None:
    captured_queries = []
    created_jobs = []
    waited_jobs = []
    loaded_rows = []

    def fake_execute(query, parameters):
        rendered = {param.name: param.value for param in parameters}
        captured_queries.append((query, rendered))
        if "COUNT(*) AS total_shards" in query:
            return BigQueryQueryResult(
                rows=({"total_shards": 256, "ready_shards": 256},),
                total_bytes_processed=1,
            )
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 8,
                        "candidate_ac_object_count": 3,
                        "candidate_cas_delete_object_count": 5,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 8, 0, 0, tzinfo=UTC),
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=2,
            )
        if "SELECT COUNT(*) AS object_count FROM" in query:
            if "delete_ac" in query:
                return BigQueryQueryResult(rows=({"object_count": 1},), total_bytes_processed=1)
            if "delete_cas" in query:
                return BigQueryQueryResult(rows=({"object_count": 2},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        if "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_ac_" in query:
            yield {"object_name": "ac/one"}
            yield {"object_name": "ac/missing"}
            return
        if "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_cas_" in query:
            yield {"object_name": "cas/one"}
            yield {"object_name": "cas/two"}
            yield {"object_name": "cas/missing"}
            return
        raise AssertionError(f"Unexpected stream query: {query}")

    def fake_resolve_object_metadata(**kwargs):
        names = tuple(kwargs["object_names"])
        mapping = {
            "ac/one": GcsObjectMetadata("ac/one", True, 101),
            "ac/missing": GcsObjectMetadata("ac/missing", False, None),
            "cas/one": GcsObjectMetadata("cas/one", True, 201),
            "cas/two": GcsObjectMetadata("cas/two", True, 202),
            "cas/missing": GcsObjectMetadata("cas/missing", False, None),
        }
        return tuple(mapping[name] for name in names)

    def fake_load_json_rows(table_ref, rows, schema, write_disposition):
        loaded_rows.append((table_ref, tuple(rows), tuple(schema), write_disposition))

    def fake_create_batch_job(**kwargs):
        created_jobs.append(kwargs)
        return StorageBatchOperationsJob(
            job_name=f"projects/pingcap-testing-account/locations/global/jobs/{kwargs['job_id']}",
            operation_name="operations/op-1",
        )

    def fake_wait_for_batch_job(**kwargs):
        waited_jobs.append(kwargs)
        return StorageBatchOperationsJobStatus(
            job_name=kwargs["job_name"],
            state="SUCCEEDED",
            total_object_count=1,
            succeeded_object_count=1,
            failed_object_count=0,
            total_bytes_transformed=123,
            complete_time=datetime(2026, 6, 15, 10, 31, tzinfo=UTC),
        )

    run_started_at = datetime(2026, 6, 15, 10, 30, tzinfo=UTC)
    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="cas",
        cas_retention_days=14,
        safety_buffer_days=1,
        max_delete_objects=10000000,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_object_metadata,
        load_json_rows=fake_load_json_rows,
        create_batch_job=fake_create_batch_job,
        wait_for_batch_job=fake_wait_for_batch_job,
        now=lambda: run_started_at,
        run_id_factory=lambda: "steady-run-001",
    )

    assert summary.candidate_cas_object_count == 8
    assert summary.candidate_ac_object_count == 3
    assert summary.candidate_cas_delete_object_count == 5
    assert summary.selected_ac_object_count == 1
    assert summary.selected_cas_object_count == 2
    assert summary.ac_manifest_uri is not None
    assert summary.cas_manifest_uri is not None
    assert len(created_jobs) == 2
    assert created_jobs[0]["manifest_uri"] == summary.ac_manifest_uri
    assert created_jobs[1]["manifest_uri"] == summary.cas_manifest_uri
    assert len(waited_jobs) == 2
    assert any("DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_ac_cas_references`" in query for query, _ in captured_queries)
    assert any("manifest-*.csv" in query for query, _ in captured_queries)
    assert any("WRITE_APPEND" == disposition for _, _, _, disposition in loaded_rows)


def test_run_cleanup_gcs_cache_delete_requires_execute_kind_cas() -> None:
    with pytest.raises(ValueError, match="requires --execute-kind cas"):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            mode="delete",
            execute_kind="all",
        )


def test_run_cleanup_gcs_cache_fails_closed_when_index_not_ready() -> None:
    def fake_execute(query, parameters):
        if "COUNT(*) AS total_shards" in query:
            return BigQueryQueryResult(
                rows=({"total_shards": 256, "ready_shards": 255},),
                total_bytes_processed=1,
            )
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    with pytest.raises(RuntimeError, match="AC reference index is not ready"):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            mode="dry-run",
            execute=fake_execute,
        )
