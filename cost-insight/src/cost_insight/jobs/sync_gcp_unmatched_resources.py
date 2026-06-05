from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
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
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled, upsert_cost_source
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.sources.gcp_billing_export import (
    decimal_or_none,
    fetch_gcp_unmatched_resource_rows,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_gcp_unmatched_resources"
HASH_FIELDS = (
    "vendor",
    "account_id",
    "billing_account_id",
    "export_partition_date",
    "usage_date",
    "service_name",
    "sku_name",
    "namespace",
    "author",
    "org",
    "repo",
    "resource_name",
)

RowFetcher = Callable[..., Iterable[dict[str, Any]]]


@dataclass(frozen=True)
class SyncGcpUnmatchedResourcesSummary:
    account_id: str
    usage_start_date: date
    usage_end_date: date
    export_partition_start: date
    export_partition_end: date
    rows_seen: int
    rows_written: int
    dry_run: bool


def run_sync_gcp_unmatched_resources(
    engine: Engine,
    *,
    settings: GcpBillingSettings,
    usage_start_date: date,
    usage_end_date: date,
    export_partition_start: date | None = None,
    export_partition_end: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    fetch_rows: RowFetcher = fetch_gcp_unmatched_resource_rows,
) -> SyncGcpUnmatchedResourcesSummary:
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    job_name = source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id)
    resolved_export_start = export_partition_start or usage_start_date
    resolved_export_end = export_partition_end or (
        usage_end_date + timedelta(days=settings.unmatched_resource_lag_days)
    )
    LOG.info(
        "sync_gcp_unmatched_resources resolved export partition window",
        extra={
            "usage_start_date": usage_start_date,
            "usage_end_date": usage_end_date,
            "export_partition_start": resolved_export_start,
            "export_partition_end": resolved_export_end,
        },
    )
    watermark = _watermark(
        account_id=settings.account_id,
        usage_start_date=usage_start_date,
        usage_end_date=usage_end_date,
        export_partition_start=resolved_export_start,
        export_partition_end=resolved_export_end,
    )

    with engine.begin() as connection:
        ensure_cost_source_enabled(
            connection,
            vendor="gcp",
            account_id=settings.account_id,
            dry_run=dry_run,
            display_name=settings.account_id,
        )
        if not dry_run:
            state_store.mark_job_started(connection, job_name, watermark)

    try:
        rows_seen = 0
        rows_written = 0
        source_billing_account_id: str | None = None
        batch: list[dict[str, Any]] = []
        for source_row in fetch_rows(
            billing_table=settings.billing_table,
            account_id=settings.account_id,
            export_partition_start=resolved_export_start,
            export_partition_end=resolved_export_end,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            page_size=settings.page_size,
            limit=limit,
        ):
            rows_seen += 1
            normalized = _normalize_resource_row(source_row)
            source_billing_account_id = source_billing_account_id or normalized[
                "billing_account_id"
            ]
            batch.append(normalized)
            if len(batch) >= settings.page_size:
                rows_written += write_unmatched_resource_rows(engine, batch, dry_run=dry_run)
                batch.clear()
        rows_written += write_unmatched_resource_rows(engine, batch, dry_run=dry_run)

        if not dry_run:
            with engine.begin() as connection:
                if source_billing_account_id:
                    upsert_cost_source(
                        connection,
                        vendor="gcp",
                        account_id=settings.account_id,
                        billing_account_id=source_billing_account_id,
                        display_name=settings.account_id,
                    )
                state_store.mark_job_succeeded(connection, job_name, watermark)

        return SyncGcpUnmatchedResourcesSummary(
            account_id=settings.account_id,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            export_partition_start=resolved_export_start,
            export_partition_end=resolved_export_end,
            rows_seen=rows_seen,
            rows_written=rows_written,
            dry_run=dry_run,
        )
    except Exception as exc:
        LOG.exception("sync_gcp_unmatched_resources failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def _watermark(
    *,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    export_partition_start: date,
    export_partition_end: date,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "usage_start_date": usage_start_date.isoformat(),
        "usage_end_date": usage_end_date.isoformat(),
        "export_partition_start": export_partition_start.isoformat(),
        "export_partition_end": export_partition_end.isoformat(),
    }


