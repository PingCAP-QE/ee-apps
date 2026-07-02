from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from time import perf_counter, sleep

from cost_insight.common.bigquery import (
    BigQueryExecutionError,
    BigQueryParameter,
    BigQueryQueryResult,
    execute_query,
)
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.gcs_cache_references import (
    AcReferenceExtraction,
    extract_action_cache_references_batch,
)

QueryExecutor = Callable[[str, Sequence[BigQueryParameter]], BigQueryQueryResult]
ReferenceExtractor = Callable[..., tuple[AcReferenceExtraction, ...]]
RowStreamer = Callable[[str, Sequence[BigQueryParameter]], Iterator[dict[str, object]]]
JsonLoader = Callable[..., str]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncGcsCacheAcReferenceParseErrorSample:
    object_name: str
    parse_error: str


@dataclass(frozen=True)
class SyncGcsCacheAcReferencesSummary:
    account_id: str
    bucket_name: str
    mode: str
    shard_start: int
    shard_end: int
    source_object_count: int
    missing_object_count: int
    parse_error_count: int
    replaced_ac_object_count: int
    reference_row_count: int
    sample_parse_errors: tuple[SyncGcsCacheAcReferenceParseErrorSample, ...]
    dry_run: bool
    indexed_through: datetime | None
    bytes_processed: int | None
    run_started_at: datetime
    run_finished_at: datetime


_AC_HEX_REGEX = r'^ac/[0-9a-fA-F]{64}$'


