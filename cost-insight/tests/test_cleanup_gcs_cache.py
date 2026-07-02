import json
import re
from datetime import UTC, datetime

import pytest

from cost_insight.common.bigquery import BigQueryQueryResult
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.gcs_cache_references import AcReferenceExtraction
from cost_insight.common.gcs_objects import GcsObjectMetadata
from cost_insight.common.storage_batch_operations import (
    StorageBatchOperationsJob,
    StorageBatchOperationsJobStatus,
)
from cost_insight.jobs.cleanup_gcs_cache import (
    _populate_ac_stage_tables,
    build_cleanup_gcs_cache_cas_candidate_table_query,
    build_cleanup_gcs_cache_final_cas_delete_table_query,
    build_cleanup_gcs_cache_final_ac_delete_table_query,
    build_cleanup_gcs_cache_manifest_export_query,
    build_cleanup_gcs_cache_metadata_stage_tables_query,
    build_cleanup_gcs_cache_reconcile_deleted_cas_query,
    build_cleanup_gcs_cache_run_references_table_query,
    build_cleanup_gcs_cache_summary_query,
    run_cleanup_gcs_cache,
)


def test_cleanup_gcs_cache_summary_query_targets_current_and_reference_tables() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_cleanup_gcs_cache_summary_query(settings)

    assert (
        "`pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`" in query
    )
    assert "candidate_cas_object_count" in query
    assert "candidate_ac_object_count" in query
    assert "@ac_cutoff_days" in query
    assert "WITH cold_ac AS" in query
    assert "0 AS candidate_cas_object_count" in query


def test_cleanup_gcs_cache_manifest_export_query_uses_generation() -> None:
    query = build_cleanup_gcs_cache_manifest_export_query(
        candidate_table="`project.dataset.table`",
        manifest_uri="gs://manifest-bucket/prefix/manifest-*.csv",
        bucket_name="pingcap-ci-bazel-remote-cache-us-central1",
    )

    assert "'pingcap-ci-bazel-remote-cache-us-central1' AS bucket" in query
    assert "object_name AS name" in query
    assert "generation" in query


def test_cleanup_gcs_cache_reconcile_deleted_cas_query_matches_object_name_and_last_seen_at() -> (
    None
):
    query = build_cleanup_gcs_cache_reconcile_deleted_cas_query(
        GcsCacheSettings(project_id="pingcap-testing-account"),
        candidate_table="`project.dataset.candidates`",
    )

    assert (
        "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`"
        in query
    )
    assert "STRUCT(object_name, last_seen_at)" in query
    assert "FROM `project.dataset.candidates`" in query


def test_cleanup_gcs_cache_final_ac_delete_table_excludes_missing_metadata() -> None:
    query = build_cleanup_gcs_cache_final_ac_delete_table_query(
        ac_live_metadata_table="`project.dataset.ac_live`",
        ac_missing_metadata_table="`project.dataset.ac_missing`",
        candidate_table="`project.dataset.delete_ac`",
        ttl_days=7,
    )

    assert "FROM `project.dataset.ac_live` AS live" in query
    assert "WHERE NOT EXISTS" in query
    assert "FROM `project.dataset.ac_missing` AS missing" in query
    assert "missing.object_name = live.object_name" in query


def test_cleanup_gcs_cache_metadata_stage_tables_query_separates_ddl_statements() -> None:
    query = build_cleanup_gcs_cache_metadata_stage_tables_query(
        ttl_days=7,
        ac_live_metadata_table="`project.dataset.ac_live`",
        ac_missing_metadata_table="`project.dataset.ac_missing`",
        cas_live_metadata_table="`project.dataset.cas_live`",
        cas_missing_metadata_table="`project.dataset.cas_missing`",
    )

    assert query.count("CREATE OR REPLACE TABLE") == 4
    assert query.count(";\n\nCREATE OR REPLACE TABLE") == 3
    assert "INTERVAL 7 DAY" in query


def test_cleanup_gcs_cache_run_references_table_query_uses_nullable_load_schema() -> None:
    query = build_cleanup_gcs_cache_run_references_table_query(
        run_references_table="`project.dataset.run_refs`",
        ttl_days=7,
    )

    assert "ac_object_name STRING," in query
    assert "cas_object_name STRING" in query
    assert "NOT NULL" not in query


def test_cleanup_gcs_cache_cas_queries_avoid_current_reserved_alias() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    queries = (
        build_cleanup_gcs_cache_cas_candidate_table_query(
            settings,
            run_references_table="`project.dataset.run_refs`",
            candidate_table="`project.dataset.candidate_cas`",
            ttl_days=7,
        ),
        build_cleanup_gcs_cache_final_cas_delete_table_query(
            settings,
            source_table="`project.dataset.candidate_cas`",
            live_metadata_table="`project.dataset.cas_live`",
            candidate_table="`project.dataset.delete_cas`",
            ttl_days=7,
        ),
    )

    for query in queries:
        assert " AS current\n" not in query
        assert " AS current_obj\n" in query


def test_cleanup_gcs_cache_final_cas_delete_ignores_cleanup_metadata_gets() -> None:
    query = build_cleanup_gcs_cache_final_cas_delete_table_query(
        GcsCacheSettings(
            project_id="pingcap-testing-account",
            last_seen_excluded_get_principal_email=(
                "ci-dashboard@pingcap-testing-account.iam.gserviceaccount.com"
            ),
        ),
        source_table="`project.dataset.candidate_cas`",
        live_metadata_table="`project.dataset.cas_live`",
        candidate_table="`project.dataset.delete_cas`",
        ttl_days=7,
    )

    assert (
        "FROM `pingcap-testing-account.ci_bazel_cache_logs.cloudaudit_googleapis_com_data_access` AS audit"
        in query
    )
    assert "audit.protopayload_auditlog.methodName IN" in query
    assert "audit.timestamp > source.last_seen_at" in query
    assert "AND NOT (" in query
    assert "audit.protopayload_auditlog.methodName = 'storage.objects.get'" in query
    assert (
        "audit.protopayload_auditlog.authenticationInfo.principalEmail = "
        "'ci-dashboard@pingcap-testing-account.iam.gserviceaccount.com'"
    ) in query


