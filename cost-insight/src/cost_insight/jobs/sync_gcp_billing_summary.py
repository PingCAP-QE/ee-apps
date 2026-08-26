from __future__ import annotations

import logging
import pickle
import re
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, BinaryIO

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.cost_drivers import classify_cost_driver
from cost_insight.common.gcp_summary_identity import build_gcp_summary_row_hash
from cost_insight.common.row_utils import (
    bind_decimal_rows,
    coerce_date,
    coerce_datetime,
    normalize_vendor_tags_json,
    nullable_text,
)
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled, upsert_cost_source
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.sources.gcp_billing_export import (
    DEFAULT_COST_OWNER_AUTHOR,
    decimal_or_none,
    fetch_gcp_billing_summary_rows,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_gcp_billing_summary"
OWNER_OVERRIDE_DELETE_CHUNK_SIZE = 1000
SUMMARY_TABLE = "cost_bq_export_summary_daily"
_SQL_TABLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# usage_type and cost_driver_key are derived display fields, not source row identity.

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
    replace_existing_partitions: bool = False,
    replacement_usage_start_date: date | None = None,
    replacement_usage_end_date: date | None = None,
    fetch_rows: RowFetcher = fetch_gcp_billing_summary_rows,
) -> SyncGcpBillingSummaryResult:
    if (replacement_usage_start_date is None) != (replacement_usage_end_date is None):
        raise ValueError(
            "replacement_usage_start_date and replacement_usage_end_date must be set together"
        )
    if replacement_usage_start_date and replacement_usage_start_date > replacement_usage_end_date:
        raise ValueError("replacement usage start date must be before or equal to end date")
    if replacement_usage_start_date and not replace_existing_partitions:
        raise ValueError("scoped usage-date replacement requires replace_existing_partitions")

    resolved_end = export_partition_end or (
        datetime.now(UTC).date() - timedelta(days=settings.sync_lag_days)
    )
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
        resolved_start = export_partition_start or _start_partition_from_state(
            state.watermark if state else {},
            end_date=resolved_end,
            overlap_days=settings.export_overlap_days,
            initial_lookback_days=settings.sync_initial_lookback_days,
        )
        if replacement_usage_start_date and resolved_start != resolved_end:
            raise ValueError("scoped usage-date replacement requires one export partition")
        watermark = _watermark(
            account_id=settings.account_id,
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
                    account_id=settings.account_id,
                    export_partition_start=resolved_start,
                    export_partition_end=resolved_end,
                    earliest_usage_date=earliest_usage_date or settings.earliest_usage_date,
                    page_size=settings.page_size,
                    limit=limit,
                ):
                    rows_seen += 1
                    normalized = _normalize_summary_row(source_row)
                    if replacement_usage_start_date and not (
                        replacement_usage_start_date
                        <= normalized["usage_date"]
                        <= replacement_usage_end_date
                    ):
                        continue
                    if normalized["billing_account_id"]:
                        source_billing_account_ids.add(normalized["billing_account_id"])
                    if not dry_run:
                        _dump_spooled_row(row_spool, normalized)
                if replacement_usage_start_date:
                    rows_written += replace_summary_partition_usage_dates(
                        engine,
                        _iter_spooled_rows(row_spool),
                        vendor="gcp",
                        account_id=settings.account_id,
                        export_partition_date=resolved_start,
                        usage_start_date=replacement_usage_start_date,
                        usage_end_date=replacement_usage_end_date,
                        dry_run=dry_run,
                        batch_size=settings.page_size,
                    )
                else:
                    rows_written += replace_summary_partitions(
                        engine,
                        _iter_spooled_rows(row_spool),
                        row_count=rows_seen,
                        vendor="gcp",
                        account_id=settings.account_id,
                        export_partition_start=resolved_start,
                        export_partition_end=resolved_end,
                        dry_run=dry_run,
                        batch_size=settings.page_size,
                    )
        else:
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
                    upsert_cost_source(
                        connection,
                        vendor="gcp",
                        account_id=settings.account_id,
                        billing_account_id=source_billing_account_id,
                        display_name=settings.account_id,
                    )
                touched_usage_dates = (
                    tuple(
                        replacement_usage_start_date + timedelta(days=offset)
                        for offset in range(
                            (replacement_usage_end_date - replacement_usage_start_date).days + 1
                        )
                    )
                    if replacement_usage_start_date
                    else _get_touched_usage_dates(
                        connection,
                        account_id=settings.account_id,
                        export_partition_start=resolved_start,
                        export_partition_end=resolved_end,
                    )
                )
                state_store.mark_job_succeeded(connection, job_name, watermark)

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
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
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
    return min(billing_account_ids)


