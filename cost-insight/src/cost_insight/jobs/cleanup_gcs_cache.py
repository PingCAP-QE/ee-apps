from __future__ import annotations

import json
from contextlib import ExitStack
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cost_insight.common.bigquery import BigQueryParameter, BigQueryQueryResult, execute_query
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.datetime_utils import coerce_datetime, coerce_optional_datetime
from cost_insight.common.gcs_cache_references import (
    AcReferenceExtraction,
    extract_action_cache_references_batch,
)
from cost_insight.jobs.sync_gcs_cache_ac_references import (
    run_sync_gcs_cache_ac_references,
)
from cost_insight.common.gcs_objects import GcsObjectMetadata, fetch_object_metadata_batch
from cost_insight.common.storage_batch_operations import (
    StorageBatchOperationsJob,
    StorageBatchOperationsJobStatus,
    create_delete_job,
    wait_for_delete_job,
)

QueryExecutor = Callable[[str, Sequence[BigQueryParameter]], BigQueryQueryResult]
RowStreamer = Callable[[str, Sequence[BigQueryParameter]], Iterator[dict[str, object]]]
MetadataResolver = Callable[..., tuple[GcsObjectMetadata, ...]]
ReferenceExtractor = Callable[..., tuple[AcReferenceExtraction, ...]]
JsonFileLoader = Callable[[str, Path, Sequence[tuple[str, str]], str], None]
BatchJobCreator = Callable[..., StorageBatchOperationsJob]
BatchJobWaiter = Callable[..., StorageBatchOperationsJobStatus]

# "all" is kept as a compatibility alias for older dry-run CronJob arguments.
# "cas" is the legacy v3 per-run AC-driven cascade.
# "cas-from-index" is the new index-based CAS reverse-lookup cleanup.
VALID_EXECUTE_KINDS = ("all", "cas", "cas-from-index")
MAX_ZERO_PROGRESS_AC_REFILL_ROUNDS = 3


@dataclass(frozen=True)
class CleanupGcsCacheSample:
    object_name: str
    last_seen_at: datetime
    idle_days: int


@dataclass(frozen=True)
class CleanupGcsCacheParseErrorSample:
    object_name: str
    parse_error: str


@dataclass(frozen=True)
class CleanupGcsCacheSummary:
    """Runtime summary for one cleanup run.

    The singular manifest/job fields are deprecated compatibility fields that
    contain only the last submitted batch. New consumers should use the plural
    fields, which retain every batch manifest URI and Storage Batch Operations
    job name created by the run.
    """

    account_id: str
    bucket_name: str
    run_id: str
    mode: str
    execute_kind: str
    dry_run: bool
    ac_retention_days: int
    cas_retention_days: int
    safety_buffer_days: int
    candidate_cas_object_count: int
    candidate_ac_object_count: int
    candidate_cas_delete_object_count: int
    ac_parse_error_count: int
    selected_ac_object_count: int
    selected_cas_object_count: int
    oldest_last_seen_at: datetime | None
    newest_last_seen_at: datetime | None
    sample_candidates: tuple[CleanupGcsCacheSample, ...]
    sample_ac_parse_errors: tuple[CleanupGcsCacheParseErrorSample, ...]
    bytes_processed: int | None
    run_started_at: datetime
    run_finished_at: datetime
    # Deprecated: last batch only. Prefer ac_manifest_uris / ac_batch_job_names.
    ac_manifest_uri: str | None = None
    ac_batch_job_name: str | None = None
    # Deprecated: last batch only. Prefer cas_manifest_uris / cas_batch_job_names.
    cas_manifest_uri: str | None = None
    cas_batch_job_name: str | None = None
    ac_manifest_uris: tuple[str, ...] | None = None
    ac_batch_job_names: tuple[str, ...] | None = None
    cas_manifest_uris: tuple[str, ...] | None = None
    cas_batch_job_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TableCountResult:
    object_count: int
    bytes_processed: int | None


@dataclass(frozen=True)
class AcSeedCursor:
    last_seen_at: datetime
    object_name: str


@dataclass(frozen=True)
class AcStageResult:
    parse_error_count: int
    sample_parse_errors: tuple[CleanupGcsCacheParseErrorSample, ...]
    seeded_object_count: int
    last_seed_cursor: AcSeedCursor | None