def run_sync_gcs_cache_ac_references(
    *,
    settings: GcsCacheSettings,
    mode: str,
    shard_start: int = 0,
    shard_end: int | None = None,
    dry_run: bool = False,
    execute: QueryExecutor = execute_query,
    stream_rows: RowStreamer | None = None,
    load_json_rows: JsonLoader | None = None,
    extract_references: ReferenceExtractor = extract_action_cache_references_batch,
    ensure_tables: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SyncGcsCacheAcReferencesSummary:
    if mode not in {"bootstrap", "incremental"}:
        raise ValueError(f"Unsupported AC reference sync mode: {mode}")

    shard_count = settings.ac_reference_shard_count
    resolved_shard_end = shard_count - 1 if shard_end is None else shard_end
    _validate_shard_range(
        shard_start=shard_start, shard_end=resolved_shard_end, shard_count=shard_count
    )

    stream_rows = _stream_query_rows if stream_rows is None else stream_rows
    load_json_rows = _load_json_rows if load_json_rows is None else load_json_rows

    run_started_at = now()
    bytes_processed_total = 0
    source_object_count = 0
    missing_object_count = 0
    parse_error_count = 0
    replaced_ac_object_count = 0
    reference_row_count = 0
    sample_parse_errors: list[SyncGcsCacheAcReferenceParseErrorSample] = []
    indexed_through: datetime | None = None
    failed_shards: list[int] = []

    if ensure_tables:
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            _execute_with_bigquery_dml_retry(
                execute,
                build_ensure_gcs_cache_ac_reference_tables_query(settings),
                parameters=[],
            ).total_bytes_processed,
        )

    for shard in range(shard_start, resolved_shard_end + 1):
        shard_started_at = perf_counter()
        shard_source_start = source_object_count
        shard_missing_start = missing_object_count
        shard_parse_error_start = parse_error_count
        shard_replaced_start = replaced_ac_object_count
        shard_reference_start = reference_row_count
        logger.info(
            "AC reference sync shard started: mode=%s shard=%s shard_end=%s",
            mode,
            shard,
            resolved_shard_end,
        )

        def log_shard_finished(status: str) -> None:
            logger.info(
                "AC reference sync shard finished: mode=%s shard=%s status=%s "
                "source=%s replaced=%s missing=%s references=%s parse_errors=%s "
                "elapsed_seconds=%.1f",
                mode,
                shard,
                status,
                source_object_count - shard_source_start,
                replaced_ac_object_count - shard_replaced_start,
                missing_object_count - shard_missing_start,
                reference_row_count - shard_reference_start,
                parse_error_count - shard_parse_error_start,
                perf_counter() - shard_started_at,
            )

        watermark = (
            None
            if mode == "bootstrap"
            else _read_indexed_through(
                execute=execute,
                settings=settings,
                shard=shard,
            )
        )
        if mode == "incremental" and watermark is None:
            raise RuntimeError(
                "AC reference index bootstrap is incomplete: "
                f"shard {shard} has no indexed_through watermark"
            )

        query = (
            build_bootstrap_gcs_cache_ac_reference_source_query(settings)
            if mode == "bootstrap"
            else build_incremental_gcs_cache_ac_reference_source_query(settings)
        )
        parameters = _source_query_parameters(
            mode=mode,
            shard=shard,
            shard_count=shard_count,
            bucket_name=settings.bucket_name,
            indexed_through=watermark,
            run_until=run_started_at,
        )

        rows = stream_rows(query, parameters)
        shard_edge_rows: list[dict[str, object]] = []
        shard_affected_names: set[str] = set()
        shard_missing_names: set[str] = set()
        shard_parse_error_count = 0

        for batch_rows in _batched(rows, settings.ac_reference_batch_size):
            batch_object_names = tuple(str(row["object_name"]) for row in batch_rows)
            if not batch_object_names:
                continue
            source_object_count += len(batch_object_names)
            extractions = extract_references(
                project_id=settings.project_id,
                bucket_name=settings.bucket_name,
                ac_object_names=batch_object_names,
                max_workers=settings.ac_reference_download_workers,
            )

            for extraction in extractions:
                if extraction.parse_error:
                    parse_error_count += 1
                    shard_parse_error_count += 1
                    if len(sample_parse_errors) < 10:
                        sample_parse_errors.append(
                            SyncGcsCacheAcReferenceParseErrorSample(
                                object_name=extraction.ac_object_name,
                                parse_error=extraction.parse_error,
                            )
                        )
                    continue
                replaced_ac_object_count += 1
                if not extraction.exists:
                    missing_object_count += 1
                    shard_missing_names.add(extraction.ac_object_name)
                    continue
                shard_affected_names.add(extraction.ac_object_name)
                for cas_object_name in extraction.cas_object_names:
                    shard_edge_rows.append(
                        {
                            "ac_object_name": extraction.ac_object_name,
                            "cas_object_name": cas_object_name,
                        }
                    )
        reference_row_count += len(shard_edge_rows)

        # Write sentinel rows for affected ACs with zero CAS refs.
        # These need their old refs cleaned up but contribute no new edge rows.
        zero_ref_ac_names = shard_affected_names - {
            r["ac_object_name"] for r in shard_edge_rows
        }
        for ac_name in sorted(zero_ref_ac_names):
            shard_edge_rows.append(
                {"ac_object_name": ac_name, "cas_object_name": ""}
            )

        if dry_run:
            indexed_through = run_started_at
            log_shard_finished("dry_run")
            continue

        # Parse error fail-closed: any parse error → clear indexed_through to NULL.
        # This invalidates the shard regardless of whether it had a previous
        # successful watermark (incremental) or was NULL (bootstrap).
        if shard_parse_error_count > 0:
            failed_shards.append(shard)
            if not dry_run:
                bytes_processed_total = _add_bytes(
                    bytes_processed_total,
                    _execute_with_bigquery_dml_retry(
                        execute,
                        f"""UPDATE {_table_ref(settings.project_id, settings.dataset, settings.ac_reference_index_state_table)}
SET indexed_through = NULL
WHERE shard = {shard}""",
                        parameters=[],
                    ).total_bytes_processed,
                )
            log_shard_finished("parse_error")
            continue

        # Reconcile missing AC objects from last_seen_current and by_ac.
        if shard_missing_names:
            missing_stage = _write_missing_stage(
                settings, shard=shard, missing_names=shard_missing_names,
                run_suffix=run_started_at.strftime("%Y%m%dt%H%M%S%f"),
            )
            bytes_processed_total = _add_bytes(
                bytes_processed_total,
                _execute_with_bigquery_dml_retry(
                    execute,
                    build_reconcile_missing_ac_query(
                        settings,
                        shard=shard,
                        missing_stage_table=missing_stage,
                    ),
                    parameters=[],
                ).total_bytes_processed,
            )

        # Per-shard replace: DELETE old rows, INSERT new rows.
        # Single DELETE+INSERT pair per shard — no O(n²) stage accumulation.
        stage_table = load_json_rows(
            settings, shard=shard, edge_rows=shard_edge_rows,
            run_suffix=run_started_at.strftime("%Y%m%dt%H%M%S%f"),
        )
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            _execute_with_bigquery_dml_retry(
                execute,
                build_replace_gcs_cache_ac_references_query(
                    settings,
                    shard=shard,
                    edge_row_count=len(shard_edge_rows),
                    stage_table=stage_table,
                    mode=mode,
                ),
                parameters=[],
            ).total_bytes_processed,
        )

        # Only update indexed_through if parse error count is 0.
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            _execute_with_bigquery_dml_retry(
                execute,
                build_update_gcs_cache_ac_reference_index_state_query(settings),
                parameters=[
                    BigQueryParameter("shard", "INT64", shard),
                    BigQueryParameter("indexed_through", "TIMESTAMP", run_started_at),
                ],
            ).total_bytes_processed,
        )
        indexed_through = run_started_at
        log_shard_finished("succeeded")

    if failed_shards and not dry_run:
        raise RuntimeError(
            f"AC reference sync failed: {len(failed_shards)} shard(s) had parse errors: "
            f"{failed_shards}. Fix parse errors and re-run the affected shards."
        )

    run_finished_at = now()
    return SyncGcsCacheAcReferencesSummary(
        account_id=settings.project_id,
        bucket_name=settings.bucket_name,
        mode=mode,
        shard_start=shard_start,
        shard_end=resolved_shard_end,
        source_object_count=source_object_count,
        missing_object_count=missing_object_count,
        parse_error_count=parse_error_count,
        replaced_ac_object_count=replaced_ac_object_count,
        reference_row_count=reference_row_count,
        sample_parse_errors=tuple(sample_parse_errors),
        dry_run=dry_run,
        indexed_through=indexed_through,
        bytes_processed=bytes_processed_total,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
    )