def _normalize_summary_row(row: dict[str, Any]) -> dict[str, Any]:
    is_split_source = bool(row.get("source_schema_version")) or row.get(
        "source_allocation_scope"
    ) not in {None, "direct"}
    normalized = {
        "vendor": nullable_text(row.get("vendor")) or "gcp",
        "account_id": nullable_text(row.get("account_id")),
        "billing_account_id": nullable_text(row.get("billing_account_id")),
        "export_partition_date": coerce_date(row.get("export_partition_date")),
        "usage_date": coerce_date(row.get("usage_date")),
        "service_name": nullable_text(row.get("service_name")),
        "sku_name": nullable_text(row.get("sku_name")),
        "usage_type": nullable_text(row.get("usage_type")),
        "region": nullable_text(row.get("region")),
        "author": nullable_text(row.get("author")),
        "org": nullable_text(row.get("org")),
        "repo": nullable_text(row.get("repo")),
        "target_branch": nullable_text(row.get("target_branch")),
        "resource_name": nullable_text(row.get("resource_name")),
        "vendor_tags_json": normalize_vendor_tags_json(row.get("vendor_tags_json")),
        "source_schema_version": nullable_text(row.get("source_schema_version")),
        "source_allocation_scope": nullable_text(row.get("source_allocation_scope")) or "direct",
        "cluster_name": nullable_text(row.get("cluster_name")),
        "cluster_location": nullable_text(row.get("cluster_location")),
        "kubernetes_cost_class": nullable_text(row.get("kubernetes_cost_class")),
        "kubernetes_residual_type": nullable_text(row.get("kubernetes_residual_type")),
        "kubernetes_cost_component": nullable_text(row.get("kubernetes_cost_component")),
        "namespace": nullable_text(row.get("namespace")),
        "workload_name": nullable_text(row.get("workload_name")),
        "workload_type": nullable_text(row.get("workload_type")),
        "owner": nullable_text(row.get("owner")),
        "service": nullable_text(row.get("service")),
        "project": nullable_text(row.get("project")),
        "service_exec_id": nullable_text(row.get("service_exec_id")),
        "list_cost": decimal_or_none(row.get("list_cost")),
        "effective_cost": decimal_or_none(row.get("effective_cost")),
        "credit_amount": decimal_or_none(row.get("credit_amount")),
        "net_cost": decimal_or_none(row.get("net_cost")),
        "source_export_time": coerce_datetime(row.get("source_export_time")),
    }
    normalized["cost_driver_key"] = classify_cost_driver(normalized)
    if normalized["account_id"] is None:
        raise ValueError(f"Missing account_id in billing summary row: {row!r}")
    if normalized["export_partition_date"] is None:
        raise ValueError(f"Missing export_partition_date in billing summary row: {row!r}")
    if normalized["usage_date"] is None:
        raise ValueError(f"Missing usage_date in billing summary row: {row!r}")
    normalized["is_split_source"] = is_split_source
    normalized["source_row_hash"] = build_summary_row_hash(normalized)
    return normalized


def build_summary_row_hash(row: dict[str, Any]) -> str:
    return build_gcp_summary_row_hash(row)


def write_summary_rows(
    engine: Engine,
    rows: Sequence[dict[str, Any]],
    *,
    dry_run: bool,
    target_table: str = SUMMARY_TABLE,
) -> int:
    if not rows:
        return 0
    if dry_run:
        LOG.info("dry-run skipped summary upsert", extra={"row_count": len(rows), "target_table": target_table})
        return 0
    with engine.begin() as connection:
        _write_summary_rows(connection, rows, target_table=target_table)
    return len(rows)


def replace_summary_partitions(
    engine: Engine,
    rows: Iterable[dict[str, Any]],
    *,
    row_count: int,
    vendor: str = "gcp",
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    dry_run: bool,
    batch_size: int,
    target_table: str = SUMMARY_TABLE,
) -> int:
    if dry_run:
        LOG.info(
            "dry-run skipped summary partition replacement",
            extra={
                "row_count": row_count,
                "vendor": vendor,
                "account_id": account_id,
                "export_partition_start": export_partition_start,
                "export_partition_end": export_partition_end,
                "target_table": target_table,
            },
        )
        return 0
    rows_written = 0
    batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        _delete_existing_summary_partitions(
            connection,
            vendor=vendor,
            account_id=account_id,
            export_partition_start=export_partition_start,
            export_partition_end=export_partition_end,
            target_table=target_table,
        )
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                _write_summary_rows(
                    connection,
                    batch,
                    cleanup_superseded=False,
                    target_table=target_table,
                )
                rows_written += len(batch)
                batch.clear()
        if batch:
            _write_summary_rows(
                connection,
                batch,
                cleanup_superseded=False,
                target_table=target_table,
            )
            rows_written += len(batch)
    return rows_written