def run_cleanup_gcs_cache(
    *,
    settings: GcsCacheSettings,
    mode: str = "dry-run",
    execute_kind: str = "all",
    ac_retention_days: int | None = None,
    cas_retention_days: int | None = None,
    safety_buffer_days: int | None = None,
    max_delete_objects: int | None = None,
    max_delete_cas_objects: int | None = None,
    sample_limit: int | None = None,
    execute: QueryExecutor = execute_query,
    stream_rows: RowStreamer | None = None,
    resolve_object_metadata: MetadataResolver = fetch_object_metadata_batch,
    extract_references: ReferenceExtractor = extract_action_cache_references_batch,
    load_jsonl_file: JsonFileLoader | None = None,
    create_batch_job: BatchJobCreator = create_delete_job,
    wait_for_batch_job: BatchJobWaiter = wait_for_delete_job,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid4()),
) -> CleanupGcsCacheSummary:
    _validate_mode_and_execute_kind(mode=mode, execute_kind=execute_kind)

    stream_rows = _stream_query_rows if stream_rows is None else stream_rows
    load_jsonl_file = _load_jsonl_file if load_jsonl_file is None else load_jsonl_file

    run_started_at = now()
    resolved_execute_kind = "cas" if execute_kind == "all" else execute_kind
    resolved_ac_days = (
        ac_retention_days if ac_retention_days is not None else settings.ac_retention_days
    )
    resolved_cas_days = (
        cas_retention_days if cas_retention_days is not None else settings.cas_retention_days
    )
    resolved_safety_buffer_days = (
        safety_buffer_days
        if safety_buffer_days is not None
        else settings.cleanup_safety_buffer_days
    )
    resolved_sample_limit = (
        sample_limit if sample_limit is not None else settings.cleanup_sample_limit
    )
    resolved_max_delete_objects = (
        max_delete_objects
        if max_delete_objects is not None
        else settings.cleanup_max_delete_objects
    )
    resolved_max_delete_cas_objects = (
        max_delete_cas_objects
        if max_delete_cas_objects is not None
        else settings.cleanup_max_delete_cas_objects
    )
    ac_cutoff_days = resolved_ac_days + resolved_safety_buffer_days
    cas_cutoff_days = resolved_cas_days + resolved_safety_buffer_days

    # Route cas-from-index: pass resolved overrides through temporary settings.
    if execute_kind == "cas-from-index":
        overridden = GcsCacheSettings(
            project_id=settings.project_id,
            bucket_name=settings.bucket_name,
            dataset=settings.dataset,
            audit_log_table=settings.audit_log_table,
            last_seen_daily_table=settings.last_seen_daily_table,
            last_seen_current_table=settings.last_seen_current_table,
            last_seen_excluded_get_user_agent=settings.last_seen_excluded_get_user_agent,
            last_seen_excluded_get_principal_email=settings.last_seen_excluded_get_principal_email,
            ac_cas_references_table=settings.ac_cas_references_table,
            ac_cas_refs_by_ac_table=settings.ac_cas_refs_by_ac_table,
            ac_cas_refs_by_cas_table=settings.ac_cas_refs_by_cas_table,
            ac_reference_index_state_table=settings.ac_reference_index_state_table,
            ac_reference_shard_count=settings.ac_reference_shard_count,
            ac_reference_batch_size=settings.ac_reference_batch_size,
            ac_reference_download_workers=settings.ac_reference_download_workers,
            ac_reference_max_index_staleness_hours=settings.ac_reference_max_index_staleness_hours,
            ac_retention_days=resolved_ac_days,
            cas_retention_days=resolved_cas_days,
            cleanup_safety_buffer_days=resolved_safety_buffer_days,
            cleanup_sample_limit=resolved_sample_limit,
            cleanup_max_delete_objects=resolved_max_delete_objects,
            cleanup_max_delete_cas_objects=(
                resolved_max_delete_objects
                if max_delete_objects is not None
                else resolved_max_delete_cas_objects
            ),
            cleanup_max_delete_cas_bytes=settings.cleanup_max_delete_cas_bytes,
            cleanup_cas_preselect_limit=settings.cleanup_cas_preselect_limit,
            cleanup_ac_delete_batch_size=settings.cleanup_ac_delete_batch_size,
            cleanup_batch_size=settings.cleanup_batch_size,
            cleanup_manifest_bucket=settings.cleanup_manifest_bucket,
            cleanup_manifest_prefix=settings.cleanup_manifest_prefix,
            cleanup_candidate_ttl_days=settings.cleanup_candidate_ttl_days,
            cleanup_require_fresh_index=settings.cleanup_require_fresh_index,
        )
        return run_cleanup_gcs_cache_from_index(
            settings=overridden,
            mode=mode,
            execute=execute,
            stream_rows=stream_rows,
            resolve_object_metadata=resolve_object_metadata,
            load_jsonl_file=load_jsonl_file,
            create_batch_job=create_batch_job,
            wait_for_batch_job=wait_for_batch_job,
            now=now,
            run_id_factory=run_id_factory,
        )

    bytes_processed_total = 0
    summary_result = execute(
        build_cleanup_gcs_cache_summary_query(settings),
        parameters=[
            BigQueryParameter("ac_cutoff_days", "INT64", ac_cutoff_days),
            BigQueryParameter("sample_limit", "INT64", resolved_sample_limit),
        ],
    )
    bytes_processed_total = _add_bytes(bytes_processed_total, summary_result.total_bytes_processed)
    row = summary_result.rows[0] if summary_result.rows else {}

    sample_candidates = tuple(
        CleanupGcsCacheSample(
            object_name=str(item["object_name"]),
            last_seen_at=coerce_datetime(item["last_seen_at"]),
            idle_days=int(item["idle_days"]),
        )
        for item in (row.get("sample_candidates") or [])
    )

    candidate_cas_object_count = int(row.get("candidate_cas_object_count", 0) or 0)
    candidate_ac_object_count = int(row.get("candidate_ac_object_count", 0) or 0)
    candidate_cas_delete_object_count = int(row.get("candidate_cas_delete_object_count", 0) or 0)
    oldest_last_seen_at = coerce_optional_datetime(row.get("oldest_last_seen_at"))
    newest_last_seen_at = coerce_optional_datetime(row.get("newest_last_seen_at"))

    if mode == "dry-run":
        run_finished_at = now()
        return CleanupGcsCacheSummary(
            account_id=settings.project_id,
            bucket_name=settings.bucket_name,
            run_id=run_id_factory(),
            mode=mode,
            execute_kind=resolved_execute_kind,
            dry_run=True,
            ac_retention_days=resolved_ac_days,
            cas_retention_days=resolved_cas_days,
            safety_buffer_days=resolved_safety_buffer_days,
            candidate_cas_object_count=candidate_cas_object_count,
            candidate_ac_object_count=candidate_ac_object_count,
            candidate_cas_delete_object_count=candidate_cas_delete_object_count,
            ac_parse_error_count=0,
            selected_ac_object_count=0,
            selected_cas_object_count=0,
            oldest_last_seen_at=oldest_last_seen_at,
            newest_last_seen_at=newest_last_seen_at,
            sample_candidates=sample_candidates,
            sample_ac_parse_errors=(),
            bytes_processed=bytes_processed_total,
            run_started_at=run_started_at,
            run_finished_at=run_finished_at,
        )

    run_id = run_id_factory()
    ttl_days = settings.cleanup_candidate_ttl_days
    ac_parse_error_count = 0
    sample_ac_parse_errors: list[CleanupGcsCacheParseErrorSample] = []
    selected_ac_object_count = 0
    selected_cas_object_count = 0
    candidate_cas_object_count = 0
    candidate_cas_delete_object_count = 0

    ac_manifest_uri = None
    ac_batch_job_name = None
    cas_manifest_uri = None
    cas_batch_job_name = None
    ac_manifest_uris: list[str] = []
    ac_batch_job_names: list[str] = []
    cas_manifest_uris: list[str] = []
    cas_batch_job_names: list[str] = []
    ac_seed_cursor: AcSeedCursor | None = None
    ac_candidates_exhausted = False
    batch_index = 0

    while selected_ac_object_count < resolved_max_delete_objects and not ac_candidates_exhausted:
        batch_index += 1
        batch_target = min(
            settings.cleanup_ac_delete_batch_size,
            resolved_max_delete_objects - selected_ac_object_count,
        )

        ac_candidate_table = _batch_candidate_table_name(
            settings, prefix="candidate_ac", run_id=run_id, batch_index=batch_index
        )
        run_references_table = _batch_candidate_table_name(
            settings, prefix="run_ac_cas_refs", run_id=run_id, batch_index=batch_index
        )
        cas_candidate_table = _batch_candidate_table_name(
            settings, prefix="candidate_cas", run_id=run_id, batch_index=batch_index
        )
        ac_live_metadata_table = _batch_candidate_table_name(
            settings, prefix="ac_live_metadata", run_id=run_id, batch_index=batch_index
        )
        ac_missing_metadata_table = _batch_candidate_table_name(
            settings, prefix="ac_missing_metadata", run_id=run_id, batch_index=batch_index
        )
        cas_live_metadata_table = _batch_candidate_table_name(
            settings, prefix="cas_live_metadata", run_id=run_id, batch_index=batch_index
        )
        cas_missing_metadata_table = _batch_candidate_table_name(
            settings, prefix="cas_missing_metadata", run_id=run_id, batch_index=batch_index
        )
        ac_delete_table = _batch_candidate_table_name(
            settings, prefix="delete_ac", run_id=run_id, batch_index=batch_index
        )
        cas_delete_table = _batch_candidate_table_name(
            settings, prefix="delete_cas", run_id=run_id, batch_index=batch_index
        )

        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_run_references_table_query(
                    run_references_table=run_references_table,
                    ttl_days=ttl_days,
                ),
                parameters=[],
            ).total_bytes_processed,
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_metadata_stage_tables_query(
                    ttl_days=ttl_days,
                    ac_live_metadata_table=ac_live_metadata_table,
                    ac_missing_metadata_table=ac_missing_metadata_table,
                    cas_live_metadata_table=cas_live_metadata_table,
                    cas_missing_metadata_table=cas_missing_metadata_table,
                ),
                parameters=[],
            ).total_bytes_processed,
        )

        selected_ac_in_batch = 0
        zero_progress_refill_rounds = 0
        while selected_ac_in_batch < batch_target:
            remaining_ac_target = batch_target - selected_ac_in_batch
            ac_seed_parameters = [
                BigQueryParameter("ac_cutoff_days", "INT64", ac_cutoff_days),
                BigQueryParameter("limit", "INT64", remaining_ac_target),
            ]
            if ac_seed_cursor is not None:
                ac_seed_parameters.extend(
                    [
                        BigQueryParameter(
                            "cursor_last_seen_at", "TIMESTAMP", ac_seed_cursor.last_seen_at
                        ),
                        BigQueryParameter(
                            "cursor_object_name", "STRING", ac_seed_cursor.object_name
                        ),
                    ]
                )
            bytes_processed_total = _add_bytes(
                bytes_processed_total,
                execute(
                    build_cleanup_gcs_cache_ac_seed_table_query(
                        settings,
                        candidate_table=ac_candidate_table,
                        ttl_days=ttl_days,
                        has_cursor=ac_seed_cursor is not None,
                    ),
                    parameters=ac_seed_parameters,
                ).total_bytes_processed,
            )
            ac_stage_result = _populate_ac_stage_tables(
                settings=settings,
                stream_rows=stream_rows,
                resolve_object_metadata=resolve_object_metadata,
                extract_references=extract_references,
                load_jsonl_file=load_jsonl_file,
                source_table=ac_candidate_table,
                live_table=ac_live_metadata_table,
                missing_table=ac_missing_metadata_table,
                references_table=run_references_table,
                stream_batch_size=settings.cleanup_batch_size,
                reference_batch_size=settings.ac_reference_batch_size,
            )
            ac_parse_error_count += ac_stage_result.parse_error_count
            sample_ac_parse_errors.extend(ac_stage_result.sample_parse_errors)
            del sample_ac_parse_errors[10:]
            if ac_stage_result.seeded_object_count == 0:
                ac_candidates_exhausted = True
                break
            ac_seed_cursor = ac_stage_result.last_seed_cursor

            bytes_processed_total = _add_bytes(
                bytes_processed_total,
                execute(
                    build_cleanup_gcs_cache_reconcile_missing_ac_query(
                        settings,
                        missing_metadata_table=ac_missing_metadata_table,
                    ),
                    parameters=[BigQueryParameter("run_started_at", "TIMESTAMP", run_started_at)],
                ).total_bytes_processed,
            )

            bytes_processed_total = _add_bytes(
                bytes_processed_total,
                execute(
                    build_cleanup_gcs_cache_final_ac_delete_table_query(
                        ac_live_metadata_table=ac_live_metadata_table,
                        ac_missing_metadata_table=ac_missing_metadata_table,
                        candidate_table=ac_delete_table,
                        ttl_days=ttl_days,
                    ),
                    parameters=[],
                ).total_bytes_processed,
            )

            previous_selected_ac_in_batch = selected_ac_in_batch
            selected_ac_count = _count_table_rows(execute=execute, table_ref=ac_delete_table)
            selected_ac_in_batch = selected_ac_count.object_count
            bytes_processed_total = _add_bytes(
                bytes_processed_total, selected_ac_count.bytes_processed
            )
            if selected_ac_in_batch == previous_selected_ac_in_batch:
                zero_progress_refill_rounds += 1
            else:
                zero_progress_refill_rounds = 0
            if selected_ac_in_batch >= batch_target:
                break
            if zero_progress_refill_rounds >= MAX_ZERO_PROGRESS_AC_REFILL_ROUNDS:
                ac_candidates_exhausted = True
                break
            if ac_stage_result.seeded_object_count < remaining_ac_target:
                ac_candidates_exhausted = True
                break

        if selected_ac_in_batch <= 0:
            continue

        cas_from_ac_count = _count_distinct_run_cas_rows(
            execute=execute,
            table_ref=run_references_table,
        )
        candidate_cas_object_count += cas_from_ac_count.object_count
        bytes_processed_total = _add_bytes(bytes_processed_total, cas_from_ac_count.bytes_processed)

        ac_manifest_uri = _manifest_uri(
            settings,
            object_kind="ac",
            run_started_at=run_started_at,
            run_id=run_id,
            batch_index=batch_index,
        )
        ac_manifest_uris.append(ac_manifest_uri)
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_manifest_export_query(
                    candidate_table=ac_delete_table,
                    manifest_uri=ac_manifest_uri,
                    bucket_name=settings.bucket_name,
                ),
                parameters=[],
            ).total_bytes_processed,
        )
        ac_batch_job = create_batch_job(
            project_id=settings.project_id,
            job_id=_batch_job_id(
                object_kind="ac",
                run_started_at=run_started_at,
                run_id=run_id,
                batch_index=batch_index,
            ),
            bucket_name=settings.bucket_name,
            manifest_uri=ac_manifest_uri,
            dry_run=False,
            description=(
                "Cascading GCS cache AC cleanup "
                f"run {run_id} (ac={resolved_ac_days}, cas={resolved_cas_days}, "
                f"buffer={resolved_safety_buffer_days})"
            ),
        )
        ac_batch_job_name = ac_batch_job.job_name
        ac_batch_job_names.append(ac_batch_job_name)
        _wait_for_successful_batch_job(
            wait_for_batch_job=wait_for_batch_job, job_name=ac_batch_job.job_name
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_reconcile_deleted_ac_query(
                    settings,
                    candidate_table=ac_delete_table,
                ),
                parameters=[BigQueryParameter("run_started_at", "TIMESTAMP", run_started_at)],
            ).total_bytes_processed,
        )
        selected_ac_object_count += selected_ac_in_batch

        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_cas_candidate_table_query(
                    settings,
                    run_references_table=run_references_table,
                    candidate_table=cas_candidate_table,
                    ttl_days=ttl_days,
                ),
                parameters=[
                    BigQueryParameter("cas_cutoff_days", "INT64", cas_cutoff_days),
                ],
            ).total_bytes_processed,
        )
        _populate_metadata_stage_table(
            settings=settings,
            stream_rows=stream_rows,
            resolve_object_metadata=resolve_object_metadata,
            load_jsonl_file=load_jsonl_file,
            source_table=cas_candidate_table,
            live_table=cas_live_metadata_table,
            missing_table=cas_missing_metadata_table,
            batch_size=settings.cleanup_batch_size,
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_reconcile_missing_cas_query(
                    settings,
                    candidate_table=cas_candidate_table,
                    missing_metadata_table=cas_missing_metadata_table,
                ),
                parameters=[],
            ).total_bytes_processed,
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_final_cas_delete_table_query(
                    settings,
                    source_table=cas_candidate_table,
                    live_metadata_table=cas_live_metadata_table,
                    candidate_table=cas_delete_table,
                    ttl_days=ttl_days,
                ),
                parameters=[],
            ).total_bytes_processed,
        )
        selected_cas_count = _count_table_rows(execute=execute, table_ref=cas_delete_table)
        selected_cas_in_batch = selected_cas_count.object_count
        bytes_processed_total = _add_bytes(
            bytes_processed_total, selected_cas_count.bytes_processed
        )
        candidate_cas_delete_object_count += selected_cas_in_batch
        selected_cas_object_count += selected_cas_in_batch

        if selected_cas_in_batch <= 0:
            continue

        cas_manifest_uri = _manifest_uri(
            settings,
            object_kind="cas",
            run_started_at=run_started_at,
            run_id=run_id,
            batch_index=batch_index,
        )
        cas_manifest_uris.append(cas_manifest_uri)
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_manifest_export_query(
                    candidate_table=cas_delete_table,
                    manifest_uri=cas_manifest_uri,
                    bucket_name=settings.bucket_name,
                ),
                parameters=[],
            ).total_bytes_processed,
        )
        cas_batch_job = create_batch_job(
            project_id=settings.project_id,
            job_id=_batch_job_id(
                object_kind="cas",
                run_started_at=run_started_at,
                run_id=run_id,
                batch_index=batch_index,
            ),
            bucket_name=settings.bucket_name,
            manifest_uri=cas_manifest_uri,
            dry_run=False,
            description=(
                "Cascading GCS cache CAS cleanup "
                f"run {run_id} (ac={resolved_ac_days}, cas={resolved_cas_days}, "
                f"buffer={resolved_safety_buffer_days})"
            ),
        )
        cas_batch_job_name = cas_batch_job.job_name
        cas_batch_job_names.append(cas_batch_job_name)
        _wait_for_successful_batch_job(
            wait_for_batch_job=wait_for_batch_job, job_name=cas_batch_job.job_name
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_cleanup_gcs_cache_reconcile_deleted_cas_query(
                    settings,
                    candidate_table=cas_delete_table,
                ),
                parameters=[],
            ).total_bytes_processed,
        )

    run_finished_at = now()
    return CleanupGcsCacheSummary(
        account_id=settings.project_id,
        bucket_name=settings.bucket_name,
        run_id=run_id,
        mode=mode,
        execute_kind=resolved_execute_kind,
        dry_run=False,
        ac_retention_days=resolved_ac_days,
        cas_retention_days=resolved_cas_days,
        safety_buffer_days=resolved_safety_buffer_days,
        candidate_cas_object_count=candidate_cas_object_count,
        candidate_ac_object_count=candidate_ac_object_count,
        candidate_cas_delete_object_count=candidate_cas_delete_object_count,
        ac_parse_error_count=ac_parse_error_count,
        selected_ac_object_count=selected_ac_object_count,
        selected_cas_object_count=selected_cas_object_count,
        oldest_last_seen_at=oldest_last_seen_at,
        newest_last_seen_at=newest_last_seen_at,
        sample_candidates=(),
        sample_ac_parse_errors=tuple(sample_ac_parse_errors),
        bytes_processed=bytes_processed_total,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
        ac_manifest_uri=ac_manifest_uri,
        ac_batch_job_name=ac_batch_job_name,
        cas_manifest_uri=cas_manifest_uri,
        cas_batch_job_name=cas_batch_job_name,
        ac_manifest_uris=tuple(ac_manifest_uris) or None,
        ac_batch_job_names=tuple(ac_batch_job_names) or None,
        cas_manifest_uris=tuple(cas_manifest_uris) or None,
        cas_batch_job_names=tuple(cas_batch_job_names) or None,
    )


