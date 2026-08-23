from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.row_utils import bind_decimal_rows, coerce_date, hash_value, nullable_text
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.sources.gcp_billing_export import decimal_or_none

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_gcp_kubernetes_workload_allocations"
ALLOCATION_TABLE = "cost_kubernetes_workload_allocation_daily"
ALLOCATION_SOURCE_TABLE = "cost_kubernetes_workload_allocation_source_daily"
ALLOCATION_VERSION = "gke_cost_allocation_v1"
_AMOUNT_QUANTUM = Decimal("0.000000001")
_WEIGHT = Decimal("0.0000000000000001")
_ALLOCATABLE_RESIDUALS = {"idle", "system_overhead"}


@dataclass(frozen=True)
class SyncGcpKubernetesWorkloadAllocationsSummary:
    account_id: str
    usage_start_date: date
    usage_end_date: date
    export_partition_start: date
    export_partition_end: date
    billing_rows_seen: int
    direct_rows_seen: int
    rows_written: int
    dry_run: bool


def run_sync_gcp_kubernetes_workload_allocations(
    engine: Engine,
    *,
    settings: GcpBillingSettings,
    usage_start_date: date,
    usage_end_date: date,
    export_partition_start: date | None = None,
    export_partition_end: date | None = None,
    dry_run: bool = False,
) -> SyncGcpKubernetesWorkloadAllocationsSummary:
    """Allocate native GKE residuals using native direct list-cost shares."""
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")

    resolved_export_start = export_partition_start or usage_start_date
    resolved_export_end = export_partition_end or (
        usage_end_date + timedelta(days=settings.unmatched_resource_lag_days)
    )
    job_name = source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id)
    watermark = {
        "account_id": settings.account_id,
        "usage_start_date": usage_start_date.isoformat(),
        "usage_end_date": usage_end_date.isoformat(),
        "export_partition_start": resolved_export_start.isoformat(),
        "export_partition_end": resolved_export_end.isoformat(),
        "allocation_version": ALLOCATION_VERSION,
    }

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
        with engine.begin() as connection:
            summary_rows = tuple(
                dict(row)
                for row in connection.execute(
                    _SELECT_GKE_SUMMARY_ROWS,
                    {
                        "account_id": settings.account_id,
                        "usage_start_date": usage_start_date,
                        "usage_end_date": usage_end_date,
                    },
                ).mappings()
            )
        rows, source_rows = build_gke_workload_allocation_rows(
            account_id=settings.account_id,
            summary_rows=summary_rows,
        )
        rows_written = replace_gke_workload_allocations(
            engine,
            rows,
            source_rows=source_rows,
            billing_row_count=len(summary_rows),
            account_id=settings.account_id,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            dry_run=dry_run,
            batch_size=settings.page_size,
        )
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_succeeded(connection, job_name, watermark)
        return SyncGcpKubernetesWorkloadAllocationsSummary(
            account_id=settings.account_id,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            export_partition_start=resolved_export_start,
            export_partition_end=resolved_export_end,
            billing_rows_seen=len(summary_rows),
            direct_rows_seen=sum(
                row.get("kubernetes_cost_class") == "direct" for row in summary_rows
            ),
            rows_written=rows_written,
            dry_run=dry_run,
        )
    except Exception as exc:
        LOG.exception("sync_gcp_kubernetes_workload_allocations failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def build_gke_workload_allocation_rows(
    *,
    account_id: str,
    summary_rows: Iterable[dict[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    normalized = tuple(_normalize_summary_row(row) for row in summary_rows)
    direct: dict[tuple[Any, ...], dict[tuple[str, ...], dict[str, Any]]] = defaultdict(dict)
    residual: dict[tuple[tuple[Any, ...], str], list[dict[str, Any]]] = defaultdict(list)
    allocations: list[dict[str, Any]] = []

    for row in normalized:
        key = _group_key(row)
        if row["kubernetes_cost_class"] == "direct":
            allocations.append(_source_fact(account_id=account_id, source=row, direct=True))
            if row["list_cost"] > 0:
                identity = _workload_identity(row)
                existing = direct[key].get(identity)
                if existing is None:
                    direct[key][identity] = dict(row)
                else:
                    existing["list_cost"] += row["list_cost"]
        else:
            residual[(key, row["kubernetes_residual_type"] or "unclassified")].append(row)

    source_rows: list[dict[str, Any]] = []
    for residual_key in sorted(residual):
        key, residual_type = residual_key
        group_residual = residual[residual_key]
        participants = [direct[key][identity] for identity in sorted(direct.get(key, {}))]
        denominator = sum((row["list_cost"] for row in participants), Decimal())
        source_list_cost = sum((row["list_cost"] for row in group_residual), Decimal())
        if (
            residual_type not in _ALLOCATABLE_RESIDUALS
            or denominator <= 0
            or source_list_cost == 0
        ):
            allocations.extend(
                _source_fact(account_id=account_id, source=source, direct=False)
                for source in group_residual
            )
            continue

        allocation_group_hash = _allocation_group_hash(account_id, (*key, residual_type))
        remaining_weight = Decimal(1)
        remaining_cost = source_list_cost
        for participant in participants[:-1]:
            weight = (participant["list_cost"] / denominator).quantize(
                _WEIGHT, rounding=ROUND_HALF_UP
            )
            allocated = (source_list_cost * weight).quantize(
                _AMOUNT_QUANTUM, rounding=ROUND_HALF_UP
            )
            allocations.append(
                _allocation_row(
                    account_id=account_id,
                    allocation_group_hash=allocation_group_hash,
                    source_list_cost=source_list_cost,
                    participant=participant,
                    weight=weight,
                    list_cost=allocated,
                )
            )
            remaining_weight -= weight
            remaining_cost -= allocated
        allocations.append(
            _allocation_row(
                account_id=account_id,
                allocation_group_hash=allocation_group_hash,
                source_list_cost=source_list_cost,
                participant=participants[-1],
                weight=remaining_weight,
                list_cost=remaining_cost.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_UP),
            )
        )
        source_rows.extend(
            {
                "usage_date": source["usage_date"],
                "vendor": "gcp",
                "account_id": account_id,
                "source_summary_row_hash": source["source_row_hash"],
                "allocation_group_hash": allocation_group_hash,
                "source_list_cost": source["list_cost"],
                "allocation_version": ALLOCATION_VERSION,
            }
            for source in group_residual
        )
    return tuple(allocations), tuple(source_rows)


def replace_gke_workload_allocations(
    engine: Engine,
    rows: Iterable[dict[str, Any]],
    *,
    source_rows: Iterable[dict[str, Any]],
    billing_row_count: int,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    dry_run: bool,
    batch_size: int,
) -> int:
    if billing_row_count <= 0:
        LOG.warning(
            "skipped GKE allocation replacement because no native GKE billing rows were found",
            extra={
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        return 0
    if dry_run:
        return 0

    materialized_rows = tuple(rows)
    materialized_sources = tuple(source_rows)
    params = {
        "account_id": account_id,
        "usage_start_date": usage_start_date,
        "usage_end_date": usage_end_date,
    }
    _delete_gke_rows_in_batches(
        engine,
        statement=_DELETE_ALLOCATIONS_FOR_USAGE_DATES,
        limited_statement=_DELETE_ALLOCATIONS_FOR_USAGE_DATES_LIMITED,
        params=params,
        batch_size=batch_size,
    )
    _delete_gke_rows_in_batches(
        engine,
        statement=_DELETE_ALLOCATION_SOURCES_FOR_USAGE_DATES,
        limited_statement=_DELETE_ALLOCATION_SOURCES_FOR_USAGE_DATES_LIMITED,
        params=params,
        batch_size=batch_size,
    )
    total_batches = (
        (len(materialized_rows) + batch_size - 1) // batch_size
        + (len(materialized_sources) + batch_size - 1) // batch_size
    )
    completed_batches = 0
    for start in range(0, len(materialized_rows), batch_size):
        with engine.begin() as connection:
            _write_rows(connection, materialized_rows[start : start + batch_size])
        completed_batches += 1
        _log_gke_write_progress(account_id, completed_batches, total_batches)
    for start in range(0, len(materialized_sources), batch_size):
        with engine.begin() as connection:
            _write_source_rows(connection, materialized_sources[start : start + batch_size])
        completed_batches += 1
        _log_gke_write_progress(account_id, completed_batches, total_batches)
    return len(materialized_rows)


def _delete_gke_rows_in_batches(
    engine: Engine,
    *,
    statement,
    limited_statement,
    params: dict[str, Any],
    batch_size: int,
) -> None:
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.execute(statement, params)
        return
    while True:
        with engine.begin() as connection:
            deleted = connection.execute(
                limited_statement,
                {**params, "delete_batch_size": batch_size},
            ).rowcount
        if deleted < batch_size:
            return


def _log_gke_write_progress(account_id: str, completed: int, total: int) -> None:
    LOG.info(
        "GKE allocation write progress: account=%s batches=%d/%d percent=%.1f",
        account_id,
        completed,
        total,
        completed * 100 / total if total else 100,
    )


def _normalize_summary_row(source: dict[str, Any]) -> dict[str, Any]:
    usage_date = coerce_date(source.get("usage_date"))
    required = {
        name: nullable_text(source.get(name))
        for name in (
            "source_row_hash",
            "kubernetes_cost_class",
            "kubernetes_cost_component",
            "service_name",
            "sku_name",
        )
    }
    list_cost = decimal_or_none(source.get("list_cost"))
    if usage_date is None or list_cost is None or any(value is None for value in required.values()):
        raise ValueError(f"Missing native GKE allocation dimensions: {source!r}")
    return {
        **source,
        "usage_date": usage_date,
        **required,
        "cluster_name": nullable_text(source.get("cluster_name")),
        "cluster_location": nullable_text(source.get("cluster_location")),
        "kubernetes_residual_type": nullable_text(source.get("kubernetes_residual_type")),
        "namespace": nullable_text(source.get("namespace")),
        "workload_name": nullable_text(source.get("workload_name")),
        "workload_type": nullable_text(source.get("workload_type")),
        "author": nullable_text(source.get("author")),
        "org": nullable_text(source.get("org")),
        "repo": nullable_text(source.get("repo")),
        "target_branch": nullable_text(source.get("target_branch")),
        "list_cost": list_cost.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_UP),
    }


def _group_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["usage_date"],
        row.get("cluster_name"),
        row.get("cluster_location"),
        row["service_name"],
        row["sku_name"],
        row["kubernetes_cost_component"],
    )


def _workload_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(field) or "")
        for field in (
            "namespace",
            "workload_name",
            "workload_type",
            "author",
            "org",
            "repo",
            "target_branch",
        )
    )


