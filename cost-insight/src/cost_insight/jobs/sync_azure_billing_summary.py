from __future__ import annotations

import hashlib
import json
import logging
import pickle
import tempfile
from decimal import Decimal, InvalidOperation
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from cost_insight.common.config import AzureBillingSettings
from cost_insight.common.cost_drivers import classify_cost_driver
from cost_insight.common.row_utils import (
    coerce_date,
    coerce_datetime,
    hash_value,
    normalize_vendor_tags_json,
    nullable_text,
)
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled, upsert_cost_source
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_billing_summary import (
    replace_summary_partitions,
    replace_summary_partition_usage_dates,
    write_summary_rows,
)
from cost_insight.sources.azure_billing_export import fetch_azure_billing_summary_rows

LOG = logging.getLogger(__name__)
JOB_NAME = "sync_azure_billing_summary"
SUMMARY_TABLE = "cost_bq_export_summary_daily"
AZURE_SUBSCRIPTIONS = (
    ("aaa5414d-7537-4e24-99bd-a7a841221810", "azure-testing-infra-dev"),
    ("abd27163-b965-4217-8cba-2a4c799579fe", "azure-testing-infra-prod-dataplane"),
)
RowFetcher = Callable[..., Iterable[dict[str, Any]]]


@dataclass(frozen=True)
class SyncAzureBillingSummaryResult:
    account_id: str
    export_partition_start: date
    export_partition_end: date
    rows_seen: int
    rows_written: int
    dry_run: bool
    touched_usage_dates: tuple[date, ...] = ()