def build_cleanup_gcs_cache_summary_query(settings: GcsCacheSettings) -> str:
    """Build an AC-only planning summary for per-run cascade cleanup.

    v3 no longer depends on a persistent global AC->CAS index. CAS candidates are
    known only after the selected AC objects are downloaded and parsed in the
    delete run, so the summary intentionally reports CAS counts as zero.
    """
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
WITH cold_ac AS (
  SELECT
    object_name,
    last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_seen_at, DAY) AS idle_days
  FROM {current_table}
  WHERE object_kind = 'ac'
    AND REGEXP_CONTAINS(object_name, r'^ac/[0-9a-fA-F]{64}$')
    AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_cutoff_days DAY)
)
SELECT
  0 AS candidate_cas_object_count,
  COUNT(*) AS candidate_ac_object_count,
  0 AS candidate_cas_delete_object_count,
  MIN(cold_ac.last_seen_at) AS oldest_last_seen_at,
  MAX(cold_ac.last_seen_at) AS newest_last_seen_at,
  ARRAY_AGG(
    STRUCT(
      cold_ac.object_name AS object_name,
      cold_ac.last_seen_at AS last_seen_at,
      cold_ac.idle_days AS idle_days
    )
    ORDER BY cold_ac.last_seen_at ASC, cold_ac.object_name ASC
    LIMIT @sample_limit
  ) AS sample_candidates
FROM cold_ac
""".strip()


def build_cleanup_gcs_cache_ac_seed_table_query(
    settings: GcsCacheSettings,
    *,
    candidate_table: str,
    ttl_days: int,
    has_cursor: bool = False,
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    cursor_filter = ""
    if has_cursor:
        cursor_filter = """
  AND (
    last_seen_at > @cursor_last_seen_at
    OR (last_seen_at = @cursor_last_seen_at AND object_name > @cursor_object_name)
  )"""
    return f"""
CREATE OR REPLACE TABLE {candidate_table}
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
) AS
SELECT
  object_name,
  last_seen_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_seen_at, DAY) AS idle_days
FROM {current_table}
WHERE object_kind = 'ac'
  AND REGEXP_CONTAINS(object_name, r'^ac/[0-9a-fA-F]{64}$')
  AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_cutoff_days DAY)
{cursor_filter}
ORDER BY last_seen_at ASC, object_name ASC
LIMIT @limit
""".strip()


def build_cleanup_gcs_cache_run_references_table_query(
    *,
    run_references_table: str,
    ttl_days: int,
) -> str:
    return f"""
CREATE OR REPLACE TABLE {run_references_table} (
  ac_object_name STRING,
  cas_object_name STRING
)
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
)
""".strip()


def build_cleanup_gcs_cache_cas_candidate_table_query(
    settings: GcsCacheSettings,
    *,
    run_references_table: str,
    candidate_table: str,
    ttl_days: int,
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
CREATE OR REPLACE TABLE {candidate_table}
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
) AS
WITH cas_from_deleted_ac AS (
  SELECT DISTINCT cas_object_name
  FROM {run_references_table}
),
cas_after_extra_idle AS (
  SELECT DISTINCT
    current_obj.object_name,
    current_obj.last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), current_obj.last_seen_at, DAY) AS idle_days
  FROM cas_from_deleted_ac AS candidate
  JOIN {current_table} AS current_obj
    ON current_obj.object_name = candidate.cas_object_name
   AND current_obj.object_kind = 'cas'
   AND current_obj.last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_cutoff_days DAY)
)
SELECT
  cas.object_name,
  cas.last_seen_at,
  cas.idle_days
FROM cas_after_extra_idle AS cas
ORDER BY cas.last_seen_at ASC, cas.object_name ASC
""".strip()


def build_cleanup_gcs_cache_metadata_stage_tables_query(
    *,
    ttl_days: int,
    ac_live_metadata_table: str,
    ac_missing_metadata_table: str,
    cas_live_metadata_table: str,
    cas_missing_metadata_table: str,
) -> str:
    return ";\n\n".join(
        [
            _create_table_query(
                ac_live_metadata_table,
                columns=(("object_name", "STRING"), ("generation", "INT64"), ("size_bytes", "INT64")),
                ttl_days=ttl_days,
            ),
            _create_table_query(
                ac_missing_metadata_table,
                columns=(("object_name", "STRING"),),
                ttl_days=ttl_days,
            ),
            _create_table_query(
                cas_live_metadata_table,
                columns=(("object_name", "STRING"), ("generation", "INT64"), ("size_bytes", "INT64")),
                ttl_days=ttl_days,
            ),
            _create_table_query(
                cas_missing_metadata_table,
                columns=(("object_name", "STRING"),),
                ttl_days=ttl_days,
            ),
        ]
    )


def build_cleanup_gcs_cache_final_ac_delete_table_query(
    *,
    ac_live_metadata_table: str,
    ac_missing_metadata_table: str,
    candidate_table: str,
    ttl_days: int,
) -> str:
    return f"""
CREATE OR REPLACE TABLE {candidate_table}
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
) AS
SELECT
  object_name,
  generation
FROM {ac_live_metadata_table} AS live
WHERE NOT EXISTS (
  SELECT 1
  FROM {ac_missing_metadata_table} AS missing
  WHERE missing.object_name = live.object_name
)
ORDER BY object_name ASC
""".strip()


