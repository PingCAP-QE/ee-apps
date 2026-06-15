from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from cost_insight.common.bigquery import BigQueryParameter, BigQueryQueryResult, execute_query
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.storage_batch_operations import (
    StorageBatchOperationsJob,
    StorageBatchOperationsJobStatus,
    create_delete_job,
    wait_for_delete_job,
)

QueryExecutor = Callable[[str, list[BigQueryParameter]], BigQueryQueryResult]
BatchJobCreator = Callable[..., StorageBatchOperationsJob]
BatchJobWaiter = Callable[..., StorageBatchOperationsJobStatus]

VALID_EXECUTE_KINDS = ("all", "ac", "cas", "mixed-canary")


@dataclass(frozen=True)
class CleanupGcsCacheSample:
    object_name: str
    object_kind: str
    last_seen_at: datetime
    idle_days: int


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
    candidate_object_count: int
    selected_object_count: int
    ac_candidate_count: int
    cas_candidate_count: int
    oldest_last_seen_at: datetime | None
    newest_last_seen_at: datetime | None
    sample_candidates: tuple[CleanupGcsCacheSample, ...]
    bytes_processed: int | None
    run_started_at: datetime
    run_finished_at: datetime
    manifest_uri: str | None = None
    batch_job_name: str | None = None


