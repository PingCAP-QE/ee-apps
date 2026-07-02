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

    assert "gcs_cache_ac_cas_refs_by_ac" in query
    assert "gcs_cache_ac_cas_refs_by_cas" in query
    assert "gcs_cache_ac_reference_index_state" in query
    assert "GENERATE_ARRAY(0, 255)" in query


def test_bootstrap_gcs_cache_ac_reference_source_query_targets_last_seen_current() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_bootstrap_gcs_cache_ac_reference_source_query(settings)

    assert "gcs_cache_object_last_seen_current" in query
    assert "object_kind = 'ac'" in query
    assert "TO_CODE_POINTS(FROM_HEX(SUBSTR(object_name, 4, 2)))" in query
    assert "REGEXP_CONTAINS(object_name, r'^ac/[0-9a-fA-F]{64}$')" in query


def test_incremental_gcs_cache_ac_reference_source_query_targets_create_events() -> None:
    settings = GcsCacheSettings(project_id="pingcap-testing-account")
    query = build_incremental_gcs_cache_ac_reference_source_query(settings)

    assert "cloudaudit_googleapis_com_data_access" in query
    assert "storage.objects.create" in query
    assert "timestamp > @indexed_through" in query
    assert "timestamp <= @run_until" in query
    assert "REGEXP_CONTAINS(object_name, r'^ac/[0-9a-fA-F]{64}$')" in query


def test_run_sync_gcs_cache_ac_references_bootstrap_dry_run_counts_objects_and_refs() -> None:
    def fake_execute(query, parameters):
        return BigQueryQueryResult(rows=(), total_bytes_processed=5)

    def fake_stream_rows(query, parameters):
        yield {"object_name": "ac/00aaaa"}
        yield {"object_name": "ac/00bbbb"}
        yield {"object_name": "ac/00cccc"}

    def fake_extract_references(**kwargs):
        assert kwargs["ac_object_names"] == ("ac/00aaaa", "ac/00bbbb", "ac/00cccc")
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
            AcReferenceExtraction(
                ac_object_name="ac/00cccc",
                exists=True,
                cas_object_names=(),
                parse_error="Unsupported protobuf wire type: 3",
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
    assert summary.source_object_count == 3
    assert summary.missing_object_count == 1
    assert summary.parse_error_count == 1
    assert summary.replaced_ac_object_count == 2
    assert summary.reference_row_count == 2
    assert summary.sample_parse_errors[0].object_name == "ac/00cccc"
    assert summary.sample_parse_errors[0].parse_error == "Unsupported protobuf wire type: 3"
    assert summary.indexed_through == now


def test_run_sync_gcs_cache_ac_references_incremental_updates_watermark_and_replaces_refs() -> None:
    captured_queries = []

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

    loaded_stage_rows: list[dict] = []

    def fake_load_json_rows(settings, *, shard, edge_rows, run_suffix=""):
        loaded_stage_rows.extend(edge_rows)
        suffix = f"_{run_suffix}" if run_suffix else ""
        return f"`project.dataset._tmp_shard_edge_stage_{shard}{suffix}`"

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
    assert summary.parse_error_count == 0
    assert summary.replaced_ac_object_count == 1
    assert summary.reference_row_count == 1
    assert loaded_stage_rows == [{"ac_object_name": "ac/00cccc", "cas_object_name": "cas/three"}]
    assert any(
        "UPDATE `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_ac_reference_index_state`"
        in query
        for query, _ in captured_queries
    )


def test_run_sync_gcs_cache_ac_references_replaces_once_per_shard() -> None:
    captured_queries = []
    load_calls = []
    extract_calls = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {param.name: param.value for param in parameters}))
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        yield {"object_name": "ac/00aaaa"}
        yield {"object_name": "ac/00bbbb"}
        yield {"object_name": "ac/00cccc"}

    def fake_extract_references(**kwargs):
        names = kwargs["ac_object_names"]
        extract_calls.append(names)
        return tuple(
            AcReferenceExtraction(
                ac_object_name=name,
                exists=True,
                cas_object_names=(f"cas/{name[-6:]}",),
            )
            for name in names
        )

    def fake_load_json_rows(settings, *, shard, edge_rows, run_suffix=""):
        load_calls.append((shard, tuple(edge_rows)))
        return f"`project.dataset._tmp_shard_edge_stage_{shard}`"

    run_sync_gcs_cache_ac_references(
        settings=GcsCacheSettings(
            project_id="pingcap-testing-account",
            ac_reference_batch_size=2,
        ),
        mode="bootstrap",
        shard_start=0,
        shard_end=0,
        dry_run=False,
        execute=fake_execute,
        stream_rows=fake_stream_rows,
        load_json_rows=fake_load_json_rows,
        extract_references=fake_extract_references,
        now=lambda: datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
    )

    assert extract_calls == [("ac/00aaaa", "ac/00bbbb"), ("ac/00cccc",)]
    assert len(load_calls) == 1
    assert len(load_calls[0][1]) == 3
    replace_queries = [
        query
        for query, _ in captured_queries
        if "DELETE FROM `pingcap-testing-account.ci_bazel_cache_logs.gcs_cache_ac_cas_refs_by_ac`"
        in query
    ]
    assert len(replace_queries) == 1


def test_run_sync_incremental_zero_ref_ac_writes_sentinel_and_filters_insert() -> None:
    """Incremental sync: zero-CAS-ref AC gets sentinel row, INSERT filters it out."""
    captured_queries = []
    loaded_stage_rows: list[dict] = []

    def fake_execute(query, parameters):
        captured_queries.append((query, {p.name: p.value for p in parameters}))
        if "SELECT indexed_through" in query:
            return BigQueryQueryResult(
                rows=({"indexed_through": datetime(2026, 6, 21, 0, 0, tzinfo=UTC)},),
                total_bytes_processed=1,
            )
        return BigQueryQueryResult(rows=(), total_bytes_processed=1)

    def fake_stream_rows(query, parameters):
        yield {"object_name": "ac/00cafe"}

    def fake_extract_references(**kwargs):
        return (
            AcReferenceExtraction(
                ac_object_name="ac/00cafe",
                exists=True,
                cas_object_names=(),  # zero CAS refs
            ),
        )

    def fake_load_json_rows(settings, *, shard, edge_rows, run_suffix=""):
        loaded_stage_rows.extend(edge_rows)
        suffix = f"_{run_suffix}" if run_suffix else ""
        return f"`project.dataset._tmp_shard_edge_stage_{shard}{suffix}`"

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

    # AC was parsed successfully (no parse error, exists)
    assert summary.replaced_ac_object_count == 1
    assert summary.reference_row_count == 0
    # Sentinel row written
    assert {"ac_object_name": "ac/00cafe", "cas_object_name": ""} in loaded_stage_rows
    # INSERT filters sentinel
    replace_queries = [q for q, _ in captured_queries if "INSERT INTO" in q and "cas_object_name !=" in q]
    assert len(replace_queries) == 1
