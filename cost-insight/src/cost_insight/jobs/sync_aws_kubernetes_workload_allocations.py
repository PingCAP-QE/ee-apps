from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.row_utils import bind_decimal_rows, coerce_date, hash_value, nullable_text
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_aws_billing_summary import AWS_SPLIT_COST_SCHEMA_VERSION, AwsBillingSource
from cost_insight.sources.gcp_billing_export import decimal_or_none

LOG = logging.getLogger(__name__)

JOB_NAME = "sync_aws_kubernetes_workload_allocations"
ALLOCATION_TABLE = "cost_kubernetes_workload_allocation_daily"
ALLOCATION_VERSION = "eks_split_cost_v2"
_CURRENCY_SCALE = Decimal("0.01")


@dataclass(frozen=True)
class SyncAwsKubernetesWorkloadAllocationsSummary:
    account_id: str
    usage_start_date: date
    usage_end_date: date
    summary_rows_seen: int
    residual_allocation_rows_seen: int
    rows_written: int
    dry_run: bool


def run_sync_aws_kubernetes_workload_allocations(
    engine: Engine,
    *,
    source: AwsBillingSource,
    usage_start_date: date,
    usage_end_date: date,
    dry_run: bool = False,
    batch_size: int = 1_000,
) -> SyncAwsKubernetesWorkloadAllocationsSummary:
    """Publish conservative EKS allocation facts from normalized AWS split-cost data."""
    if usage_start_date > usage_end_date:
        raise ValueError("usage_start_date must be before or equal to usage_end_date")
    if source.schema_version != AWS_SPLIT_COST_SCHEMA_VERSION:
        raise ValueError("AWS Kubernetes allocation requires an aws_split_cost_v1 source")
    if source.available_from is not None and usage_start_date < source.available_from:
        raise ValueError("usage_start_date is before the AWS source availability date")

    job_name = source_job_name(JOB_NAME, vendor="aws", account_id=source.account_id)
    watermark = {
        "account_id": source.account_id,
        "usage_start_date": usage_start_date.isoformat(),
        "usage_end_date": usage_end_date.isoformat(),
        "allocation_version": ALLOCATION_VERSION,
    }
    with engine.begin() as connection:
        ensure_cost_source_enabled(
            connection,
            vendor="aws",
            account_id=source.account_id,
            dry_run=dry_run,
            display_name=source.account_id,
        )
        if not dry_run:
            state_store.mark_job_started(connection, job_name, watermark)

    try:
        with engine.begin() as connection:
            summary_rows = list(
                _fetch_summary_rows(
                    connection,
                    account_id=source.account_id,
                    usage_start_date=usage_start_date,
                    usage_end_date=usage_end_date,
                )
            )
            residual_rows = list(
                _fetch_parent_residual_rows(
                    connection,
                    account_id=source.account_id,
                    usage_start_date=usage_start_date,
                    usage_end_date=usage_end_date,
                )
            )

        rows = build_aws_kubernetes_workload_allocation_rows(
            account_id=source.account_id,
            summary_rows=summary_rows,
            residual_rows=residual_rows,
        )
        parent_residual_cost = sum(
            (
                decimal_or_none(row.get("list_cost")) or Decimal()
                for row in summary_rows
                if row.get("source_allocation_scope") == "eks_parent_residual"
            ),
            Decimal(),
        )
        if parent_residual_cost.copy_abs() > Decimal("0.01") and not residual_rows:
            raise RuntimeError(
                "AWS EKS parent residual costs exist but the residual allocation ledger is empty; "
                "run sync-aws-parent-residual-allocations before publishing Kubernetes facts"
            )

        rows_written = replace_aws_kubernetes_workload_allocations(
            engine,
            rows,
            workload_source_row_count=sum(
                1
                for row in summary_rows
                if row.get("source_allocation_scope") == "eks_pod"
                or (
                    row.get("source_allocation_scope") == "split_child"
                    and nullable_text(row.get("namespace")) is not None
                )
            ),
            account_id=source.account_id,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            dry_run=dry_run,
            batch_size=batch_size,
        )
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_succeeded(connection, job_name, watermark)
        return SyncAwsKubernetesWorkloadAllocationsSummary(
            account_id=source.account_id,
            usage_start_date=usage_start_date,
            usage_end_date=usage_end_date,
            summary_rows_seen=len(summary_rows),
            residual_allocation_rows_seen=len(residual_rows),
            rows_written=rows_written,
            dry_run=dry_run,
        )
    except Exception as exc:
        LOG.exception("sync_aws_kubernetes_workload_allocations failed")
        if not dry_run:
            with engine.begin() as connection:
                state_store.mark_job_failed(connection, job_name, watermark, repr(exc))
        raise