def run_cleanup_gcs_cache(
    *,
    settings: GcsCacheSettings,
    mode: str = "dry-run",
    execute_kind: str = "all",
    ac_retention_days: int | None = None,
    cas_retention_days: int | None = None,
    safety_buffer_days: int | None = None,
    max_delete_objects: int | None = None,
    sample_limit: int | None = None,
    execute: QueryExecutor = execute_query,
    create_batch_job: BatchJobCreator = create_delete_job,
    wait_for_batch_job: BatchJobWaiter = wait_for_delete_job,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id_factory: Callable[[], str] = lambda: str(uuid4()),
) -> CleanupGcsCacheSummary:
    run_started_at = now()
    _validate_mode_and_execute_kind(mode=mode, execute_kind=execute_kind)

    resolved_ac_days = ac_retention_days or settings.ac_retention_days
    resolved_cas_days = cas_retention_days or settings.cas_retention_days
    resolved_safety_buffer_days = safety_buffer_days or settings.cleanup_safety_buffer_days
    resolved_sample_limit = sample_limit or settings.cleanup_sample_limit
    resolved_max_delete_objects = max_delete_objects or settings.cleanup_max_delete_objects
    ac_cutoff_days = resolved_ac_days + resolved_safety_buffer_days
    cas_cutoff_days = resolved_cas_days + resolved_safety_buffer_days

    bytes_processed_total = 0
    summary_parameters = _summary_query_parameters(
        execute_kind=execute_kind,
        ac_cutoff_days=ac_cutoff_days,
        cas_cutoff_days=cas_cutoff_days,
        sample_limit=resolved_sample_limit,
    )
    summary_result = execute(
        build_cleanup_gcs_cache_summary_query(settings, execute_kind=execute_kind),
        parameters=summary_parameters,
    )
    bytes_processed_total = _add_bytes(bytes_processed_total, summary_result.total_bytes_processed)
    row = summary_result.rows[0] if summary_result.rows else {}

    sample_candidates = tuple(
        CleanupGcsCacheSample(
            object_name=str(item["object_name"]),
            object_kind=str(item["object_kind"]),
            last_seen_at=_coerce_datetime(item["last_seen_at"]),
            idle_days=int(item["idle_days"]),
        )
        for item in (row.get("sample_candidates") or [])
    )

    candidate_object_count = int(row.get("candidate_object_count", 0) or 0)
    ac_candidate_count = int(row.get("ac_candidate_count", 0) or 0)
    cas_candidate_count = int(row.get("cas_candidate_count", 0) or 0)
    oldest_last_seen_at = _coerce_optional_datetime(row.get("oldest_last_seen_at"))
    newest_last_seen_at = _coerce_optional_datetime(row.get("newest_last_seen_at"))

    if mode == "dry-run":
        run_finished_at = now()
        return CleanupGcsCacheSummary(
            account_id=settings.project_id,
            bucket_name=settings.bucket_name,
            run_id=run_id_factory(),
            mode=mode,
            execute_kind=execute_kind,
            dry_run=True,
            ac_retention_days=resolved_ac_days,
            cas_retention_days=resolved_cas_days,
            safety_buffer_days=resolved_safety_buffer_days,
            candidate_object_count=candidate_object_count,
            selected_object_count=0,
            ac_candidate_count=ac_candidate_count,
            cas_candidate_count=cas_candidate_count,
            oldest_last_seen_at=oldest_last_seen_at,
            newest_last_seen_at=newest_last_seen_at,
            sample_candidates=sample_candidates,
            bytes_processed=bytes_processed_total,
            run_started_at=run_started_at,
            run_finished_at=run_finished_at,
        )

    run_id = run_id_factory()
    candidate_table = _candidate_table_name(settings, execute_kind=execute_kind, run_id=run_id)
    limit = _selected_limit(
        execute_kind=execute_kind,
        candidate_object_count=candidate_object_count,
        ac_candidate_count=ac_candidate_count,
        cas_candidate_count=cas_candidate_count,
        max_delete_objects=resolved_max_delete_objects,
    )
    create_candidate_result = execute(
        build_cleanup_gcs_cache_candidate_table_query(
            settings,
            execute_kind=execute_kind,
            candidate_table=candidate_table,
        ),
        parameters=_candidate_table_query_parameters(
            execute_kind=execute_kind,
            ac_cutoff_days=ac_cutoff_days,
            cas_cutoff_days=cas_cutoff_days,
            run_id=run_id,
            limit=limit,
            ttl_days=settings.cleanup_candidate_ttl_days,
        ),
    )
    bytes_processed_total = _add_bytes(
        bytes_processed_total, create_candidate_result.total_bytes_processed
    )

    count_result = execute(
        f"SELECT COUNT(*) AS selected_object_count FROM {candidate_table}",
        parameters=[],
    )
    bytes_processed_total = _add_bytes(bytes_processed_total, count_result.total_bytes_processed)
    selected_object_count = int(
        (count_result.rows[0] if count_result.rows else {}).get("selected_object_count", 0) or 0
    )

    manifest_uri = None
    batch_job_name = None
    if selected_object_count > 0:
        manifest_uri = _manifest_uri(
            settings,
            execute_kind=execute_kind,
            run_started_at=run_started_at,
            run_id=run_id,
        )
        export_result = execute(
            build_cleanup_gcs_cache_manifest_export_query(
                candidate_table=candidate_table,
                manifest_uri=manifest_uri,
                bucket_name=settings.bucket_name,
            ),
            parameters=[],
        )
        bytes_processed_total = _add_bytes(bytes_processed_total, export_result.total_bytes_processed)

        batch_job = create_batch_job(
            project_id=settings.project_id,
            job_id=_batch_job_id(execute_kind=execute_kind, run_started_at=run_started_at, run_id=run_id),
            bucket_name=settings.bucket_name,
            manifest_uri=manifest_uri,
            dry_run=False,
            description=(
                "Steady-state GCS cache cleanup "
                f"{execute_kind} run {run_id} "
                f"(ac={resolved_ac_days}, cas={resolved_cas_days}, buffer={resolved_safety_buffer_days})"
            ),
        )
        batch_job_name = batch_job.job_name
        batch_status = wait_for_batch_job(job_name=batch_job.job_name)
        if batch_status.state != "SUCCEEDED":
            raise RuntimeError(
                f"Storage Batch Operations job did not succeed for {batch_job.job_name}: "
                f"state={batch_status.state}"
            )
        if batch_status.failed_object_count > 0:
            raise RuntimeError(
                f"Storage Batch Operations job reported failed objects for {batch_job.job_name}: "
                f"{batch_status.failed_object_count}"
            )
        reconcile_result = execute(
            build_cleanup_gcs_cache_reconcile_current_table_query(
                settings,
                candidate_table=candidate_table,
            ),
            parameters=[],
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total, reconcile_result.total_bytes_processed
        )

    run_finished_at = now()
    return CleanupGcsCacheSummary(
        account_id=settings.project_id,
        bucket_name=settings.bucket_name,
        run_id=run_id,
        mode=mode,
        execute_kind=execute_kind,
        dry_run=False,
        ac_retention_days=resolved_ac_days,
        cas_retention_days=resolved_cas_days,
        safety_buffer_days=resolved_safety_buffer_days,
        candidate_object_count=candidate_object_count,
        selected_object_count=selected_object_count,
        ac_candidate_count=ac_candidate_count,
        cas_candidate_count=cas_candidate_count,
        oldest_last_seen_at=oldest_last_seen_at,
        newest_last_seen_at=newest_last_seen_at,
        sample_candidates=(),
        bytes_processed=bytes_processed_total,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
        manifest_uri=manifest_uri,
        batch_job_name=batch_job_name,
    )


