from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from uuid import uuid4

from cost_insight.common.bigquery import BigQueryParameter, BigQueryQueryResult, execute_query
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.datetime_utils import coerce_datetime, coerce_optional_datetime
from cost_insight.common.gcs_cache_references import (
    AcReferenceExtraction,
    extract_action_cache_references_batch,
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
JsonLoader = Callable[[str, Sequence[dict[str, object]], Sequence[tuple[str, str]], str], None]
BatchJobCreator = Callable[..., StorageBatchOperationsJob]
BatchJobWaiter = Callable[..., StorageBatchOperationsJobStatus]

# "all" is kept as a compatibility alias for older dry-run CronJob arguments.
# v3 cleanup has only one real execution path: AC-driven CAS cascade.
VALID_EXECUTE_KINDS = ("all", "cas")


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
    ac_manifest_uri: str | None = None
    ac_batch_job_name: str | None = None
    cas_manifest_uri: str | None = None
    cas_batch_job_name: str | None = None


@dataclass(frozen=True)
class TableCountResult:
    object_count: int
    bytes_processed: int | None


@dataclass(frozen=True)
class AcStageResult:
    parse_error_count: int
    sample_parse_errors: tuple[CleanupGcsCacheParseErrorSample, ...]


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
    load_json_rows: JsonLoader | None = None,
    create_batch_job: BatchJobCreator = create_delete_job,
    wait_for_batch_job: BatchJobWaiter = wait_for_delete_job,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid4()),
) -> CleanupGcsCacheSummary:
    _validate_mode_and_execute_kind(mode=mode, execute_kind=execute_kind)

    stream_rows = _stream_query_rows if stream_rows is None else stream_rows
    load_json_rows = _load_json_rows if load_json_rows is None else load_json_rows

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
    ac_candidate_table = _candidate_table_name(settings, prefix="candidate_ac", run_id=run_id)
    run_references_table = _candidate_table_name(settings, prefix="run_ac_cas_refs", run_id=run_id)
    cas_candidate_table = _candidate_table_name(settings, prefix="candidate_cas", run_id=run_id)
    ac_live_metadata_table = _candidate_table_name(
        settings, prefix="ac_live_metadata", run_id=run_id
    )
    ac_missing_metadata_table = _candidate_table_name(
        settings, prefix="ac_missing_metadata", run_id=run_id
    )
    cas_live_metadata_table = _candidate_table_name(
        settings, prefix="cas_live_metadata", run_id=run_id
    )
    cas_missing_metadata_table = _candidate_table_name(
        settings, prefix="cas_missing_metadata", run_id=run_id
    )
    ac_delete_table = _candidate_table_name(settings, prefix="delete_ac", run_id=run_id)
    cas_delete_table = _candidate_table_name(settings, prefix="delete_cas", run_id=run_id)

    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(
            build_cleanup_gcs_cache_ac_seed_table_query(
                settings,
                candidate_table=ac_candidate_table,
                ttl_days=ttl_days,
            ),
            parameters=[
                BigQueryParameter("ac_cutoff_days", "INT64", ac_cutoff_days),
                BigQueryParameter("limit", "INT64", resolved_max_delete_objects),
            ],
        ).total_bytes_processed,
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
    ac_stage_result = _populate_ac_stage_tables(
        settings=settings,
        stream_rows=stream_rows,
        resolve_object_metadata=resolve_object_metadata,
        extract_references=extract_references,
        source_table=ac_candidate_table,
        live_table=ac_live_metadata_table,
        missing_table=ac_missing_metadata_table,
        references_table=run_references_table,
        load_json_rows=load_json_rows,
        stream_batch_size=settings.cleanup_batch_size,
        reference_batch_size=settings.ac_reference_batch_size,
    )
    cas_from_ac_count = _count_distinct_run_cas_rows(
        execute=execute,
        table_ref=run_references_table,
    )
    candidate_cas_object_count = cas_from_ac_count.object_count
    bytes_processed_total = _add_bytes(bytes_processed_total, cas_from_ac_count.bytes_processed)

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

    selected_ac_count = _count_table_rows(execute=execute, table_ref=ac_delete_table)
    selected_ac_object_count = selected_ac_count.object_count
    bytes_processed_total = _add_bytes(bytes_processed_total, selected_ac_count.bytes_processed)
    selected_cas_object_count = 0

    ac_manifest_uri = None
    ac_batch_job_name = None
    cas_manifest_uri = None
    cas_batch_job_name = None

    if selected_ac_object_count > 0:
        ac_manifest_uri = _manifest_uri(
            settings,
            object_kind="ac",
            run_started_at=run_started_at,
            run_id=run_id,
        )
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
            job_id=_batch_job_id(object_kind="ac", run_started_at=run_started_at, run_id=run_id),
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
                BigQueryParameter("limit", "INT64", resolved_max_delete_cas_objects),
            ],
        ).total_bytes_processed,
    )
    _populate_metadata_stage_table(
        settings=settings,
        stream_rows=stream_rows,
        resolve_object_metadata=resolve_object_metadata,
        load_json_rows=load_json_rows,
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
    selected_cas_object_count = selected_cas_count.object_count
    bytes_processed_total = _add_bytes(bytes_processed_total, selected_cas_count.bytes_processed)
    candidate_cas_delete_object_count = selected_cas_object_count

    if selected_cas_object_count > 0:
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
            job_id=_batch_job_id(object_kind="cas", run_started_at=run_started_at, run_id=run_id),
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
        ac_parse_error_count=ac_stage_result.parse_error_count,
        selected_ac_object_count=selected_ac_object_count,
        selected_cas_object_count=selected_cas_object_count,
        oldest_last_seen_at=oldest_last_seen_at,
        newest_last_seen_at=newest_last_seen_at,
        sample_candidates=(),
        sample_ac_parse_errors=ac_stage_result.sample_parse_errors,
        bytes_processed=bytes_processed_total,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
        ac_manifest_uri=ac_manifest_uri,
        ac_batch_job_name=ac_batch_job_name,
        cas_manifest_uri=cas_manifest_uri,
        cas_batch_job_name=cas_batch_job_name,
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
) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
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
  AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_cutoff_days DAY)
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
    current.object_name,
    current.last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), current.last_seen_at, DAY) AS idle_days
  FROM cas_from_deleted_ac AS candidate
  JOIN {current_table} AS current
    ON current.object_name = candidate.cas_object_name
   AND current.object_kind = 'cas'
   AND current.last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_cutoff_days DAY)
)
SELECT
  cas.object_name,
  cas.last_seen_at,
  cas.idle_days