def test_cleanup_gcs_cache_final_cas_delete_keeps_all_audit_events_without_exclusion() -> None:
    query = build_cleanup_gcs_cache_final_cas_delete_table_query(
        GcsCacheSettings(project_id="pingcap-testing-account"),
        source_table="`project.dataset.candidate_cas`",
        live_metadata_table="`project.dataset.cas_live`",
        candidate_table="`project.dataset.delete_cas`",
        ttl_days=7,
    )

    assert "authenticationInfo.principalEmail" not in query


def test_populate_ac_stage_tables_skips_parse_failed_ac_from_delete_inputs() -> None:
    loaded_files = []

    def fake_stream_rows(query, parameters):
        del query, parameters
        yield {"object_name": "ac/ok", "last_seen_at": datetime(2026, 5, 1, tzinfo=UTC)}
        yield {"object_name": "ac/corrupt", "last_seen_at": datetime(2026, 5, 2, tzinfo=UTC)}
        yield {"object_name": "ac/missing", "last_seen_at": datetime(2026, 5, 3, tzinfo=UTC)}

    def fake_resolve_object_metadata(**kwargs):
        assert tuple(kwargs["object_names"]) == ("ac/ok", "ac/corrupt", "ac/missing")
        return (
            GcsObjectMetadata("ac/ok", True, 101),
            GcsObjectMetadata("ac/corrupt", True, 102),
            GcsObjectMetadata("ac/missing", False, None),
        )

    def fake_extract_references(**kwargs):
        assert tuple(kwargs["ac_object_names"]) == ("ac/ok", "ac/corrupt")
        return (
            AcReferenceExtraction(
                ac_object_name="ac/ok",
                exists=True,
                cas_object_names=("cas/one",),
            ),
            AcReferenceExtraction(
                ac_object_name="ac/corrupt",
                exists=True,
                cas_object_names=(),
                parse_error="Unsupported protobuf wire type: 3",
            ),
        )

    def fake_load_jsonl_file(table_ref, jsonl_path, schema, write_disposition):
        with jsonl_path.open(encoding="utf-8") as handle:
            payload = tuple(json.loads(line) for line in handle if line.strip())
        loaded_files.append((table_ref, payload, tuple(schema), write_disposition))

    result = _populate_ac_stage_tables(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_object_metadata,
        extract_references=fake_extract_references,
        source_table="`project.dataset.candidate_ac`",
        live_table="`project.dataset.ac_live`",
        missing_table="`project.dataset.ac_missing`",
        references_table="`project.dataset.run_refs`",
        load_jsonl_file=fake_load_jsonl_file,
        stream_batch_size=100,
        reference_batch_size=100,
    )

    assert result.parse_error_count == 1
    assert result.seeded_object_count == 3
    assert result.last_seed_cursor is not None
    assert result.last_seed_cursor.object_name == "ac/missing"
    assert result.last_seed_cursor.last_seen_at == datetime(2026, 5, 3, tzinfo=UTC)
    assert result.sample_parse_errors[0].object_name == "ac/corrupt"
    assert result.sample_parse_errors[0].parse_error == "Unsupported protobuf wire type: 3"
    assert (
        "`project.dataset.ac_live`",
        ({"object_name": "ac/ok", "generation": 101, "size_bytes": None},),
        (("object_name", "STRING"), ("generation", "INT64"), ("size_bytes", "INT64")),
        "WRITE_APPEND",
    ) in loaded_files
    assert (
        "`project.dataset.run_refs`",
        ({"ac_object_name": "ac/ok", "cas_object_name": "cas/one"},),
        (("ac_object_name", "STRING"), ("cas_object_name", "STRING")),
        "WRITE_APPEND",
    ) in loaded_files
    assert (
        "`project.dataset.ac_missing`",
        ({"object_name": "ac/missing"},),
        (("object_name", "STRING"),),
        "WRITE_APPEND",
    ) in loaded_files