def build_ensure_gcs_cache_ac_reference_tables_query(settings: GcsCacheSettings) -> str:
    by_ac_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table
    )
    by_cas_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_cas_table
    )
    state_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_reference_index_state_table
    )
    return f"""
CREATE TABLE IF NOT EXISTS {by_ac_table} (
  shard INT64 NOT NULL,
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
)
CLUSTER BY shard, ac_object_name;

CREATE TABLE IF NOT EXISTS {by_cas_table} (
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
)
CLUSTER BY cas_object_name, ac_object_name;

CREATE TABLE IF NOT EXISTS {state_table} (
  shard INT64 NOT NULL,
  indexed_through TIMESTAMP
);

MERGE {state_table} AS target
USING (
  SELECT shard
  FROM UNNEST(GENERATE_ARRAY(0, {settings.ac_reference_shard_count - 1})) AS shard
) AS source
ON target.shard = source.shard
WHEN NOT MATCHED THEN
  INSERT (shard, indexed_through)
  VALUES (source.shard, NULL);
""".strip()


def build_bootstrap_gcs_cache_ac_reference_source_query(settings: GcsCacheSettings) -> str:
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    return f"""
SELECT object_name
FROM {current_table}
WHERE object_kind = 'ac'
  AND REGEXP_CONTAINS(object_name, r'{_AC_HEX_REGEX}')
  AND {_ac_shard_expression("object_name")} = @shard
ORDER BY object_name ASC
""".strip()


def build_incremental_gcs_cache_ac_reference_source_query(settings: GcsCacheSettings) -> str:
    audit_table = _table_ref(settings.project_id, settings.dataset, settings.audit_log_table)
    return f"""
WITH extracted AS (
  SELECT
    REGEXP_EXTRACT(protopayload_auditlog.resourceName, r"/objects/(.+)$") AS object_name
  FROM {audit_table}
  WHERE resource.labels.bucket_name = @bucket_name
    AND protopayload_auditlog.methodName = 'storage.objects.create'
    AND timestamp > @indexed_through
    AND timestamp <= @run_until
)
SELECT DISTINCT object_name
FROM extracted
WHERE object_name IS NOT NULL
  AND STARTS_WITH(object_name, 'ac/')
  AND REGEXP_CONTAINS(object_name, r'{_AC_HEX_REGEX}')
  AND {_ac_shard_expression("object_name")} = @shard
ORDER BY object_name ASC
""".strip()


