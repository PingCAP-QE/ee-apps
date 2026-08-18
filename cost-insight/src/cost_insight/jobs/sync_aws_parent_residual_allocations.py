from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
import logging
import pickle
import tempfile
from typing import Any, BinaryIO

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.row_utils import bind_decimal_rows, coerce_date, nullable_text
from cost_insight.jobs.parent_residual_allocation import (
    ParentResidualInput,
    PodSplitInput,
    allocate_parent_residual_list_cost,
)
from cost_insight.jobs.sync_aws_billing_summary import AwsBillingSource
from cost_insight.sources.aws_split_cost_export import (
    fetch_aws_split_cost_parent_residual_allocation_rows,
)
from cost_insight.sources.gcp_billing_export import decimal_or_none

ALLOCATION_TABLE = "cost_aws_parent_residual_allocation_daily"
RowFetcher = Callable[..., Iterable[dict[str, Any]]]
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncAwsParentResidualAllocationsResult:
    account_id: str
    usage_start_date: date
    usage_end_date: date
    rows_seen: int
    rows_written: int
    parent_days: int
    dry_run: bool


def run_sync_aws_parent_residual_allocations(
    engine: Engine,
    *,
    source: AwsBillingSource,
    usage_start_date: date,
    usage_end_date: date,
    export_partition_start: date,
    export_partition_end: date,
    page_size: int,
    dry_run: bool = False,
    validate_guardrail: bool = True,
    fetch_rows: RowFetcher = fetch_aws_split_cost_parent_residual_allocation_rows,
) -> SyncAwsParentResidualAllocationsResult:
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    if source.available_from is not None and usage_start_date < source.available_from:
        raise ValueError("usage_start_date is before the AWS source availability date")

    rows_seen = 0
    parent_days = 0
    with tempfile.TemporaryFile("w+b") as allocation_spool:
        current_parent: ParentResidualInput | None = None
        current_pods: list[PodSplitInput] = []
        for source_row in fetch_rows(
            billing_table=source.billing_table,
            account_id=source.account_id,
            export_partition_start=export_partition_start,
            export_partition_end=export_partition_end,
            earliest_usage_date=usage_start_date,
            usage_end_date=usage_end_date,
            page_size=page_size,
            validate_guardrail=validate_guardrail,
        ):
            rows_seen += 1
            parent, pod = _normalize_ledger_row(source_row)
            if current_parent is not None and parent != current_parent:
                _spool_parent_allocations(allocation_spool, current_parent, current_pods)
                parent_days += 1
                current_pods = []
            current_parent = parent
            current_pods.append(pod)
        if current_parent is not None:
            _spool_parent_allocations(allocation_spool, current_parent, current_pods)
            parent_days += 1
        rows_written = replace_parent_residual_allocations(
            engine,
            _iter_spooled_rows(allocation_spool),
            source_row_count=rows_seen,
            vendor="aws",
            account_id=source.account_id,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            dry_run=dry_run,
            batch_size=page_size,
        )
    return SyncAwsParentResidualAllocationsResult(
        account_id=source.account_id,
        usage_start_date=usage_start_date,
        usage_end_date=usage_end_date,
        rows_seen=rows_seen,
        rows_written=rows_written,
        parent_days=parent_days,
        dry_run=dry_run,
    )