def test_run_cleanup_gcs_cache_returns_dry_run_summary_with_samples() -> None:
    captured = {}

    def fake_execute(query, parameters):
        captured.setdefault("queries", []).append(
            (query, {param.name: param.value for param in parameters})
        )
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 0,
                        "candidate_ac_object_count": 5,
                        "candidate_cas_delete_object_count": 0,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
                        "sample_candidates": [
                            {
                                "object_name": "ac/foo",
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
        ac_retention_days=10,
        cas_retention_days=15,
        safety_buffer_days=1,
        sample_limit=1,
        execute=fake_execute,
        now=lambda: now,
        run_id_factory=lambda: "run-dry-001",
    )

    assert summary.run_id == "run-dry-001"
    assert summary.execute_kind == "cas"
    assert summary.ac_retention_days == 10
    assert summary.cas_retention_days == 15
    assert summary.candidate_cas_object_count == 0
    assert summary.candidate_ac_object_count == 5
    assert summary.candidate_cas_delete_object_count == 0
    assert summary.ac_parse_error_count == 0
    assert summary.selected_ac_object_count == 0
    assert summary.selected_cas_object_count == 0
    assert len(summary.sample_candidates) == 1
    assert summary.sample_candidates[0].object_name == "ac/foo"
    assert summary.bytes_processed == 10
    assert summary.run_started_at == now
    assert summary.run_finished_at == now


def test_run_cleanup_gcs_cache_delete_cascades_ac_then_cas() -> None:
    captured_queries = []
    created_jobs = []
    waited_jobs = []
    loaded_files = []
    table_schemas = {}

    def fake_execute(query, parameters):
        rendered = {param.name: param.value for param in parameters}
        captured_queries.append((query, rendered))
        _record_explicit_create_table_schemas(query, table_schemas)
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 0,
                        "candidate_ac_object_count": 3,
                        "candidate_cas_delete_object_count": 0,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 8, 0, 0, tzinfo=UTC),
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=2,
            )
        if "SELECT COUNT(DISTINCT cas_object_name) AS object_count FROM" in query:
            return BigQueryQueryResult(rows=({"object_count": 3},), total_bytes_processed=1)
        if "SELECT COUNT(*) AS object_count FROM" in query:
            if "delete_ac" in query:
                return BigQueryQueryResult(rows=({"object_count": 1},), total_bytes_processed=1)
            if "delete_cas" in query:
                return BigQueryQueryResult(rows=({"object_count": 2},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_ac_"
            in query
        ):
            yield {"object_name": "ac/one", "last_seen_at": datetime(2026, 5, 1, tzinfo=UTC)}
            yield {
                "object_name": "ac/missing",
                "last_seen_at": datetime(2026, 5, 2, tzinfo=UTC),
            }
            return
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_cas_"
            in query
        ):
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

    def fake_extract_references(**kwargs):
        names = tuple(kwargs["ac_object_names"])
        mapping = {
            "ac/one": AcReferenceExtraction(
                ac_object_name="ac/one",
                exists=True,
                cas_object_names=("cas/one", "cas/two", "cas/missing"),
            ),
            "ac/missing": AcReferenceExtraction(
                ac_object_name="ac/missing",
                exists=False,
                cas_object_names=(),
            ),
        }
        return tuple(mapping[name] for name in names)

    def fake_load_jsonl_file(table_ref, jsonl_path, schema, write_disposition):
        _assert_load_schema_matches_created_table(table_schemas, table_ref, schema)
        with jsonl_path.open(encoding="utf-8") as handle:
            payload = tuple(json.loads(line) for line in handle if line.strip())
        loaded_files.append((table_ref, payload, tuple(schema), write_disposition))

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
        ac_retention_days=10,
        cas_retention_days=15,
        safety_buffer_days=1,
        max_delete_objects=10000000,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_object_metadata,
        extract_references=fake_extract_references,
        load_jsonl_file=fake_load_jsonl_file,
        create_batch_job=fake_create_batch_job,
        wait_for_batch_job=fake_wait_for_batch_job,
        now=lambda: run_started_at,
        run_id_factory=lambda: "steady-run-001",
    )

    assert summary.candidate_cas_object_count == 3
    assert summary.candidate_ac_object_count == 3
    assert summary.candidate_cas_delete_object_count == 2
    assert summary.ac_parse_error_count == 0
    assert summary.selected_ac_object_count == 1
    assert summary.selected_cas_object_count == 2
    assert summary.ac_manifest_uri is not None
    assert summary.cas_manifest_uri is not None
    assert len(created_jobs) == 2
    assert created_jobs[0]["manifest_uri"] == summary.ac_manifest_uri
    assert created_jobs[1]["manifest_uri"] == summary.cas_manifest_uri
    assert len(waited_jobs) == 2
    assert any("manifest-*.csv" in query for query, _ in captured_queries)
    assert all(disposition == "WRITE_APPEND" for _, _, _, disposition in loaded_files)
    assert any(
        "run_ac_cas_refs" in table_ref
        and ("ac_object_name", "STRING") in schema
        and ("cas_object_name", "STRING") in schema
        for table_ref, _rows, schema, _disposition in loaded_files
    )
    assert {
        table_ref.rsplit(".", 1)[-1].rstrip("`")
        for table_ref, _rows, _schema, _disposition in loaded_files
    } == {
        "_tmp_gcs_cache_ac_live_metadata_b0001_steadyrun001",
        "_tmp_gcs_cache_ac_missing_metadata_b0001_steadyrun001",
        "_tmp_gcs_cache_run_ac_cas_refs_b0001_steadyrun001",
        "_tmp_gcs_cache_cas_live_metadata_b0001_steadyrun001",
        "_tmp_gcs_cache_cas_missing_metadata_b0001_steadyrun001",
    }
    assert any(
        "cas_from_deleted_ac" in query and "LIMIT @limit" not in query and "limit" not in params
        for query, params in captured_queries
    )
    deleted_ac_reconcile_index = next(
        index
        for index, (query, _params) in enumerate(captured_queries)
        if "_tmp_gcs_cache_delete_ac_" in query
        and "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`"
        in query
    )
    cas_candidate_index = next(
        index
        for index, (query, _params) in enumerate(captured_queries)
        if "cas_from_deleted_ac" in query
    )
    assert deleted_ac_reconcile_index < cas_candidate_index