def build_replace_gcs_cache_ac_references_query(
    settings: GcsCacheSettings,
    *,
    shard: int,
    edge_row_count: int,
    stage_table: str,
    mode: str,
) -> str:
    """Per-shard replace.

    Bootstrap: full shard DELETE (complete replace of all ACs in shard).
    Incremental: targeted DELETE via stage table (only ACs with create events).
    """
    by_ac_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table
    )
    del edge_row_count
    if mode == "bootstrap":
        return f"""
DELETE FROM {by_ac_table}
WHERE shard = {shard};

INSERT INTO {by_ac_table} (shard, ac_object_name, cas_object_name)
SELECT {shard}, ac_object_name, cas_object_name
FROM {stage_table}
WHERE cas_object_name != '';
""".strip()
    # incremental: only delete rows for ACs in the stage table.
    # Sentinels (cas_object_name = '') ensure zero-ref ACs have old refs cleaned up.
    return f"""
DELETE FROM {by_ac_table}
WHERE shard = {shard}
  AND ac_object_name IN (SELECT DISTINCT ac_object_name FROM {stage_table});

INSERT INTO {by_ac_table} (shard, ac_object_name, cas_object_name)
SELECT {shard}, ac_object_name, cas_object_name
FROM {stage_table}
WHERE cas_object_name != '';
""".strip()


def build_reconcile_missing_ac_query(
    settings: GcsCacheSettings,
    *,
    shard: int,
    missing_stage_table: str,
) -> str:
    """Remove missing AC objects from last_seen_current and by_ac.

    Uses `missing_stage_table` instead of a literal list to avoid
    oversized SQL when there are many missing ACs.
    """
    current_table = _table_ref(
        settings.project_id, settings.dataset, settings.last_seen_current_table
    )
    by_ac_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_cas_refs_by_ac_table
    )
    del shard  # kept for caller symmetry
    return f"""
DELETE FROM {current_table}
WHERE object_kind = 'ac'
  AND object_name IN (SELECT object_name FROM {missing_stage_table});

DELETE FROM {by_ac_table}
WHERE ac_object_name IN (SELECT object_name FROM {missing_stage_table});
""".strip()


def build_update_gcs_cache_ac_reference_index_state_query(settings: GcsCacheSettings) -> str:
    state_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_reference_index_state_table
    )
    return f"""
UPDATE {state_table}
SET indexed_through = @indexed_through
WHERE shard = @shard
""".strip()


def _source_query_parameters(
    *,
    mode: str,
    shard: int,
    shard_count: int,
    bucket_name: str,
    indexed_through: datetime | None,
    run_until: datetime,
) -> list[BigQueryParameter]:
    del shard_count
    parameters = [BigQueryParameter("shard", "INT64", shard)]
    if mode == "incremental":
        parameters.extend(
            [
                BigQueryParameter("bucket_name", "STRING", bucket_name),
                BigQueryParameter("indexed_through", "TIMESTAMP", indexed_through),
                BigQueryParameter("run_until", "TIMESTAMP", run_until),
            ]
        )
    return parameters


def _read_indexed_through(
    *,
    execute: QueryExecutor,
    settings: GcsCacheSettings,
    shard: int,
) -> datetime | None:
    state_table = _table_ref(
        settings.project_id, settings.dataset, settings.ac_reference_index_state_table
    )
    result = execute(
        f"SELECT indexed_through FROM {state_table} WHERE shard = @shard",
        parameters=[BigQueryParameter("shard", "INT64", shard)],
    )
    row = result.rows[0] if result.rows else {}
    value = row.get("indexed_through")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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


def _write_missing_stage(
    settings: GcsCacheSettings,
    *,
    shard: int,
    missing_names: set[str],
    run_suffix: str = "",
) -> str:
    """Write missing AC names to a stage table. Returns fully-qualified ref."""
    from google.cloud import bigquery

    client = bigquery.Client()
    ttl_days = settings.cleanup_candidate_ttl_days
    run_part = f"_{run_suffix}" if run_suffix else ""
    stage_table = f"{settings.project_id}.{settings.dataset}._tmp_missing_stage_{shard}{run_part}"
    stage_table_ref = f"`{stage_table}`"

    client.query(
        f"""CREATE OR REPLACE TABLE {stage_table_ref} (
  object_name STRING NOT NULL
)
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY))"""
    ).result()

    rows = [{"object_name": name} for name in sorted(missing_names)]
    if rows:
        schema = [bigquery.SchemaField("object_name", "STRING", mode="REQUIRED")]
        job = client.load_table_from_json(
            rows,
            stage_table,
            job_config=bigquery.LoadJobConfig(
                schema=schema,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            ),
        )
        job.result()
    return stage_table_ref