def _allocation_group_hash(account_id: str, key: tuple[Any, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "vendor": "gcp",
                "account_id": account_id,
                "group": [str(value or "") for value in key],
                "allocation_version": ALLOCATION_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _source_fact(
    *,
    account_id: str,
    source: dict[str, Any],
    direct: bool,
) -> dict[str, Any]:
    row = {
        "usage_date": source["usage_date"],
        "vendor": "gcp",
        "account_id": account_id,
        "source_summary_row_hash": source["source_row_hash"],
        "allocation_group_hash": None,
        "cluster_name": source.get("cluster_name"),
        "cluster_location": source.get("cluster_location"),
        "allocation_scope": "workload_split" if direct else "unallocated",
        "cost_component": source["kubernetes_cost_component"],
        "namespace": source.get("namespace") if direct else None,
        "workload_name": source.get("workload_name") if direct else None,
        "workload_type": source.get("workload_type") if direct else None,
        "author": source.get("author") if direct else None,
        "org": source.get("org") if direct else None,
        "repo": source.get("repo") if direct else None,
        "target_branch": source.get("target_branch") if direct else None,
        "allocation_weight": Decimal(1) if direct else Decimal(),
        "source_node_list_cost": source["list_cost"],
        "list_cost": source["list_cost"],
        "allocation_method": (
            "gke_native_direct" if direct else f"gke_{source['kubernetes_residual_type'] or 'unclassified'}_retained"
        ),
        "allocation_version": ALLOCATION_VERSION,
    }
    row["dimension_hash"] = _dimension_hash(row)
    return row


def _allocation_row(
    *,
    account_id: str,
    allocation_group_hash: str,
    source_list_cost: Decimal,
    participant: dict[str, Any],
    weight: Decimal,
    list_cost: Decimal,
) -> dict[str, Any]:
    row = {
        "usage_date": participant["usage_date"],
        "vendor": "gcp",
        "account_id": account_id,
        "source_summary_row_hash": None,
        "allocation_group_hash": allocation_group_hash,
        "cluster_name": participant.get("cluster_name"),
        "cluster_location": participant.get("cluster_location"),
        "allocation_scope": "workload_split",
        "cost_component": participant["kubernetes_cost_component"],
        "namespace": participant.get("namespace"),
        "workload_name": participant.get("workload_name"),
        "workload_type": participant.get("workload_type"),
        "author": participant.get("author"),
        "org": participant.get("org"),
        "repo": participant.get("repo"),
        "target_branch": participant.get("target_branch"),
        "allocation_weight": weight,
        "source_node_list_cost": source_list_cost,
        "list_cost": list_cost,
        "allocation_method": "gke_native_direct_list_cost",
        "allocation_version": ALLOCATION_VERSION,
    }
    row["dimension_hash"] = _dimension_hash(row)
    return row


def _dimension_hash(row: dict[str, Any]) -> str:
    fields = (
        "usage_date",
        "vendor",
        "account_id",
        "source_summary_row_hash",
        "allocation_group_hash",
        "cluster_name",
        "cluster_location",
        "allocation_scope",
        "cost_component",
        "namespace",
        "workload_name",
        "workload_type",
        "author",
        "org",
        "repo",
        "target_branch",
        "allocation_version",
    )
    payload = {field: hash_value(row.get(field)) for field in fields}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    bound = bind_decimal_rows(rows) if connection.dialect.name == "sqlite" else rows
    connection.execute(_build_upsert_statement(connection), bound)


def _write_source_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    bound = bind_decimal_rows(rows) if connection.dialect.name == "sqlite" else rows
    connection.execute(_build_source_upsert_statement(connection), bound)


def _build_upsert_statement(connection: Connection):
    conflict = (
        "ON CONFLICT(usage_date, dimension_hash) DO UPDATE SET "
        "allocation_weight=excluded.allocation_weight, source_node_list_cost=excluded.source_node_list_cost, "
        "list_cost=excluded.list_cost, updated_at=CURRENT_TIMESTAMP"
        if connection.dialect.name == "sqlite"
        else "ON DUPLICATE KEY UPDATE allocation_weight=VALUES(allocation_weight), "
        "source_node_list_cost=VALUES(source_node_list_cost), list_cost=VALUES(list_cost), "
        "updated_at=CURRENT_TIMESTAMP"
    )
    return text(
        f"""
        INSERT INTO {ALLOCATION_TABLE} (
          usage_date, vendor, account_id, cluster_name, cluster_location,
          allocation_scope, cost_component, namespace, workload_name, workload_type,
          author, org, repo, target_branch, allocation_weight, source_node_list_cost,
          list_cost, allocation_method, allocation_version, dimension_hash,
          source_summary_row_hash, allocation_group_hash
        ) VALUES (
          :usage_date, :vendor, :account_id, :cluster_name, :cluster_location,
          :allocation_scope, :cost_component, :namespace, :workload_name, :workload_type,
          :author, :org, :repo, :target_branch, :allocation_weight, :source_node_list_cost,
          :list_cost, :allocation_method, :allocation_version, :dimension_hash,
          :source_summary_row_hash, :allocation_group_hash
        ) {conflict}
        """
    )


def _build_source_upsert_statement(connection: Connection):
    conflict = (
        "ON CONFLICT(vendor, account_id, usage_date, source_summary_row_hash) DO UPDATE SET "
        "allocation_group_hash=excluded.allocation_group_hash, source_list_cost=excluded.source_list_cost, "
        "allocation_version=excluded.allocation_version, updated_at=CURRENT_TIMESTAMP"
        if connection.dialect.name == "sqlite"
        else "ON DUPLICATE KEY UPDATE allocation_group_hash=VALUES(allocation_group_hash), "
        "source_list_cost=VALUES(source_list_cost), allocation_version=VALUES(allocation_version), "
        "updated_at=CURRENT_TIMESTAMP"
    )
    return text(
        f"""
        INSERT INTO {ALLOCATION_SOURCE_TABLE} (
          usage_date, vendor, account_id, source_summary_row_hash,
          allocation_group_hash, source_list_cost, allocation_version
        ) VALUES (
          :usage_date, :vendor, :account_id, :source_summary_row_hash,
          :allocation_group_hash, :source_list_cost, :allocation_version
        ) {conflict}
        """
    )


_SELECT_GKE_SUMMARY_ROWS = text(
    """
    SELECT
      usage_date, vendor, account_id, service_name, sku_name, source_allocation_scope,
      cluster_name, cluster_location, kubernetes_cost_class, kubernetes_residual_type,
      kubernetes_cost_component, namespace, workload_name, workload_type, author, org,
      repo, target_branch, list_cost, effective_cost, credit_amount, net_cost,
      source_row_hash
    FROM cost_bq_export_summary_daily
    WHERE vendor = 'gcp'
      AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
      AND kubernetes_cost_class IN ('direct', 'residual')
    """
)

_DELETE_ALLOCATIONS_FOR_USAGE_DATES = text(
    f"""
    DELETE FROM {ALLOCATION_TABLE}
    WHERE vendor = 'gcp' AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)
_DELETE_ALLOCATION_SOURCES_FOR_USAGE_DATES = text(
    f"""
    DELETE FROM {ALLOCATION_SOURCE_TABLE}
    WHERE vendor = 'gcp' AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)
_DELETE_ALLOCATIONS_FOR_USAGE_DATES_LIMITED = text(
    f"""
    DELETE FROM {ALLOCATION_TABLE}
    WHERE vendor = 'gcp' AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    LIMIT :delete_batch_size
    """
)
_DELETE_ALLOCATION_SOURCES_FOR_USAGE_DATES_LIMITED = text(
    f"""
    DELETE FROM {ALLOCATION_SOURCE_TABLE}
    WHERE vendor = 'gcp' AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    LIMIT :delete_batch_size
    """
)