def test_run_cleanup_gcs_cache_delete_cascades_cas_after_each_ac_batch() -> None:
    captured_queries = []
    created_jobs = []
    waited_jobs = []

    def fake_execute(query, parameters):
        rendered = {param.name: param.value for param in parameters}
        captured_queries.append((query, rendered))
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 0,
                        "candidate_ac_object_count": 2,
                        "candidate_cas_delete_object_count": 0,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=2,
            )
        if "SELECT COUNT(DISTINCT cas_object_name) AS object_count FROM" in query:
            return BigQueryQueryResult(rows=({"object_count": 1},), total_bytes_processed=1)
        if "SELECT COUNT(*) AS object_count FROM" in query:
            return BigQueryQueryResult(rows=({"object_count": 1},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        del parameters
        if "_tmp_gcs_cache_candidate_ac_b0001_" in query:
            yield {"object_name": "ac/one", "last_seen_at": datetime(2026, 5, 1, tzinfo=UTC)}
            return
        if "_tmp_gcs_cache_candidate_ac_b0002_" in query:
            yield {"object_name": "ac/two", "last_seen_at": datetime(2026, 5, 2, tzinfo=UTC)}
            return
        if "_tmp_gcs_cache_candidate_cas_b0001_" in query:
            yield {"object_name": "cas/one"}
            return
        if "_tmp_gcs_cache_candidate_cas_b0002_" in query:
            yield {"object_name": "cas/two"}
            return
        raise AssertionError(f"Unexpected stream query: {query}")

    def fake_resolve_object_metadata(**kwargs):
        names = tuple(kwargs["object_names"])
        generations = {
            "ac/one": 101,
            "ac/two": 102,
            "cas/one": 201,
            "cas/two": 202,
        }
        return tuple(GcsObjectMetadata(name, True, generations[name]) for name in names)

    def fake_extract_references(**kwargs):
        names = tuple(kwargs["ac_object_names"])
        refs = {
            "ac/one": ("cas/one",),
            "ac/two": ("cas/two",),
        }
        return tuple(
            AcReferenceExtraction(
                ac_object_name=name,
                exists=True,
                cas_object_names=refs[name],
            )
            for name in names
        )

    def fake_load_jsonl_file(table_ref, jsonl_path, schema, write_disposition):
        del table_ref, schema, write_disposition
        assert jsonl_path.exists()

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

    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(
            project_id="pingcap-testing-account",
            cleanup_ac_delete_batch_size=1,
        ),
        mode="delete",
        execute_kind="cas",
        ac_retention_days=10,
        cas_retention_days=15,
        safety_buffer_days=1,
        max_delete_objects=2,
        max_delete_cas_objects=1,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_object_metadata,
        extract_references=fake_extract_references,
        load_jsonl_file=fake_load_jsonl_file,
        create_batch_job=fake_create_batch_job,
        wait_for_batch_job=fake_wait_for_batch_job,
        now=lambda: datetime(2026, 6, 15, 10, 30, tzinfo=UTC),
        run_id_factory=lambda: "steady-run-batched",
    )

    assert summary.selected_ac_object_count == 2
    assert summary.selected_cas_object_count == 2
    assert summary.candidate_cas_object_count == 2
    assert summary.candidate_cas_delete_object_count == 2
    assert [job["job_id"] for job in created_jobs] == [
        "gcs-cache-cleanup-ac-20260615t103000-steadyrunbat-b0001",
        "gcs-cache-cleanup-cas-20260615t103000-steadyrunbat-b0001",
        "gcs-cache-cleanup-ac-20260615t103000-steadyrunbat-b0002",
        "gcs-cache-cleanup-cas-20260615t103000-steadyrunbat-b0002",
    ]
    assert all("/batch-000" in job["manifest_uri"] for job in created_jobs)
    assert summary.ac_manifest_uris == (
        created_jobs[0]["manifest_uri"],
        created_jobs[2]["manifest_uri"],
    )
    assert summary.cas_manifest_uris == (
        created_jobs[1]["manifest_uri"],
        created_jobs[3]["manifest_uri"],
    )
    assert summary.ac_batch_job_names == (
        "projects/pingcap-testing-account/locations/global/jobs/"
        "gcs-cache-cleanup-ac-20260615t103000-steadyrunbat-b0001",
        "projects/pingcap-testing-account/locations/global/jobs/"
        "gcs-cache-cleanup-ac-20260615t103000-steadyrunbat-b0002",
    )
    assert summary.cas_batch_job_names == (
        "projects/pingcap-testing-account/locations/global/jobs/"
        "gcs-cache-cleanup-cas-20260615t103000-steadyrunbat-b0001",
        "projects/pingcap-testing-account/locations/global/jobs/"
        "gcs-cache-cleanup-cas-20260615t103000-steadyrunbat-b0002",
    )
    assert summary.ac_manifest_uri == created_jobs[2]["manifest_uri"]
    assert summary.cas_manifest_uri == created_jobs[3]["manifest_uri"]
    assert len(waited_jobs) == 4
    first_cas_candidate_index = next(
        index
        for index, (query, _params) in enumerate(captured_queries)
        if "_tmp_gcs_cache_candidate_cas_b0001_" in query
    )
    second_ac_seed_index = next(
        index
        for index, (query, _params) in enumerate(captured_queries)
        if "_tmp_gcs_cache_candidate_ac_b0002_" in query
    )
    assert first_cas_candidate_index < second_ac_seed_index
    assert all(
        "LIMIT @limit" not in query and "limit" not in params
        for query, params in captured_queries
        if "cas_from_deleted_ac" in query
    )


def test_run_cleanup_gcs_cache_delete_refills_live_ac_target_after_stale_rows() -> None:
    captured_queries = []
    created_jobs = []
    waited_jobs = []
    loaded_files = []
    table_schemas = {}
    delete_ac_counts = iter((1, 3))
    ac_stream_call_count = 0

    def fake_execute(query, parameters):
        rendered = {param.name: param.value for param in parameters}
        captured_queries.append((query, rendered))
        _record_explicit_create_table_schemas(query, table_schemas)
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 0,
                        "candidate_ac_object_count": 6,
                        "candidate_cas_delete_object_count": 0,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 6, 0, 0, tzinfo=UTC),
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=2,
            )
        if "SELECT COUNT(DISTINCT cas_object_name) AS object_count FROM" in query:
            return BigQueryQueryResult(rows=({"object_count": 0},), total_bytes_processed=1)
        if "SELECT COUNT(*) AS object_count FROM" in query:
            if "delete_ac" in query:
                return BigQueryQueryResult(
                    rows=({"object_count": next(delete_ac_counts)},), total_bytes_processed=1
                )
            if "delete_cas" in query:
                return BigQueryQueryResult(rows=({"object_count": 0},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        nonlocal ac_stream_call_count
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_ac_"
            in query
        ):
            ac_stream_call_count += 1
            if ac_stream_call_count == 1:
                yield {
                    "object_name": "ac/stale-1",
                    "last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                }
                yield {
                    "object_name": "ac/live-1",
                    "last_seen_at": datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
                }
                yield {
                    "object_name": "ac/stale-2",
                    "last_seen_at": datetime(2026, 5, 3, 0, 0, tzinfo=UTC),
                }
                return
            if ac_stream_call_count == 2:
                yield {
                    "object_name": "ac/live-2",
                    "last_seen_at": datetime(2026, 5, 4, 0, 0, tzinfo=UTC),
                }
                yield {
                    "object_name": "ac/live-3",
                    "last_seen_at": datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
                }
                return
            return
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_cas_"
            in query
        ):
            return
        raise AssertionError(f"Unexpected stream query: {query}")

    def fake_resolve_object_metadata(**kwargs):
        names = tuple(kwargs["object_names"])
        mapping = {
            "ac/stale-1": GcsObjectMetadata("ac/stale-1", False, None),
            "ac/live-1": GcsObjectMetadata("ac/live-1", True, 101),
            "ac/stale-2": GcsObjectMetadata("ac/stale-2", False, None),
            "ac/live-2": GcsObjectMetadata("ac/live-2", True, 102),
            "ac/live-3": GcsObjectMetadata("ac/live-3", True, 103),
        }
        return tuple(mapping[name] for name in names)

    def fake_extract_references(**kwargs):
        names = tuple(kwargs["ac_object_names"])
        mapping = {
            "ac/live-1": AcReferenceExtraction(
                ac_object_name="ac/live-1",
                exists=True,
                cas_object_names=(),
            ),
            "ac/live-2": AcReferenceExtraction(
                ac_object_name="ac/live-2",
                exists=True,
                cas_object_names=(),
            ),
            "ac/live-3": AcReferenceExtraction(
                ac_object_name="ac/live-3",
                exists=True,
                cas_object_names=(),
            ),
        }
        return tuple(mapping[name] for name in names)

    def fake_load_jsonl_file(table_ref, jsonl_path, schema, write_disposition):
        _assert_load_schema_matches_created_table(table_schemas, table_ref, schema)
        with jsonl_path.open(encoding="utf-8") as handle:
            payload = tuple(json.loads(line) for line in handle if line.strip())
        loaded_files.append((table_ref, payload, tuple(schema), write_disposition))

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

    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="cas",
        ac_retention_days=10,
        cas_retention_days=15,
        safety_buffer_days=1,
        max_delete_objects=3,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_object_metadata,
        extract_references=fake_extract_references,
        load_jsonl_file=fake_load_jsonl_file,
        create_batch_job=fake_create_batch_job,
        wait_for_batch_job=fake_wait_for_batch_job,
        now=lambda: datetime(2026, 6, 15, 10, 30, tzinfo=UTC),
        run_id_factory=lambda: "steady-run-refill",
    )

    assert summary.selected_ac_object_count == 3
    assert summary.selected_cas_object_count == 0
    assert ac_stream_call_count == 2
    assert len(created_jobs) == 1
    assert len(waited_jobs) == 1
    ac_seed_queries = [
        params
        for query, params in captured_queries
        if "_tmp_gcs_cache_candidate_ac_b0001_steadyrunrefill" in query and "LIMIT @limit" in query
    ]
    assert len(ac_seed_queries) == 2
    assert ac_seed_queries[0]["limit"] == 3
    assert ac_seed_queries[1]["limit"] == 2
    assert ac_seed_queries[1]["cursor_object_name"] == "ac/stale-2"
    assert ac_seed_queries[1]["cursor_last_seen_at"] == datetime(2026, 5, 3, 0, 0, tzinfo=UTC)
    reconcile_queries = [
        query
        for query, _params in captured_queries
        if "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_object_last_seen_current`"
        in query
        and "object_kind = 'ac'" in query
        and "_tmp_gcs_cache_ac_missing_metadata_b0001_steadyrunrefill" in query
    ]
    assert len(reconcile_queries) == 2