def build_cleanup_gcs_cache_final_cas_delete_table_query(
    settings: GcsCacheSettings,
    *,
    source_table: str,
    live_metadata_table: str,
    candidate_table: str,
    ttl_days: int,
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    audit_table = _table_ref(settings.project_id, settings.dataset, settings.audit_log_table)
    ignored_cleanup_get_filter = _ignored_cleanup_get_filter(settings)
    return f"""
CREATE OR REPLACE TABLE {candidate_table}
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
) AS
SELECT
  source.object_name,
  source.last_seen_at,
  source.idle_days,
  live.generation
FROM {source_table} AS source
JOIN {live_metadata_table} AS live
  USING (object_name)
JOIN {current_table} AS current_obj
  ON current_obj.object_name = source.object_name
 -- Defensive re-check: source_table is expected to contain only CAS rows, but
 -- this keeps accidental table reuse from deleting non-CAS objects.
 AND current_obj.object_kind = 'cas'
 AND current_obj.last_seen_at = source.last_seen_at
WHERE NOT EXISTS (
  SELECT 1
  FROM {audit_table} AS audit
  WHERE audit.resource.labels.bucket_name = '{settings.bucket_name}'
    AND audit.protopayload_auditlog.methodName IN (
      'storage.objects.get',
      'storage.objects.create'
    )
    AND REGEXP_EXTRACT(audit.protopayload_auditlog.resourceName, r"/objects/(.+)$") = source.object_name
    AND audit.timestamp > source.last_seen_at
    {ignored_cleanup_get_filter}
)
ORDER BY source.last_seen_at ASC, source.object_name ASC
""".strip()


def build_cleanup_gcs_cache_reconcile_missing_ac_query(
    settings: GcsCacheSettings,
    *,
    missing_metadata_table: str,
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
DELETE FROM {current_table}
WHERE object_kind = 'ac'
  AND object_name IN (
    SELECT object_name
    FROM {missing_metadata_table}
  )
  AND last_seen_at < @run_started_at
""".strip()


def build_cleanup_gcs_cache_reconcile_missing_cas_query(
    settings: GcsCacheSettings,
    *,
    candidate_table: str,
    missing_metadata_table: str,
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    # The per-run reference table is a short-lived snapshot derived from selected AC
    # objects. Missing CAS only needs to be reconciled from last_seen_current.
    return f"""
DELETE FROM {current_table}
WHERE STRUCT(object_name, last_seen_at) IN (
  SELECT AS STRUCT candidate.object_name, candidate.last_seen_at
  FROM {candidate_table} AS candidate
  JOIN {missing_metadata_table} AS missing
    ON missing.object_name = candidate.object_name
)
""".strip()


def build_cleanup_gcs_cache_reconcile_deleted_ac_query(
    settings: GcsCacheSettings,
    *,
    candidate_table: str,
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
DELETE FROM {current_table}
WHERE object_kind = 'ac'
  AND object_name IN (
    SELECT object_name
    FROM {candidate_table}
  )
  AND last_seen_at < @run_started_at
""".strip()


def build_cleanup_gcs_cache_reconcile_deleted_cas_query(
    settings: GcsCacheSettings,
    *,
    candidate_table: str,
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
DELETE FROM {current_table}
WHERE STRUCT(object_name, last_seen_at) IN (
  SELECT AS STRUCT object_name, last_seen_at
  FROM {candidate_table}
)
""".strip()


def build_cleanup_gcs_cache_manifest_export_query(
    *,
    candidate_table: str,
    manifest_uri: str,
    bucket_name: str,
) -> str:
    return f"""
EXPORT DATA OPTIONS (
  uri = '{manifest_uri}',
  format = 'CSV',
  overwrite = true,
  header = true,
  field_delimiter = ','
) AS
SELECT
  '{bucket_name}' AS bucket,
  object_name AS name,
  generation
FROM {candidate_table}
ORDER BY object_name ASC
""".strip()


# ---------------------------------------------------------------------------
# cas-from-index query builders
# ---------------------------------------------------------------------------


def build_rebuild_by_cas_from_by_ac_query(
    settings: GcsCacheSettings,
) -> str:
    """Rebuild by_cas as a full snapshot from by_ac."""
    by_ac_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table
    )
    by_cas_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_cas_table
    )
    return f"""
CREATE OR REPLACE TABLE {by_cas_table}
CLUSTER BY cas_object_name, ac_object_name
AS
SELECT ac_object_name, cas_object_name
FROM {by_ac_table};
""".strip()


def build_stale_ac_candidates_query(
    settings: GcsCacheSettings,
    *,
    limit: int = 100000,
) -> str:
    """Find AC objects in by_ac that are NOT in last_seen_current."""
    by_ac_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table
    )
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
SELECT DISTINCT by_ac.ac_object_name AS object_name
FROM {by_ac_table} AS by_ac
WHERE NOT EXISTS (
  SELECT 1
  FROM {current_table} AS cur
  WHERE cur.object_name = by_ac.ac_object_name
    AND cur.object_kind = 'ac'
)
LIMIT {limit}
""".strip()


def build_cold_cas_preselect_query(
    settings: GcsCacheSettings,
    *,
    snapshot_time: str,
    cas_cutoff_days: int,
    preselect_limit: int,
) -> str:
    """Select cold CAS candidates, ordered by oldest first."""
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
SELECT object_name, last_seen_at
FROM {current_table}
WHERE object_kind = 'cas'
  AND last_seen_at < TIMESTAMP_SUB(TIMESTAMP('{snapshot_time}'), INTERVAL {cas_cutoff_days} DAY)
ORDER BY last_seen_at ASC
LIMIT {preselect_limit}
""".strip()


def build_ac_reverse_lookup_query(
    settings: GcsCacheSettings,
    *,
    cold_cas_table: str,
    snapshot_time: str,
    ac_cutoff_days: int,
    by_cas_table_override: str | None = None,
) -> str:
    """Find cold ACs that reference the bounded cold CAS set."""
    by_cas_table = by_cas_table_override or _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_cas_table
    )
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
SELECT DISTINCT
  refs.ac_object_name,
  cur.last_seen_at
FROM {by_cas_table} AS refs
JOIN {cold_cas_table} AS cas
  ON refs.cas_object_name = cas.object_name
JOIN {current_table} AS cur
  ON cur.object_name = refs.ac_object_name
 AND cur.object_kind = 'ac'
 AND cur.last_seen_at < TIMESTAMP_SUB(TIMESTAMP('{snapshot_time}'), INTERVAL {ac_cutoff_days} DAY)
ORDER BY cur.last_seen_at ASC
""".strip()


def build_cas_ready_for_cascade_query(
    settings: GcsCacheSettings,
    *,
    cold_cas_table: str,
    snapshot_time: str,
    ac_cutoff_days: int,
    by_cas_table_override: str | None = None,
) -> str:
    """Keep cold CAS only when every indexed AC reference is known and cold."""
    by_cas_table = by_cas_table_override or _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_cas_table
    )
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
SELECT cas.object_name, cas.last_seen_at, cas.size_bytes, cas.generation
FROM {cold_cas_table} AS cas
WHERE NOT EXISTS (
  SELECT 1
  FROM {by_cas_table} AS refs
  LEFT JOIN {current_table} AS cur
    ON cur.object_name = refs.ac_object_name
   AND cur.object_kind = 'ac'
  WHERE refs.cas_object_name = cas.object_name
    AND (
      cur.object_name IS NULL
      OR cur.last_seen_at >= TIMESTAMP_SUB(TIMESTAMP('{snapshot_time}'), INTERVAL {ac_cutoff_days} DAY)
    )
)
""".strip()


def build_zero_ref_cas_query(
    settings: GcsCacheSettings,
    *,
    cold_cas_table: str,
    by_cas_table_override: str | None = None,
) -> str:
    """After AC deletion and by_cas rebuild: find CAS with zero remaining references."""
    by_cas_table = by_cas_table_override or _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_cas_table
    )
    return f"""
SELECT cas.object_name, cas.last_seen_at
FROM {cold_cas_table} AS cas
LEFT JOIN {by_cas_table} AS refs
  ON refs.cas_object_name = cas.object_name
GROUP BY cas.object_name, cas.last_seen_at
HAVING COUNT(refs.ac_object_name) = 0
""".strip()


def build_cas_audit_recheck_query(
    settings: GcsCacheSettings,
    *,
    candidate_table: str,
    zero_ref_table: str,
    live_metadata_table: str,
    ttl_days: int,
) -> str:
    """Final CAS delete table with audit recheck and generation."""
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    audit_table = _table_ref(settings.project_id, settings.dataset, settings.audit_log_table)
    ignored_cleanup_get_filter = _ignored_cleanup_get_filter(settings)
    return f"""
CREATE OR REPLACE TABLE {candidate_table}
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
) AS
SELECT
  source.object_name,
  source.last_seen_at,
  live.generation,
  live.size_bytes
FROM {zero_ref_table} AS source
JOIN {live_metadata_table} AS live
  USING (object_name)
JOIN {current_table} AS cur
  ON cur.object_name = source.object_name
 AND cur.object_kind = 'cas'
 AND cur.last_seen_at = source.last_seen_at
WHERE NOT EXISTS (
  SELECT 1
  FROM {audit_table} AS audit
  WHERE audit.resource.labels.bucket_name = '{settings.bucket_name}'
    AND audit.protopayload_auditlog.methodName IN (
      'storage.objects.get',
      'storage.objects.create'
    )
    AND REGEXP_EXTRACT(audit.protopayload_auditlog.resourceName, r"/objects/(.+)$") = source.object_name
    AND audit.timestamp > source.last_seen_at
    {ignored_cleanup_get_filter}
)
ORDER BY source.last_seen_at ASC, live.size_bytes DESC
""".strip()


# ---------------------------------------------------------------------------
# cas-from-index orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupGcsCacheAcDeleteResult:
    candidate_ac_count: int
    selected_ac_count: int
    manifest_uri: str
    batch_job_name: str


def run_cleanup_gcs_cache_from_index(
    *,
    settings: GcsCacheSettings,
    mode: str = "dry-run",
    execute: QueryExecutor = execute_query,
    stream_rows: RowStreamer | None = None,
    resolve_object_metadata: MetadataResolver = fetch_object_metadata_batch,
    load_jsonl_file: JsonFileLoader | None = None,
    create_batch_job: BatchJobCreator = create_delete_job,
    wait_for_batch_job: BatchJobWaiter = wait_for_delete_job,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid4()),
) -> CleanupGcsCacheSummary:
    """Index-based CAS cleanup: rebuild by_cas, reverse-lookup AC, delete CAS.

    This is the cas-from-index execution path. It requires a complete
    by_ac index (all 256 shard indexed_through IS NOT NULL).
    """
    stream_rows = _stream_query_rows if stream_rows is None else stream_rows
    load_jsonl_file = _load_jsonl_file if load_jsonl_file is None else load_jsonl_file

    run_started_at = now()
    run_id = run_id_factory()
    bytes_processed_total = 0

    # ---- Gate 1: completeness (bootstrap must be done) ----
    # Fail fast if any shard has no watermark at all.
    if settings.cleanup_require_fresh_index:
        state_table = _table_ref(
            settings.project_id, settings.dataset, settings.ac_reference_index_state_table
        )
        result = execute(
            f"SELECT COUNTIF(indexed_through IS NOT NULL) AS ready_shards FROM {state_table}",
            parameters=[],
        )
        ready = int(result.rows[0].get("ready_shards", 0) or 0) if result.rows else 0
        if ready < settings.ac_reference_shard_count:
            raise RuntimeError(
                f"AC reference index is incomplete: {ready}/{settings.ac_reference_shard_count} "
                "shards have indexed_through. Run bootstrap first, or set "
                "COST_INSIGHT_GCS_CACHE_CLEANUP_REQUIRE_FRESH_INDEX=false to bypass."
            )
        bytes_processed_total = _add_bytes(bytes_processed_total, result.total_bytes_processed)

    # ---- First incremental catch-up (design requirement) ----
    # Catch up to snapshot_time before any CAS selection or AC deletion.
    # Skip in dry-run to avoid mutating index watermarks.
    snapshot_time = run_started_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    if mode != "dry-run":
        _run_catch_up_sync(settings=settings, until=run_started_at, execute=execute)

    # ---- Gate 2: freshness (catch-up must have reached snapshot_time) ----
    # Now verify all watermarks are within staleness of the cleanup snapshot.
    if settings.cleanup_require_fresh_index:
        staleness_hours = settings.ac_reference_max_index_staleness_hours
        result = execute(
            f"""SELECT COUNTIF(indexed_through >= TIMESTAMP_SUB(TIMESTAMP('{snapshot_time}'), INTERVAL {staleness_hours} HOUR)) AS fresh_shards
FROM {state_table}""",
            parameters=[],
        )
        fresh = int(result.rows[0].get("fresh_shards", 0) or 0) if result.rows else 0
        if fresh < settings.ac_reference_shard_count:
            raise RuntimeError(
                f"AC reference index is stale after catch-up: only {fresh}/{settings.ac_reference_shard_count} "
                f"shards have indexed_through within {staleness_hours}h of cleanup snapshot "
                f"({snapshot_time}). Check incremental sync logs, increase max_staleness_hours, "
                "or set COST_INSIGHT_GCS_CACHE_CLEANUP_REQUIRE_FRESH_INDEX=false to bypass."
            )
        bytes_processed_total = _add_bytes(bytes_processed_total, result.total_bytes_processed)
    cas_cutoff_days = settings.cas_retention_days + settings.cleanup_safety_buffer_days
    ac_cutoff_days = settings.ac_retention_days + settings.cleanup_safety_buffer_days

    # ---- Create metadata stage tables (fresh for this run) ----
    ttl_days = settings.cleanup_candidate_ttl_days
    for suffix in ("cas_live_metadata", "cas_missing_metadata",
                   "ac_live_metadata", "ac_missing_metadata",
                   "stale_ac_live", "stale_ac_missing"):
        stage_table = _tmp_table_ref(settings, suffix, run_id=run_id)
        if "missing" in suffix:
            columns = "object_name STRING"
        else:
            columns = "object_name STRING, generation INT64, size_bytes INT64"
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                f"""CREATE OR REPLACE TABLE {stage_table} ({columns})
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY))""",
                parameters=[],
            ).total_bytes_processed,
        )

    # ---- Stale AC reconciliation (clean ghost refs from by_ac) ----
    # Skip in dry-run to avoid mutating persistent index tables.
    if mode != "dry-run":
        _reconcile_stale_ac_references(
            settings=settings,
            execute=execute,
            stream_rows=stream_rows,
            resolve_object_metadata=resolve_object_metadata,
            load_jsonl_file=load_jsonl_file,
            run_id=run_id,
        )

    # ---- Rebuild by_cas snapshot from by_ac ----
    # In dry-run, use a temp snapshot to avoid mutating the persistent by_cas table.
    if mode == "dry-run":
        by_cas_table = _tmp_table_ref(settings, "by_cas_dryrun", run_id=run_id)
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                f"""CREATE OR REPLACE TABLE {by_cas_table}
CLUSTER BY cas_object_name, ac_object_name
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
SELECT ac_object_name, cas_object_name
FROM {_table_ref(settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table)}""",
                parameters=[],
            ).total_bytes_processed,
        )
    else:
        by_cas_table = _table_ref(
            settings.project_id, settings.dataset, settings.ac_cas_refs_by_cas_table
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_rebuild_by_cas_from_by_ac_query(settings),
                parameters=[],
            ).total_bytes_processed,
        )

    # ---- Two-phase CAS selection ----
    # Phase A: preselect by last_seen_at
    cas_preselect_table = _tmp_table_ref(settings, "cas_preselect", run_id=run_id)
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            f"""CREATE OR REPLACE TABLE {cas_preselect_table}
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
{build_cold_cas_preselect_query(
    settings,
    snapshot_time=snapshot_time,
    cas_cutoff_days=cas_cutoff_days,
    preselect_limit=settings.cleanup_cas_preselect_limit,
)}""",
            parameters=[],
        ).total_bytes_processed,
    )

    # Phase B: resolve live metadata (generation + size_bytes) for preselected CAS
    _populate_metadata_stage_table(
        settings=settings,
        stream_rows=stream_rows,
        resolve_object_metadata=resolve_object_metadata,
        load_jsonl_file=load_jsonl_file,
        source_table=cas_preselect_table,
        live_table=_tmp_table_ref(settings, "cas_live_metadata", run_id=run_id),
        missing_table=_tmp_table_ref(settings, "cas_missing_metadata", run_id=run_id),
        batch_size=settings.cleanup_batch_size,
    )

    # Reconcile missing CAS from last_seen_current so stale entries don't
    # occupy the preselect window on every run and block progress.
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            build_cleanup_gcs_cache_reconcile_missing_cas_query(
                settings,
                candidate_table=cas_preselect_table,
                missing_metadata_table=_tmp_table_ref(
                    settings, "cas_missing_metadata", run_id=run_id
                ),
            ),
            parameters=[],
        ).total_bytes_processed,
    )

    # Phase C: rank by size_bytes DESC, apply object + bytes cap
    cas_live_table = _tmp_table_ref(settings, "cas_live_metadata", run_id=run_id)
    cold_cas_table = _tmp_table_ref(settings, "cold_cas", run_id=run_id)

    # ponytail: apply caps in SQL with rolling sum; accept that window function
    # over the preselected set is enough, per-design preselect window trade-off.
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            f"""CREATE OR REPLACE TABLE {cold_cas_table}
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
WITH ranked AS (
  SELECT
    live.object_name,
    preselect.last_seen_at,
    live.size_bytes,
    live.generation,
    SUM(live.size_bytes) OVER (ORDER BY live.size_bytes DESC, preselect.last_seen_at ASC) AS running_bytes,
    ROW_NUMBER() OVER (ORDER BY live.size_bytes DESC, preselect.last_seen_at ASC) AS rn
  FROM {cas_preselect_table} AS preselect
  JOIN {cas_live_table} AS live
    USING (object_name)
)
SELECT object_name, last_seen_at, size_bytes, generation
FROM ranked
WHERE rn <= {settings.cleanup_max_delete_cas_objects}
  AND running_bytes <= {settings.cleanup_max_delete_cas_bytes}
