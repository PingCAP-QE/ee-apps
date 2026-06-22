from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice

from cost_insight.common.bigquery import BigQueryParameter, BigQueryQueryResult, execute_query
from cost_insight.common.config import GcsCacheSettings
from cost_insight.common.gcs_cache_references import (
    AcReferenceExtraction,
    extract_action_cache_references_batch,
)

QueryExecutor = Callable[[str, Sequence[BigQueryParameter]], BigQueryQueryResult]
ReferenceExtractor = Callable[..., tuple[AcReferenceExtraction, ...]]
RowStreamer = Callable[[str, Sequence[BigQueryParameter]], Iterator[dict[str, object]]]
JsonLoader = Callable[[str, Sequence[dict[str, object]]], None]


@dataclass(frozen=True)
class SyncGcsCacheAcReferencesSummary:
    account_id: str
    bucket_name: str
    mode: str
    shard_start: int
    shard_end: int
    source_object_count: int
    missing_object_count: int
    replaced_ac_object_count: int
    reference_row_count: int
    dry_run: bool
    indexed_through: datetime
    bytes_processed: int | None
    run_started_at: datetime
    run_finished_at: datetime


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
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SyncGcsCacheAcReferencesSummary:
    if mode not in {"bootstrap", "incremental"}:
        raise ValueError(f"Unsupported AC reference sync mode: {mode}")

    shard_count = settings.ac_reference_shard_count
    resolved_shard_end = shard_count - 1 if shard_end is None else shard_end
    _validate_shard_range(shard_start=shard_start, shard_end=resolved_shard_end, shard_count=shard_count)

    stream_rows = _stream_query_rows if stream_rows is None else stream_rows
    load_json_rows = _load_json_rows if load_json_rows is None else load_json_rows

    run_started_at = now()
    bytes_processed_total = 0
    source_object_count = 0
    missing_object_count = 0
    replaced_ac_object_count = 0
    reference_row_count = 0

    bytes_processed_total = _add_bytes(
        bytes_processed_total,
        execute(build_ensure_gcs_cache_ac_reference_tables_query(settings), parameters=[]).total_bytes_processed,
    )

    run_id = run_started_at.strftime("%Y%m%dt%H%M%S")
    object_stage_table = _table_ref(
        settings.project_id,
        settings.dataset,
        f"_tmp_gcs_cache_ac_reference_objects_{run_id.lower()}_{shard_start}_{resolved_shard_end}",
    )
    edge_stage_table = _table_ref(
        settings.project_id,
        settings.dataset,
        f"_tmp_gcs_cache_ac_reference_edges_{run_id.lower()}_{shard_start}_{resolved_shard_end}",
    )

    if not dry_run:
        bytes_processed_total = _add_bytes(
            bytes_processed_total,
            execute(
                build_create_gcs_cache_ac_reference_stage_tables_query(
                    object_stage_table=object_stage_table,
                    edge_stage_table=edge_stage_table,
                    ttl_days=settings.cleanup_candidate_ttl_days,
                ),
                parameters=[],
            ).total_bytes_processed,
        )

    for shard in range(shard_start, resolved_shard_end + 1):
        watermark = None if mode == "bootstrap" else _read_indexed_through(
            execute=execute,
            settings=settings,
            shard=shard,
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

            object_rows = []
            edge_rows = []
            for extraction in extractions:
                object_rows.append({"ac_object_name": extraction.ac_object_name})
                replaced_ac_object_count += 1
                if not extraction.exists:
                    missing_object_count += 1
                    continue
                for cas_object_name in extraction.cas_object_names:
                    edge_rows.append(
                        {
                            "ac_object_name": extraction.ac_object_name,
                            "cas_object_name": cas_object_name,
                        }
                    )
            reference_row_count += len(edge_rows)

            if dry_run:
                continue

            load_json_rows(object_stage_table, object_rows)
            load_json_rows(edge_stage_table, edge_rows)
            bytes_processed_total = _add_bytes(
                bytes_processed_total,
                execute(
                    build_replace_gcs_cache_ac_references_query(
                        settings,
                        object_stage_table=object_stage_table,
                        edge_stage_table=edge_stage_table,
                    ),
                    parameters=[],
                ).total_bytes_processed,
            )

        if not dry_run:
            bytes_processed_total = _add_bytes(
                bytes_processed_total,
                execute(
                    build_update_gcs_cache_ac_reference_index_state_query(settings),
                    parameters=[
                        BigQueryParameter("shard", "INT64", shard),
                        BigQueryParameter("indexed_through", "TIMESTAMP", run_started_at),
                    ],
                ).total_bytes_processed,
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
        replaced_ac_object_count=replaced_ac_object_count,
        reference_row_count=reference_row_count,
        dry_run=dry_run,
        indexed_through=run_started_at,
        bytes_processed=bytes_processed_total,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
    )


def build_ensure_gcs_cache_ac_reference_tables_query(settings: GcsCacheSettings) -> str:
    references_table = _table_ref(settings.project_id, settings.dataset, settings.ac_cas_references_table)
    state_table = _table_ref(settings.project_id, settings.dataset, settings.ac_reference_index_state_table)
    return f"""
CREATE TABLE IF NOT EXISTS {references_table} (
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
);

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


def build_create_gcs_cache_ac_reference_stage_tables_query(
    *,
    object_stage_table: str,
    edge_stage_table: str,
    ttl_days: int,
) -> str:
    return f"""
CREATE OR REPLACE TABLE {object_stage_table} (
  ac_object_name STRING NOT NULL
)
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
);

