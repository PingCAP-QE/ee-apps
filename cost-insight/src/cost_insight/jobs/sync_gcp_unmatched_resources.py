from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.gcp_summary_identity import build_gcp_summary_row_hash
from cost_insight.common.row_utils import (
    bind_decimal_rows,
    coerce_date,
    coerce_datetime,
    hash_value,
    normalize_vendor_tags_json,
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
    "region",
    "service_name",
    "sku_name",
    "namespace",
    "author",
    "org",
    "repo",
    "target_branch",
    "vendor_tags_json",
    "resource_name",
    "source_summary_row_hash",
)
SPLIT_HASH_FIELDS = HASH_FIELDS + (
    "source_allocation_scope",
    "parent_resource_name",
    "workload_name",
    "workload_type",
    "owner",
    "service",
    "project",
    "service_exec_id",
)
UNMATCHED_RESOURCE_TABLE = "cost_unmatched_resource_daily"
# Larger batches with large resource labels exceed TiDB's per-query memory limit.
RESOURCE_WRITE_BATCH_SIZE = 10
_SQL_TABLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

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
    is_split_source = bool(row.get("source_schema_version"))
    normalized = {
        "vendor": nullable_text(row.get("vendor")) or "gcp",
        "account_id": nullable_text(row.get("account_id")),
        "billing_account_id": nullable_text(row.get("billing_account_id")),
        "export_partition_date": coerce_date(row.get("export_partition_date")),
        "usage_date": coerce_date(row.get("usage_date")),
        "region": nullable_text(row.get("region")),
        "service_name": nullable_text(row.get("service_name")),

        "sku_name": nullable_text(row.get("sku_name")),
        "namespace": nullable_text(row.get("namespace")),
        "author": nullable_text(row.get("author")),
        "org": nullable_text(row.get("org")),
        "repo": nullable_text(row.get("repo")),
        "target_branch": nullable_text(row.get("target_branch")),
        "vendor_tags_json": normalize_vendor_tags_json(row.get("vendor_tags_json")),
        # ``resource_name`` is concrete display identity. The source summary can
        # intentionally use a workload name (or NULL), so keep it separately.
        "resource_name": nullable_text(row.get("resource_name")),
        "summary_resource_name": nullable_text(row.get("summary_resource_name")),
        "parent_resource_name": nullable_text(row.get("parent_resource_name")),
        "source_schema_version": nullable_text(row.get("source_schema_version")),
        "source_allocation_scope": nullable_text(row.get("source_allocation_scope")) or "direct",
        "cluster_name": nullable_text(row.get("cluster_name")),
        "cluster_location": nullable_text(row.get("cluster_location")),
        "kubernetes_cost_class": nullable_text(row.get("kubernetes_cost_class")),
        "kubernetes_residual_type": nullable_text(row.get("kubernetes_residual_type")),
        "kubernetes_cost_component": nullable_text(row.get("kubernetes_cost_component")),
        "workload_name": nullable_text(row.get("workload_name")),
        "workload_type": nullable_text(row.get("workload_type")),

        "owner": nullable_text(row.get("owner")),
        "service": nullable_text(row.get("service")),
        "project": nullable_text(row.get("project")),
        "service_exec_id": nullable_text(row.get("service_exec_id")),
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
    normalized["is_split_source"] = is_split_source
    summary_identity = {
        **normalized,
        "resource_name": normalized["summary_resource_name"],
    }
    # GCP's summary query intentionally rolls all resource labels into one
    # attribution fact; labels remain resource metadata, not summary identity.
    if normalized["vendor"] == "gcp":
        summary_identity["vendor_tags_json"] = None
    normalized["source_summary_row_hash"] = build_gcp_summary_row_hash(summary_identity)
    normalized["source_row_hash"] = build_unmatched_resource_row_hash(normalized)
    return normalized


def build_unmatched_resource_row_hash(row: dict[str, Any]) -> str:
    hash_fields = SPLIT_HASH_FIELDS if row.get("is_split_source") else HASH_FIELDS
    if row.get("vendor_tags_json") is None:
        hash_fields = tuple(field for field in hash_fields if field != "vendor_tags_json")
    payload = {field: hash_value(row.get(field)) for field in hash_fields}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_unmatched_resource_rows(
    engine: Engine,
    rows: Sequence[dict[str, Any]],
    *,
    dry_run: bool,
    target_table: str = UNMATCHED_RESOURCE_TABLE,
) -> int:
    if not rows:
        return 0
    if dry_run:
        LOG.info(
            "dry-run skipped unmatched-resource upsert",
            extra={"row_count": len(rows), "target_table": target_table},
        )
        return 0
    with engine.begin() as connection:
        if target_table == UNMATCHED_RESOURCE_TABLE:
            _delete_superseded_unlabeled_resource_rows(connection, rows)
        for start in range(0, len(rows), RESOURCE_WRITE_BATCH_SIZE):
            _write_unmatched_resource_rows(
                connection,
                rows[start : start + RESOURCE_WRITE_BATCH_SIZE],
                target_table=target_table,
            )
        if target_table == UNMATCHED_RESOURCE_TABLE:
            _invalidate_resource_serving_publications(connection, rows)
    return len(rows)


def replace_unmatched_resource_usage_dates(
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
    target_table: str = UNMATCHED_RESOURCE_TABLE,
) -> int:
    """Replace a bounded usage-date range without deleting other month rows."""
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    if row_count <= 0:
        LOG.warning(
            "skipped unmatched-resource usage-date replacement for empty source",
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
            "dry-run skipped unmatched-resource usage-date replacement",
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
    batch_size = min(batch_size, RESOURCE_WRITE_BATCH_SIZE)
    batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        replacement_params = {
            "vendor": vendor,
            "account_id": account_id,
            "usage_start_date": usage_start_date,
            "usage_end_date": usage_end_date,
        }
        connection.execute(
            _delete_unmatched_resource_usage_dates_statement(target_table),
            replacement_params,
        )
        if target_table == UNMATCHED_RESOURCE_TABLE:
            _invalidate_resource_serving_publication_range(connection, replacement_params)
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                _write_unmatched_resource_rows(connection, batch, target_table=target_table)
                rows_written += len(batch)
                batch.clear()
        if batch:
            _write_unmatched_resource_rows(connection, batch, target_table=target_table)
            rows_written += len(batch)
    return rows_written


def _write_unmatched_resource_rows(
    connection: Connection,
    rows: Sequence[dict[str, Any]],
    *,
    target_table: str,
) -> None:
    connection.execute(
        _build_upsert_statement(connection, target_table=target_table),
        _bind_rows(connection, rows),
    )


def _invalidate_resource_serving_publication_range(
    connection: Connection,
    params: dict[str, Any],
) -> None:
    if _table_exists(connection, "cost_resource_serving_publication"):
        connection.execute(_INVALIDATE_RESOURCE_SERVING_PUBLICATION_RANGE, params)


def _invalidate_resource_serving_publications(
    connection: Connection,
    rows: Sequence[dict[str, Any]],
) -> None:
    if not _table_exists(connection, "cost_resource_serving_publication"):
        return
    for vendor, account_id, usage_date in {
        (row["vendor"], row["account_id"], row["usage_date"]) for row in rows
    }:
        connection.execute(
            _INVALIDATE_RESOURCE_SERVING_PUBLICATION,
            {"vendor": vendor, "account_id": account_id, "usage_date": usage_date},
        )


def _table_exists(connection: Connection, table_name: str) -> bool:
    if connection.dialect.name == "sqlite":
        return connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
            {"table_name": table_name},
        ).first() is not None
    return connection.execute(
        text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = :table_name
            LIMIT 1
            """
        ),
        {"table_name": table_name},
    ).first() is not None


def _bind_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    bound_rows = list(rows)
    if connection.dialect.name != "sqlite":
        return bound_rows
    return bind_decimal_rows(bound_rows)


def _delete_superseded_unlabeled_resource_rows(
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
            "namespace": row.get("namespace") or "",
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
        connection.execute(_DELETE_SUPERSEDED_UNLABELED_RESOURCE_ROWS, params)


def _build_upsert_statement(
    connection: Connection,
    *,
    target_table: str = UNMATCHED_RESOURCE_TABLE,
):
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
              region,
              service_name,
              sku_name,
              namespace,
              org,
              repo,
              target_branch,
              vendor_tags_json,
              author,
              resource_name,
              parent_resource_name,
              source_allocation_scope,
              workload_name,
              workload_type,
              owner,
              service,
              project,
              service_exec_id,
              usage_seconds,
              list_cost,
              effective_cost,
              credit_amount,
              net_cost,
              source_export_time,
              source_row_hash,
              source_summary_row_hash
            ) VALUES (
              :vendor,
              :account_id,
              :billing_account_id,
              :export_partition_date,
              :usage_date,
              :region,
              :service_name,
              :sku_name,
              :namespace,
              :org,
              :repo,
              :target_branch,
              :vendor_tags_json,
              :author,
              :resource_name,
              :parent_resource_name,
              :source_allocation_scope,
              :workload_name,
              :workload_type,
              :owner,
              :service,
              :project,
              :service_exec_id,
              :usage_seconds,
              :list_cost,
              :effective_cost,
              :credit_amount,
              :net_cost,
              :source_export_time,
              :source_row_hash,
              :source_summary_row_hash
            )
            ON CONFLICT(vendor, account_id, export_partition_date, source_row_hash)
            DO UPDATE SET
              billing_account_id = excluded.billing_account_id,
              usage_seconds = excluded.usage_seconds,
              list_cost = excluded.list_cost,
              effective_cost = excluded.effective_cost,
              credit_amount = excluded.credit_amount,
              net_cost = excluded.net_cost,
              parent_resource_name = excluded.parent_resource_name,
              source_allocation_scope = excluded.source_allocation_scope,
              workload_name = excluded.workload_name,
              workload_type = excluded.workload_type,
              owner = excluded.owner,
              service = excluded.service,
              project = excluded.project,
              service_exec_id = excluded.service_exec_id,
              source_export_time = excluded.source_export_time,
              source_summary_row_hash = excluded.source_summary_row_hash,
              region = excluded.region,
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
          region,
          service_name,
          sku_name,
          namespace,
          org,
          repo,
          target_branch,
          vendor_tags_json,
          author,
          resource_name,
          parent_resource_name,
          source_allocation_scope,
          workload_name,
          workload_type,
          owner,
          service,
          project,
          service_exec_id,
          usage_seconds,
          list_cost,
          effective_cost,
          credit_amount,
          net_cost,
          source_export_time,
          source_row_hash,
          source_summary_row_hash
        ) VALUES (
          :vendor,
          :account_id,
          :billing_account_id,
          :export_partition_date,
          :usage_date,
          :region,
          :service_name,
          :sku_name,
          :namespace,
          :org,
          :repo,
          :target_branch,
          :vendor_tags_json,
          :author,
          :resource_name,
          :parent_resource_name,
          :source_allocation_scope,
          :workload_name,
          :workload_type,
          :owner,
          :service,
          :project,
          :service_exec_id,
          :usage_seconds,
          :list_cost,
          :effective_cost,
          :credit_amount,
          :net_cost,
          :source_export_time,
          :source_row_hash,
          :source_summary_row_hash
        )
        ON DUPLICATE KEY UPDATE
          -- Dimension columns are part of source_row_hash; same hash means same dimensions.
          billing_account_id = VALUES(billing_account_id),
          usage_seconds = VALUES(usage_seconds),
          list_cost = VALUES(list_cost),
          effective_cost = VALUES(effective_cost),
          credit_amount = VALUES(credit_amount),
          net_cost = VALUES(net_cost),
          parent_resource_name = VALUES(parent_resource_name),
          source_allocation_scope = VALUES(source_allocation_scope),
          workload_name = VALUES(workload_name),
          workload_type = VALUES(workload_type),
          owner = VALUES(owner),
          service = VALUES(service),
          project = VALUES(project),
          service_exec_id = VALUES(service_exec_id),
          source_export_time = VALUES(source_export_time),
          source_summary_row_hash = VALUES(source_summary_row_hash),
          region = VALUES(region),
          updated_at = CURRENT_TIMESTAMP
        """
    )


def _quote_sql_table(table: str) -> str:
    if not _SQL_TABLE_RE.fullmatch(table):
        raise ValueError(f"Invalid SQL table identifier: {table!r}")
    return f"`{table}`"


def _delete_unmatched_resource_usage_dates_statement(target_table: str):
    return text(
        f"""
        DELETE FROM {_quote_sql_table(target_table)}
        WHERE vendor = :vendor
          AND account_id = :account_id
          AND usage_date BETWEEN :usage_start_date AND :usage_end_date
        """
    )


_INVALIDATE_RESOURCE_SERVING_PUBLICATION = text(
    """
    DELETE FROM cost_resource_serving_publication
    WHERE vendor = :vendor AND account_id = :account_id AND usage_date = :usage_date
    """
)
_INVALIDATE_RESOURCE_SERVING_PUBLICATION_RANGE = text(
    """
    DELETE FROM cost_resource_serving_publication
    WHERE vendor = :vendor AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)


_DELETE_SUPERSEDED_UNLABELED_RESOURCE_ROWS = text(
    """
    DELETE FROM cost_unmatched_resource_daily
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND COALESCE(billing_account_id, '') = :billing_account_id
      AND export_partition_date = :export_partition_date
      AND usage_date = :usage_date
      AND COALESCE(service_name, '') = :service_name
      AND COALESCE(sku_name, '') = :sku_name
      AND COALESCE(namespace, '') = :namespace
      AND COALESCE(author, '') = :author
      AND COALESCE(org, '') = :org
      AND COALESCE(repo, '') = :repo
      AND COALESCE(target_branch, '') = :target_branch
      AND resource_name = :resource_name
      AND vendor_tags_json IS NULL
    """
)