def test_run_cleanup_gcs_cache_delete_stops_after_repeated_zero_progress_refills() -> None:
    captured_queries = []
    loaded_files = []
    ac_stream_call_count = 0

    def fake_execute(query, parameters):
        rendered = {param.name: param.value for param in parameters}
        captured_queries.append((query, rendered))
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 0,
                        "candidate_ac_object_count": 50,
                        "candidate_cas_delete_object_count": 0,
                        "oldest_last_seen_at": datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                        "newest_last_seen_at": datetime(2026, 5, 20, 0, 0, tzinfo=UTC),
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=2,
            )
        if "SELECT COUNT(*) AS object_count FROM" in query and "delete_ac" in query:
            return BigQueryQueryResult(rows=({"object_count": 0},), total_bytes_processed=1)
        if "SELECT COUNT(DISTINCT cas_object_name) AS object_count FROM" in query:
            return BigQueryQueryResult(rows=({"object_count": 0},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        nonlocal ac_stream_call_count
        del parameters
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_ac_"
            in query
        ):
            ac_stream_call_count += 1
            if ac_stream_call_count > 3:
                raise AssertionError("zero-progress guard should stop after three refill rounds")
            for index in range(5):
                yield {
                    "object_name": f"ac/stale-{ac_stream_call_count}-{index}",
                    "last_seen_at": datetime(2026, 5, ac_stream_call_count, index, tzinfo=UTC),
                }
            return
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_cas_"
            in query
        ):
            raise AssertionError("CAS stage should not run without live AC")
        raise AssertionError(f"Unexpected stream query: {query}")

    def fake_resolve_object_metadata(**kwargs):
        return tuple(GcsObjectMetadata(str(name), False, None) for name in kwargs["object_names"])

    def fake_load_jsonl_file(table_ref, jsonl_path, schema, write_disposition):
        with jsonl_path.open(encoding="utf-8") as handle:
            payload = tuple(json.loads(line) for line in handle if line.strip())
        loaded_files.append((table_ref, payload, tuple(schema), write_disposition))

    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="cas",
        ac_retention_days=10,
        cas_retention_days=15,
        safety_buffer_days=1,
        max_delete_objects=5,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_object_metadata,
        extract_references=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"should not extract references: {kwargs}")
        ),
        load_jsonl_file=fake_load_jsonl_file,
        create_batch_job=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"should not create batch job: {kwargs}")
        ),
        wait_for_batch_job=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"should not wait for batch job: {kwargs}")
        ),
        now=lambda: datetime(2026, 6, 15, 10, 30, tzinfo=UTC),
        run_id_factory=lambda: "steady-run-zero-progress",
    )

    assert summary.selected_ac_object_count == 0
    assert summary.selected_cas_object_count == 0
    assert summary.ac_manifest_uri is None
    assert summary.ac_manifest_uris is None
    assert ac_stream_call_count == 3
    assert len(loaded_files) == 3
    ac_seed_queries = [
        params
        for query, params in captured_queries
        if "_tmp_gcs_cache_candidate_ac_b0001_steadyrunzeroprogress" in query
        and "LIMIT @limit" in query
    ]
    assert len(ac_seed_queries) == 3