def build_cleanup_gcs_cache_summary_query(settings: GcsCacheSettings, *, execute_kind: str) -> str:
    current_table = _table_ref(settings.project_id, settings.dataset, settings.last_seen_current_table)
    return f"""
WITH candidates AS (
  SELECT
    object_name,
    object_kind,
    last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_seen_at, DAY) AS idle_days
  FROM {current_table}
  WHERE {_candidate_filter_clause(execute_kind=execute_kind)}
)
SELECT
  COUNT(*) AS candidate_object_count,
  COUNTIF(object_kind = 'ac') AS ac_candidate_count,
  COUNTIF(object_kind = 'cas') AS cas_candidate_count,
  MIN(last_seen_at) AS oldest_last_seen_at,
  MAX(last_seen_at) AS newest_last_seen_at,
  ARRAY_AGG(
    STRUCT(
      object_name AS object_name,
      object_kind AS object_kind,
      last_seen_at AS last_seen_at,
      idle_days AS idle_days
    )
    ORDER BY last_seen_at ASC, object_name ASC
    LIMIT @sample_limit
  ) AS sample_candidates
FROM candidates
""".strip()


def build_cleanup_gcs_cache_candidate_table_query(
    settings: GcsCacheSettings,
    *,
    execute_kind: str,
    candidate_table: str,
) -> str:
    current_table = _table_ref(settings.project_id, settings.dataset, settings.last_seen_current_table)
    if execute_kind == "mixed-canary":
        return f"""
CREATE OR REPLACE TABLE {candidate_table}
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @ttl_days DAY)
) AS
WITH ac_candidates AS (
  SELECT
    object_name,
    last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_seen_at, DAY) AS idle_days
  FROM {current_table}
  WHERE object_kind = 'ac'
    AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_cutoff_days DAY)
  ORDER BY last_seen_at ASC, object_name ASC
  LIMIT 500
),
cas_candidates AS (
  SELECT
    object_name,
    last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_seen_at, DAY) AS idle_days
  FROM {current_table}
  WHERE object_kind = 'cas'
    AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_cutoff_days DAY)
  ORDER BY last_seen_at ASC, object_name ASC
  LIMIT 500
)
SELECT
  @run_id AS run_id,
  object_name,
  last_seen_at,
  idle_days,
  CURRENT_TIMESTAMP() AS selected_at
FROM (
  SELECT * FROM ac_candidates
  UNION ALL
  SELECT * FROM cas_candidates
)
""".strip()
    object_kind = execute_kind
    cutoff_parameter = "@ac_cutoff_days" if execute_kind == "ac" else "@cas_cutoff_days"
    return f"""
CREATE OR REPLACE TABLE {candidate_table}
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @ttl_days DAY)
) AS
WITH candidates AS (
  SELECT
    object_name,
    last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_seen_at, DAY) AS idle_days
  FROM {current_table}
  WHERE object_kind = '{object_kind}'
    AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {cutoff_parameter} DAY)
)
SELECT
  @run_id AS run_id,
  object_name,
  last_seen_at,
  idle_days,
  CURRENT_TIMESTAMP() AS selected_at
FROM candidates
ORDER BY last_seen_at ASC, object_name ASC
LIMIT @limit
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
  object_name AS name
FROM {candidate_table}
ORDER BY last_seen_at ASC, object_name ASC
""".strip()


