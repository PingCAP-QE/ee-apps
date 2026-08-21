from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.gcp_summary_identity import build_gcp_summary_row_hash
from cost_insight.common.row_utils import bind_decimal_rows, coerce_date, hash_value, nullable_text
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.sources.gcp_billing_export import decimal_or_none
from cost_insight.sources.gcp_gke_allocation import (
    fetch_gcp_gke_node_cost_rows,
    fetch_gcp_gke_workload_usage_rows,
)

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_gcp_kubernetes_workload_allocations"
ALLOCATION_TABLE = "cost_kubernetes_workload_allocation_daily"
ALLOCATION_SOURCE_TABLE = "cost_kubernetes_workload_allocation_source_daily"
ALLOCATION_VERSION = "gke_metering_v4"
_CURRENCY_SCALE = Decimal("0.01")
_WEIGHT_SCALE = Decimal("0.0000000000000001")

NodeCostFetcher = Callable[..., Iterable[dict[str, Any]]]
WorkloadUsageFetcher = Callable[..., Iterable[dict[str, Any]]]


@dataclass(frozen=True)
class GkeNodeCost:
    usage_date: date
    source_summary_row_hash: str
    cluster_name: str | None
    cluster_location: str | None
    cost_component: str
    list_cost: Decimal

    def allocation_group_key(self) -> tuple[date, str, str, str]:
        if self.cluster_name is None or self.cluster_location is None:
            raise ValueError("GKE allocation group requires cluster dimensions")
        return (
            self.usage_date,
            self.cluster_name,
            self.cluster_location,
            self.cost_component,
        )


@dataclass(frozen=True)
class GkeWorkloadUsage:
    usage_date: date
    cluster_name: str
    cluster_location: str
    namespace: str
    workload_name: str
    workload_type: str
    author: str | None
    org: str | None
    repo: str | None
    target_branch: str | None
    cpu_seconds: Decimal
    memory_byte_seconds: Decimal

    def weight_for(self, cost_component: str) -> Decimal:
        if cost_component == "cpu":
            return self.cpu_seconds
        if cost_component == "memory":
            return self.memory_byte_seconds
        return Decimal()

    def identity(self) -> tuple[str, ...]:
        return (
            self.namespace,
            self.workload_name,
            self.workload_type,
            self.author or "",
            self.org or "",
            self.repo or "",
            self.target_branch or "",
        )


