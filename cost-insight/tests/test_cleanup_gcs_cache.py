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

    assert "FROM `pingcap-testing-account.ci_bazel_cache_logs.cloudaudit_googleapis_com_data_access` AS audit" in query
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
        yield {"object_name": "ac/ok"}
        yield {"object_name": "ac/corrupt"}
        yield {"object_name": "ac/missing"}

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
    assert result.sample_parse_errors[0].object_name == "ac/corrupt"
    assert result.sample_parse_errors[0].parse_error == "Unsupported protobuf wire type: 3"
    assert (
        "`project.dataset.ac_live`",
        ({"object_name": "ac/ok", "generation": 101},),
        (("object_name", "STRING"), ("generation", "INT64")),
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
            yield {"object_name": "ac/one"}
            yield {"object_name": "ac/missing"}
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
        max_delete_cas_objects=2,
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
        table_ref.rsplit(".", 1)[-1].rstrip("`") for table_ref, _rows, _schema, _disposition in loaded_files
    } == {
        "_tmp_gcs_cache_ac_live_metadata_steadyrun001",
        "_tmp_gcs_cache_ac_missing_metadata_steadyrun001",
        "_tmp_gcs_cache_run_ac_cas_refs_steadyrun001",
        "_tmp_gcs_cache_cas_live_metadata_steadyrun001",
        "_tmp_gcs_cache_cas_missing_metadata_steadyrun001",
    }
    assert any(
        "cas_from_deleted_ac" in query and params.get("limit") == 2
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