def build_cleanup_gcs_cache_reconcile_current_table_query(
    settings: GcsCacheSettings,
    *,
    candidate_table: str,
) -> str:
    current_table = _table_ref(settings.project_id, settings.dataset, settings.last_seen_current_table)
    return f"""
DELETE FROM {current_table}
WHERE STRUCT(object_name, last_seen_at) IN (
  SELECT AS STRUCT object_name, last_seen_at
  FROM {candidate_table}
)
""".strip()


def _candidate_filter_clause(*, execute_kind: str) -> str:
    if execute_kind == "ac":
        return (
            "object_kind = 'ac' "
            "AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_cutoff_days DAY)"
        )
    if execute_kind == "cas":
        return (
            "object_kind = 'cas' "
            "AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_cutoff_days DAY)"
        )
    return """
(
  object_kind = 'ac'
  AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_cutoff_days DAY)
) OR (
  object_kind = 'cas'
  AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_cutoff_days DAY)
)
""".strip()


def _table_ref(project_id: str, dataset: str, table: str) -> str:
    return f"`{project_id}.{dataset}.{table}`"


def _candidate_table_name(settings: GcsCacheSettings, *, execute_kind: str, run_id: str) -> str:
    suffix = run_id.replace("-", "")
    table_name = f"_tmp_gcs_cache_cleanup_candidates_{execute_kind.replace('-', '_')}_{suffix}"
    return _table_ref(settings.project_id, settings.dataset, table_name)


def _manifest_uri(
    settings: GcsCacheSettings,
    *,
    execute_kind: str,
    run_started_at: datetime,
    run_id: str,
) -> str:
    date_prefix = run_started_at.strftime("%Y-%m-%d")
    return (
        f"gs://{settings.cleanup_manifest_bucket}/"
        f"{settings.cleanup_manifest_prefix}/{date_prefix}/{execute_kind}/{run_id}/manifest-*.csv"
    )


def _batch_job_id(*, execute_kind: str, run_started_at: datetime, run_id: str) -> str:
    timestamp = run_started_at.strftime("%Y%m%dt%H%M%S")
    suffix = run_id.replace("-", "")[:12]
    return f"gcs-cache-cleanup-{execute_kind}-{timestamp}-{suffix}".lower()


def _selected_limit(
    *,
    execute_kind: str,
    candidate_object_count: int,
    ac_candidate_count: int,
    cas_candidate_count: int,
    max_delete_objects: int,
) -> int:
    if execute_kind == "mixed-canary":
        return min(ac_candidate_count, 500) + min(cas_candidate_count, 500)
    return min(candidate_object_count, max_delete_objects)


def _summary_query_parameters(
    *,
    execute_kind: str,
    ac_cutoff_days: int,
    cas_cutoff_days: int,
    sample_limit: int,
) -> list[BigQueryParameter]:
    parameters = [BigQueryParameter("sample_limit", "INT64", sample_limit)]
    if execute_kind in {"all", "ac", "mixed-canary"}:
        parameters.insert(0, BigQueryParameter("ac_cutoff_days", "INT64", ac_cutoff_days))
    if execute_kind in {"all", "cas", "mixed-canary"}:
        parameters.insert(0, BigQueryParameter("cas_cutoff_days", "INT64", cas_cutoff_days))
    return parameters


def _candidate_table_query_parameters(
    *,
    execute_kind: str,
    ac_cutoff_days: int,
    cas_cutoff_days: int,
    run_id: str,
    limit: int,
    ttl_days: int,
) -> list[BigQueryParameter]:
    parameters = [
        BigQueryParameter("run_id", "STRING", run_id),
        BigQueryParameter("ttl_days", "INT64", ttl_days),
    ]
    if execute_kind in {"ac", "mixed-canary"}:
        parameters.append(BigQueryParameter("ac_cutoff_days", "INT64", ac_cutoff_days))
    if execute_kind in {"cas", "mixed-canary"}:
        parameters.append(BigQueryParameter("cas_cutoff_days", "INT64", cas_cutoff_days))
    if execute_kind != "mixed-canary":
        parameters.append(BigQueryParameter("limit", "INT64", limit))
    return parameters


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
    if mode == "delete" and execute_kind == "all":
        raise ValueError("cleanup-gcs-cache --mode delete requires --execute-kind ac, cas, or mixed-canary")


def _coerce_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _coerce_datetime(value)


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Unsupported datetime value: {value!r}")