def _normalize_resource_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "vendor": nullable_text(row.get("vendor")) or "gcp",
        "account_id": nullable_text(row.get("account_id")),
        "billing_account_id": nullable_text(row.get("billing_account_id")),
        "export_partition_date": coerce_date(row.get("export_partition_date")),
        "usage_date": coerce_date(row.get("usage_date")),
        "service_name": nullable_text(row.get("service_name")),
        "sku_name": nullable_text(row.get("sku_name")),
        "namespace": nullable_text(row.get("namespace")),
        "author": nullable_text(row.get("author")),
        "org": nullable_text(row.get("org")),
        "repo": nullable_text(row.get("repo")),
        "resource_name": nullable_text(row.get("resource_name")),
        "usage_seconds": decimal_or_none(row.get("usage_seconds")),
        "list_cost": decimal_or_none(row.get("list_cost")),
        "effective_cost": decimal_or_none(row.get("effective_cost")),
        "credit_amount": decimal_or_none(row.get("credit_amount")),
        "net_cost": decimal_or_none(row.get("net_cost")),
        "source_export_time": coerce_datetime(row.get("source_export_time")),
    }
    if normalized["account_id"] is None:
        raise ValueError(f"Missing account_id in unmatched resource row: {row!r}")
    if normalized["export_partition_date"] is None:
        raise ValueError(f"Missing export_partition_date in unmatched resource row: {row!r}")
    if normalized["usage_date"] is None:
        raise ValueError(f"Missing usage_date in unmatched resource row: {row!r}")
    if normalized["resource_name"] is None:
        raise ValueError(f"Missing resource_name in unmatched resource row: {row!r}")
    normalized["source_row_hash"] = build_unmatched_resource_row_hash(normalized)
    return normalized


def build_unmatched_resource_row_hash(row: dict[str, Any]) -> str:
    payload = {field: hash_value(row.get(field)) for field in HASH_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_unmatched_resource_rows(
    engine: Engine,
    rows: Sequence[dict[str, Any]],
    *,
    dry_run: bool,
) -> int:
    if not rows:
        return 0
    if dry_run:
        LOG.info("dry-run skipped cost_unmatched_resource_daily upsert", extra={"row_count": len(rows)})
        return 0
    with engine.begin() as connection:
        connection.execute(_build_upsert_statement(connection), _bind_rows(connection, rows))
    return len(rows)


def _bind_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    bound_rows = list(rows)
    if connection.dialect.name != "sqlite":
        return bound_rows
    return bind_decimal_rows(bound_rows)


def _build_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            """
            INSERT INTO cost_unmatched_resource_daily (
              vendor,
              account_id,
              billing_account_id,
              export_partition_date,
              usage_date,
              service_name,
              sku_name,
              namespace,
              org,
              repo,
              author,
              resource_name,
              usage_seconds,
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
              :namespace,
              :org,
              :repo,
              :author,
              :resource_name,
              :usage_seconds,
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
              usage_seconds = excluded.usage_seconds,
              list_cost = excluded.list_cost,
              effective_cost = excluded.effective_cost,
              credit_amount = excluded.credit_amount,
              net_cost = excluded.net_cost,
              source_export_time = excluded.source_export_time,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO cost_unmatched_resource_daily (
          vendor,
          account_id,
          billing_account_id,
          export_partition_date,
          usage_date,
          service_name,
          sku_name,
          namespace,
          org,
          repo,
          author,
          resource_name,
          usage_seconds,
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
          :namespace,
          :org,
          :repo,
          :author,
          :resource_name,
          :usage_seconds,
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
          usage_seconds = VALUES(usage_seconds),
          list_cost = VALUES(list_cost),
          effective_cost = VALUES(effective_cost),
          credit_amount = VALUES(credit_amount),
          net_cost = VALUES(net_cost),
          source_export_time = VALUES(source_export_time),
          updated_at = CURRENT_TIMESTAMP
        """
    )