def _normalize_ledger_row(row: dict[str, Any]) -> tuple[ParentResidualInput, PodSplitInput]:
    usage_date = coerce_date(row.get("usage_date"))
    account_id = nullable_text(row.get("account_id"))
    parent_resource_id = nullable_text(row.get("parent_resource_id"))
    pod_resource_id = nullable_text(row.get("pod_resource_id"))
    parent_direct_list_cost = decimal_or_none(row.get("parent_direct_list_cost"))
    parent_residual_list_cost = decimal_or_none(row.get("parent_residual_list_cost"))
    source_pod_split_list_cost = decimal_or_none(row.get("source_pod_split_list_cost"))
    if usage_date is None or account_id is None or parent_resource_id is None or pod_resource_id is None:
        raise ValueError(f"Missing parent/pod identity in residual allocation row: {row!r}")
    if (
        parent_direct_list_cost is None
        or parent_residual_list_cost is None
        or source_pod_split_list_cost is None
    ):
        raise ValueError(f"Missing cost in residual allocation row: {row!r}")
    parent = ParentResidualInput(
        usage_date=usage_date,
        vendor=nullable_text(row.get("vendor")) or "aws",
        account_id=account_id,
        parent_resource_id=parent_resource_id,
        parent_direct_list_cost=parent_direct_list_cost,
        parent_residual_list_cost=parent_residual_list_cost,
    )
    pod = PodSplitInput(
        pod_resource_id=pod_resource_id,
        source_pod_split_list_cost=source_pod_split_list_cost,
        namespace=nullable_text(row.get("namespace")),
        workload_name=nullable_text(row.get("workload_name")),
        workload_type=nullable_text(row.get("workload_type")),
        owner=nullable_text(row.get("owner")),
        service=nullable_text(row.get("service")),
        project=nullable_text(row.get("project")),
        service_exec_id=nullable_text(row.get("service_exec_id")),
    )
    return parent, pod


def _allocation_row(allocation) -> dict[str, Any]:
    return {
        "usage_date": allocation.parent.usage_date,
        "vendor": allocation.parent.vendor,
        "account_id": allocation.parent.account_id,
        "parent_resource_id": allocation.parent.parent_resource_id,
        "pod_resource_id": allocation.pod.pod_resource_id,
        "namespace": allocation.pod.namespace,
        "workload_name": allocation.pod.workload_name,
        "workload_type": allocation.pod.workload_type,
        "owner": allocation.pod.owner,
        "service": allocation.pod.service,
        "project": allocation.pod.project,
        "service_exec_id": allocation.pod.service_exec_id,
        "source_pod_split_list_cost": allocation.pod.source_pod_split_list_cost,
        "parent_direct_list_cost": allocation.parent.parent_direct_list_cost,
        "parent_residual_list_cost": allocation.parent.parent_residual_list_cost,
        "allocation_weight": allocation.allocation_weight,
        "derived_parent_residual_list_cost": allocation.derived_parent_residual_list_cost,
        "allocation_origin": allocation.allocation_origin,
        "allocation_method": allocation.allocation_method,
        "allocation_version": allocation.allocation_version,
        "parent_input_hash": allocation.parent_input_hash,
    }


def _spool_parent_allocations(
    allocation_spool: BinaryIO,
    parent: ParentResidualInput,
    pods: Iterable[PodSplitInput],
) -> None:
    for allocation in allocate_parent_residual_list_cost(parent, pods):
        pickle.dump(_allocation_row(allocation), allocation_spool, protocol=pickle.HIGHEST_PROTOCOL)


