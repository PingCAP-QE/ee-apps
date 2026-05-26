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
from cost_insight.common.row_utils import coerce_date, coerce_datetime, hash_value, nullable_text
from cost_insight.jobs import state_store
from cost_insight.sources.gcp_billing_export import decimal_or_none, fetch_gcp_billing_rows

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_gcp_billing_export"
HASH_FIELDS = (
    "vendor",
    "account_id",
    "billing_account_id",
    "usage_date",
    "service_name",
    "sku_name",
    "region",
    "namespace",
    "author",
    "org",
    "repo",
    "resource_name",
)

RowFetcher = Callable[..., Iterable[dict[str, Any]]]


@dataclass(frozen=True)
class SyncGcpBillingSummary:
    account_id: str
    start_date: date
    end_date: date
    rows_seen: int
    rows_written: int
    dry_run: bool


def run_sync_gcp_billing_export(
    engine: Engine,
    *,
    settings: GcpBillingSettings,
    start_date: date | None = None,
    end_date: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    fetch_rows: RowFetcher = fetch_gcp_billing_rows,
) -> SyncGcpBillingSummary:
    resolved_end_date = end_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    with engine.begin() as connection:
        _ensure_cost_source_enabled(connection, settings, dry_run=dry_run)
        state = state_store.get_job_state(connection, JOB_NAME)
        resolved_start_date = start_date or _start_date_from_state(
            state.watermark if state else {},
            end_date=resolved_end_date,
            overlap_days=settings.sync_overlap_days,
        )
        watermark = _watermark(
            account_id=settings.account_id,
            start_date=resolved_start_date,
            end_date=resolved_end_date,
        )
        if not dry_run:
            state_store.mark_job_started(connection, JOB_NAME, watermark)

    try:
        rows_seen = 0
        rows_written = 0
        source_billing_account_id: str | None = None
        batch: list[dict[str, Any]] = []
        for source_row in fetch_rows(
            billing_table=settings.billing_table,
            account_id=settings.account_id,
            start_date=resolved_start_date,
            end_date=resolved_end_date,
            page_size=settings.page_size,
            limit=limit,
        ):
            rows_seen += 1
            normalized = _normalize_row(source_row)
            source_billing_account_id = source_billing_account_id or normalized["billing_account_id"]
            batch.append(normalized)
            if len(batch) >= settings.page_size:
                rows_written += _write_batch(engine, batch, dry_run=dry_run)
                batch.clear()
        rows_written += _write_batch(engine, batch, dry_run=dry_run)

        if not dry_run:
            with engine.begin() as connection:
                if source_billing_account_id:
                    _upsert_cost_source(
                        connection,
                        account_id=settings.account_id,
                        billing_account_id=source_billing_account_id,
                    )
                state_store.mark_job_succeeded(connection, JOB_NAME, watermark)
        return SyncGcpBillingSummary(
            account_id=settings.account_id,
            start_date=resolved_start_date,
            end_date=resolved_end_date,
            rows_seen=rows_seen,
            rows_written=rows_written,
            dry_run=dry_run,
        )
    except Exception as exc:
        LOG.exception("sync_gcp_billing_export failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, JOB_NAME, watermark, repr(exc))
        raise


def _start_date_from_state(
    watermark: dict[str, Any],
    *,
    end_date: date,
    overlap_days: int,
) -> date:
    last_end_date = watermark.get("end_date")
    if not last_end_date:
        return end_date
    parsed = date.fromisoformat(str(last_end_date))
    return min(parsed - timedelta(days=overlap_days), end_date)


def _watermark(*, account_id: str, start_date: date, end_date: date) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def _ensure_cost_source_enabled(
    connection: Connection,
    settings: GcpBillingSettings,
    *,
    dry_run: bool,
) -> None:
    source = _get_cost_source(connection, account_id=settings.account_id)
    if source is not None:
        if int(source["is_active"]) != 1:
            raise ValueError(f"Cost source gcp/{settings.account_id} is inactive")
        return
    if not dry_run:
        _upsert_cost_source(connection, account_id=settings.account_id)


def _get_cost_source(connection: Connection, *, account_id: str) -> dict[str, Any] | None:
    row = (
        connection.execute(
            _SELECT_COST_SOURCE,
            {"vendor": "gcp", "account_id": account_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _upsert_cost_source(
    connection: Connection,
    *,
    account_id: str,
    billing_account_id: str | None = None,
) -> None:
    connection.execute(
        _build_upsert_cost_source_statement(connection),
        {
            "vendor": "gcp",
            "account_id": account_id,
            "billing_account_id": billing_account_id,
            "display_name": account_id,
        },
    )


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "vendor": nullable_text(row.get("vendor")) or "gcp",
        "account_id": nullable_text(row.get("account_id")),
        "billing_account_id": nullable_text(row.get("billing_account_id")),
        "usage_date": coerce_date(row.get("usage_date")),
        "service_name": nullable_text(row.get("service_name")),
        "sku_name": nullable_text(row.get("sku_name")),
        "region": nullable_text(row.get("region")),
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
        raise ValueError(f"Missing account_id in billing row: {row!r}")
    if normalized["usage_date"] is None:
        raise ValueError(f"Missing usage_date in billing row: {row!r}")
    normalized["source_row_hash"] = build_source_row_hash(normalized)
    return normalized


def build_source_row_hash(row: dict[str, Any]) -> str:
    payload = {field: hash_value(row.get(field)) for field in HASH_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_batch(engine: Engine, rows: Sequence[dict[str, Any]], *, dry_run: bool) -> int:
    if not rows:
        return 0
    if dry_run:
        LOG.info("dry-run skipped cost_raw_details upsert", extra={"row_count": len(rows)})
        return 0
    with engine.begin() as connection:
        connection.execute(_UPSERT_COST_RAW_DETAILS, list(rows))
    return len(rows)


_SELECT_COST_SOURCE = text(
    """
    SELECT vendor, account_id, billing_account_id, display_name, is_active
    FROM cost_sources
    WHERE vendor = :vendor AND account_id = :account_id
    """
)


def _build_upsert_cost_source_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            """
            INSERT INTO cost_sources (
              vendor,
              account_id,
              billing_account_id,
              display_name,
              is_active,
              updated_at
            ) VALUES (
              :vendor,
              :account_id,
              :billing_account_id,
              :display_name,
              1,
              CURRENT_TIMESTAMP
            )
            ON CONFLICT(vendor, account_id) DO UPDATE SET
              billing_account_id = COALESCE(
                excluded.billing_account_id,
                cost_sources.billing_account_id
              ),
              display_name = COALESCE(cost_sources.display_name, excluded.display_name),
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO cost_sources (
          vendor,
          account_id,
          billing_account_id,
          display_name,
          is_active
        ) VALUES (
          :vendor,
          :account_id,
          :billing_account_id,
          :display_name,
          1
        )
        ON DUPLICATE KEY UPDATE
          billing_account_id = COALESCE(VALUES(billing_account_id), billing_account_id),
          display_name = COALESCE(display_name, VALUES(display_name)),
          updated_at = CURRENT_TIMESTAMP
        """
    )


_UPSERT_COST_RAW_DETAILS = text(
    """
    INSERT INTO cost_raw_details (
      vendor,
      account_id,
      billing_account_id,
      usage_date,
      service_name,
      sku_name,
      region,
      namespace,
      author,
      org,
      repo,
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
      :usage_date,
      :service_name,
      :sku_name,
      :region,
      :namespace,
      :author,
      :org,
      :repo,
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