def test_run_cleanup_gcs_cache_delete_exits_when_seed_batch_is_empty() -> None:
    captured_queries = []

    def fake_execute(query, parameters):
        rendered = {param.name: param.value for param in parameters}
        captured_queries.append((query, rendered))
        if "candidate_cas_object_count" in query:
            return BigQueryQueryResult(
                rows=(
                    {
                        "candidate_cas_object_count": 0,
                        "candidate_ac_object_count": 0,
                        "candidate_cas_delete_object_count": 0,
                        "oldest_last_seen_at": None,
                        "newest_last_seen_at": None,
                        "sample_candidates": [],
                    },
                ),
                total_bytes_processed=2,
            )
        if "SELECT COUNT(DISTINCT cas_object_name) AS object_count FROM" in query:
            return BigQueryQueryResult(rows=({"object_count": 0},), total_bytes_processed=1)
        if "SELECT COUNT(*) AS object_count FROM" in query:
            return BigQueryQueryResult(rows=({"object_count": 0},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        del parameters
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_ac_"
            in query
        ):
            return iter(())
        if (
            "FROM `pingcap-testing-account.ci_bazel_cache_logs._tmp_gcs_cache_candidate_cas_"
            in query
        ):
            return iter(())
        raise AssertionError(f"Unexpected stream query: {query}")

    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="cas",
        ac_retention_days=10,
        cas_retention_days=15,
        safety_buffer_days=1,
        max_delete_objects=3,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        create_batch_job=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"should not create batch job: {kwargs}")
        ),
        wait_for_batch_job=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"should not wait for batch job: {kwargs}")
        ),
        now=lambda: datetime(2026, 6, 15, 10, 30, tzinfo=UTC),
        run_id_factory=lambda: "steady-run-empty",
    )

    assert summary.selected_ac_object_count == 0
    assert summary.selected_cas_object_count == 0
    assert summary.ac_manifest_uri is None
    assert summary.cas_manifest_uri is None
    assert not any(
        "_tmp_gcs_cache_ac_missing_metadata_steadyrunempty" in query
        and "object_kind = 'ac'" in query
        for query, _params in captured_queries
    )


def test_run_cleanup_gcs_cache_delete_requires_execute_kind_cas() -> None:
    with pytest.raises(ValueError, match="requires --execute-kind cas"):
        run_cleanup_gcs_cache(
            settings=GcsCacheSettings(project_id="pingcap-testing-account"),
            mode="delete",
            execute_kind="all",
        )


def _record_explicit_create_table_schemas(
    query: str,
    table_schemas: dict[str, dict[str, tuple[str, str]]],
) -> None:
    for match in re.finditer(
        r"CREATE OR REPLACE TABLE\s+(`[^`]+`)\s*\((.*?)\)\s*OPTIONS",
        query,
        flags=re.DOTALL,
    ):
        table_ref = match.group(1)
        columns: dict[str, tuple[str, str]] = {}
        for raw_line in match.group(2).splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            mode = "REQUIRED" if "NOT NULL" in line.upper() else "NULLABLE"
            columns[parts[0]] = (parts[1], mode)
        table_schemas[table_ref] = columns


def _assert_load_schema_matches_created_table(
    table_schemas: dict[str, dict[str, tuple[str, str]]],
    table_ref: str,
    load_schema: tuple[tuple[str, str], ...],
) -> None:
    table_schema = table_schemas.get(table_ref)
    assert table_schema is not None, f"load target table was not created first: {table_ref}"
    for name, field_type in load_schema:
        assert name in table_schema
        created_type, created_mode = table_schema[name]
        assert created_type == field_type
        # load_table_from_json uses NULLABLE fields when mode is not explicitly set.
        assert created_mode == "NULLABLE"