@dataclass(frozen=True)
class SyncGcpKubernetesWorkloadAllocationsSummary:
    account_id: str
    usage_start_date: date
    usage_end_date: date
    export_partition_start: date
    export_partition_end: date
    node_cost_rows_seen: int
    metering_rows_seen: int
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
    node_cost_fetcher: NodeCostFetcher = fetch_gcp_gke_node_cost_rows,
    workload_usage_fetcher: WorkloadUsageFetcher = fetch_gcp_gke_workload_usage_rows,
) -> SyncGcpKubernetesWorkloadAllocationsSummary:
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
        node_costs = tuple(
            _normalize_node_cost(row)
            for row in node_cost_fetcher(
                billing_table=settings.billing_table,
                account_id=settings.account_id,
                export_partition_start=resolved_export_start,
                export_partition_end=resolved_export_end,
                usage_start_date=usage_start_date,
                usage_end_date=usage_end_date,
                page_size=settings.page_size,
            )
        )
        workload_usage = tuple(
            _normalize_workload_usage(row)
            for row in workload_usage_fetcher(
                gke_usage_table=settings.gke_usage_table,
                account_id=settings.account_id,
                usage_start_date=usage_start_date,
                usage_end_date=usage_end_date,
                page_size=settings.page_size,
            )
        )
        rows, source_rows = build_gke_workload_allocation_rows(
            account_id=settings.account_id,
            node_costs=node_costs,
            workload_usage=workload_usage,
        )
        rows_written = replace_gke_workload_allocations(
            engine,
            rows,
            source_rows=source_rows,
            node_cost_row_count=len(node_costs),
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
            node_cost_rows_seen=len(node_costs),
            metering_rows_seen=len(workload_usage),
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
    node_costs: Iterable[GkeNodeCost],
    workload_usage: Iterable[GkeWorkloadUsage],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Build workload facts and source lineage without source-by-workload expansion.

    A billing export can have tens of thousands of rows for one cluster and day.
    Each source cost keeps a lineage row, while the allocation facts operate at the
    cluster/day/component group. The dashboard may replace the original sources
    only after every source in the group has reconciled to its allocation total.
    """
    usage_by_cluster: dict[tuple[date, str, str], list[GkeWorkloadUsage]] = defaultdict(list)
    for usage in workload_usage:
        usage_by_cluster[(usage.usage_date, usage.cluster_name, usage.cluster_location)].append(usage)

    rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    node_costs_by_group: dict[tuple[date, str, str, str], list[GkeNodeCost]] = defaultdict(list)
    for node_cost in sorted(
        node_costs,
        key=lambda item: (
            item.usage_date,
            item.cluster_name or "",
            item.cluster_location or "",
            item.cost_component,
        ),
    ):
        if (
            node_cost.cost_component in {"cpu", "memory"}
            and node_cost.cluster_name is not None
            and node_cost.cluster_location is not None
        ):
            node_costs_by_group[node_cost.allocation_group_key()].append(node_cost)

    for group_key in sorted(node_costs_by_group):
        group_node_costs = node_costs_by_group[group_key]
        usage_date, cluster_name, cluster_location, cost_component = group_key
        participants = sorted(
            (
                workload
                for workload in usage_by_cluster.get((usage_date, cluster_name, cluster_location), [])
                if workload.weight_for(cost_component) > 0
            ),
            key=GkeWorkloadUsage.identity,
        )
        if not participants:
            continue
        group_list_cost = sum((node_cost.list_cost for node_cost in group_node_costs), Decimal())
        allocation_group_hash = _allocation_group_hash(
            usage_date=usage_date,
            account_id=account_id,
            cluster_name=cluster_name,
            cluster_location=cluster_location,
            cost_component=cost_component,
        )
        rows.extend(
            _allocate_group_component_to_workloads(
                account_id=account_id,
                allocation_group_hash=allocation_group_hash,
                usage_date=usage_date,
                cluster_name=cluster_name,
                cluster_location=cluster_location,
                cost_component=cost_component,
                source_node_list_cost=group_list_cost,
                participants=participants,
            )
        )
        source_rows.extend(
            _allocation_source_row(
                account_id=account_id,
                allocation_group_hash=allocation_group_hash,
                node_cost=node_cost,
            )
            for node_cost in group_node_costs
        )
    return tuple(rows), tuple(source_rows)


def replace_gke_workload_allocations(
    engine: Engine,
    rows: Iterable[dict[str, Any]],
    *,
    source_rows: Iterable[dict[str, Any]],
    node_cost_row_count: int,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    dry_run: bool,
    batch_size: int,
) -> int:
    if node_cost_row_count <= 0:
        LOG.warning(
            "skipped GKE allocation replacement because no eligible GKE CPU or memory costs were fetched",
            extra={
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        return 0
    if dry_run:
        return 0

    rows_written = 0
    batch: list[dict[str, Any]] = []
    source_batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        connection.execute(
            _DELETE_ALLOCATIONS_FOR_USAGE_DATES,
            {
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        connection.execute(
            _DELETE_ALLOCATION_SOURCES_FOR_USAGE_DATES,
            {
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                _write_rows(connection, batch)
                rows_written += len(batch)
                batch.clear()
        if batch:
            _write_rows(connection, batch)
            rows_written += len(batch)
        for source_row in source_rows:
            source_batch.append(source_row)
            if len(source_batch) >= batch_size:
                _write_source_rows(connection, source_batch)
                source_batch.clear()
        if source_batch:
            _write_source_rows(connection, source_batch)
    return rows_written


def _allocate_group_component_to_workloads(
    *,
    account_id: str,
    allocation_group_hash: str,
    usage_date: date,
    cluster_name: str,
    cluster_location: str,
    cost_component: str,
    source_node_list_cost: Decimal,
    participants: Sequence[GkeWorkloadUsage],
) -> list[dict[str, Any]]:
    denominator = sum(
        (workload.weight_for(cost_component) for workload in participants),
        Decimal(),
    )
    remaining_cost = source_node_list_cost
    remaining_weight = Decimal(1)
    rows = []
    for workload in participants[:-1]:
        weight = (workload.weight_for(cost_component) / denominator).quantize(
            _WEIGHT_SCALE,
            rounding=ROUND_HALF_UP,
        )
        remaining_weight -= weight
        allocated_cost = (source_node_list_cost * weight).quantize(
            _CURRENCY_SCALE,
            rounding=ROUND_HALF_UP,
        )
        remaining_cost -= allocated_cost
        rows.append(
            _workload_row(
                account_id=account_id,
                allocation_group_hash=allocation_group_hash,
                usage_date=usage_date,
                cluster_name=cluster_name,
                cluster_location=cluster_location,
                cost_component=cost_component,
                source_node_list_cost=source_node_list_cost,
                workload=workload,
                allocation_weight=weight,
                list_cost=allocated_cost,
            )
        )
    final_workload = participants[-1]
    rows.append(
        _workload_row(
            account_id=account_id,
            allocation_group_hash=allocation_group_hash,
            usage_date=usage_date,
            cluster_name=cluster_name,
            cluster_location=cluster_location,
            cost_component=cost_component,
            source_node_list_cost=source_node_list_cost,
            workload=final_workload,
            allocation_weight=remaining_weight,
            list_cost=remaining_cost.quantize(_CURRENCY_SCALE, rounding=ROUND_HALF_UP),
        )
    )
    return rows


def _workload_row(
    *,
    account_id: str,
    allocation_group_hash: str,
    usage_date: date,
    cluster_name: str,
    cluster_location: str,
    cost_component: str,
    source_node_list_cost: Decimal,
    workload: GkeWorkloadUsage,
    allocation_weight: Decimal,
    list_cost: Decimal,
) -> dict[str, Any]:
    row = {
        "usage_date": usage_date,
        "vendor": "gcp",
        "account_id": account_id,
        "source_summary_row_hash": None,
        "allocation_group_hash": allocation_group_hash,
        "cluster_name": cluster_name,
        "cluster_location": cluster_location,
        "allocation_scope": "workload_split",
        "cost_component": cost_component,
        "namespace": workload.namespace,
        "workload_name": workload.workload_name,
        "workload_type": workload.workload_type,
        "author": workload.author,
        "org": workload.org,
        "repo": workload.repo,
        "target_branch": workload.target_branch,
        "allocation_weight": allocation_weight,
        "source_node_list_cost": source_node_list_cost,
        "list_cost": list_cost,
        "allocation_method": f"gke_{cost_component}_metering_weight_v2",
        "allocation_version": ALLOCATION_VERSION,
    }
    row["dimension_hash"] = _dimension_hash(row)
    return row


def _allocation_source_row(
    *,
    account_id: str,
    allocation_group_hash: str,
    node_cost: GkeNodeCost,
) -> dict[str, Any]:
    return {
        "usage_date": node_cost.usage_date,
        "vendor": "gcp",
        "account_id": account_id,
        "source_summary_row_hash": node_cost.source_summary_row_hash,
        "allocation_group_hash": allocation_group_hash,
        "source_list_cost": node_cost.list_cost,
        "allocation_version": ALLOCATION_VERSION,
    }


def _allocation_group_hash(
    *,
    usage_date: date,
    account_id: str,
    cluster_name: str,
    cluster_location: str,
    cost_component: str,
) -> str:
    payload = {
        "usage_date": usage_date.isoformat(),
        "vendor": "gcp",
        "account_id": account_id,
        "cluster_name": cluster_name,
        "cluster_location": cluster_location,
        "cost_component": cost_component,
        "allocation_version": ALLOCATION_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_node_cost(row: dict[str, Any]) -> GkeNodeCost:
    usage_date = coerce_date(row.get("usage_date"))
    cost_component = nullable_text(row.get("cost_component"))
    list_cost = decimal_or_none(row.get("list_cost"))
    account_id = nullable_text(row.get("account_id"))
    export_partition_date = coerce_date(row.get("export_partition_date"))
    if (
        usage_date is None
        or cost_component is None
        or list_cost is None
        or account_id is None
        or export_partition_date is None
    ):
        raise ValueError(f"Missing GKE node cost dimensions: {row!r}")
    if cost_component not in {"cpu", "memory", "other", "control_plane"}:
        raise ValueError(f"Unsupported GKE node cost component: {cost_component!r}")
    return GkeNodeCost(
        usage_date=usage_date,
        source_summary_row_hash=build_gcp_summary_row_hash(
            {
                **row,
                "vendor": "gcp",
                "account_id": account_id,
                "export_partition_date": export_partition_date,
                "usage_date": usage_date,
            }
        ),
        cluster_name=nullable_text(row.get("cluster_name")),
        cluster_location=nullable_text(row.get("cluster_location")),
        cost_component=cost_component,
        list_cost=list_cost.quantize(_CURRENCY_SCALE, rounding=ROUND_HALF_UP),
    )


def _normalize_workload_usage(row: dict[str, Any]) -> GkeWorkloadUsage:
    usage_date = coerce_date(row.get("usage_date"))
    required = {
        name: nullable_text(row.get(name))
        for name in ("cluster_name", "cluster_location", "namespace", "workload_name", "workload_type")
    }
    cpu_seconds = decimal_or_none(row.get("cpu_seconds")) or Decimal()
    memory_byte_seconds = decimal_or_none(row.get("memory_byte_seconds")) or Decimal()
    if usage_date is None or any(value is None for value in required.values()):
        raise ValueError(f"Missing GKE workload metering dimensions: {row!r}")
    return GkeWorkloadUsage(
        usage_date=usage_date,
        cluster_name=required["cluster_name"] or "",
        cluster_location=required["cluster_location"] or "",
        namespace=required["namespace"] or "",
        workload_name=required["workload_name"] or "",
        workload_type=required["workload_type"] or "",
        author=nullable_text(row.get("author")),
        org=nullable_text(row.get("org")),
        repo=nullable_text(row.get("repo")),
        target_branch=nullable_text(row.get("target_branch")),
        cpu_seconds=cpu_seconds,
        memory_byte_seconds=memory_byte_seconds,
    )


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
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> None:
    bound_rows = bind_decimal_rows(rows) if connection.dialect.name == "sqlite" else rows
    connection.execute(_build_upsert_statement(connection), bound_rows)


def _write_source_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> None:
    bound_rows = bind_decimal_rows(rows) if connection.dialect.name == "sqlite" else rows
    connection.execute(_build_source_upsert_statement(connection), bound_rows)


def _build_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            f"""
            INSERT INTO {ALLOCATION_TABLE} (
              usage_date, vendor, account_id, cluster_name, cluster_location,
              allocation_scope, cost_component, namespace, workload_name, workload_type,
              author, org, repo, target_branch, allocation_weight, source_node_list_cost,
              list_cost, allocation_method, allocation_version, dimension_hash, source_summary_row_hash,
              allocation_group_hash
            ) VALUES (
              :usage_date, :vendor, :account_id, :cluster_name, :cluster_location,
              :allocation_scope, :cost_component, :namespace, :workload_name, :workload_type,
              :author, :org, :repo, :target_branch, :allocation_weight, :source_node_list_cost,
              :list_cost, :allocation_method, :allocation_version, :dimension_hash, :source_summary_row_hash,
              :allocation_group_hash
            )
            ON CONFLICT(usage_date, dimension_hash) DO UPDATE SET
              allocation_weight = excluded.allocation_weight,
              source_node_list_cost = excluded.source_node_list_cost,
              list_cost = excluded.list_cost,
              allocation_method = excluded.allocation_method,
              allocation_version = excluded.allocation_version,
              source_summary_row_hash = excluded.source_summary_row_hash,
              allocation_group_hash = excluded.allocation_group_hash,
              calculated_at = CURRENT_TIMESTAMP,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        f"""
        INSERT INTO {ALLOCATION_TABLE} (
          usage_date, vendor, account_id, cluster_name, cluster_location,
          allocation_scope, cost_component, namespace, workload_name, workload_type,
          author, org, repo, target_branch, allocation_weight, source_node_list_cost,
          list_cost, allocation_method, allocation_version, dimension_hash, source_summary_row_hash,
          allocation_group_hash
        ) VALUES (
          :usage_date, :vendor, :account_id, :cluster_name, :cluster_location,
          :allocation_scope, :cost_component, :namespace, :workload_name, :workload_type,
          :author, :org, :repo, :target_branch, :allocation_weight, :source_node_list_cost,
          :list_cost, :allocation_method, :allocation_version, :dimension_hash, :source_summary_row_hash,
          :allocation_group_hash
        )
        ON DUPLICATE KEY UPDATE
          allocation_weight = VALUES(allocation_weight),
          source_node_list_cost = VALUES(source_node_list_cost),
          list_cost = VALUES(list_cost),
          allocation_method = VALUES(allocation_method),
          allocation_version = VALUES(allocation_version),
          source_summary_row_hash = VALUES(source_summary_row_hash),
          allocation_group_hash = VALUES(allocation_group_hash),
          calculated_at = CURRENT_TIMESTAMP,
          updated_at = CURRENT_TIMESTAMP
        """
    )