FROM cas_after_extra_idle AS cas
ORDER BY cas.last_seen_at ASC, cas.object_name ASC
LIMIT @limit
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
                columns=(("object_name", "STRING"), ("generation", "INT64")),
                ttl_days=ttl_days,
            ),
            _create_table_query(
                ac_missing_metadata_table,
                columns=(("object_name", "STRING"),),
                ttl_days=ttl_days,
            ),
            _create_table_query(
                cas_live_metadata_table,
                columns=(("object_name", "STRING"), ("generation", "INT64")),
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
JOIN {current_table} AS current
  ON current.object_name = source.object_name
 AND current.object_kind = 'cas'
 AND current.last_seen_at = source.last_seen_at
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


def _populate_metadata_stage_table(
    *,
    settings: GcsCacheSettings,
    stream_rows: RowStreamer,
    resolve_object_metadata: MetadataResolver,
    load_json_rows: JsonLoader,
    source_table: str,
    live_table: str,
    missing_table: str,
    batch_size: int,
) -> None:
    rows = stream_rows(
        f"SELECT object_name FROM {source_table} ORDER BY object_name ASC",
        [],
    )
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
            }
            for item in metadata_rows
            if item.exists and item.generation is not None
        ]
        missing_rows = [
            {"object_name": item.object_name} for item in metadata_rows if not item.exists
        ]
        if live_rows:
            load_json_rows(
                live_table,
                live_rows,
                (("object_name", "STRING"), ("generation", "INT64")),
                "WRITE_APPEND",
            )
        if missing_rows:
            load_json_rows(
                missing_table,
                missing_rows,
                (("object_name", "STRING"),),
                "WRITE_APPEND",
            )


