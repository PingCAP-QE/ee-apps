from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.row_utils import (
    bind_decimal_rows,
    coerce_date,
    coerce_datetime,
    hash_value,
    nullable_text,
)
from cost_insight.jobs import state_store
from cost_insight.jobs.sync_gcp_billing_export import (
    _ensure_cost_source_enabled,
    _upsert_cost_source,
)
from cost_insight.sources.gcp_billing_export import (
    DEFAULT_COST_OWNER_AUTHOR,
    decimal_or_none,
    fetch_gcp_billing_summary_rows,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_gcp_billing_summary"
OWNER_OVERRIDE_DELETE_CHUNK_SIZE = 1000
HASH_FIELDS = (
    "vendor",
    "account_id",
    "billing_account_id",
    "export_partition_date",
    "usage_date",
    "service_name",
    "sku_name",
    "author",
    "org",
    "repo",
)

RowFetcher = Callable[..., Iterable[dict[str, Any]]]


@dataclass(frozen=True)
class SyncGcpBillingSummaryResult:
    account_id: str
    export_partition_start: date
    export_partition_end: date
    rows_seen: int
    rows_written: int
    dry_run: bool
    touched_usage_dates: tuple[date, ...] = ()


def run_sync_gcp_billing_summary(
    engine: Engine,
    *,
    settings: GcpBillingSettings,
    export_partition_start: date | None = None,
    export_partition_end: date | None = None,
    earliest_usage_date: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    fetch_rows: RowFetcher = fetch_gcp_billing_summary_rows,
) -> SyncGcpBillingSummaryResult:
    resolved_end = export_partition_end or (
        datetime.now(timezone.utc).date() - timedelta(days=settings.sync_lag_days)
    )
    with engine.begin() as connection:
        _ensure_cost_source_enabled(connection, settings, dry_run=dry_run)
        state = state_store.get_job_state(connection, JOB_NAME)
        resolved_start = export_partition_start or _start_partition_from_state(
            state.watermark if state else {},
            end_date=resolved_end,
            overlap_days=settings.export_overlap_days,
            initial_lookback_days=settings.sync_initial_lookback_days,
        )
        watermark = _watermark(
            account_id=settings.account_id,
            export_partition_start=resolved_start,
            export_partition_end=resolved_end,
        )
        if not dry_run:
            state_store.mark_job_started(connection, JOB_NAME, watermark)

    try:
        rows_seen = 0
        rows_written = 0
        source_billing_account_ids: set[str] = set()
        batch: list[dict[str, Any]] = []
        for source_row in fetch_rows(
            billing_table=settings.billing_table,
            account_id=settings.account_id,
            export_partition_start=resolved_start,
            export_partition_end=resolved_end,
            earliest_usage_date=earliest_usage_date or settings.earliest_usage_date,
            page_size=settings.page_size,
            limit=limit,
        ):
            rows_seen += 1
            normalized = _normalize_summary_row(source_row)
            if normalized["billing_account_id"]:
                source_billing_account_ids.add(normalized["billing_account_id"])
            batch.append(normalized)
            if len(batch) >= settings.page_size:
                rows_written += write_summary_rows(engine, batch, dry_run=dry_run)
                batch.clear()
        rows_written += write_summary_rows(engine, batch, dry_run=dry_run)

        touched_usage_dates: tuple[date, ...] = ()
        if not dry_run:
            with engine.begin() as connection:
                source_billing_account_id = _select_billing_account_id(source_billing_account_ids)
                if source_billing_account_id:
                    _upsert_cost_source(
                        connection,
                        account_id=settings.account_id,
                        billing_account_id=source_billing_account_id,
                    )
                touched_usage_dates = _get_touched_usage_dates(
                    connection,
                    account_id=settings.account_id,
                    export_partition_start=resolved_start,
                    export_partition_end=resolved_end,
                )
                state_store.mark_job_succeeded(connection, JOB_NAME, watermark)

        return SyncGcpBillingSummaryResult(
            account_id=settings.account_id,
            export_partition_start=resolved_start,
            export_partition_end=resolved_end,
            rows_seen=rows_seen,
            rows_written=rows_written,
            dry_run=dry_run,
            touched_usage_dates=touched_usage_dates,
        )
    except Exception as exc:
        LOG.exception("sync_gcp_billing_summary failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, JOB_NAME, watermark, repr(exc))
        raise


def _start_partition_from_state(
    watermark: dict[str, Any],
    *,
    end_date: date,
    overlap_days: int,
    initial_lookback_days: int | None,
) -> date:
    last_end_date = watermark.get("export_partition_end")
    if last_end_date:
        parsed = date.fromisoformat(str(last_end_date))
        return min(parsed + timedelta(days=1) - timedelta(days=overlap_days), end_date)
    if initial_lookback_days is not None:
        return end_date - timedelta(days=initial_lookback_days - 1)
    return end_date


def _watermark(
    *,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "export_partition_start": export_partition_start.isoformat(),
        "export_partition_end": export_partition_end.isoformat(),
    }


def _select_billing_account_id(billing_account_ids: set[str]) -> str | None:
    if not billing_account_ids:
        return None
    if len(billing_account_ids) > 1:
        LOG.warning(
            "multiple billing account ids observed for cost source; keeping the first sorted id",
            extra={"billing_account_ids": sorted(billing_account_ids)},
        )
    return sorted(billing_account_ids)[0]


def _normalize_summary_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "vendor": nullable_text(row.get("vendor")) or "gcp",
        "account_id": nullable_text(row.get("account_id")),
        "billing_account_id": nullable_text(row.get("billing_account_id")),
        "export_partition_date": coerce_date(row.get("export_partition_date")),
        "usage_date": coerce_date(row.get("usage_date")),
        "service_name": nullable_text(row.get("service_name")),
        "sku_name": nullable_text(row.get("sku_name")),
        "author": nullable_text(row.get("author")),
        "org": nullable_text(row.get("org")),
        "repo": nullable_text(row.get("repo")),
        "list_cost": decimal_or_none(row.get("list_cost")),
        "effective_cost": decimal_or_none(row.get("effective_cost")),
        "credit_amount": decimal_or_none(row.get("credit_amount")),
        "net_cost": decimal_or_none(row.get("net_cost")),
        "source_export_time": coerce_datetime(row.get("source_export_time")),
    }
    if normalized["account_id"] is None:
        raise ValueError(f"Missing account_id in billing summary row: {row!r}")
    if normalized["export_partition_date"] is None:
        raise ValueError(f"Missing export_partition_date in billing summary row: {row!r}")
    if normalized["usage_date"] is None:
        raise ValueError(f"Missing usage_date in billing summary row: {row!r}")
    normalized["source_row_hash"] = build_summary_row_hash(normalized)
    return normalized


def build_summary_row_hash(row: dict[str, Any]) -> str:
    payload = {field: hash_value(row.get(field)) for field in HASH_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_summary_rows(engine: Engine, rows: Sequence[dict[str, Any]], *, dry_run: bool) -> int:
    if not rows:
        return 0
    if dry_run:
        LOG.info("dry-run skipped cost_bq_export_summary_daily upsert", extra={"row_count": len(rows)})
        return 0
    with engine.begin() as connection:
        _delete_legacy_summary_rows(connection, rows)
        _delete_superseded_owner_override_rows(connection, rows)
        connection.execute(_build_upsert_statement(connection), _bind_rows(connection, rows))
    return len(rows)


def _bind_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    bound_rows = list(rows)
    if connection.dialect.name != "sqlite":
        return bound_rows
    return bind_decimal_rows(bound_rows)


def _delete_legacy_summary_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> None:
    partitions = {
        (
            row["vendor"],
            row["account_id"],
            row["export_partition_date"],
        )
        for row in rows
    }
    if not partitions:
        return
    for vendor, account_id, export_partition_date in partitions:
        connection.execute(
            _DELETE_LEGACY_SUMMARY_ROWS,
            {
                "vendor": vendor,
                "account_id": account_id,
                "export_partition_date": export_partition_date,
            },
        )


def _delete_superseded_owner_override_rows(
    connection: Connection,
    rows: Sequence[dict[str, Any]],
) -> None:
    for offset in range(0, len(rows), OWNER_OVERRIDE_DELETE_CHUNK_SIZE):
        params = [
            {
                "vendor": row.get("vendor") or "",
                "account_id": row.get("account_id") or "",
                "billing_account_id": row.get("billing_account_id") or "",
                "export_partition_date": row["export_partition_date"],
                "usage_date": row["usage_date"],
                "service_name": row.get("service_name") or "",
                "sku_name": row.get("sku_name") or "",
                "org": row.get("org") or "",
                "repo": row.get("repo") or "",
            }
            for row in rows[offset : offset + OWNER_OVERRIDE_DELETE_CHUNK_SIZE]
            if _is_owner_override_row(row)
        ]
        if params:
            connection.execute(_DELETE_SUPERSEDED_OWNER_OVERRIDE_ROWS, params)


def _is_owner_override_row(row: dict[str, Any]) -> bool:
    if row.get("author") != DEFAULT_COST_OWNER_AUTHOR:
        return False
    return (
        row.get("service_name") == "Cloud Logging"
        or row.get("sku_name") == "Compute Flexible Committed Use Discounts - 3 Year"
        or row.get("sku_name") == "Compute Flexible Committed Use Discounts - 1 Year"
    )


def _get_touched_usage_dates(
    connection: Connection,
    *,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
) -> tuple[date, ...]:
    rows = connection.execute(
        _SELECT_TOUCHED_USAGE_DATES,
        {
            "vendor": "gcp",
            "account_id": account_id,
            "export_partition_start": export_partition_start,
            "export_partition_end": export_partition_end,
        },
    ).scalars()
    usage_dates = []
    for row in rows:
        usage_date = coerce_date(row)
        if usage_date is not None:
            usage_dates.append(usage_date)
    return tuple(usage_dates)


def _build_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            """
            INSERT INTO cost_bq_export_summary_daily (
              vendor,
              account_id,
              billing_account_id,
              export_partition_date,
              usage_date,
              service_name,
              sku_name,
              org,
              repo,
              author,
              list_cost,
              effective_cost,
              credit_amount,
              net_cost,
              source_export_time,
              source_row_hash
            ) VALUES (
              :vendor,
              :account_id,
              :billing_account_id,
              :export_partition_date,
              :usage_date,
              :service_name,
              :sku_name,
              :org,
              :repo,
              :author,
              :list_cost,
              :effective_cost,
              :credit_amount,
              :net_cost,
              :source_export_time,
              :source_row_hash
            )
            ON CONFLICT(vendor, account_id, export_partition_date, source_row_hash)
            DO UPDATE SET
              billing_account_id = excluded.billing_account_id,
          list_cost = excluded.list_cost,
          effective_cost = excluded.effective_cost,
          credit_amount = excluded.credit_amount,
          net_cost = excluded.net_cost,
          service_name = excluded.service_name,
          sku_name = excluded.sku_name,
          source_export_time = excluded.source_export_time,
          updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO cost_bq_export_summary_daily (
          vendor,
          account_id,
          billing_account_id,
          export_partition_date,
          usage_date,
          service_name,
          sku_name,
          org,
          repo,
          author,
          list_cost,
          effective_cost,
          credit_amount,
          net_cost,
          source_export_time,
          source_row_hash
        ) VALUES (
          :vendor,
          :account_id,
          :billing_account_id,
          :export_partition_date,
          :usage_date,
          :service_name,
          :sku_name,
          :org,
          :repo,
          :author,
          :list_cost,
          :effective_cost,
          :credit_amount,
          :net_cost,
          :source_export_time,
          :source_row_hash
        )
        ON DUPLICATE KEY UPDATE
          -- Dimension columns are part of source_row_hash; same hash means same dimensions.
          billing_account_id = VALUES(billing_account_id),
          list_cost = VALUES(list_cost),
          effective_cost = VALUES(effective_cost),
          credit_amount = VALUES(credit_amount),
          net_cost = VALUES(net_cost),
          service_name = VALUES(service_name),
          sku_name = VALUES(sku_name),
          source_export_time = VALUES(source_export_time),
          updated_at = CURRENT_TIMESTAMP
        """
    )


_SELECT_TOUCHED_USAGE_DATES = text(
    """
    SELECT DISTINCT usage_date
    FROM cost_bq_export_summary_daily
    WHERE export_partition_date BETWEEN :export_partition_start AND :export_partition_end
      AND vendor = :vendor
      AND account_id = :account_id
    ORDER BY usage_date
    """
)


_DELETE_LEGACY_SUMMARY_ROWS = text(
    """
    DELETE FROM cost_bq_export_summary_daily
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND export_partition_date = :export_partition_date
      AND service_name IS NULL
      AND sku_name IS NULL
    """
)


_DELETE_SUPERSEDED_OWNER_OVERRIDE_ROWS = text(
    """
    DELETE FROM cost_bq_export_summary_daily
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND COALESCE(billing_account_id, '') = :billing_account_id
      AND export_partition_date = :export_partition_date
      AND usage_date = :usage_date
      AND COALESCE(service_name, '') = :service_name
      AND COALESCE(sku_name, '') = :sku_name
      AND COALESCE(org, '') = :org
      AND COALESCE(repo, '') = :repo
      AND author IS NULL
    """
)
