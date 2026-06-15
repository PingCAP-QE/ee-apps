from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from cost_insight.common.bigquery import BigQueryParameter, BigQueryQueryResult, execute_query
from cost_insight.common.config import GcsCacheSettings

QueryExecutor = Callable[[str, list[BigQueryParameter]], BigQueryQueryResult]


@dataclass(frozen=True)
class BootstrapGcsCacheLastSeenResult:
    account_id: str
    bucket_name: str
    start_date: date
    end_date: date
    source_rows_seen: int
    distinct_objects: int
    dry_run: bool
    bytes_processed: int | None


def run_bootstrap_gcs_cache_last_seen(
    *,
    settings: GcsCacheSettings,
    start_date: date,
    end_date: date | None = None,
    dry_run: bool = False,
    execute: QueryExecutor = execute_query,
) -> BootstrapGcsCacheLastSeenResult:
    resolved_end_date = end_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    if start_date > resolved_end_date:
        raise ValueError("--start-date must be before or equal to --end-date")

    parameters = [
        BigQueryParameter("start_date", "DATE", start_date),
        BigQueryParameter("end_date", "DATE", resolved_end_date),
        BigQueryParameter("bucket_name", "STRING", settings.bucket_name),
    ]
    query = (
        build_bootstrap_gcs_cache_last_seen_dry_run_query(settings)
        if dry_run
        else build_bootstrap_gcs_cache_last_seen_query(settings)
    )
    result = execute(query, parameters=parameters)
    row = result.rows[0] if result.rows else {}
    return BootstrapGcsCacheLastSeenResult(
        account_id=settings.project_id,
        bucket_name=settings.bucket_name,
        start_date=start_date,
        end_date=resolved_end_date,
        source_rows_seen=int(row.get("source_rows_seen", 0) or 0),
        distinct_objects=int(row.get("distinct_objects", 0) or 0),
        dry_run=dry_run,
        bytes_processed=result.total_bytes_processed,
    )


def build_bootstrap_gcs_cache_last_seen_dry_run_query(settings: GcsCacheSettings) -> str:
    return f"""
WITH bootstrap_rollup AS (
  {_build_bootstrap_rollup_select(settings)}
)
SELECT
  COUNT(*) AS distinct_objects,
  COALESCE(SUM(source_event_count), 0) AS source_rows_seen
FROM bootstrap_rollup
""".strip()


def build_bootstrap_gcs_cache_last_seen_query(settings: GcsCacheSettings) -> str:
    current_table = _table_ref(
        settings.project_id,
        settings.dataset,
        settings.last_seen_current_table,
    )
    return f"""
CREATE TEMP TABLE bootstrap_rollup AS
{_build_bootstrap_rollup_select(settings)};

CREATE OR REPLACE TABLE {current_table}
CLUSTER BY object_kind, last_seen_date AS
SELECT
  object_name,
  object_kind,
  first_seen_at,
  last_seen_at,
  DATE(last_seen_at) AS last_seen_date,
  total_get_count,
  CURRENT_TIMESTAMP() AS updated_at
FROM bootstrap_rollup;

SELECT
  COUNT(*) AS distinct_objects,
  COALESCE(SUM(source_event_count), 0) AS source_rows_seen
FROM bootstrap_rollup
""".strip()


def _build_bootstrap_rollup_select(settings: GcsCacheSettings) -> str:
    audit_log_table = _table_ref(
        settings.project_id,
        settings.dataset,
        settings.audit_log_table,
    )
    return f"""
WITH extracted AS (
  SELECT
    REGEXP_EXTRACT(protopayload_auditlog.resourceName, r"/objects/(.+)$") AS object_name,
    timestamp,
    protopayload_auditlog.methodName AS method_name
  FROM {audit_log_table}
  WHERE DATE(timestamp) BETWEEN @start_date AND @end_date
    AND resource.labels.bucket_name = @bucket_name
    AND protopayload_auditlog.methodName IN ("storage.objects.get", "storage.objects.create")
)
SELECT
  object_name,
  CASE
    WHEN STARTS_WITH(object_name, "cas/") THEN "cas"
    WHEN STARTS_WITH(object_name, "ac/") THEN "ac"
    ELSE "other"
  END AS object_kind,
  MIN(timestamp) AS first_seen_at,
  MAX(timestamp) AS last_seen_at,
  COUNTIF(method_name = "storage.objects.get") AS total_get_count,
  COUNT(*) AS source_event_count
FROM extracted
GROUP BY object_name, object_kind
""".strip()


def _table_ref(project_id: str, dataset: str, table: str) -> str:
    return f"`{project_id}.{dataset}.{table}`"