""",
            parameters=[],
        ).total_bytes_processed,
    )

    cold_cas_count = _count_table_rows(execute=execute, table_ref=cold_cas_table)
    bytes_processed_total = _add_bytes(bytes_processed_total, cold_cas_count.bytes_processed)

    ready_cas_table = _tmp_table_ref(settings, "ready_cas", run_id=run_id)
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            f"""CREATE OR REPLACE TABLE {ready_cas_table}
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
{build_cas_ready_for_cascade_query(
    settings,
    cold_cas_table=cold_cas_table,
    snapshot_time=snapshot_time,
    ac_cutoff_days=ac_cutoff_days,
    by_cas_table_override=by_cas_table,
)}""",
            parameters=[],
        ).total_bytes_processed,
    )
    ready_cas_count = _count_table_rows(execute=execute, table_ref=ready_cas_table)
    bytes_processed_total = _add_bytes(bytes_processed_total, ready_cas_count.bytes_processed)

    # ---- AC reverse lookup + deletion (dry-run: compute only) ----
    if mode == "dry-run":
        ac_candidate_table = _tmp_table_ref(settings, "ac_to_delete", run_id=run_id)
        execute(
            f"""CREATE OR REPLACE TABLE {ac_candidate_table}
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
{build_ac_reverse_lookup_query(
    settings,
    cold_cas_table=ready_cas_table,
    snapshot_time=snapshot_time,
    ac_cutoff_days=ac_cutoff_days,
    by_cas_table_override=by_cas_table,
)}""",
            parameters=[],
        )
        ac_candidate_count = _count_table_rows(execute=execute, table_ref=ac_candidate_table)
        bytes_processed_total = _add_bytes(bytes_processed_total, ac_candidate_count.bytes_processed)
        candidate_ac_count = ac_candidate_count.object_count

        run_finished_at = now()
        return CleanupGcsCacheSummary(
            account_id=settings.project_id,
            bucket_name=settings.bucket_name,
            run_id=run_id,
            mode=mode,
            execute_kind="cas-from-index",
            dry_run=True,
            ac_retention_days=settings.ac_retention_days,
            cas_retention_days=settings.cas_retention_days,
            safety_buffer_days=settings.cleanup_safety_buffer_days,
            candidate_cas_object_count=cold_cas_count.object_count,
            candidate_ac_object_count=candidate_ac_count,
            candidate_cas_delete_object_count=ready_cas_count.object_count,
            ac_parse_error_count=0,
            selected_ac_object_count=0,
            selected_cas_object_count=0,
            oldest_last_seen_at=None,
            newest_last_seen_at=None,
            sample_candidates=(),
            sample_ac_parse_errors=(),
            bytes_processed=bytes_processed_total,
            run_started_at=run_started_at,
            run_finished_at=run_finished_at,
        )

    # ---- DELETE mode from here on ----
    ac_delete_result = _delete_acs_referenced_by_cold_cas(
        settings=settings,
        execute=execute,
        stream_rows=stream_rows,
        resolve_object_metadata=resolve_object_metadata,
        extract_references=None,
        load_jsonl_file=load_jsonl_file,
        create_batch_job=create_batch_job,
        wait_for_batch_job=wait_for_batch_job,
        cold_cas_table=ready_cas_table,
        snapshot_time=snapshot_time,
        ac_cutoff_days=ac_cutoff_days,
        run_id=run_id,
        run_started_at=run_started_at,
    )
    bytes_processed_total = _add_bytes(bytes_processed_total, 0)
    selected_ac_count = ac_delete_result.selected_ac_count

    # ---- Second incremental catch-up (design requirement) ----
    # Catch up again after AC deletion to pick up any new ACs created during
    # the delete phase, before we recompute zero-ref CAS.
    snapshot_time_2 = now()
    _run_catch_up_sync(settings=settings, until=snapshot_time_2, execute=execute)

    # ---- Second by_cas rebuild (after AC deletion + catch-up) ----
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            build_rebuild_by_cas_from_by_ac_query(settings),
            parameters=[],
        ).total_bytes_processed,
    )

    # ---- Final zero-ref CAS selection ----
    zero_ref_table = _tmp_table_ref(settings, "cas_zero_ref", run_id=run_id)
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            f"""CREATE OR REPLACE TABLE {zero_ref_table}
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
{build_zero_ref_cas_query(settings, cold_cas_table=ready_cas_table)}""",
            parameters=[],
        ).total_bytes_processed,
    )

    # ---- CAS delete with audit recheck ----
    cas_delete_table = _tmp_table_ref(settings, "delete_cas", run_id=run_id)
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            build_cas_audit_recheck_query(
                settings,
                candidate_table=cas_delete_table,
                zero_ref_table=zero_ref_table,
                live_metadata_table=_tmp_table_ref(settings, "cas_live_metadata", run_id=run_id),
                ttl_days=settings.cleanup_candidate_ttl_days,
            ),
            parameters=[],
        ).total_bytes_processed,
    )

    selected_cas_count = _count_table_rows(execute=execute, table_ref=cas_delete_table)
    bytes_processed_total = _add_bytes(bytes_processed_total, selected_cas_count.bytes_processed)

    if selected_cas_count.object_count <= 0:
        run_finished_at = now()
        return CleanupGcsCacheSummary(
            account_id=settings.project_id,
            bucket_name=settings.bucket_name,
            run_id=run_id,
            mode=mode,
            execute_kind="cas-from-index",
            dry_run=False,
            ac_retention_days=settings.ac_retention_days,
            cas_retention_days=settings.cas_retention_days,
            safety_buffer_days=settings.cleanup_safety_buffer_days,
            candidate_cas_object_count=cold_cas_count.object_count,
            candidate_ac_object_count=ac_delete_result.candidate_ac_count,
            candidate_cas_delete_object_count=0,
            ac_parse_error_count=0,
            selected_ac_object_count=selected_ac_count,
            selected_cas_object_count=0,
            oldest_last_seen_at=None,
            newest_last_seen_at=None,
            sample_candidates=(),
            sample_ac_parse_errors=(),
            bytes_processed=bytes_processed_total,
            run_started_at=run_started_at,
            run_finished_at=run_finished_at,
            ac_manifest_uri=ac_delete_result.manifest_uri or None,
            ac_batch_job_name=ac_delete_result.batch_job_name or None,
        )

    # ---- CAS manifest export + delete ----
    cas_manifest_uri = _manifest_uri(
        settings,
        object_kind="cas",
        run_started_at=run_started_at,
        run_id=run_id,
    )
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            build_cleanup_gcs_cache_manifest_export_query(
                candidate_table=cas_delete_table,
                manifest_uri=cas_manifest_uri,
                bucket_name=settings.bucket_name,
            ),
            parameters=[],
        ).total_bytes_processed,
    )
    cas_batch_job = create_batch_job(
        project_id=settings.project_id,
        job_id=_batch_job_id(
            object_kind="cas",
            run_started_at=run_started_at,
            run_id=run_id,
        ),
        bucket_name=settings.bucket_name,
        manifest_uri=cas_manifest_uri,
        dry_run=False,
        description=(
            f"Index-based CAS cleanup run {run_id} "
            f"(ac={settings.ac_retention_days}, cas={settings.cas_retention_days}, "
            f"buffer={settings.cleanup_safety_buffer_days})"
        ),
    )
    _wait_for_successful_batch_job(
        wait_for_batch_job=wait_for_batch_job, job_name=cas_batch_job.job_name
    )
    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            build_cleanup_gcs_cache_reconcile_deleted_cas_query(
                settings,
                candidate_table=cas_delete_table,
            ),
            parameters=[],
        ).total_bytes_processed,
    )

    run_finished_at = now()
    return CleanupGcsCacheSummary(
        account_id=settings.project_id,
        bucket_name=settings.bucket_name,
        run_id=run_id,
        mode=mode,
        execute_kind="cas-from-index",
        dry_run=False,
        ac_retention_days=settings.ac_retention_days,
        cas_retention_days=settings.cas_retention_days,
        safety_buffer_days=settings.cleanup_safety_buffer_days,
        candidate_cas_object_count=cold_cas_count.object_count,
        candidate_ac_object_count=ac_delete_result.candidate_ac_count,
        candidate_cas_delete_object_count=selected_cas_count.object_count,
        ac_parse_error_count=0,
        selected_ac_object_count=selected_ac_count,
        selected_cas_object_count=selected_cas_count.object_count,
        oldest_last_seen_at=None,
        newest_last_seen_at=None,
        sample_candidates=(),
        sample_ac_parse_errors=(),
        bytes_processed=bytes_processed_total,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
        ac_manifest_uri=ac_delete_result.manifest_uri or None,
        ac_batch_job_name=ac_delete_result.batch_job_name or None,
        cas_manifest_uri=cas_manifest_uri,
        cas_batch_job_name=cas_batch_job.job_name,
        cas_manifest_uris=(cas_manifest_uri,),
        cas_batch_job_names=(cas_batch_job.job_name,),
    )


def _reconcile_stale_ac_references(
    *,
    settings: GcsCacheSettings,
    execute: QueryExecutor,
    stream_rows: RowStreamer,
    resolve_object_metadata: MetadataResolver,
    load_jsonl_file: JsonFileLoader,
    run_id: str,
) -> None:
    """Find and remove ghost AC references from by_ac.

    ACs in by_ac that are absent from last_seen_current AND confirmed NotFound
    in GCS are removed from both by_ac and last_seen_current.
    """
    stale_candidates_table = _tmp_table_ref(settings, "stale_ac_candidates", run_id=run_id)
    execute(
        f"CREATE OR REPLACE TABLE {stale_candidates_table}\nOPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))\nAS\n"
        + build_stale_ac_candidates_query(settings),
        parameters=[],
    ).total_bytes_processed

    stale_count = _count_table_rows(execute=execute, table_ref=stale_candidates_table)
    if stale_count.object_count <= 0:
        return

    # Verify against GCS: only reconcile objects confirmed NotFound.
    _populate_metadata_stage_table(
        settings=settings,
        stream_rows=stream_rows,
        resolve_object_metadata=resolve_object_metadata,
        load_jsonl_file=load_jsonl_file,
        source_table=stale_candidates_table,
        live_table=_tmp_table_ref(settings, "stale_ac_live", run_id=run_id),
        missing_table=_tmp_table_ref(settings, "stale_ac_missing", run_id=run_id),
        batch_size=settings.cleanup_batch_size,
    )

    # Reconcile NotFound ACs from by_ac and last_seen_current.
    missing_table = _tmp_table_ref(settings, "stale_ac_missing", run_id=run_id)
    by_ac_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table
    )
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    execute(
        f"""DELETE FROM {by_ac_table}
