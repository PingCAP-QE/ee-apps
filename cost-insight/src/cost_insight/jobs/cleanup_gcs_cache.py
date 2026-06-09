from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from cost_insight.common.bigquery import BigQueryParameter, BigQueryQueryResult, execute_query
from cost_insight.common.config import GcsCacheSettings

QueryExecutor = Callable[[str, list[BigQueryParameter]], BigQueryQueryResult]


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
    mode: str
    dry_run: bool
    ac_retention_days: int
    cas_retention_days: int
    candidate_object_count: int
    ac_candidate_count: int
    cas_candidate_count: int
    oldest_last_seen_at: datetime | None
    newest_last_seen_at: datetime | None
    sample_candidates: tuple[CleanupGcsCacheSample, ...]
    bytes_processed: int | None


def run_cleanup_gcs_cache(
    *,
    settings: GcsCacheSettings,
    mode: str = "dry-run",
    ac_retention_days: int | None = None,
    cas_retention_days: int | None = None,
    sample_limit: int | None = None,
    execute: QueryExecutor = execute_query,
) -> CleanupGcsCacheSummary:
    if mode != "dry-run":
        raise ValueError("cleanup-gcs-cache only supports --mode dry-run in the first slice")
    resolved_ac_days = ac_retention_days or settings.ac_retention_days
    resolved_cas_days = cas_retention_days or settings.cas_retention_days
    resolved_sample_limit = sample_limit or settings.cleanup_sample_limit
    parameters = [
        BigQueryParameter("ac_retention_days", "INT64", resolved_ac_days),
        BigQueryParameter("cas_retention_days", "INT64", resolved_cas_days),
        BigQueryParameter("sample_limit", "INT64", resolved_sample_limit),
    ]
    result = execute(build_cleanup_gcs_cache_dry_run_query(settings), parameters=parameters)
    row = result.rows[0] if result.rows else {}
    sample_candidates = tuple(
        CleanupGcsCacheSample(
            object_name=str(item["object_name"]),
            object_kind=str(item["object_kind"]),
            last_seen_at=_coerce_datetime(item["last_seen_at"]),
            idle_days=int(item["idle_days"]),
        )
        for item in (row.get("sample_candidates") or [])
    )
    return CleanupGcsCacheSummary(
        account_id=settings.project_id,
        bucket_name=settings.bucket_name,
        mode=mode,
        dry_run=True,
        ac_retention_days=resolved_ac_days,
        cas_retention_days=resolved_cas_days,
        candidate_object_count=int(row.get("candidate_object_count", 0) or 0),
        ac_candidate_count=int(row.get("ac_candidate_count", 0) or 0),
        cas_candidate_count=int(row.get("cas_candidate_count", 0) or 0),
        oldest_last_seen_at=_coerce_optional_datetime(row.get("oldest_last_seen_at")),
        newest_last_seen_at=_coerce_optional_datetime(row.get("newest_last_seen_at")),
        sample_candidates=sample_candidates,
        bytes_processed=result.total_bytes_processed,
    )


def build_cleanup_gcs_cache_dry_run_query(settings: GcsCacheSettings) -> str:
    current_table = _table_ref(
        settings.project_id,
        settings.dataset,
        settings.last_seen_current_table,
    )
    return f"""
WITH candidates AS (
  SELECT
    object_name,
    object_kind,
    last_seen_at,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_seen_at, DAY) AS idle_days
  FROM {current_table}
  WHERE (
      object_kind = 'ac'
      AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @ac_retention_days DAY)
    ) OR (
      object_kind = 'cas'
      AND last_seen_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @cas_retention_days DAY)
    )
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
    ORDER BY last_seen_at
    LIMIT @sample_limit
  ) AS sample_candidates
FROM candidates
""".strip()


def _table_ref(project_id: str, dataset: str, table: str) -> str:
    return f"`{project_id}.{dataset}.{table}`"


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