def _write_summary_rows(
    connection: Connection,
    rows: Sequence[dict[str, Any]],
    *,
    cleanup_superseded: bool = True,
    target_table: str = SUMMARY_TABLE,
) -> None:
    if not rows:
        return
    if cleanup_superseded and target_table == SUMMARY_TABLE:
        _delete_legacy_summary_rows(connection, rows)
        _delete_superseded_unlabeled_summary_rows(connection, rows)
        _delete_superseded_owner_override_rows(connection, rows)
    connection.execute(
        _build_upsert_statement(connection, target_table=target_table),
        _bind_rows(connection, rows),
    )


def replace_summary_partition_usage_dates(
    engine: Engine,
    rows: Iterable[dict[str, Any]],
    *,
    vendor: str,
    account_id: str,
    export_partition_date: date,
    usage_start_date: date,
    usage_end_date: date,
    dry_run: bool,
    batch_size: int,
    target_table: str = SUMMARY_TABLE,
) -> int:
    """Replace one export partition within a bounded usage-date scope.

    An empty source is authoritative and removes stale summary rows in the scope,
    so callers must refresh every requested usage date downstream.
    """
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if dry_run:
        return 0

    rows_written = 0
    batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        connection.execute(
            _delete_summary_partition_usage_dates_statement(target_table),
            {
                "vendor": vendor,
                "account_id": account_id,
                "export_partition_date": export_partition_date,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        for row in rows:
            row_export_partition_date = coerce_date(row.get("export_partition_date"))
            row_usage_date = coerce_date(row.get("usage_date"))
            if (
                row.get("vendor") != vendor
                or row.get("account_id") != account_id
                or row_export_partition_date != export_partition_date
                or row_usage_date is None
                or not usage_start_date <= row_usage_date <= usage_end_date
            ):
                raise ValueError(
                    "summary row is outside the partition usage-date replacement scope: "
                    f"{row.get('source_row_hash')}"
                )
            batch.append(row)
            if len(batch) >= batch_size:
                _write_summary_rows(
                    connection,
                    batch,
                    cleanup_superseded=False,
                    target_table=target_table,
                )
                rows_written += len(batch)
                batch.clear()
        if batch:
            _write_summary_rows(
                connection,
                batch,
                cleanup_superseded=False,
                target_table=target_table,
            )
            rows_written += len(batch)
    return rows_written


def replace_summary_usage_dates(
    engine: Engine,
    rows: Iterable[dict[str, Any]],
    *,
    row_count: int,
    vendor: str,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    dry_run: bool,
    batch_size: int,
    target_table: str = SUMMARY_TABLE,
) -> int:
    """Replace a bounded usage-date range without touching sibling export dates."""
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    if row_count <= 0:
        LOG.warning(
            "skipped summary usage-date replacement for empty source",
            extra={
                "row_count": row_count,
                "vendor": vendor,
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
                "target_table": target_table,
            },
        )
        return 0
    if dry_run:
        LOG.info(
            "dry-run skipped summary usage-date replacement",
            extra={
                "row_count": row_count,
                "vendor": vendor,
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
                "target_table": target_table,
            },
        )
        return 0
    rows_written = 0
    batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        connection.execute(
            _delete_summary_usage_dates_statement(target_table),
            {
                "vendor": vendor,
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                _write_summary_rows(
                    connection,
                    batch,
                    cleanup_superseded=False,
                    target_table=target_table,
                )
                rows_written += len(batch)
                batch.clear()
        if batch:
            _write_summary_rows(
                connection,
                batch,
                cleanup_superseded=False,
                target_table=target_table,
            )
            rows_written += len(batch)
    return rows_written


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
                "region": row.get("region") or "",
                "org": row.get("org") or "",
                "repo": row.get("repo") or "",
                "target_branch": row.get("target_branch") or "",
                "vendor_tags_json": row.get("vendor_tags_json") or "",
            }
            for row in rows[offset : offset + OWNER_OVERRIDE_DELETE_CHUNK_SIZE]
            if _is_owner_override_row(row)
        ]
        if params:
            connection.execute(_DELETE_SUPERSEDED_OWNER_OVERRIDE_ROWS, params)


def _delete_superseded_unlabeled_summary_rows(
    connection: Connection,
    rows: Sequence[dict[str, Any]],
) -> None:
    # Label backfills change the hash shape; remove the old legacy unlabeled row first.
    # The reverse direction is handled by partition replacement to avoid deleting
    # legitimate labeled rows when labeled and unlabeled groups coexist.
    params = [
        {
            "vendor": row.get("vendor") or "",
            "account_id": row.get("account_id") or "",
            "billing_account_id": row.get("billing_account_id") or "",
            "export_partition_date": row["export_partition_date"],
            "usage_date": row["usage_date"],
            "service_name": row.get("service_name") or "",
            "sku_name": row.get("sku_name") or "",
            "region": row.get("region") or "",
            "author": row.get("author") or "",
            "org": row.get("org") or "",
            "repo": row.get("repo") or "",
            "target_branch": row.get("target_branch") or "",
            "resource_name": row.get("resource_name") or "",
        }
        for row in rows
        if row.get("vendor_tags_json") is not None
    ]
    if params:
        connection.execute(_DELETE_SUPERSEDED_UNLABELED_SUMMARY_ROWS, params)


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


def _delete_existing_summary_partitions(
    connection: Connection,
    *,
    vendor: str = "gcp",
    account_id: str,
    export_partition_start: date,
    export_partition_end: date,
    target_table: str = SUMMARY_TABLE,
) -> None:
    connection.execute(
        _delete_summary_partitions_statement(target_table),
        {
            "vendor": vendor,
            "account_id": account_id,
            "export_partition_start": export_partition_start,
            "export_partition_end": export_partition_end,
        },
    )


def _build_upsert_statement(connection: Connection, *, target_table: str = SUMMARY_TABLE):
    table = _quote_sql_table(target_table)
    if connection.dialect.name == "sqlite":
        return text(
            f"""
            INSERT INTO {table} (
              vendor,
              account_id,
              billing_account_id,
              export_partition_date,
              usage_date,
              service_name,
              sku_name,
              usage_type,
              cost_driver_key,
              region,
              org,
              repo,
              target_branch,
              resource_name,
              vendor_tags_json,
              author,
              source_schema_version,
              source_allocation_scope,
              cluster_name,
              cluster_location,
              kubernetes_cost_class,
              kubernetes_residual_type,
              kubernetes_cost_component,
              namespace,
              workload_name,
              workload_type,
              owner,
              service,
              project,
              service_exec_id,
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
              :usage_type,
              :cost_driver_key,
              :region,
              :org,
              :repo,
              :target_branch,
              :resource_name,
              :vendor_tags_json,
              :author,
              :source_schema_version,
              :source_allocation_scope,
              :cluster_name,
              :cluster_location,
              :kubernetes_cost_class,
              :kubernetes_residual_type,
              :kubernetes_cost_component,
              :namespace,
              :workload_name,
              :workload_type,
              :owner,
              :service,
              :project,
              :service_exec_id,
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
          usage_type = excluded.usage_type,
          cost_driver_key = excluded.cost_driver_key,
          region = excluded.region,
          resource_name = excluded.resource_name,
          source_schema_version = excluded.source_schema_version,
          source_allocation_scope = excluded.source_allocation_scope,
          cluster_name = excluded.cluster_name,
          cluster_location = excluded.cluster_location,
          kubernetes_cost_class = excluded.kubernetes_cost_class,
          kubernetes_residual_type = excluded.kubernetes_residual_type,
          kubernetes_cost_component = excluded.kubernetes_cost_component,
          namespace = excluded.namespace,
          workload_name = excluded.workload_name,
          workload_type = excluded.workload_type,
          owner = excluded.owner,
          service = excluded.service,
          project = excluded.project,
          service_exec_id = excluded.service_exec_id,
          source_export_time = excluded.source_export_time,
          updated_at = CURRENT_TIMESTAMP
        """
        )
    return text(
        f"""
        INSERT INTO {table} (
          vendor,
          account_id,
          billing_account_id,
          export_partition_date,
          usage_date,
          service_name,
          sku_name,
          usage_type,
          cost_driver_key,
          region,
          org,
          repo,
          target_branch,
          resource_name,
          vendor_tags_json,
          author,
          source_schema_version,
          source_allocation_scope,
          cluster_name,
          cluster_location,
          kubernetes_cost_class,
          kubernetes_residual_type,
          kubernetes_cost_component,
          namespace,
          workload_name,
          workload_type,
          owner,
          service,
          project,
          service_exec_id,
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
          :usage_type,
          :cost_driver_key,
          :region,
          :org,
          :repo,
          :target_branch,
          :resource_name,
          :vendor_tags_json,
          :author,
          :source_schema_version,
          :source_allocation_scope,
          :cluster_name,
          :cluster_location,
          :kubernetes_cost_class,
          :kubernetes_residual_type,
          :kubernetes_cost_component,
          :namespace,
          :workload_name,
          :workload_type,
          :owner,
          :service,
          :project,
          :service_exec_id,
          :list_cost,
          :effective_cost,
          :credit_amount,
          :net_cost,
          :source_export_time,
          :source_row_hash
        )
        ON DUPLICATE KEY UPDATE
          -- usage_type and cost_driver_key are derived display fields refreshed in place.
          billing_account_id = VALUES(billing_account_id),
          list_cost = VALUES(list_cost),
          effective_cost = VALUES(effective_cost),
          credit_amount = VALUES(credit_amount),
          net_cost = VALUES(net_cost),
          service_name = VALUES(service_name),
          sku_name = VALUES(sku_name),
          usage_type = VALUES(usage_type),
          cost_driver_key = VALUES(cost_driver_key),
          region = VALUES(region),
          resource_name = VALUES(resource_name),
          source_schema_version = VALUES(source_schema_version),
          source_allocation_scope = VALUES(source_allocation_scope),
          cluster_name = VALUES(cluster_name),
          cluster_location = VALUES(cluster_location),
          kubernetes_cost_class = VALUES(kubernetes_cost_class),
          kubernetes_residual_type = VALUES(kubernetes_residual_type),
          kubernetes_cost_component = VALUES(kubernetes_cost_component),
          namespace = VALUES(namespace),
          workload_name = VALUES(workload_name),
          workload_type = VALUES(workload_type),
          owner = VALUES(owner),
          service = VALUES(service),
          project = VALUES(project),
          service_exec_id = VALUES(service_exec_id),
          source_export_time = VALUES(source_export_time),
          updated_at = CURRENT_TIMESTAMP
        """
    )


def _quote_sql_table(table: str) -> str:
    if not _SQL_TABLE_RE.fullmatch(table):
        raise ValueError(f"Invalid SQL table identifier: {table!r}")
    return f"`{table}`"


def _delete_summary_partition_usage_dates_statement(target_table: str):
    return text(
        f"""
        DELETE FROM {_quote_sql_table(target_table)}
        WHERE vendor = :vendor
          AND account_id = :account_id
          AND export_partition_date = :export_partition_date
          AND usage_date BETWEEN :usage_start_date AND :usage_end_date
        """
    )


def _delete_summary_usage_dates_statement(target_table: str):
    return text(
        f"""
        DELETE FROM {_quote_sql_table(target_table)}
        WHERE vendor = :vendor
          AND account_id = :account_id
          AND usage_date BETWEEN :usage_start_date AND :usage_end_date
        """
    )


def _delete_summary_partitions_statement(target_table: str):
    return text(
        f"""
        DELETE FROM {_quote_sql_table(target_table)}
        WHERE vendor = :vendor
          AND account_id = :account_id
          AND export_partition_date BETWEEN :export_partition_start AND :export_partition_end
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


_DELETE_EXISTING_SUMMARY_PARTITIONS = text(
    """
    DELETE FROM cost_bq_export_summary_daily
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND export_partition_date BETWEEN :export_partition_start AND :export_partition_end
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
      AND COALESCE(region, '') = :region
      AND COALESCE(org, '') = :org
      AND COALESCE(repo, '') = :repo
      AND COALESCE(target_branch, '') = :target_branch
      AND COALESCE(vendor_tags_json, '') = :vendor_tags_json
      AND author IS NULL
    """
)


_DELETE_SUPERSEDED_UNLABELED_SUMMARY_ROWS = text(
    """
    DELETE FROM cost_bq_export_summary_daily
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND COALESCE(billing_account_id, '') = :billing_account_id
      AND export_partition_date = :export_partition_date
      AND usage_date = :usage_date
      AND COALESCE(service_name, '') = :service_name
      AND COALESCE(sku_name, '') = :sku_name
      AND COALESCE(region, '') = :region
      AND COALESCE(author, '') = :author
      AND COALESCE(org, '') = :org
      AND COALESCE(repo, '') = :repo
      AND COALESCE(target_branch, '') = :target_branch
      AND COALESCE(resource_name, '') = :resource_name
      AND vendor_tags_json IS NULL
    """
)


def _dump_spooled_row(row_spool: BinaryIO, row: dict[str, Any]) -> None:
    pickle.dump(row, row_spool, protocol=pickle.HIGHEST_PROTOCOL)


def _iter_spooled_rows(row_spool: BinaryIO) -> Iterable[dict[str, Any]]:
    row_spool.seek(0)
    while True:
        try:
            yield pickle.load(row_spool)
        except EOFError:
            return