WHERE ac_object_name IN (SELECT object_name FROM {missing_table})""",
        parameters=[],
    ).total_bytes_processed
    execute(
        f"""DELETE FROM {current_table}
WHERE object_kind = 'ac'
  AND object_name IN (SELECT object_name FROM {missing_table})""",
        parameters=[],
    ).total_bytes_processed


def _delete_acs_referenced_by_cold_cas(
    *,
    settings: GcsCacheSettings,
    execute: QueryExecutor,
    stream_rows: RowStreamer,
    resolve_object_metadata: MetadataResolver,
    extract_references: ReferenceExtractor | None,
    load_jsonl_file: JsonFileLoader,
    create_batch_job: BatchJobCreator,
    wait_for_batch_job: BatchJobWaiter,
    cold_cas_table: str,
    snapshot_time: str,
    ac_cutoff_days: int,
    run_id: str,
    run_started_at: datetime,
) -> CleanupGcsCacheAcDeleteResult:
    """Plan and execute AC deletion for ACs referenced by the bounded cold CAS set."""
    ac_candidate_table = _tmp_table_ref(settings, "ac_to_delete", run_id=run_id)
    execute(
        f"""CREATE OR REPLACE TABLE {ac_candidate_table}
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
{build_ac_reverse_lookup_query(
    settings,
    cold_cas_table=cold_cas_table,
    snapshot_time=snapshot_time,
    ac_cutoff_days=ac_cutoff_days,
)}""",
        parameters=[],
    ).total_bytes_processed

    ac_candidate_count = _count_table_rows(execute=execute, table_ref=ac_candidate_table)

    if ac_candidate_count.object_count <= 0:
        return CleanupGcsCacheAcDeleteResult(
            candidate_ac_count=0,
            selected_ac_count=0,
            manifest_uri="",
            batch_job_name="",
        )

    # Resolve live AC metadata
    _populate_metadata_stage_table(
        settings=settings,
        stream_rows=stream_rows,
        resolve_object_metadata=resolve_object_metadata,
        load_jsonl_file=load_jsonl_file,
        source_table=ac_candidate_table,
        live_table=_tmp_table_ref(settings, "ac_live_metadata", run_id=run_id),
        missing_table=_tmp_table_ref(settings, "ac_missing_metadata", run_id=run_id),
        batch_size=settings.cleanup_batch_size,
    )

    # Reconcile missing ACs from last_seen_current and by_ac so ghost refs
    # don't survive into the second by_cas rebuild.
    missing_ac_table = _tmp_table_ref(settings, "ac_missing_metadata", run_id=run_id)
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    by_ac_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table
    )
    execute(
        f"""DELETE FROM {current_table}