CREATE OR REPLACE TABLE {edge_stage_table} (
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
)
OPTIONS (
  expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl_days} DAY)
);
""".strip()


def build_bootstrap_gcs_cache_ac_reference_source_query(settings: GcsCacheSettings) -> str:
    current_table = _table_ref(settings.project_id, settings.dataset, settings.last_seen_current_table)
    return f"""
SELECT object_name
FROM {current_table}
WHERE object_kind = 'ac'
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
  AND {_ac_shard_expression("object_name")} = @shard
ORDER BY object_name ASC
""".strip()


def build_replace_gcs_cache_ac_references_query(
    settings: GcsCacheSettings,
    *,
    object_stage_table: str,
    edge_stage_table: str,
) -> str:
    references_table = _table_ref(settings.project_id, settings.dataset, settings.ac_cas_references_table)
    return f"""
DELETE FROM {references_table}
WHERE ac_object_name IN (
  SELECT ac_object_name
  FROM {object_stage_table}
);

INSERT INTO {references_table} (
  ac_object_name,
  cas_object_name
)
SELECT
  ac_object_name,
  cas_object_name
FROM {edge_stage_table};
""".strip()


def build_update_gcs_cache_ac_reference_index_state_query(settings: GcsCacheSettings) -> str:
    state_table = _table_ref(settings.project_id, settings.dataset, settings.ac_reference_index_state_table)
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
    state_table = _table_ref(settings.project_id, settings.dataset, settings.ac_reference_index_state_table)
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
            bigquery.ScalarQueryParameter(param.name, param.type_, param.value) for param in parameters
        ]
    )
    job = client.query(query, job_config=job_config)
    for row in job.result():
        yield dict(row.items())


def _load_json_rows(table_ref: str, rows: Sequence[dict[str, object]]) -> None:
    from google.cloud import bigquery

    client = bigquery.Client()
    destination = table_ref.strip("`")
    if rows:
        if len(rows[0]) == 1:
            schema = [bigquery.SchemaField("ac_object_name", "STRING", mode="REQUIRED")]
        else:
            schema = [
                bigquery.SchemaField("ac_object_name", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("cas_object_name", "STRING", mode="REQUIRED"),
            ]
        job = client.load_table_from_json(
            list(rows),
            destination,
            job_config=bigquery.LoadJobConfig(
                schema=schema,
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            ),
        )
        job.result()
        return

    job = client.query(f"TRUNCATE TABLE {table_ref}")
    job.result()


def _batched(values: Iterable[dict[str, object]], batch_size: int) -> Iterator[tuple[dict[str, object], ...]]:
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
    return (
        "TO_CODE_POINTS(FROM_HEX(SUBSTR("
        f"{column_name}, 4, 2"
        ")))[OFFSET(0)]"
    )


def _table_ref(project_id: str, dataset: str, table: str) -> str:
    return f"`{project_id}.{dataset}.{table}`"


def _add_bytes(total: int, value: int | None) -> int:
    if value is None:
        return total
    return total + int(value)