def _load_json_rows(
    settings: GcsCacheSettings,
    *,
    shard: int,
    edge_rows: Sequence[dict[str, object]],
    run_suffix: str = "",
) -> str:
    """Load edge rows into a run-scoped per-shard stage table. Returns the fully-qualified table ref."""
    from google.cloud import bigquery

    client = bigquery.Client()
    ttl_days = settings.cleanup_candidate_ttl_days
    run_part = f"_{run_suffix}" if run_suffix else ""
    stage_table = f"{settings.project_id}.{settings.dataset}._tmp_shard_edge_stage_{shard}{run_part}"
    stage_table_ref = f"`{stage_table}`"

    # Create with TTL first, then load data.
    client.query(
        f"""CREATE OR REPLACE TABLE {stage_table_ref} (
  shard INT64 NOT NULL,
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
)
OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY))"""
    ).result()

    rows_with_shard = [
        {"shard": shard, "ac_object_name": r["ac_object_name"], "cas_object_name": r["cas_object_name"]}
        for r in edge_rows
    ]
    if rows_with_shard:
        schema = [
            bigquery.SchemaField("shard", "INT64", mode="REQUIRED"),
            bigquery.SchemaField("ac_object_name", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("cas_object_name", "STRING", mode="REQUIRED"),
        ]
        job = client.load_table_from_json(
            list(rows_with_shard),
            stage_table,
            job_config=bigquery.LoadJobConfig(
                schema=schema,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            ),
        )
        job.result()
    return stage_table_ref


def _batched(
    values: Iterable[dict[str, object]], batch_size: int
) -> Iterator[tuple[dict[str, object], ...]]:
    iterator = iter(values)
    while True:
        batch = tuple(islice(iterator, batch_size))
        if not batch:
            return
        yield batch


def _validate_shard_range(*, shard_start: int, shard_end: int, shard_count: int) -> None:
    if shard_start < 0 or shard_end < 0:
        raise ValueError("Shard range must be non-negative")
    if shard_start > shard_end:
        raise ValueError("Shard range start must be less than or equal to shard range end")
    if shard_end >= shard_count:
        raise ValueError(
            f"Shard range end {shard_end} exceeds configured shard count {shard_count}"
        )


def _ac_shard_expression(column_name: str) -> str:
    return f"MOD(TO_CODE_POINTS(FROM_HEX(SUBSTR({column_name}, 4, 2)))[OFFSET(0)], 256)"


def _table_ref(project_id: str, dataset: str, table: str) -> str:
    return f"`{project_id}.{dataset}.{table}`"


def _execute_with_bigquery_dml_retry(
    execute: QueryExecutor,
    query: str,
    *,
    parameters: Sequence[BigQueryParameter],
    max_attempts: int = 5,
    sleep_seconds: Callable[[float], None] = sleep,
) -> BigQueryQueryResult:
    for attempt in range(1, max_attempts + 1):
        try:
            return execute(query, parameters)
        except BigQueryExecutionError as exc:
            if attempt >= max_attempts or not _is_retryable_bigquery_dml_error(exc):
                raise
            delay_seconds = min(60.0, 5.0 * (2 ** (attempt - 1)))
            logger.warning(
                "Retry BigQuery DML after transient error: attempt=%s max_attempts=%s "
                "delay_seconds=%.1f error=%s",
                attempt,
                max_attempts,
                delay_seconds,
                exc,
            )
            sleep_seconds(delay_seconds)
    raise RuntimeError("unreachable BigQuery DML retry state")


def _is_retryable_bigquery_dml_error(exc: BigQueryExecutionError) -> bool:
    message = str(exc).lower()
    return (
        "could not serialize access" in message
        or "too many table update operations" in message
    )


def _add_bytes(total: int, value: int | None) -> int:
    if value is None:
        return total
    return total + int(value)