WHERE object_kind = 'ac'
  AND object_name IN (SELECT object_name FROM {missing_ac_table})
  AND last_seen_at < TIMESTAMP('{snapshot_time}')""",
        parameters=[],
    ).total_bytes_processed
    execute(
        f"""DELETE FROM {by_ac_table}
WHERE ac_object_name IN (SELECT object_name FROM {missing_ac_table})""",
        parameters=[],
    ).total_bytes_processed

    # Build final AC delete table (live metadata only)
    ac_delete_table = _tmp_table_ref(settings, "delete_ac", run_id=run_id)
    ac_live_table = _tmp_table_ref(settings, "ac_live_metadata", run_id=run_id)
    ac_missing_table = _tmp_table_ref(settings, "ac_missing_metadata", run_id=run_id)
    execute(
        f"""CREATE OR REPLACE TABLE {ac_delete_table}
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
AS
SELECT object_name, generation
FROM {ac_live_table} AS live
WHERE NOT EXISTS (
  SELECT 1 FROM {ac_missing_table} AS missing
  WHERE missing.object_name = live.object_name
)
ORDER BY object_name ASC""",
        parameters=[],
    ).total_bytes_processed

    selected_ac_count = _count_table_rows(execute=execute, table_ref=ac_delete_table)

    if selected_ac_count.object_count <= 0:
        return CleanupGcsCacheAcDeleteResult(
            candidate_ac_count=ac_candidate_count.object_count,
            selected_ac_count=0,
            manifest_uri="",
            batch_job_name="",
        )

    # Export AC manifest + delete
    ac_manifest_uri = _manifest_uri(
        settings,
        object_kind="ac",
        run_started_at=run_started_at,
        run_id=run_id,
    )
    execute(
        build_cleanup_gcs_cache_manifest_export_query(
            candidate_table=ac_delete_table,
            manifest_uri=ac_manifest_uri,
            bucket_name=settings.bucket_name,
        ),
        parameters=[],
    ).total_bytes_processed

    ac_batch_job = create_batch_job(
        project_id=settings.project_id,
        job_id=_batch_job_id(
            object_kind="ac",
            run_started_at=run_started_at,
            run_id=run_id,
        ),
        bucket_name=settings.bucket_name,
        manifest_uri=ac_manifest_uri,
        dry_run=False,
        description=(
            f"Index-based AC cleanup run {run_id} "
            f"(ac={settings.ac_retention_days})"
        ),
    )
    _wait_for_successful_batch_job(
        wait_for_batch_job=wait_for_batch_job, job_name=ac_batch_job.job_name
    )

    # Reconcile deleted ACs from last_seen_current + by_ac
    execute(
        build_cleanup_gcs_cache_reconcile_deleted_ac_query(
            settings,
            candidate_table=ac_delete_table,
        ),
        parameters=[BigQueryParameter("run_started_at", "TIMESTAMP", run_started_at)],
    ).total_bytes_processed
    execute(
        f"""DELETE FROM {_table_ref(settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table)}
