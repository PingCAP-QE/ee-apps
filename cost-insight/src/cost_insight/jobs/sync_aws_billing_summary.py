from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.engine import Engine

from cost_insight.common.config import AwsBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled, upsert_cost_source
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_billing_summary import (
    SyncGcpBillingSummaryResult,
    _dump_spooled_row,
    _iter_spooled_rows,
    _normalize_summary_row,
    _select_billing_account_id,
    _watermark as gcp_watermark,
    replace_summary_partitions,
    write_summary_rows,
)
from cost_insight.sources.aws_billing_export import fetch_aws_billing_summary_rows

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_aws_billing_summary"
RowFetcher = Callable[..., Iterable[dict[str, Any]]]


@dataclass(frozen=True)
class SyncAwsBillingSummaryResult(SyncGcpBillingSummaryResult):
    pass


def run_sync_aws_billing_summary(
    engine: Engine,
    *,
    settings: AwsBillingSettings,
    account_id: str,
    export_partition_start: date | None = None,
    export_partition_end: date | None = None,
    earliest_usage_date: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    replace_existing_partitions: bool = False,
    fetch_rows: RowFetcher = fetch_aws_billing_summary_rows,
) -> SyncAwsBillingSummaryResult:
    resolved_end = export_partition_end or _month_floor(datetime.now(timezone.utc).date())
    job_name = source_job_name(JOB_NAME, vendor="aws", account_id=account_id)
    with engine.begin() as connection:
        ensure_cost_source_enabled(
            connection,
            vendor="aws",
            account_id=account_id,
            dry_run=dry_run,
            display_name=account_id,
        )
        state = state_store.get_job_state(connection, job_name)
        resolved_start = export_partition_start or _start_partition_from_state(
            state.watermark if state else {},
            end_date=resolved_end,
            overlap_months=settings.export_overlap_months,
            initial_lookback_months=settings.sync_initial_lookback_months,
        )
        watermark = _watermark(
            account_id=account_id,
            export_partition_start=resolved_start,
            export_partition_end=resolved_end,
        )
        if not dry_run:
            state_store.mark_job_started(connection, job_name, watermark)

    try:
        rows_seen = 0
        rows_written = 0
        source_billing_account_ids: set[str] = set()
        batch: list[dict[str, Any]] = []
        if replace_existing_partitions:
            with tempfile.TemporaryFile("w+b") as row_spool:
                for source_row in fetch_rows(
                    billing_table=settings.billing_table,
                    account_id=account_id,
                    export_partition_start=resolved_start,
                    export_partition_end=resolved_end,
                    earliest_usage_date=earliest_usage_date or settings.earliest_usage_date,
                    page_size=settings.page_size,
                    limit=limit,
                ):
                    rows_seen += 1
                    normalized = _normalize_summary_row(source_row)
                    if normalized["billing_account_id"]:
                        source_billing_account_ids.add(str(normalized["billing_account_id"]))
                    _dump_spooled_row(row_spool, normalized)
                rows_written += replace_summary_partitions(
                    engine,
                    _iter_spooled_rows(row_spool),
                    row_count=rows_seen,
                    vendor="aws",
                    account_id=account_id,
                    export_partition_start=resolved_start,
                    export_partition_end=resolved_end,
                    dry_run=dry_run,
                    batch_size=settings.page_size,
                )
        else:
            for source_row in fetch_rows(
                billing_table=settings.billing_table,
                account_id=account_id,
                export_partition_start=resolved_start,
                export_partition_end=resolved_end,
                earliest_usage_date=earliest_usage_date or settings.earliest_usage_date,
                page_size=settings.page_size,
                limit=limit,
            ):
                rows_seen += 1
                normalized = _normalize_summary_row(source_row)
                if normalized["billing_account_id"]:
                    source_billing_account_ids.add(str(normalized["billing_account_id"]))
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
                    upsert_cost_source(
                        connection,
                        vendor="aws",
                        account_id=account_id,
                        billing_account_id=source_billing_account_id,
                        display_name=account_id,
                    )
                touched_usage_dates = _get_touched_usage_dates(
                    engine,
                    account_id=account_id,
                    export_partition_start=resolved_start,
                    export_partition_end=resolved_end,
                )
                state_store.mark_job_succeeded(connection, job_name, watermark)

        return SyncAwsBillingSummaryResult(
            account_id=account_id,
            export_partition_start=resolved_start,
            export_partition_end=resolved_end,
            rows_seen=rows_seen,
            rows_written=rows_written,
            dry_run=dry_run,
            touched_usage_dates=touched_usage_dates,
        )
    except Exception as exc:
        LOG.exception("sync_aws_billing_summary failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def _watermark(
    *,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
) -> dict[str, Any]:
    payload = gcp_watermark(
        account_id=account_id,
        export_partition_start=export_partition_start,
        export_partition_end=export_partition_end,
    )
    payload["vendor"] = "aws"
    return payload


def _start_partition_from_state(
    watermark: dict[str, Any],
    *,
    end_date: date,
    overlap_months: int,
    initial_lookback_months: int | None,
) -> date:
    last_end_date = watermark.get("export_partition_end")
    if last_end_date:
        parsed = _month_floor(date.fromisoformat(str(last_end_date)))
        return min(_add_months(parsed, 1 - overlap_months), end_date)
    if initial_lookback_months is not None:
        return _add_months(end_date, 1 - initial_lookback_months)
    return end_date


def _month_floor(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _get_touched_usage_dates(
    engine: Engine,
    *,
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
) -> tuple[date, ...]:
    from sqlalchemy import text

    query = text(
        """
        SELECT DISTINCT usage_date
        FROM cost_bq_export_summary_daily
        WHERE vendor = 'aws'
          AND account_id = :account_id
          AND export_partition_date BETWEEN :export_partition_start AND :export_partition_end
        ORDER BY usage_date
        """
    )
    with engine.begin() as connection:
        rows = connection.execute(
            query,
            {
                "account_id": account_id,
                "export_partition_start": export_partition_start,
                "export_partition_end": export_partition_end,
            },
        ).scalars()
        return tuple(date.fromisoformat(str(value)) if isinstance(value, str) else value for value in rows)