def _populate_ac_stage_tables(
    *,
    settings: GcsCacheSettings,
    stream_rows: RowStreamer,
    resolve_object_metadata: MetadataResolver,
    extract_references: ReferenceExtractor,
    source_table: str,
    live_table: str,
    missing_table: str,
    references_table: str,
    load_json_rows: JsonLoader,
    stream_batch_size: int,
    reference_batch_size: int,
) -> AcStageResult:
    parse_error_count = 0
    sample_parse_errors: list[CleanupGcsCacheParseErrorSample] = []
    rows = stream_rows(
        f"SELECT object_name FROM {source_table} ORDER BY object_name ASC",
        [],
    )
    for batch in _batched(rows, stream_batch_size):
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
            extraction.ac_object_name for extraction in extractions if extraction.parse_error
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
        missing_rows = [{"object_name": object_name} for object_name in sorted(missing_names)]
        if deletable_live_rows:
            load_json_rows(
                live_table,
                deletable_live_rows,
                (("object_name", "STRING"), ("generation", "INT64")),
                "WRITE_APPEND",
            )
        if reference_rows:
            load_json_rows(
                references_table,
                reference_rows,
                (("ac_object_name", "STRING"), ("cas_object_name", "STRING")),
                "WRITE_APPEND",
            )
        if missing_rows:
            load_json_rows(
                missing_table,
                missing_rows,
                (("object_name", "STRING"),),
                "WRITE_APPEND",
            )
    return AcStageResult(
        parse_error_count=parse_error_count,
        sample_parse_errors=tuple(sample_parse_errors),
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


def _load_json_rows(
    table_ref: str,
    rows: Sequence[dict[str, object]],
    schema: Sequence[tuple[str, str]],
    write_disposition: str,
) -> None:
    from google.cloud import bigquery

    client = bigquery.Client()
    if not rows:
        return
    job = client.load_table_from_json(
        list(rows),
        table_ref.strip("`"),
        job_config=bigquery.LoadJobConfig(
            schema=[bigquery.SchemaField(name, field_type) for name, field_type in schema],
            write_disposition=write_disposition,
        ),
    )
    job.result()


def _candidate_table_name(settings: GcsCacheSettings, *, prefix: str, run_id: str) -> str:
    suffix = run_id.replace("-", "")
    return _table_ref(settings.project_id, settings.dataset, f"_tmp_gcs_cache_{prefix}_{suffix}")


def _manifest_uri(
    settings: GcsCacheSettings,
    *,
    object_kind: str,
    run_started_at: datetime,
    run_id: str,
) -> str:
    date_prefix = run_started_at.strftime("%Y-%m-%d")
    return (
        f"gs://{settings.cleanup_manifest_bucket}/"
        f"{settings.cleanup_manifest_prefix}/{date_prefix}/{object_kind}/{run_id}/manifest-*.csv"
    )


def _batch_job_id(*, object_kind: str, run_started_at: datetime, run_id: str) -> str:
    timestamp = run_started_at.strftime("%Y%m%dt%H%M%S")
    suffix = run_id.replace("-", "")[:12]
    return f"gcs-cache-cleanup-{object_kind}-{timestamp}-{suffix}".lower()


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


def _add_bytes(total: int, value: int | None) -> int:
    if value is None:
        return total
    return total + int(value)


def _validate_mode_and_execute_kind(*, mode: str, execute_kind: str) -> None:
    if mode not in {"dry-run", "delete"}:
        raise ValueError(f"Unsupported cleanup mode: {mode}")
    if execute_kind not in VALID_EXECUTE_KINDS:
        raise ValueError(
            f"Unsupported cleanup execute kind: {execute_kind} (expected one of {VALID_EXECUTE_KINDS})"
        )
    if mode == "delete" and execute_kind != "cas":
        raise ValueError("cleanup-gcs-cache --mode delete requires --execute-kind cas")