_DELETE_ALLOCATIONS_FOR_USAGE_DATES = text(
    f"""
    DELETE FROM {ALLOCATION_TABLE}
    WHERE vendor = 'gcp'
      AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)


_DELETE_ALLOCATION_SOURCES_FOR_USAGE_DATES = text(
    f"""
    DELETE FROM {ALLOCATION_SOURCE_TABLE}
    WHERE vendor = 'gcp'
      AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)


def _build_source_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            f"""
            INSERT INTO {ALLOCATION_SOURCE_TABLE} (
              usage_date, vendor, account_id, source_summary_row_hash,
              allocation_group_hash, source_list_cost, allocation_version
            ) VALUES (
              :usage_date, :vendor, :account_id, :source_summary_row_hash,
              :allocation_group_hash, :source_list_cost, :allocation_version
            )
            ON CONFLICT(vendor, account_id, usage_date, source_summary_row_hash) DO UPDATE SET
              allocation_group_hash = excluded.allocation_group_hash,
              source_list_cost = excluded.source_list_cost,
              allocation_version = excluded.allocation_version,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        f"""
        INSERT INTO {ALLOCATION_SOURCE_TABLE} (
          usage_date, vendor, account_id, source_summary_row_hash,
          allocation_group_hash, source_list_cost, allocation_version
        ) VALUES (
          :usage_date, :vendor, :account_id, :source_summary_row_hash,
          :allocation_group_hash, :source_list_cost, :allocation_version
        )
        ON DUPLICATE KEY UPDATE
          allocation_group_hash = VALUES(allocation_group_hash),
          source_list_cost = VALUES(source_list_cost),
          allocation_version = VALUES(allocation_version),
          updated_at = CURRENT_TIMESTAMP
        """
    )
