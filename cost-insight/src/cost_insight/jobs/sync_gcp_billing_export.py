from __future__ import annotations

import hashlib
import json
import logging
import pickle
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, BinaryIO

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
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled, upsert_cost_source
from cost_insight.jobs.job_keys import source_job_name
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
    "target_branch",
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
    replace_existing_dates: bool = False,
    fetch_rows: RowFetcher = fetch_gcp_billing_rows,
) -> SyncGcpBillingSummary:
    resolved_end_date = end_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    job_name = source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id)
    with engine.begin() as connection:
        ensure_cost_source_enabled(
            connection,
            vendor="gcp",
            account_id=settings.account_id,
            dry_run=dry_run,
            display_name=settings.account_id,
        )
        state = state_store.get_job_state(connection, job_name)
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
            state_store.mark_job_started(connection, job_name, watermark)

    try:
        if replace_existing_dates and not dry_run:
            rows_seen = 0
            source_billing_account_id: str | None = None
            with tempfile.TemporaryFile("w+b") as row_spool:
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
                    source_billing_account_id = source_billing_account_id or normalized[
                        "billing_account_id"
                    ]
                    _dump_spooled_row(row_spool, normalized)
                rows_written = replace_raw_dates_from_spool(
                    engine,
                    row_spool,
                    account_id=settings.account_id,
                    start_date=resolved_start_date,
                    end_date=resolved_end_date,
                    source_billing_account_id=source_billing_account_id,
                    job_name=job_name,
                    watermark=watermark,
                    batch_size=settings.page_size,
                )
                return SyncGcpBillingSummary(
                    account_id=settings.account_id,
                    start_date=resolved_start_date,
                    end_date=resolved_end_date,
                    rows_seen=rows_seen,
                    rows_written=rows_written,
                    dry_run=dry_run,
                )

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
                    upsert_cost_source(
                        connection,
                        vendor="gcp",
                        account_id=settings.account_id,
                        billing_account_id=source_billing_account_id,
                        display_name=settings.account_id,
                    )
                state_store.mark_job_succeeded(connection, job_name, watermark)
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
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
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
        "target_branch": nullable_text(row.get("target_branch")),
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
        _write_rows(connection, rows)
    return len(rows)


def replace_raw_dates_from_spool(
    engine: Engine,
    row_spool: BinaryIO,
    *,
    account_id: str,
    start_date: date,
    end_date: date,
    source_billing_account_id: str | None,
    job_name: str,
    watermark: dict[str, Any],
    batch_size: int,
) -> int:
    rows_written = 0
    batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        _delete_existing_raw_dates(
            connection,
            account_id=account_id,
            start_date=start_date,
            end_date=end_date,
        )
        for row in _iter_spooled_rows(row_spool):
            batch.append(row)
            if len(batch) >= batch_size:
                _write_rows(connection, batch)
                rows_written += len(batch)
                batch.clear()
        if batch:
            _write_rows(connection, batch)
            rows_written += len(batch)
        if source_billing_account_id:
            upsert_cost_source(
                connection,
                vendor="gcp",
                account_id=account_id,
                billing_account_id=source_billing_account_id,
                display_name=account_id,
            )
        state_store.mark_job_succeeded(connection, job_name, watermark)
    return rows_written


def _write_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> None:
    bound_rows = list(rows)
    statement = _UPSERT_COST_RAW_DETAILS
    dialect_name = getattr(getattr(connection, "dialect", None), "name", None)
    if dialect_name == "sqlite":
        bound_rows = bind_decimal_rows(bound_rows)
        statement = _SQLITE_UPSERT_COST_RAW_DETAILS
    connection.execute(statement, bound_rows)


def _dump_spooled_row(row_spool: BinaryIO, row: dict[str, Any]) -> None:
    pickle.dump(row, row_spool, protocol=pickle.HIGHEST_PROTOCOL)


def _iter_spooled_rows(row_spool: BinaryIO) -> Iterable[dict[str, Any]]:
    row_spool.seek(0)
    while True:
        try:
            yield pickle.load(row_spool)
        except EOFError:
            return


def _delete_existing_raw_dates(
    connection,
    *,
    account_id: str,
    start_date: date,
    end_date: date,
) -> None:
    connection.execute(
        _DELETE_EXISTING_RAW_DATES,
        {
            "vendor": "gcp",
            "account_id": account_id,
            "start_date": start_date,
            "end_date": end_date,
        },
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
      target_branch,
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
      :target_branch,
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


_SQLITE_UPSERT_COST_RAW_DETAILS = text(
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
      target_branch,
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
      :target_branch,
      :resource_name,
      :usage_seconds,
      :list_cost,
      :effective_cost,
      :credit_amount,
      :net_cost,
      :source_export_time,
      :source_row_hash
    )
    ON CONFLICT(vendor, account_id, source_row_hash)
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


_DELETE_EXISTING_RAW_DATES = text(
    """
    DELETE FROM cost_raw_details
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND usage_date BETWEEN :start_date AND :end_date
    """
)
