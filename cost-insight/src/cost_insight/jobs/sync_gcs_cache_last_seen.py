from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from cost_insight.common.bigquery import BigQueryParameter, BigQueryQueryResult, execute_query
from cost_insight.common.config import GcsCacheSettings

QueryExecutor = Callable[[str, list[BigQueryParameter]], BigQueryQueryResult]


@dataclass(frozen=True)
class SyncGcsCacheLastSeenResult:
    account_id: str
    bucket_name: str
    run_date: date
    source_rows_seen: int
    distinct_objects: int
    dry_run: bool
    bytes_processed: int | None


def run_sync_gcs_cache_last_seen(
    *,
    settings: GcsCacheSettings,
    run_date: date | None = None,
    dry_run: bool = False,
    execute: QueryExecutor = execute_query,
) -> SyncGcsCacheLastSeenResult:
    resolved_run_date = run_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    query = (
        build_sync_gcs_cache_last_seen_dry_run_query(settings)
        if dry_run
        else build_sync_gcs_cache_last_seen_query(settings)
    )
    parameters = [
        BigQueryParameter("run_date", "DATE", resolved_run_date),
        BigQueryParameter("bucket_name", "STRING", settings.bucket_name),
    ]
    if settings.last_seen_excluded_get_user_agent.strip():
        parameters.extend(
            [
                BigQueryParameter(
                    "excluded_get_user_agent",
                    "STRING",
                    settings.last_seen_excluded_get_user_agent.strip(),
                ),
                BigQueryParameter(
                    "excluded_get_principal_email",
                    "STRING",
                    settings.last_seen_excluded_get_principal_email or "",
                ),
            ]
        )
    result = execute(query, parameters=parameters)
    row = result.rows[0] if result.rows else {}
    return SyncGcsCacheLastSeenResult(
        account_id=settings.project_id,
        bucket_name=settings.bucket_name,
        run_date=resolved_run_date,
        source_rows_seen=int(row.get("source_rows_seen", 0) or 0),
        distinct_objects=int(row.get("distinct_objects", 0) or 0),
        dry_run=dry_run,
        bytes_processed=result.total_bytes_processed,
    )


def build_sync_gcs_cache_last_seen_dry_run_query(settings: GcsCacheSettings) -> str:
    return f"""
WITH daily_rollup AS (
  {_build_daily_rollup_select(settings)}
)
SELECT
  COUNT(*) AS distinct_objects,
  COALESCE(SUM(event_count_in_day), 0) AS source_rows_seen
FROM daily_rollup
""".strip()


def build_sync_gcs_cache_last_seen_query(settings: GcsCacheSettings) -> str:
    daily_table = _table_ref(
        settings.project_id,
        settings.dataset,
        settings.last_seen_daily_table,
    )
    current_table = _table_ref(
        settings.project_id,
        settings.dataset,
        settings.last_seen_current_table,
    )
    return f"""
CREATE TABLE IF NOT EXISTS {daily_table} (
  ds DATE NOT NULL,
  object_name STRING NOT NULL,
  object_kind STRING NOT NULL,
  first_seen_at TIMESTAMP NOT NULL,
  last_seen_at TIMESTAMP NOT NULL,
  get_count_in_day INT64 NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
PARTITION BY ds
CLUSTER BY object_kind, object_name;

CREATE TABLE IF NOT EXISTS {current_table} (
  object_name STRING NOT NULL,
  object_kind STRING NOT NULL,
  first_seen_at TIMESTAMP,
  last_seen_at TIMESTAMP NOT NULL,
  last_seen_date DATE NOT NULL,
  total_get_count INT64 NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
CLUSTER BY object_kind, last_seen_date;

CREATE TEMP TABLE daily_rollup AS
{_build_daily_rollup_select(settings)};

DELETE FROM {daily_table}
WHERE ds = @run_date;

INSERT INTO {daily_table} (
  ds,
  object_name,
  object_kind,
  first_seen_at,
  last_seen_at,
  get_count_in_day,
  updated_at
)
SELECT
  ds,
  object_name,
  object_kind,
  first_seen_at,
  last_seen_at,
  get_count_in_day,
  updated_at
FROM daily_rollup;

MERGE {current_table} AS target
USING daily_rollup AS source
ON target.object_name = source.object_name
WHEN MATCHED THEN
  UPDATE SET
    object_kind = source.object_kind,
    first_seen_at = IF(
      target.first_seen_at IS NULL,
      source.first_seen_at,
      LEAST(target.first_seen_at, source.first_seen_at)
    ),
    last_seen_at = GREATEST(target.last_seen_at, source.last_seen_at),
    last_seen_date = DATE(GREATEST(target.last_seen_at, source.last_seen_at)),
    total_get_count = target.total_get_count + source.get_count_in_day,
    updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN
  INSERT (
    object_name,
    object_kind,
    first_seen_at,
    last_seen_at,
    last_seen_date,
    total_get_count,
    updated_at
  )
  VALUES (
    source.object_name,
    source.object_kind,
    source.first_seen_at,
    source.last_seen_at,
    DATE(source.last_seen_at),
    source.get_count_in_day,
    CURRENT_TIMESTAMP()
  );

SELECT
  COUNT(*) AS distinct_objects,
  COALESCE(SUM(event_count_in_day), 0) AS source_rows_seen
FROM daily_rollup
""".strip()


def _build_daily_rollup_select(settings: GcsCacheSettings) -> str:
    audit_log_table = _table_ref(
        settings.project_id,
        settings.dataset,
        settings.audit_log_table,
    )
    excluded_get_user_agent = settings.last_seen_excluded_get_user_agent.strip()
    excluded_get_clause = ""
    if excluded_get_user_agent:
        excluded_get_clause = """
    AND NOT (
      protopayload_auditlog.methodName = "storage.objects.get"
      AND COALESCE(protopayload_auditlog.requestMetadata.callerSuppliedUserAgent, "") = @excluded_get_user_agent
      AND (
        @excluded_get_principal_email = ""
        OR COALESCE(protopayload_auditlog.authenticationInfo.principalEmail, "") = @excluded_get_principal_email
      )
    )"""
    return f"""
WITH extracted AS (
  SELECT
    DATE(timestamp) AS ds,
    REGEXP_EXTRACT(protopayload_auditlog.resourceName, r"/objects/(.+)$") AS object_name,
    timestamp,
    protopayload_auditlog.methodName AS method_name
  FROM {audit_log_table}
  WHERE DATE(timestamp) = @run_date
    AND resource.labels.bucket_name = @bucket_name
    AND protopayload_auditlog.methodName IN ("storage.objects.get", "storage.objects.create")
{excluded_get_clause}
)
SELECT
  ds,
  object_name,
  CASE
    WHEN STARTS_WITH(object_name, "cas/") THEN "cas"
    WHEN STARTS_WITH(object_name, "ac/") THEN "ac"
    ELSE "other"
  END AS object_kind,
  MIN(timestamp) AS first_seen_at,
  MAX(timestamp) AS last_seen_at,
  COUNT(*) AS event_count_in_day,
  COUNTIF(method_name = "storage.objects.get") AS get_count_in_day,
  CURRENT_TIMESTAMP() AS updated_at
FROM extracted
GROUP BY ds, object_name, object_kind
""".strip()


def _table_ref(project_id: str, dataset: str, table: str) -> str:
    return f"`{project_id}.{dataset}.{table}`"
