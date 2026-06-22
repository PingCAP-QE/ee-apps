from datetime import UTC, datetime

from cost_insight.common.bigquery import BigQueryQueryResult
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.gcs_cache_references import AcReferenceExtraction
from cost_insight.jobs.sync_gcs_cache_ac_references import (
    build_bootstrap_gcs_cache_ac_reference_source_query,
    build_ensure_gcs_cache_ac_reference_tables_query,
    build_incremental_gcs_cache_ac_reference_source_query,
    run_sync_gcs_cache_ac_references,
)


def test_ensure_gcs_cache_ac_reference_tables_query_creates_reference_and_state_tables() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_ensure_gcs_cache_ac_reference_tables_query(settings)

    assert "gcs_cache_ac_cas_references" in query
    assert "gcs_cache_ac_reference_index_state" in query
    assert "GENERATE_ARRAY(0, 255)" in query


def test_bootstrap_gcs_cache_ac_reference_source_query_targets_last_seen_current() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_bootstrap_gcs_cache_ac_reference_source_query(settings)

    assert "gcs_cache_object_last_seen_current" in query
    assert "object_kind = 'ac'" in query
    assert "TO_CODE_POINTS(FROM_HEX(SUBSTR(object_name, 4, 2)))" in query


def test_incremental_gcs_cache_ac_reference_source_query_targets_create_events() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_incremental_gcs_cache_ac_reference_source_query(settings)

    assert "cloudaudit_googleapis_com_data_access" in query
    assert "storage.objects.create" in query
    assert "timestamp > @indexed_through" in query
    assert "timestamp <= @run_until" in query


def test_run_sync_gcs_cache_ac_references_bootstrap_dry_run_counts_objects_and_refs() -> None:
    def fake_execute(query, parameters):
        return BigQueryQueryResult(rows=(), total_bytes_processed=5)

    def fake_stream_rows(query, parameters):
        yield {"object_name": "ac/00aaaa"}
        yield {"object_name": "ac/00bbbb"}

    def fake_extract_references(**kwargs):
        assert kwargs["ac_object_names"] == ("ac/00aaaa", "ac/00bbbb")
        return (
            AcReferenceExtraction(
                ac_object_name="ac/00aaaa",
                exists=True,
                cas_object_names=("cas/one", "cas/two"),
            ),
            AcReferenceExtraction(
                ac_object_name="ac/00bbbb",
                exists=False,
                cas_object_names=(),
            ),
        )

    now = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)
    summary = run_sync_gcs_cache_ac_references(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="bootstrap",
        shard_start=0,
        shard_end=0,
        dry_run=True,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        extract_references=fake_extract_references,
        now=lambda: now,
    )

    assert summary.mode == "bootstrap"
    assert summary.source_object_count == 2
    assert summary.missing_object_count == 1
    assert summary.replaced_ac_object_count == 2
    assert summary.reference_row_count == 2
    assert summary.indexed_through == now


def test_run_sync_gcs_cache_ac_references_incremental_updates_watermark_and_replaces_refs() -> None:
    captured_queries = []
    loaded_rows = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {param.name: param.value for param in parameters}))
        if "SELECT indexed_through" in query:
            return BigQueryQueryResult(
                rows=({"indexed_through": datetime(2026, 6, 21, 0, 0, tzinfo=UTC)},),
                total_bytes_processed=1,
            )
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        yield {"object_name": "ac/00cccc"}

    def fake_extract_references(**kwargs):
        return (
            AcReferenceExtraction(
                ac_object_name="ac/00cccc",
                exists=True,
                cas_object_names=("cas/three",),
            ),
        )

    def fake_load_json_rows(table_ref, rows):
        loaded_rows.append((table_ref, tuple(rows)))

    now = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)
    summary = run_sync_gcs_cache_ac_references(
        settings=GcsCacheSettings(project_id="pingcap-testing-account"),
        mode="incremental",
        shard_start=0,
        shard_end=0,
        dry_run=False,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        load_json_rows=fake_load_json_rows,
        extract_references=fake_extract_references,
        now=lambda: now,
    )

    assert summary.mode == "incremental"
    assert summary.source_object_count == 1
    assert summary.replaced_ac_object_count == 1
    assert summary.reference_row_count == 1
    assert any("UPDATE `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_ac_reference_index_state`" in query for query, _ in captured_queries)
    assert loaded_rows[0][1] == ({"ac_object_name": "ac/00cccc"},)
    assert loaded_rows[1][1] == ({"ac_object_name": "ac/00cccc", "cas_object_name": "cas/three"},)