def test_run_cleanup_gcs_cache_from_index_dry_run_returns_summary_with_candidates(monkeypatch) -> None:
    """cas-from-index dry-run: runs full analysis pipeline without deleting."""
    from cost_insight.jobs import cleanup_gcs_cache, sync_gcs_cache_ac_references

    # Mock the catch-up sync to avoid real BigQuery calls
    monkeypatch.setattr(
        cleanup_gcs_cache,
        "run_sync_gcs_cache_ac_references",
        lambda **kwargs: sync_gcs_cache_ac_references.SyncGcsCacheAcReferencesSummary(
            account_id="test",
            bucket_name="test",
            mode="incremental",
            shard_start=0,
            shard_end=255,
            source_object_count=0,
            missing_object_count=0,
            parse_error_count=0,
            replaced_ac_object_count=0,
            reference_row_count=0,
            sample_parse_errors=(),
            dry_run=False,
            indexed_through=datetime(2026, 7, 1, 9, 59, tzinfo=UTC),
            bytes_processed=0,
            run_started_at=datetime(2026, 7, 1, 9, 59, tzinfo=UTC),
            run_finished_at=datetime(2026, 7, 1, 9, 59, tzinfo=UTC),
        ),
    )

    captured_queries = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {p.name: p.value for p in parameters}))
        if "ready_shards" in query:
            return BigQueryQueryResult(rows=({"ready_shards": 256},), total_bytes_processed=1)
        if "fresh_shards" in query:
            return BigQueryQueryResult(rows=({"fresh_shards": 256},), total_bytes_processed=1)
        if "COUNT(*)" in query or "object_count" in query:
            return BigQueryQueryResult(rows=({"object_count": 100},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        if "cas_preselect" in query:
            yield {"object_name": "cas/deadbeef"}
        return

    def fake_resolve_metadata(**kwargs):
        return (
            GcsObjectMetadata(object_name="cas/deadbeef", exists=True, generation=1, size_bytes=1024),
        )

    def fake_load_jsonl(*args, **kwargs):
        pass

    now = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="dry-run",
        execute_kind="cas-from-index",
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_metadata,
        load_jsonl_file=fake_load_jsonl,
        now=lambda: now,
        run_id_factory=lambda: "test-run",
    )

    assert summary.execute_kind == "cas-from-index"
    assert summary.dry_run is True
    assert summary.candidate_cas_object_count >= 0
    assert summary.candidate_ac_object_count >= 0
    assert summary.candidate_cas_delete_object_count >= 0


def test_run_cleanup_gcs_cache_from_index_delete_cascades_ac_then_cas(monkeypatch) -> None:
    """cas-from-index delete: AC delete → second catch-up → by_cas rebuild → CAS delete."""
    from cost_insight.jobs import cleanup_gcs_cache, sync_gcs_cache_ac_references

    # Mock catch-up sync
    monkeypatch.setattr(
        cleanup_gcs_cache,
        "run_sync_gcs_cache_ac_references",
        lambda **kwargs: sync_gcs_cache_ac_references.SyncGcsCacheAcReferencesSummary(
            account_id="test", bucket_name="test", mode="incremental",
            shard_start=0, shard_end=255, source_object_count=0,
            missing_object_count=0, parse_error_count=0,
            replaced_ac_object_count=0, reference_row_count=0,
            sample_parse_errors=(), dry_run=False,
            indexed_through=datetime(2026, 7, 1, 9, 59, tzinfo=UTC),
            bytes_processed=0,
            run_started_at=datetime(2026, 7, 1, 9, 59, tzinfo=UTC),
            run_finished_at=datetime(2026, 7, 1, 9, 59, tzinfo=UTC),
        ),
    )

    captured_queries = []
    step_order: list[str] = []

    def fake_execute(query, parameters):
        captured_queries.append(query)
        if "ready_shards" in query:
            return BigQueryQueryResult(rows=({"ready_shards": 256},), total_bytes_processed=1)
        if "fresh_shards" in query:
            return BigQueryQueryResult(rows=({"fresh_shards": 256},), total_bytes_processed=1)
        if "COUNT(*)" in query or "object_count" in query:
            return BigQueryQueryResult(rows=({"object_count": 3},), total_bytes_processed=1)
        # Track operation order
        if "CREATE OR REPLACE TABLE" in query:
            if "by_cas" in query and "by_cas_dryrun" not in query:
                step_order.append("rebuild_by_cas")
            elif "ac_to_delete" in query:
                step_order.append("ac_reverse_lookup")
            elif "cas_zero_ref" in query:
                step_order.append("zero_ref_cas")
            elif "delete_ac" in query:
                step_order.append("ac_delete_manifest")
            elif "delete_cas" in query:
                step_order.append("cas_delete_manifest")
            elif "cas_preselect" in query:
                step_order.append("cas_preselect")
            elif "cold_cas" in query:
                step_order.append("cold_cas_ranked")
        if "EXPORT DATA" in query:
            if "ac/" in query or "delete_ac" in query:
                step_order.append("ac_manifest_export")
            elif "cas/" in query or "delete_cas" in query:
                step_order.append("cas_manifest_export")
        if "UPDATE `pingcap" in query:
            step_order.append("update_index_state")
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        if "cas_preselect" in query:
            yield {"object_name": "cas/aaa"}
            yield {"object_name": "cas/bbb"}
            yield {"object_name": "cas/ccc"}
        elif "ac_to_delete" in query:
            yield {"object_name": "ac/xxx", "last_seen_at": datetime(2026, 1, 1, tzinfo=UTC)}
        elif "ac_live_metadata" in query or "cas_live_metadata" in query:
            yield {"object_name": "dummy"}
        elif "stale" in query:
            return
        return

    def fake_resolve_metadata(**kwargs):
        return (
            GcsObjectMetadata(object_name="dummy", exists=True, generation=1, size_bytes=1024),
        )

    def fake_load_jsonl(*args, **kwargs):
        pass

    def fake_create_batch_job(**kwargs):
        from cost_insight.common.storage_batch_operations import StorageBatchOperationsJob
        return StorageBatchOperationsJob(job_name=kwargs["job_id"], operation_name=None)

    def fake_wait_for_batch_job(**kwargs):
        from cost_insight.common.storage_batch_operations import StorageBatchOperationsJobStatus
        return StorageBatchOperationsJobStatus(
            job_name=kwargs["job_name"],
            state="SUCCEEDED", failed_object_count=0,
            total_object_count=3, succeeded_object_count=3,
            total_bytes_transformed=0, complete_time=None,
        )

    now = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    summary = run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="delete",
        execute_kind="cas-from-index",
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_metadata,
        load_jsonl_file=fake_load_jsonl,
        create_batch_job=fake_create_batch_job,
        wait_for_batch_job=fake_wait_for_batch_job,
        now=lambda: now,
        run_id_factory=lambda: "test-delete-run",
    )

    assert summary.execute_kind == "cas-from-index"
    assert summary.dry_run is False
    assert summary.selected_ac_object_count > 0
    assert summary.selected_cas_object_count > 0
    # Verify operation order: AC before CAS
    assert step_order.index("ac_manifest_export") < step_order.index("cas_manifest_export")
    # Second by_cas rebuild before zero-ref
    rebuild_positions = [i for i, s in enumerate(step_order) if s == "rebuild_by_cas"]
    zero_ref_pos = step_order.index("zero_ref_cas")
    assert len(rebuild_positions) >= 2
    assert rebuild_positions[-1] < zero_ref_pos