def build_aws_kubernetes_workload_allocation_rows(
    *,
    account_id: str,
    summary_rows: Iterable[dict[str, Any]],
    residual_rows: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Build facts only for costs with direct EKS evidence.

    A cluster tag, EKS control-plane service, or the split-cost export's EKS scope
    is required. In particular, a bare shared_pool tag must not turn ordinary EC2
    or EBS spend into Kubernetes spend.
    """
    normalized_summary = tuple(_normalize_summary_row(row) for row in summary_rows)
    workload_metadata = _workload_metadata_by_identity(normalized_summary)
    facts: list[dict[str, Any]] = []
    for row in normalized_summary:
        scope = row["source_allocation_scope"]
        if scope == "eks_pod" or (scope == "split_child" and row["namespace"] is not None):
            facts.append(
                _fact_row(
                    account_id=account_id,
                    source=row,
                    allocation_scope="workload_split",
                    cost_component="pod_split",
                    allocation_method="eks_pod_split_cost_v1",
                )
            )
        elif scope == "eks_unallocated":
            facts.append(
                _fact_row(
                    account_id=account_id,
                    source=row,
                    allocation_scope="unallocated",
                    cost_component="pvc",
                    allocation_method="eks_pvc_unallocated_v1",
                )
            )
        elif scope == "direct" and row["service_name"] == "AmazonEKS":
            facts.append(
                _fact_row(
                    account_id=account_id,
                    source=row,
                    allocation_scope="unallocated",
                    cost_component="control_plane",
                    allocation_method="eks_control_plane_unallocated_v1",
                )
            )
        elif scope == "direct" and row["cluster_name"] is not None:
            facts.append(
                _fact_row(
                    account_id=account_id,
                    source=row,
                    allocation_scope="unallocated",
                    cost_component="cluster_adjacent",
                    allocation_method="eks_cluster_tag_unallocated_v1",
                )
            )

    for source_row in residual_rows:
        residual = _normalize_residual_row(source_row)
        if residual["list_cost"] == 0:
            continue
        metadata = workload_metadata.get(_workload_identity(residual))
        facts.append(
            _fact_row(
                account_id=account_id,
                source={
                    **residual,
                    **(metadata or {}),
                    "cluster_name": (metadata or {}).get("cluster_name"),
                    "cluster_location": (metadata or {}).get("cluster_location"),
                    "author": (metadata or {}).get("author") or residual["author"],
                    "org": (metadata or {}).get("org"),
                    "repo": (metadata or {}).get("repo") or residual["repo"],
                    "target_branch": (metadata or {}).get("target_branch"),
                },
                allocation_scope="workload_split",
                cost_component="parent_residual",
                allocation_method="eks_parent_residual_proportional_v1",
            )
        )
    return _aggregate_facts(facts)


def replace_aws_kubernetes_workload_allocations(
    engine: Engine,
    rows: Iterable[dict[str, Any]],
    *,
    workload_source_row_count: int,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
    dry_run: bool,
    batch_size: int,
) -> int:
    if workload_source_row_count <= 0:
        LOG.warning(
            "skipped AWS Kubernetes allocation replacement because no EKS workload source rows were found",
            extra={"account_id": account_id, "usage_start_date": usage_start_date, "usage_end_date": usage_end_date},
        )
        return 0
    if dry_run:
        return 0

    rows_written = 0
    batch: list[dict[str, Any]] = []
    with engine.begin() as connection:
        connection.execute(
            _DELETE_ALLOCATIONS_FOR_USAGE_DATES,
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
    return rows_written


def _fetch_summary_rows(
    connection: Connection,
    *,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
) -> Iterable[dict[str, Any]]:
    return connection.execute(
        _SELECT_EKS_SUMMARY_ROWS,
        {
            "account_id": account_id,
            "usage_start_date": usage_start_date,
            "usage_end_date": usage_end_date,
        },
    ).mappings()


def _fetch_parent_residual_rows(
    connection: Connection,
    *,
    account_id: str,
    usage_start_date: date,
    usage_end_date: date,
) -> Iterable[dict[str, Any]]:
    return connection.execute(
        _SELECT_EKS_PARENT_RESIDUAL_ROWS,
        {
            "account_id": account_id,
            "usage_start_date": usage_start_date,
            "usage_end_date": usage_end_date,
        },
    ).mappings()


def _normalize_summary_row(source: dict[str, Any]) -> dict[str, Any]:
    usage_date = coerce_date(source.get("usage_date"))
    list_cost = decimal_or_none(source.get("list_cost"))
    if usage_date is None or list_cost is None:
        raise ValueError(f"Missing AWS summary allocation dimensions: {source!r}")
    cluster_name, shared_pool = _cluster_tags(source.get("vendor_tags_json"))
    return {
        "usage_date": usage_date,
        "source_allocation_scope": nullable_text(source.get("source_allocation_scope")) or "direct",
        "service_name": nullable_text(source.get("service_name")),
        "cluster_name": cluster_name,
        "cluster_location": nullable_text(source.get("region")),
        "namespace": nullable_text(source.get("namespace")),
        "workload_name": nullable_text(source.get("workload_name")),
        "workload_type": nullable_text(source.get("workload_type")),
        "author": nullable_text(source.get("author")),
        "org": nullable_text(source.get("org")),
        "repo": nullable_text(source.get("repo")),
        "target_branch": nullable_text(source.get("target_branch")),
        "list_cost": list_cost.quantize(_CURRENCY_SCALE, rounding=ROUND_HALF_UP),
        "shared_pool": shared_pool,
    }


def _normalize_residual_row(source: dict[str, Any]) -> dict[str, Any]:
    usage_date = coerce_date(source.get("usage_date"))
    list_cost = decimal_or_none(source.get("derived_parent_residual_list_cost"))
    if usage_date is None or list_cost is None:
        raise ValueError(f"Missing AWS parent residual allocation dimensions: {source!r}")
    return {
        "usage_date": usage_date,
        "namespace": nullable_text(source.get("namespace")),
        "workload_name": nullable_text(source.get("workload_name")),
        "workload_type": nullable_text(source.get("workload_type")),
        "author": nullable_text(source.get("owner")),
        "org": None,
        "repo": nullable_text(source.get("project")),
        "target_branch": None,
        "list_cost": list_cost.quantize(_CURRENCY_SCALE, rounding=ROUND_HALF_UP),
    }


def _cluster_tags(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(value, dict):
        return None, None
    return nullable_text(value.get("cluster")), nullable_text(value.get("shared_pool"))


def _workload_identity(row: dict[str, Any]) -> tuple[date, str | None, str | None, str | None]:
    return (
        row["usage_date"],
        row.get("namespace"),
        row.get("workload_name"),
        row.get("workload_type"),
    )


def _workload_metadata_by_identity(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[date, str | None, str | None, str | None], dict[str, Any]]:
    metadata: dict[tuple[date, str | None, str | None, str | None], dict[str, Any]] = {}
    ambiguous: set[tuple[date, str | None, str | None, str | None]] = set()
    for row in rows:
        if row["source_allocation_scope"] != "eks_pod" and not (
            row["source_allocation_scope"] == "split_child" and row["namespace"] is not None
        ):
            continue
        key = _workload_identity(row)
        if key in ambiguous:
            continue
        candidate = {
            field: row.get(field)
            for field in ("cluster_name", "cluster_location", "author", "org", "repo", "target_branch")
        }
        existing = metadata.get(key)
        if existing is None:
            metadata[key] = candidate
        elif existing != candidate:
            metadata.pop(key)
            ambiguous.add(key)
    return metadata


def _fact_row(
    *,
    account_id: str,
    source: dict[str, Any],
    allocation_scope: str,
    cost_component: str,
    allocation_method: str,
) -> dict[str, Any]:
    row = {
        "usage_date": source["usage_date"],
        "vendor": "aws",
        "account_id": account_id,
        "cluster_name": source.get("cluster_name"),
        "cluster_location": source.get("cluster_location"),
        "allocation_scope": allocation_scope,
        "cost_component": cost_component,
        "namespace": source.get("namespace") if allocation_scope == "workload_split" else None,
        "workload_name": source.get("workload_name") if allocation_scope == "workload_split" else None,
        "workload_type": source.get("workload_type") if allocation_scope == "workload_split" else None,
        "author": source.get("author") if allocation_scope == "workload_split" else None,
        "org": source.get("org") if allocation_scope == "workload_split" else None,
        "repo": source.get("repo") if allocation_scope == "workload_split" else None,
        "target_branch": source.get("target_branch") if allocation_scope == "workload_split" else None,
        "allocation_weight": Decimal(1) if allocation_scope == "workload_split" else Decimal(),
        "source_node_list_cost": source["list_cost"],
        "list_cost": source["list_cost"],
        "allocation_method": allocation_method,
        "allocation_version": ALLOCATION_VERSION,
    }
    row["dimension_hash"] = _dimension_hash(row)
    return row


def _aggregate_facts(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        existing = grouped.get(row["dimension_hash"])
        if existing is None:
            grouped[row["dimension_hash"]] = dict(row)
            continue
        existing["source_node_list_cost"] += row["source_node_list_cost"]
        existing["list_cost"] += row["list_cost"]
    for row in grouped.values():
        row["source_node_list_cost"] = row["source_node_list_cost"].quantize(
            _CURRENCY_SCALE, rounding=ROUND_HALF_UP
        )
        row["list_cost"] = row["list_cost"].quantize(_CURRENCY_SCALE, rounding=ROUND_HALF_UP)
    return tuple(grouped[key] for key in sorted(grouped))


def _dimension_hash(row: dict[str, Any]) -> str:
    fields = (
        "usage_date",
        "vendor",
        "account_id",
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
    bound_rows = bind_decimal_rows(list(rows)) if connection.dialect.name == "sqlite" else rows
    connection.execute(_build_upsert_statement(connection), bound_rows)


def _build_upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            f"""
            INSERT INTO {ALLOCATION_TABLE} (
              usage_date, vendor, account_id, cluster_name, cluster_location,
              allocation_scope, cost_component, namespace, workload_name, workload_type,
              author, org, repo, target_branch, allocation_weight, source_node_list_cost,
              list_cost, allocation_method, allocation_version, dimension_hash
            ) VALUES (
              :usage_date, :vendor, :account_id, :cluster_name, :cluster_location,
              :allocation_scope, :cost_component, :namespace, :workload_name, :workload_type,
              :author, :org, :repo, :target_branch, :allocation_weight, :source_node_list_cost,
              :list_cost, :allocation_method, :allocation_version, :dimension_hash
            )
            ON CONFLICT(usage_date, dimension_hash) DO UPDATE SET
              allocation_weight = excluded.allocation_weight,
              source_node_list_cost = excluded.source_node_list_cost,
              list_cost = excluded.list_cost,
              allocation_method = excluded.allocation_method,
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
          list_cost, allocation_method, allocation_version, dimension_hash
        ) VALUES (
          :usage_date, :vendor, :account_id, :cluster_name, :cluster_location,
          :allocation_scope, :cost_component, :namespace, :workload_name, :workload_type,
          :author, :org, :repo, :target_branch, :allocation_weight, :source_node_list_cost,
          :list_cost, :allocation_method, :allocation_version, :dimension_hash
        )
        ON DUPLICATE KEY UPDATE
          allocation_weight = VALUES(allocation_weight),
          source_node_list_cost = VALUES(source_node_list_cost),
          list_cost = VALUES(list_cost),
          allocation_method = VALUES(allocation_method),
          calculated_at = CURRENT_TIMESTAMP,
          updated_at = CURRENT_TIMESTAMP
        """
    )


_SELECT_EKS_SUMMARY_ROWS = text(
    """
    SELECT
      usage_date, source_allocation_scope, service_name, region, vendor_tags_json,
      namespace, workload_name, workload_type, author, org, repo, target_branch, list_cost
    FROM cost_bq_export_summary_daily
    WHERE vendor = 'aws'
      AND account_id = :account_id
      AND source_schema_version = 'aws_split_cost_v1'
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
      AND (
        source_allocation_scope IN ('eks_pod', 'eks_unallocated', 'eks_parent_residual')
        OR (source_allocation_scope = 'split_child' AND namespace IS NOT NULL)
        OR (
          source_allocation_scope = 'direct'
          AND (service_name = 'AmazonEKS' OR vendor_tags_json IS NOT NULL)
        )
      )
    """
)

_SELECT_EKS_PARENT_RESIDUAL_ROWS = text(
    """
    SELECT
      usage_date, namespace, workload_name, workload_type, owner, project,
      derived_parent_residual_list_cost
    FROM cost_aws_parent_residual_allocation_daily
    WHERE vendor = 'aws'
      AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)

_DELETE_ALLOCATIONS_FOR_USAGE_DATES = text(
    f"""
    DELETE FROM {ALLOCATION_TABLE}
    WHERE vendor = 'aws'
      AND account_id = :account_id
      AND usage_date BETWEEN :usage_start_date AND :usage_end_date
    """
)