def replace_parent_residual_allocations(
    engine: Engine,
    rows: Iterable[dict[str, Any]],
    *,
    source_row_count: int,
    vendor: str,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    dry_run: bool,
    batch_size: int,
) -> int:
    if source_row_count <= 0:
        LOG.warning(
            "skipped parent residual allocation replacement for empty source",
            extra={
                "vendor": vendor,
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        return 0
    if dry_run:
        LOG.info(
            "dry-run skipped parent residual allocation replacement",
            extra={
                "source_row_count": source_row_count,
                "vendor": vendor,
                "account_id": account_id,
                "usage_start_date": usage_start_date,
                "usage_end_date": usage_end_date,
            },
        )
        return 0
    rows_written = 0
    batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        connection.execute(
            _DELETE_ALLOCATIONS_FOR_USAGE_DATES,
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
                connection.execute(_build_upsert_statement(connection), _bind_rows(connection, batch))
                rows_written += len(batch)
                batch.clear()
        if batch:
            connection.execute(_build_upsert_statement(connection), _bind_rows(connection, batch))
            rows_written += len(batch)
    return rows_written


def _iter_spooled_rows(row_spool: BinaryIO) -> Iterable[dict[str, Any]]:
    row_spool.seek(0)
    while True:
        try:
            yield pickle.load(row_spool)
        except EOFError:
            return


def _bind_rows(connection: Connection, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if connection.dialect.name != "sqlite":
        return list(rows)
    return bind_decimal_rows(list(rows))


def _build_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            f"""
            INSERT INTO {ALLOCATION_TABLE} (
              usage_date, vendor, account_id, parent_resource_id, pod_resource_id,
              namespace, workload_name, workload_type, owner, service, project, service_exec_id,
              source_pod_split_list_cost, parent_direct_list_cost, parent_residual_list_cost,
              allocation_weight, derived_parent_residual_list_cost, allocation_origin,
              allocation_method, allocation_version, parent_input_hash
            ) VALUES (
              :usage_date, :vendor, :account_id, :parent_resource_id, :pod_resource_id,
              :namespace, :workload_name, :workload_type, :owner, :service, :project, :service_exec_id,
              :source_pod_split_list_cost, :parent_direct_list_cost, :parent_residual_list_cost,
              :allocation_weight, :derived_parent_residual_list_cost, :allocation_origin,
              :allocation_method, :allocation_version, :parent_input_hash
            )
            ON CONFLICT(usage_date, vendor, account_id, parent_resource_id, pod_resource_id, allocation_version)
            DO UPDATE SET
              namespace = excluded.namespace,
              workload_name = excluded.workload_name,
              workload_type = excluded.workload_type,
              owner = excluded.owner,
              service = excluded.service,
              project = excluded.project,
              service_exec_id = excluded.service_exec_id,
              source_pod_split_list_cost = excluded.source_pod_split_list_cost,
              parent_direct_list_cost = excluded.parent_direct_list_cost,
              parent_residual_list_cost = excluded.parent_residual_list_cost,
              allocation_weight = excluded.allocation_weight,
              derived_parent_residual_list_cost = excluded.derived_parent_residual_list_cost,
              allocation_origin = excluded.allocation_origin,
              allocation_method = excluded.allocation_method,
              parent_input_hash = excluded.parent_input_hash,
              calculated_at = CURRENT_TIMESTAMP,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        f"""
        INSERT INTO {ALLOCATION_TABLE} (
          usage_date, vendor, account_id, parent_resource_id, pod_resource_id,
          namespace, workload_name, workload_type, owner, service, project, service_exec_id,
          source_pod_split_list_cost, parent_direct_list_cost, parent_residual_list_cost,
          allocation_weight, derived_parent_residual_list_cost, allocation_origin,
          allocation_method, allocation_version, parent_input_hash
        ) VALUES (
          :usage_date, :vendor, :account_id, :parent_resource_id, :pod_resource_id,
          :namespace, :workload_name, :workload_type, :owner, :service, :project, :service_exec_id,
          :source_pod_split_list_cost, :parent_direct_list_cost, :parent_residual_list_cost,
          :allocation_weight, :derived_parent_residual_list_cost, :allocation_origin,
          :allocation_method, :allocation_version, :parent_input_hash
        )
        ON DUPLICATE KEY UPDATE
          namespace = VALUES(namespace),
          workload_name = VALUES(workload_name),
          workload_type = VALUES(workload_type),
          owner = VALUES(owner),
          service = VALUES(service),
          project = VALUES(project),
          service_exec_id = VALUES(service_exec_id),
          source_pod_split_list_cost = VALUES(source_pod_split_list_cost),
          parent_direct_list_cost = VALUES(parent_direct_list_cost),
          parent_residual_list_cost = VALUES(parent_residual_list_cost),
          allocation_weight = VALUES(allocation_weight),
          derived_parent_residual_list_cost = VALUES(derived_parent_residual_list_cost),
          allocation_origin = VALUES(allocation_origin),
          allocation_method = VALUES(allocation_method),
          parent_input_hash = VALUES(parent_input_hash),
          calculated_at = CURRENT_TIMESTAMP,
          updated_at = CURRENT_TIMESTAMP
        """
    )


_DELETE_ALLOCATIONS_FOR_USAGE_DATES = text(
    f"""
    DELETE FROM {ALLOCATION_TABLE}
    WHERE vendor = :vendor
      AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)