WHERE ac_object_name IN (SELECT object_name FROM {ac_delete_table})""",
        parameters=[],
    ).total_bytes_processed

    return CleanupGcsCacheAcDeleteResult(
        candidate_ac_count=ac_candidate_count.object_count,
        selected_ac_count=selected_ac_count.object_count,
        manifest_uri=ac_manifest_uri,
        batch_job_name=ac_batch_job.job_name,
    )


def _run_catch_up_sync(
    *,
    settings: GcsCacheSettings,
    until: datetime,
    execute: QueryExecutor,
) -> None:
    """Run incremental sync for all 256 shards up to `until`."""
    run_sync_gcs_cache_ac_references(
        settings=settings,
        mode="incremental",
        shard_start=0,
        shard_end=settings.ac_reference_shard_count - 1,
        dry_run=False,
        execute=execute,
        now=lambda: until,
    )


def _tmp_table_ref(settings: GcsCacheSettings, suffix: str, *, run_id: str = "") -> str:
    rid = f"_{run_id}" if run_id else ""
    return _table_ref(settings.project_id, settings.dataset, f"_tmp_{suffix}{rid}")


def _populate_metadata_stage_table(
    *,
    settings: GcsCacheSettings,
    stream_rows: RowStreamer,
    resolve_object_metadata: MetadataResolver,
    load_jsonl_file: JsonFileLoader,
    source_table: str,
    live_table: str,
    missing_table: str,
    batch_size: int,
) -> None:
    rows = stream_rows(
        f"SELECT object_name FROM {source_table} ORDER BY object_name ASC",
        [],
    )
    live_schema = (("object_name", "STRING"), ("generation", "INT64"), ("size_bytes", "INT64"))
    missing_schema = (("object_name", "STRING"),)

    with TemporaryDirectory(prefix="cleanup-gcs-cache-stage-") as temp_dir:
        with ExitStack() as stack:
            live_path = Path(temp_dir) / "live.jsonl"
            missing_path = Path(temp_dir) / "missing.jsonl"
            live_handle = stack.enter_context(live_path.open("w", encoding="utf-8"))
            missing_handle = stack.enter_context(missing_path.open("w", encoding="utf-8"))
            live_count = 0
            missing_count = 0

            for batch in _batched(rows, batch_size):
                object_names = tuple(str(row["object_name"]) for row in batch)
                metadata_rows = resolve_object_metadata(
                    project_id=settings.project_id,
                    bucket_name=settings.bucket_name,
                    object_names=object_names,
                    max_workers=settings.ac_reference_download_workers,
                )
                live_rows = [
                    {
                        "object_name": item.object_name,
                        "generation": item.generation,
                        "size_bytes": item.size_bytes,
                    }
                    for item in metadata_rows
                    if item.exists and item.generation is not None
                ]
                missing_rows = [
                    {"object_name": item.object_name} for item in metadata_rows if not item.exists
                ]
                if live_rows:
                    for row in live_rows:
                        live_handle.write(json.dumps(row, separators=(",", ":")))
                        live_handle.write("\n")
                    live_count += len(live_rows)
                if missing_rows:
                    for row in missing_rows:
                        missing_handle.write(json.dumps(row, separators=(",", ":")))
                        missing_handle.write("\n")
                    missing_count += len(missing_rows)

            live_handle.flush()
            missing_handle.flush()

        if live_count:
            load_jsonl_file(live_table, live_path, live_schema, "WRITE_APPEND")
        if missing_count:
            load_jsonl_file(missing_table, missing_path, missing_schema, "WRITE_APPEND")


def _populate_ac_stage_tables(
    *,
    settings: GcsCacheSettings,
    stream_rows: RowStreamer,
    resolve_object_metadata: MetadataResolver,
    extract_references: ReferenceExtractor,
    load_jsonl_file: JsonFileLoader,
    source_table: str,
    live_table: str,
    missing_table: str,
    references_table: str,
    stream_batch_size: int,
    reference_batch_size: int,
) -> AcStageResult:
    parse_error_count = 0
    sample_parse_errors: list[CleanupGcsCacheParseErrorSample] = []
    seeded_object_count = 0
    last_seed_cursor: AcSeedCursor | None = None
    rows = stream_rows(
        f"SELECT object_name, last_seen_at FROM {source_table} ORDER BY last_seen_at ASC, object_name ASC",
        [],
    )
    live_schema = (("object_name", "STRING"), ("generation", "INT64"), ("size_bytes", "INT64"))
    references_schema = (("ac_object_name", "STRING"), ("cas_object_name", "STRING"))
    missing_schema = (("object_name", "STRING"),)

    with TemporaryDirectory(prefix="cleanup-gcs-cache-ac-stage-") as temp_dir:
        with ExitStack() as stack:
            live_path = Path(temp_dir) / "ac-live.jsonl"
            references_path = Path(temp_dir) / "ac-references.jsonl"
            missing_path = Path(temp_dir) / "ac-missing.jsonl"
            live_handle = stack.enter_context(live_path.open("w", encoding="utf-8"))
            references_handle = stack.enter_context(references_path.open("w", encoding="utf-8"))
            missing_handle = stack.enter_context(missing_path.open("w", encoding="utf-8"))
            live_count = 0
            references_count = 0
            missing_count = 0

            for batch in _batched(rows, stream_batch_size):
                object_names = tuple(str(row["object_name"]) for row in batch)
                seeded_object_count += len(object_names)
                last_row = batch[-1]
                last_seed_cursor = AcSeedCursor(
                    last_seen_at=coerce_datetime(last_row["last_seen_at"]),
                    object_name=str(last_row["object_name"]),
                )
                metadata_rows = resolve_object_metadata(
                    project_id=settings.project_id,
                    bucket_name=settings.bucket_name,
                    object_names=object_names,
                    max_workers=settings.ac_reference_download_workers,
                )
                live_rows = [
                    {
                        "object_name": item.object_name,
                        "generation": item.generation,
                        "size_bytes": item.size_bytes,
                    }
                    for item in metadata_rows
                    if item.exists and item.generation is not None
                ]
                live_object_names = tuple(row["object_name"] for row in live_rows)
                extractions = tuple(
                    extraction
                    for reference_batch in _batched_values(live_object_names, reference_batch_size)
                    for extraction in extract_references(
                        project_id=settings.project_id,
                        bucket_name=settings.bucket_name,
                        ac_object_names=reference_batch,
                        max_workers=settings.ac_reference_download_workers,
                    )
                )
                parse_failed_names = {
                    extraction.ac_object_name
                    for extraction in extractions
                    if extraction.parse_error
                }
                parse_error_count += len(parse_failed_names)
                for extraction in extractions:
                    if extraction.parse_error and len(sample_parse_errors) < 10:
                        sample_parse_errors.append(
                            CleanupGcsCacheParseErrorSample(
                                object_name=extraction.ac_object_name,
                                parse_error=extraction.parse_error,
                            )
                        )
                deletable_live_rows = [
                    row for row in live_rows if row["object_name"] not in parse_failed_names
                ]
                reference_rows = [
                    {
                        "ac_object_name": extraction.ac_object_name,
                        "cas_object_name": cas_object_name,
                    }
                    for extraction in extractions
                    if extraction.exists and extraction.parse_error is None
                    for cas_object_name in extraction.cas_object_names
                ]
                missing_names = {item.object_name for item in metadata_rows if not item.exists} | {
                    extraction.ac_object_name for extraction in extractions if not extraction.exists
                }
                missing_rows = [
                    {"object_name": object_name} for object_name in sorted(missing_names)
                ]
                if deletable_live_rows:
                    for row in deletable_live_rows:
                        live_handle.write(json.dumps(row, separators=(",", ":")))
                        live_handle.write("\n")
                    live_count += len(deletable_live_rows)
                if reference_rows:
                    for row in reference_rows:
                        references_handle.write(json.dumps(row, separators=(",", ":")))
                        references_handle.write("\n")
                    references_count += len(reference_rows)
                if missing_rows:
                    for row in missing_rows:
                        missing_handle.write(json.dumps(row, separators=(",", ":")))
                        missing_handle.write("\n")
                    missing_count += len(missing_rows)

            live_handle.flush()
            references_handle.flush()
            missing_handle.flush()

        if live_count:
            load_jsonl_file(live_table, live_path, live_schema, "WRITE_APPEND")
        if references_count:
            load_jsonl_file(references_table, references_path, references_schema, "WRITE_APPEND")
        if missing_count:
            load_jsonl_file(missing_table, missing_path, missing_schema, "WRITE_APPEND")
    return AcStageResult(
        parse_error_count=parse_error_count,
        sample_parse_errors=tuple(sample_parse_errors),
        seeded_object_count=seeded_object_count,
        last_seed_cursor=last_seed_cursor,
    )


def _count_table_rows(*, execute: QueryExecutor, table_ref: str) -> TableCountResult:
    result = execute(
        f"SELECT COUNT(*) AS object_count FROM {table_ref}",
        parameters=[],
    )
    row = result.rows[0] if result.rows else {}
    return TableCountResult(
        object_count=int(row.get("object_count", 0) or 0),
        bytes_processed=result.total_bytes_processed,
    )


def _count_distinct_run_cas_rows(*, execute: QueryExecutor, table_ref: str) -> TableCountResult:
    result = execute(
        f"SELECT COUNT(DISTINCT cas_object_name) AS object_count FROM {table_ref}",
        parameters=[],
    )
    row = result.rows[0] if result.rows else {}
    return TableCountResult(
        object_count=int(row.get("object_count", 0) or 0),
        bytes_processed=result.total_bytes_processed,
    )


def _wait_for_successful_batch_job(
    *,
    wait_for_batch_job: BatchJobWaiter,
    job_name: str,
) -> None:
    batch_status = wait_for_batch_job(job_name=job_name)
    if batch_status.state != "SUCCEEDED":
        raise RuntimeError(
            f"Storage Batch Operations job did not succeed for {job_name}: "
            f"state={batch_status.state}"
        )
    if batch_status.failed_object_count > 0:
        raise RuntimeError(
            f"Storage Batch Operations job reported failed objects for {job_name}: "
            f"{batch_status.failed_object_count}"
        )


def _stream_query_rows(
    query: str,
    parameters: Sequence[BigQueryParameter],
) -> Iterator[dict[str, object]]:
    from google.cloud import bigquery

    client = bigquery.Client()
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(param.name, param.type_, param.value)
            for param in parameters
        ]
    )
    job = client.query(query, job_config=job_config)
    for row in job.result():
        yield dict(row.items())


def _candidate_table_name(settings: GcsCacheSettings, *, prefix: str, run_id: str) -> str:
    suffix = run_id.replace("-", "")
    return _table_ref(settings.project_id, settings.dataset, f"_tmp_gcs_cache_{prefix}_{suffix}")


def _batch_candidate_table_name(
    settings: GcsCacheSettings,
    *,
    prefix: str,
    run_id: str,
    batch_index: int,
) -> str:
    return _candidate_table_name(
        settings,
        prefix=f"{prefix}_b{batch_index:04d}",
        run_id=run_id,
    )


def _manifest_uri(
    settings: GcsCacheSettings,
    *,
    object_kind: str,
    run_started_at: datetime,
    run_id: str,
    batch_index: int | None = None,
) -> str:
    date_prefix = run_started_at.strftime("%Y-%m-%d")
    batch_path = "" if batch_index is None else f"/batch-{batch_index:04d}"
    return (
        f"gs://{settings.cleanup_manifest_bucket}/"
        f"{settings.cleanup_manifest_prefix}/{date_prefix}/{object_kind}/{run_id}"
        f"{batch_path}/manifest-*.csv"
    )


def _batch_job_id(
    *,
    object_kind: str,
    run_started_at: datetime,
    run_id: str,
    batch_index: int | None = None,
) -> str:
    timestamp = run_started_at.strftime("%Y%m%dt%H%M%S")
    suffix = run_id.replace("-", "")[:12]
    batch_suffix = "" if batch_index is None else f"-b{batch_index:04d}"
    return f"gcs-cache-cleanup-{object_kind}-{timestamp}-{suffix}{batch_suffix}".lower()


def _create_table_query(
    table_ref: str,
    *,
    columns: Sequence[tuple[str, str]],
    ttl_days: int,
) -> str:
    rendered_columns = ",\n  ".join(f"{name} {field_type}" for name, field_type in columns)
    return f"""
CREATE OR REPLACE TABLE {table_ref} (
  {rendered_columns}
)
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
)
""".strip()


def _batched(
    values: Iterator[dict[str, object]], batch_size: int
) -> Iterator[tuple[dict[str, object], ...]]:
    while True:
        batch = tuple(islice(values, batch_size))
        if not batch:
            return
        yield batch


def _batched_values(values: Iterable[str], batch_size: int) -> Iterator[tuple[str, ...]]:
    iterator = iter(values)
    while True:
        batch = tuple(islice(iterator, batch_size))
        if not batch:
            return
        yield batch


def _table_ref(project_id: str, dataset: str, table: str) -> str:
    return f"`{project_id}.{dataset}.{table}`"


def _ignored_cleanup_get_filter(settings: GcsCacheSettings) -> str:
    principal_email = (settings.last_seen_excluded_get_principal_email or "").strip()
    if not principal_email:
        return ""
    return f"""
    AND NOT (
      audit.protopayload_auditlog.methodName = 'storage.objects.get'
      AND audit.protopayload_auditlog.authenticationInfo.principalEmail = {_sql_string_literal(principal_email)}
    )""".rstrip()


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _add_bytes(total: int, value: int | None) -> int:
    if value is None:
        return total
    return total + int(value)


def _load_jsonl_file(
    table_ref: str,
    jsonl_path: Path,
    schema: Sequence[tuple[str, str]],
    write_disposition: str,
) -> None:
    """Load a staged JSONL file into BigQuery and retain table/file context on failure."""
    from google.cloud import bigquery

    client = bigquery.Client()
    try:
        with jsonl_path.open("rb") as handle:
            job = client.load_table_from_file(
                handle,
                table_ref.strip("`"),
                job_config=bigquery.LoadJobConfig(
                    schema=[bigquery.SchemaField(name, field_type) for name, field_type in schema],
                    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                    write_disposition=write_disposition,
                ),
            )
        job.result()
    except Exception as exc:  # pragma: no cover - exercised via injected loader tests
        raise RuntimeError(
            f"Failed to load staged cleanup metadata file {jsonl_path} into {table_ref}"
        ) from exc


def _validate_mode_and_execute_kind(*, mode: str, execute_kind: str) -> None:
    if mode not in {"dry-run", "delete"}:
        raise ValueError(f"Unsupported cleanup mode: {mode}")
    if execute_kind not in VALID_EXECUTE_KINDS:
        raise ValueError(
            f"Unsupported cleanup execute kind: {execute_kind} (expected one of {VALID_EXECUTE_KINDS})"
        )
    if mode == "delete" and execute_kind not in {"cas", "cas-from-index"}:
        raise ValueError(
            "cleanup-gcs-cache --mode delete requires --execute-kind cas or cas-from-index"
        )