def run_sync_azure_billing_summary(
    engine: Engine,
    *,
    settings: AzureBillingSettings,
    account_id: str,
    display_name: str | None = None,
    export_partition_start: date | None = None,
    export_partition_end: date | None = None,
    earliest_usage_date: date | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    replace_existing_partitions: bool = False,
    replacement_usage_start_date: date | None = None,
    replacement_usage_end_date: date | None = None,
    fetch_rows: RowFetcher = fetch_azure_billing_summary_rows,
) -> SyncAzureBillingSummaryResult:
    if (replacement_usage_start_date is None) != (replacement_usage_end_date is None):
        raise ValueError("replacement usage dates must be set together")
    if replacement_usage_start_date and replacement_usage_start_date > replacement_usage_end_date:
        raise ValueError("replacement usage start date must be before or equal to end date")
    if replacement_usage_start_date and not replace_existing_partitions:
        raise ValueError("scoped usage-date replacement requires replace_existing_partitions")
    requested_end = export_partition_end or datetime.now(UTC).date() - timedelta(
        days=settings.sync_lag_days
    )
    requested_start = export_partition_start
    if requested_start and requested_start > requested_end:
        raise ValueError("export partition start date must be before or equal to end date")
    if requested_start and (requested_end - requested_start).days + 1 > 5:
        raise ValueError("Azure sync supports a maximum five-day export window")
    resolved_end = _month_start(requested_end)
    explicit_start = _month_start(requested_start) if requested_start else None
    job_name = source_job_name(JOB_NAME, vendor="azure", account_id=account_id)
    with engine.begin() as connection:
        ensure_cost_source_enabled(
            connection,
            vendor="azure",
            account_id=account_id,
            dry_run=dry_run,
            display_name=display_name or account_id,
        )
        state = state_store.get_job_state(connection, job_name)
        resolved_start = explicit_start or _start_from_state(
            state.watermark if state else {}, resolved_end, settings
        )
        if replacement_usage_start_date and resolved_start != resolved_end:
            raise ValueError("scoped usage-date replacement requires one export partition")
        watermark = {
            "account_id": account_id,
            "export_partition_start": resolved_start.isoformat(),
            "export_partition_end": resolved_end.isoformat(),
        }
        if not dry_run:
            state_store.mark_job_started(connection, job_name, watermark)
    try:
        rows_seen = rows_written = 0
        billing_ids: set[str] = set()
        batch: list[dict[str, Any]] = []
        with tempfile.TemporaryFile("w+b") as spool:
            rows_iter = fetch_rows(
                billing_table=settings.billing_table,
                account_id=account_id,
                export_partition_start=resolved_start,
                export_partition_end=resolved_end,
                earliest_usage_date=earliest_usage_date or settings.earliest_usage_date,
                page_size=settings.page_size,
                limit=limit,
            )
            for source_row in rows_iter:
                rows_seen += 1
                row = _normalize_summary_row(source_row)
                if (
                    replacement_usage_start_date
                    and not replacement_usage_start_date
                    <= row["usage_date"]
                    <= replacement_usage_end_date
                ):
                    continue
                if row["billing_account_id"]:
                    billing_ids.add(row["billing_account_id"])
                if replace_existing_partitions:
                    pickle.dump(row, spool)
                else:
                    batch.append(row)
                    if len(batch) >= settings.page_size:
                        rows_written += write_summary_rows(engine, batch, dry_run=dry_run)
                        batch.clear()
            if replace_existing_partitions:
                spool.seek(0)
                rows = _iter_spooled_rows(spool)
                if replacement_usage_start_date:
                    rows_written = replace_summary_partition_usage_dates(
                        engine,
                        rows,
                        vendor="azure",
                        account_id=account_id,
                        export_partition_date=resolved_start,
                        usage_start_date=replacement_usage_start_date,
                        usage_end_date=replacement_usage_end_date,
                        dry_run=dry_run,
                        batch_size=settings.page_size,
                    )
                else:
                    rows_written = replace_summary_partitions(
                        engine,
                        rows,
                        row_count=rows_seen,
                        vendor="azure",
                        account_id=account_id,
                        export_partition_start=resolved_start,
                        export_partition_end=resolved_end,
                        dry_run=dry_run,
                        batch_size=settings.page_size,
                    )
            else:
                rows_written += write_summary_rows(engine, batch, dry_run=dry_run)
        touched = ()
        if not dry_run:
            with engine.begin() as connection:
                if len(billing_ids) == 1:
                    upsert_cost_source(
                        connection,
                        vendor="azure",
                        account_id=account_id,
                        billing_account_id=next(iter(billing_ids)),
                        display_name=display_name or account_id,
                    )
                elif billing_ids:
                    LOG.warning(
                        "Azure source has multiple billing accounts; leaving registry billing_account_id unchanged",
                        extra={
                            "account_id": account_id,
                            "billing_account_ids": sorted(billing_ids),
                        },
                    )
                touched = _get_touched_usage_dates_azure(
                    connection,
                    account_id=account_id,
                    export_partition_start=resolved_start,
                    export_partition_end=resolved_end,
                )
                state_store.mark_job_succeeded(connection, job_name, watermark)
        return SyncAzureBillingSummaryResult(
            account_id, resolved_start, resolved_end, rows_seen, rows_written, dry_run, touched
        )
    except Exception as exc:
        LOG.exception("sync_azure_billing_summary failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def _get_touched_usage_dates_azure(
    connection, *, account_id: str, export_partition_start: date, export_partition_end: date
) -> tuple[date, ...]:
    rows = connection.execute(
        text("""
        SELECT DISTINCT usage_date FROM cost_bq_export_summary_daily
        WHERE vendor = 'azure' AND account_id = :account_id
          AND export_partition_date BETWEEN :export_partition_start AND :export_partition_end
        ORDER BY usage_date
    """),
        {
            "account_id": account_id,
            "export_partition_start": export_partition_start,
            "export_partition_end": export_partition_end,
        },
    ).scalars()
    return tuple(value for value in (coerce_date(row) for row in rows) if value is not None)


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _shift_month(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def _start_from_state(
    watermark: dict[str, Any], end_date: date, settings: AzureBillingSettings
) -> date:
    previous = watermark.get("export_partition_end")
    if previous:
        previous_month = _month_start(date.fromisoformat(str(previous)))
        if settings.export_overlap_days:
            return max(_shift_month(previous_month, -1), date.min)
        return min(_shift_month(previous_month, 1), end_date)
    if settings.sync_initial_lookback_days:
        return _month_start(end_date - timedelta(days=settings.sync_initial_lookback_days - 1))
    return end_date


def _normalize_summary_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "vendor": "azure",
        "account_id": nullable_text(row.get("account_id")),
        "billing_account_id": nullable_text(row.get("billing_account_id")),
        "export_partition_date": coerce_date(row.get("export_partition_date")),
        "usage_date": coerce_date(row.get("usage_date")),
        "service_name": nullable_text(row.get("service_name")),
        "sku_name": nullable_text(row.get("sku_name")),
        "usage_type": nullable_text(row.get("usage_type")),
        "currency": nullable_text(row.get("currency")),
        "region": nullable_text(row.get("region")),
        "resource_name": nullable_text(row.get("resource_name")),
        "vendor_tags_json": normalize_vendor_tags_json(row.get("vendor_tags_json")),
        "author": None,
        "org": None,
        "repo": None,
        "target_branch": None,
        "source_schema_version": None,
        "source_allocation_scope": "direct",
        "cluster_name": None,
        "cluster_location": None,
        "kubernetes_cost_class": None,
        "kubernetes_residual_type": None,
        "kubernetes_cost_component": None,
        "namespace": None,
        "workload_name": None,
        "workload_type": None,
        "owner": None,
        "service": None,
        "project": None,
        "service_exec_id": None,
        "list_cost": _decimal(row.get("list_cost")),
        "effective_cost": _decimal(row.get("effective_cost")),
        "credit_amount": _decimal(row.get("credit_amount")) or 0,
        "net_cost": _decimal(row.get("net_cost")),
        "source_export_time": coerce_datetime(row.get("source_export_time")),
    }
    if (
        normalized["account_id"] is None
        or normalized["export_partition_date"] is None
        or normalized["usage_date"] is None
    ):
        raise ValueError(f"Missing required Azure summary identity field: {row!r}")
    normalized["cost_driver_key"] = classify_cost_driver(normalized)
    normalized["source_row_hash"] = _build_hash(normalized)
    return normalized


def _iter_spooled_rows(spool):
    while True:
        try:
            yield pickle.load(spool)
        except EOFError:
            return


def _decimal(value: Any):
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid Azure cost value: {value!r}") from exc


def _build_hash(row: dict[str, Any]) -> str:
    fields = (
        "vendor",
        "account_id",
        "billing_account_id",
        "export_partition_date",
        "usage_date",
        "service_name",
        "sku_name",
        "usage_type",
        "currency",
        "region",
        "resource_name",
        "vendor_tags_json",
    )
    payload = {field: hash_value(row.get(field)) for field in fields}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