def test_cas_from_index_respects_max_delete_objects_canary(monkeypatch) -> None:
    """cas-from-index --max-delete-objects 500 limits AC deletion to 500."""
    from cost_insight.jobs import cleanup_gcs_cache, sync_gcs_cache_ac_references

    monkeypatch.setattr(
        cleanup_gcs_cache,
        "run_sync_gcs_cache_ac_references",
        lambda **kwargs: sync_gcs_cache_ac_references.SyncGcsCacheAcReferencesSummary(
            account_id="test", bucket_name="test", mode="incremental",
            shard_start=0, shard_end=255, source_object_count=0,
            missing_object_count=0, parse_error_count=0,
            replaced_ac_object_count=0, reference_row_count=0,
            sample_parse_errors=(), dry_run=False,
            indexed_through=datetime(2026, 7, 2, 9, 59, tzinfo=UTC),
            bytes_processed=0,
            run_started_at=datetime(2026, 7, 2, 9, 59, tzinfo=UTC),
            run_finished_at=datetime(2026, 7, 2, 9, 59, tzinfo=UTC),
        ),
    )

    captured_queries = []

    def fake_execute(query, parameters):
        captured_queries.append(query)
        if "ready_shards" in query:
            return BigQueryQueryResult(rows=({"ready_shards": 256},), total_bytes_processed=1)
        if "fresh_shards" in query:
            return BigQueryQueryResult(rows=({"fresh_shards": 256},), total_bytes_processed=1)
        if "COUNT(*)" in query or "object_count" in query:
            return BigQueryQueryResult(rows=({"object_count": 10},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        if "cas_preselect" in query:
            yield {"object_name": "cas/deadbeef"}
        return

    def fake_resolve_metadata(**kwargs):
        return (GcsObjectMetadata(object_name="dummy", exists=True, generation=1, size_bytes=1024),)

    def fake_load_jsonl(*args, **kwargs):
        pass

    now = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
    run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="dry-run",
        execute_kind="cas-from-index",
        max_delete_objects=500,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_metadata,
        load_jsonl_file=fake_load_jsonl,
        now=lambda: now,
        run_id_factory=lambda: "test-canary",
    )

    # The AC reverse lookup query should have LIMIT 500, not the default 100000
    ac_queries = [q for q in captured_queries if "ac_to_delete" in q]
    assert len(ac_queries) >= 1
    assert "LIMIT 500" in ac_queries[0]


def test_cas_from_index_default_ac_cap_without_max_delete_objects(monkeypatch) -> None:
    """cas-from-index uses default cleanup_max_delete_ac_objects=100000 when no flag."""
    from cost_insight.jobs import cleanup_gcs_cache, sync_gcs_cache_ac_references

    monkeypatch.setattr(
        cleanup_gcs_cache,
        "run_sync_gcs_cache_ac_references",
        lambda **kwargs: sync_gcs_cache_ac_references.SyncGcsCacheAcReferencesSummary(
            account_id="test", bucket_name="test", mode="incremental",
            shard_start=0, shard_end=255, source_object_count=0,
            missing_object_count=0, parse_error_count=0,
            replaced_ac_object_count=0, reference_row_count=0,
            sample_parse_errors=(), dry_run=False,
            indexed_through=datetime(2026, 7, 2, 9, 59, tzinfo=UTC),
            bytes_processed=0,
            run_started_at=datetime(2026, 7, 2, 9, 59, tzinfo=UTC),
            run_finished_at=datetime(2026, 7, 2, 9, 59, tzinfo=UTC),
        ),
    )

    captured_queries = []

    def fake_execute(query, parameters):
        captured_queries.append(query)
        if "ready_shards" in query:
            return BigQueryQueryResult(rows=({"ready_shards": 256},), total_bytes_processed=1)
        if "fresh_shards" in query:
            return BigQueryQueryResult(rows=({"fresh_shards": 256},), total_bytes_processed=1)
        if "COUNT(*)" in query or "object_count" in query:
            return BigQueryQueryResult(rows=({"object_count": 10},), total_bytes_processed=1)
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        if "cas_preselect" in query:
            yield {"object_name": "cas/deadbeef"}
        return

    def fake_resolve_metadata(**kwargs):
        return (GcsObjectMetadata(object_name="dummy", exists=True, generation=1, size_bytes=1024),)

    def fake_load_jsonl(*args, **kwargs):
        pass

    now = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
    # No max_delete_objects flag
    run_cleanup_gcs_cache(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="dry-run",
        execute_kind="cas-from-index",
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        resolve_object_metadata=fake_resolve_metadata,
        load_jsonl_file=fake_load_jsonl,
        now=lambda: now,
        run_id_factory=lambda: "test-default-ac-cap",
    )

    # Default AC cap is 100000 (not 10000000)
    ac_queries = [q for q in captured_queries if "ac_to_delete" in q]
    assert len(ac_queries) >= 1
    assert "LIMIT 100000" in ac_queries[0]
